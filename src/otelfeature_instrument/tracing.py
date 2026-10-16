"""The verbosity-aware TracerProvider/Tracer.

When verbosity is `IO`, spans of kind `INTERNAL` are never created and never
attached to context; when it is `OFF` (or `otelEnabled` is false), no span of
any kind is. Either way, `start_span`/`start_as_current_span` just hand back
whatever span was already current. Anything created "under" a suppressed
span therefore attaches directly to its real parent instead of to the
suppressed span, so no orphaned parent references are produced.

When a flag carries targeting, each span start is described to it through an
evaluation context built from semantic conventions - see
evaluation_context.py.
"""

from __future__ import annotations

from contextlib import contextmanager
import logging
from logging import getLogger
from typing import Any, Iterator, Mapping

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider as SDKTracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.id_generator import RandomIdGenerator
from opentelemetry.trace import Link, Span, SpanKind, Tracer, TracerProvider
from opentelemetry.util import types as otel_types

from otelfeature_instrument.evaluation_context import (
    PresetTraceIdGenerator,
    resource_attributes,
    span_context,
    tracer_context,
)
from otelfeature_instrument.flags import (
    TelemetryPolicy,
    cached_policy,
    current_policy,
    policy_generation,
    tracer_policy,
)
from otelfeature_instrument.sampling import flag_aware_sampler, handover


_logger = getLogger(__name__)


_random_ids = RandomIdGenerator()

# What `_decide` can say about one span start.
_KEEP, _STAND_IN, _DROP_TRACE = range(3)


def _stand_in(span: Span) -> Span:
    # Same context as the real parent - so propagation and child parenting are
    # unchanged - but non-recording: whatever the caller does to "its" span
    # (attributes, status, `end()`) must not land on a parent that is still
    # recording, e.g. when the flag flips mid-request.
    return trace.NonRecordingSpan(span.get_span_context())


class _VerbosityAwareTracer(Tracer):
    def __init__(
        self,
        wrapped: Tracer,
        *,
        scope_name: str = "",
        scope_version: str | None = None,
        resource: Mapping[str, Any] | None = None,
        ids: PresetTraceIdGenerator | None = None,
    ) -> None:
        self._wrapped = wrapped
        self._scope_name = scope_name
        self._scope_version = scope_version
        self._resource = resource or {}
        self._ids = ids
        # (generation, policy for roots, policy for children); a policy of
        # `None` means "this tracer has to evaluate per span".
        self._per_tracer: tuple[int, TelemetryPolicy | None, TelemetryPolicy | None] = (
            -1,
            None,
            None,
        )

    def _tracer_policy(self, root: bool) -> TelemetryPolicy | None:
        """Decided once per tracer, for rules that read only resource and scope
        keys; recomputed after every provider event."""
        generation = policy_generation()
        per_tracer = self._per_tracer
        if per_tracer[0] != generation:
            context = tracer_context(
                base=self._resource, scope_name=self._scope_name, scope_version=self._scope_version
            )
            # Read the generation before evaluating: an event landing in
            # between leaves this tagged stale, and the next span redoes it.
            per_tracer = (
                generation,
                tracer_policy(context, root=True),
                tracer_policy(context, root=False),
            )
            self._per_tracer = per_tracer
        return per_tracer[1] if root else per_tracer[2]

    @staticmethod
    def _decide(policy: TelemetryPolicy, kind: SpanKind) -> int:
        # Traces only: metrics are not routed through here and keep flowing.
        if not policy.enabled:
            return _STAND_IN
        if not policy.record_span:
            return _DROP_TRACE
        if kind is SpanKind.INTERNAL and policy.suppress_internal:
            return _STAND_IN
        return _KEEP

    def _drop(
        self, context: Context | None, trace_id: int | None, *, kind: SpanKind
    ) -> Span | None:
        """`recordSpan=false`. A span with a local parent is suppressed like an
        `IO` span (`None`: its children attach to the real parent).

        A root - no valid parent, or an incoming request (SERVER/CONSUMER)
        whose parent is remote - drops the whole request in this service: it
        gets a valid but *non-sampled* context, so its children are not
        sampled either - instead of each starting a trace of its own - and
        outgoing calls propagate `sampled=0`. Behind a proxy that traces its
        health checks, the trace therefore ends at the caller. Internal and
        client spans with a remote parent stay suppressed as children.
        """
        parent = trace.get_current_span(context).get_span_context()
        incoming = parent.is_remote and kind in (SpanKind.SERVER, SpanKind.CONSUMER)
        if parent.is_valid and not incoming:
            return None
        ids = self._ids or _random_ids
        if parent.is_valid:
            trace_id = parent.trace_id  # stay in the caller's trace, unsampled
        elif trace_id is None:
            trace_id = ids.generate_trace_id()
        return trace.NonRecordingSpan(
            trace.SpanContext(
                trace_id=trace_id,
                span_id=ids.generate_span_id(),
                is_remote=False,
                trace_flags=trace.TraceFlags(trace.TraceFlags.DEFAULT),
                trace_state=parent.trace_state if parent.is_valid else None,
            )
        )

    def _evaluate(
        self,
        parent: trace.SpanContext,
        name: str,
        kind: SpanKind,
        attributes: otel_types.Attributes,
    ) -> tuple[TelemetryPolicy, int | None]:
        """Evaluate with a span context; returns the policy and, for a root,
        the trace id it was evaluated on. Off the static path only."""
        if parent.is_valid:
            trace_id, root_id = parent.trace_id, None
        elif self._ids is not None:
            # A root: mint its trace id now, so `targetingKey` is the id the
            # span will actually carry and `fractional` buckets the whole trace
            # alike in every service. It is only handed to the SDK once the
            # span is definitely created - see `_start`.
            trace_id = root_id = self._ids.new_trace_id()
        else:
            trace_id = root_id = None
        policy = current_policy(
            span_context(
                base=self._resource,
                scope_name=self._scope_name,
                scope_version=self._scope_version,
                name=name,
                kind=kind,
                attributes=attributes,
                trace_id=trace_id,
            ),
            root=not parent.is_valid,
        )
        return policy, root_id

    def _start(
        self,
        name: str,
        context: Context | None,
        kind: SpanKind,
        attributes: otel_types.Attributes,
        links: list[Link] | None,
        start_time: int | None,
        record_exception: bool,
        set_status_on_exception: bool,
    ) -> Span | None:
        """The span to hand out, or `None` to stand in for the current one.

        Either the real span, or - for a root dropped by `recordSpan` - a
        non-recording, non-sampled one that carries the dropped trace.
        """
        args = (name, context, kind, attributes, links, start_time, record_exception,
                set_status_on_exception)
        policy = cached_policy()
        if policy is not None:
            # The static path: no context, no trace-id handover.
            decision = self._decide(policy, kind)
            if decision == _STAND_IN:
                return None
            if decision == _DROP_TRACE:
                return self._drop(context, None, kind=kind)
            return self._wrapped.start_span(*args)

        parent = trace.get_current_span(context).get_span_context()
        if parent.is_valid:
            # A child: only `traceSampleRatio` may be non-static, and a child
            # never reads it.
            policy = cached_policy(root=False)
            if policy is not None:
                decision = self._decide(policy, kind)
                if decision == _STAND_IN:
                    return None
                if decision == _DROP_TRACE:
                    return self._drop(context, None, kind=kind)
                return self._wrapped.start_span(*args)

        policy = self._tracer_policy(root=not parent.is_valid)
        if policy is not None:
            # Decided for the whole tracer: no span context, no trace-id
            # handover - nothing in it depends on the trace.
            decision = self._decide(policy, kind)
            if decision == _STAND_IN:
                return None
            if decision == _DROP_TRACE:
                return self._drop(context, None, kind=kind)
            handover(policy)
            try:
                return self._wrapped.start_span(*args)
            finally:
                handover(None)

        policy, root_id = self._evaluate(parent, name, kind, attributes)
        decision = self._decide(policy, kind)
        if decision == _STAND_IN:
            return None
        if decision == _DROP_TRACE:
            # A dropped root keeps the trace id it was evaluated on.
            return self._drop(context, root_id, kind=kind)
        # Both handovers are set only around the one call that consumes them:
        # anything that started a span during the flag evaluation above (a
        # hook, an instrumented provider) must not have picked them up.
        if root_id is not None:
            self._ids.preset(root_id)
        handover(policy)
        try:
            return self._wrapped.start_span(*args)
        finally:
            handover(None)
            if root_id is not None:
                self._ids.preset(None)

    def start_span(
        self,
        name: str,
        context: Context | None = None,
        kind: SpanKind = SpanKind.INTERNAL,
        attributes: otel_types.Attributes = None,
        links: list[Link] | None = None,
        start_time: int | None = None,
        record_exception: bool = True,
        set_status_on_exception: bool = True,
    ) -> Span:
        span = self._start(
            name,
            context,
            kind,
            attributes,
            links,
            start_time,
            record_exception,
            set_status_on_exception,
        )
        if span is None:
            return _stand_in(trace.get_current_span(context))
        return span

    @contextmanager
    def start_as_current_span(
        self,
        name: str,
        context: Context | None = None,
        kind: SpanKind = SpanKind.INTERNAL,
        attributes: otel_types.Attributes = None,
        links: list[Link] | None = None,
        start_time: int | None = None,
        record_exception: bool = True,
        set_status_on_exception: bool = True,
        end_on_exit: bool = True,
    ) -> Iterator[Span]:
        span = self._start(
            name,
            context,
            kind,
            attributes,
            links,
            start_time,
            record_exception,
            set_status_on_exception,
        )
        if span is None:
            if context is None:
                yield _stand_in(trace.get_current_span())
                return
            # An explicit context is how server instrumentation hands over the
            # extracted remote parent. Make it current, exactly as a real span
            # would have, so children and outgoing propagation still see it.
            token = otel_context.attach(context)
            try:
                yield _stand_in(trace.get_current_span(context))
            finally:
                otel_context.detach(token)
            return
        # What the SDK's own start_as_current_span does with the span from
        # start_span; reproduced so the decision above is made only once.
        with trace.use_span(
            span,
            end_on_exit=end_on_exit,
            record_exception=record_exception,
            set_status_on_exception=set_status_on_exception,
        ) as span:
            yield span


class _VerbosityAwareTracerProvider(TracerProvider):
    def __init__(self, wrapped: TracerProvider) -> None:
        self._wrapped = wrapped
        # Resource keys are the same for every span: extracted once, merged
        # per evaluation.
        self._resource = resource_attributes(getattr(wrapped, "resource", None))
        self._ids: PresetTraceIdGenerator | None = None
        if isinstance(wrapped, SDKTracerProvider):
            self._ids = PresetTraceIdGenerator(wrapped.id_generator)
            wrapped.id_generator = self._ids
        else:
            _logger.info(
                "otelfeature-instrument: %s is not an SDK TracerProvider - root "
                "spans are evaluated without a targetingKey",
                type(wrapped).__name__,
            )

    def get_tracer(
        self,
        instrumenting_module_name: str,
        instrumenting_library_version: str | None = None,
        schema_url: str | None = None,
        attributes: otel_types.Attributes | None = None,
    ) -> Tracer:
        if _logger.isEnabledFor(logging.DEBUG):
            # How to find the `otel.scope.name` to target: run once with
            # DEBUG logging for otelfeature_instrument.
            _logger.debug(
                "otelfeature-instrument: tracer for otel.scope.name=%r (version %r)",
                instrumenting_module_name,
                instrumenting_library_version,
            )
        real_tracer = self._wrapped.get_tracer(
            instrumenting_module_name,
            instrumenting_library_version,
            schema_url,
            attributes,
        )
        if self._ids is not None and getattr(real_tracer, "id_generator", self._ids) is not self._ids:
            # The SDK caches tracers per scope and copies the provider's
            # generator into each on creation: one obtained before this
            # wrapper existed still holds the old generator.
            real_tracer.id_generator = self._ids
        return _VerbosityAwareTracer(
            real_tracer,
            scope_name=instrumenting_module_name,
            scope_version=instrumenting_library_version,
            resource=self._resource,
            ids=self._ids,
        )


def install_tracer_provider(resource: Resource) -> None:
    provider = SDKTracerProvider(resource=resource, sampler=flag_aware_sampler())
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(_VerbosityAwareTracerProvider(provider))
