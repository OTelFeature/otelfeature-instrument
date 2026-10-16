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
from typing import Any, Iterator, Mapping

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider as SDKTracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Link, Span, SpanKind, Tracer, TracerProvider
from opentelemetry.util import types as otel_types

from otelfeature_instrument.evaluation_context import (
    PresetTraceIdGenerator,
    resource_attributes,
    span_context,
)
from otelfeature_instrument.flags import TelemetryPolicy, cached_policy, current_policy


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

    def _suppress(
        self,
        name: str,
        context: Context | None,
        kind: SpanKind,
        attributes: otel_types.Attributes,
    ) -> bool:
        # Traces only: metrics are not routed through here and keep flowing.
        policy = cached_policy()
        if policy is None:
            policy = self._evaluate(name, context, kind, attributes)
        if not policy.enabled:
            return True
        return kind is SpanKind.INTERNAL and policy.suppress_internal

    def _evaluate(
        self,
        name: str,
        context: Context | None,
        kind: SpanKind,
        attributes: otel_types.Attributes,
    ) -> TelemetryPolicy:
        # Off the static path only, so the cached path never builds a context.
        parent = trace.get_current_span(context).get_span_context()
        if parent.is_valid:
            trace_id = parent.trace_id
        elif self._ids is not None:
            # A root: mint its trace id now, so `targetingKey` is the id the
            # span will actually carry and `fractional` buckets the whole trace
            # alike in every service.
            trace_id = self._ids.new_trace_id()
            self._ids.preset(trace_id)
        else:
            trace_id = None
        return current_policy(
            span_context(
                base=self._resource,
                scope_name=self._scope_name,
                scope_version=self._scope_version,
                name=name,
                kind=kind,
                attributes=attributes,
                trace_id=trace_id,
            )
        )

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
        """The real span, or `None` when it is suppressed."""
        try:
            if self._suppress(name, context, kind, attributes):
                return None
            return self._wrapped.start_span(
                name,
                context,
                kind,
                attributes,
                links,
                start_time,
                record_exception,
                set_status_on_exception,
            )
        finally:
            if self._ids is not None:
                # Never let a preset id outlive the span it was minted for.
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
            # Tracers pick the generator up from the provider when created,
            # which is after this point.
            self._ids = PresetTraceIdGenerator(wrapped.id_generator)
            wrapped.id_generator = self._ids

    def get_tracer(
        self,
        instrumenting_module_name: str,
        instrumenting_library_version: str | None = None,
        schema_url: str | None = None,
        attributes: otel_types.Attributes | None = None,
    ) -> Tracer:
        real_tracer = self._wrapped.get_tracer(
            instrumenting_module_name,
            instrumenting_library_version,
            schema_url,
            attributes,
        )
        return _VerbosityAwareTracer(
            real_tracer,
            scope_name=instrumenting_module_name,
            scope_version=instrumenting_library_version,
            resource=self._resource,
            ids=self._ids,
        )


def install_tracer_provider(resource: Resource) -> None:
    provider = SDKTracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(_VerbosityAwareTracerProvider(provider))
