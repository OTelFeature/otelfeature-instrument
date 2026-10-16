"""The verbosity-aware TracerProvider/Tracer.

When verbosity is `IO`, spans of kind `INTERNAL` are never created and never
attached to context; when it is `OFF` (or `otelEnabled` is false), no span of
any kind is. Either way, `start_span`/`start_as_current_span` just hand back
whatever span was already current. Anything created "under" a suppressed
span therefore attaches directly to its real parent instead of to the
suppressed span, so no orphaned parent references are produced.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider as SDKTracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Link, Span, SpanKind, Tracer, TracerProvider
from opentelemetry.util import types as otel_types

from otelfeature_instrument.flags import (
    VERBOSITY_IO,
    VERBOSITY_OFF,
    current_verbosity,
    telemetry_enabled,
)


def _stand_in(span: Span) -> Span:
    # Same context as the real parent - so propagation and child parenting are
    # unchanged - but non-recording: whatever the caller does to "its" span
    # (attributes, status, `end()`) must not land on a parent that is still
    # recording, e.g. when the flag flips mid-request.
    return trace.NonRecordingSpan(span.get_span_context())


class _VerbosityAwareTracer(Tracer):
    def __init__(self, wrapped: Tracer) -> None:
        self._wrapped = wrapped

    @staticmethod
    def _suppress(kind: SpanKind) -> bool:
        # Traces only: metrics are not routed through here and keep flowing.
        if not telemetry_enabled():
            return True
        verbosity = current_verbosity()
        if verbosity == VERBOSITY_OFF:
            return True
        return kind is SpanKind.INTERNAL and verbosity == VERBOSITY_IO

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
        if self._suppress(kind):
            return _stand_in(trace.get_current_span(context))
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
        if self._suppress(kind):
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
        with self._wrapped.start_as_current_span(
            name,
            context,
            kind,
            attributes,
            links,
            start_time,
            record_exception,
            set_status_on_exception,
            end_on_exit,
        ) as span:
            yield span


class _VerbosityAwareTracerProvider(TracerProvider):
    def __init__(self, wrapped: TracerProvider) -> None:
        self._wrapped = wrapped

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
        return _VerbosityAwareTracer(real_tracer)


def install_tracer_provider(resource: Resource) -> None:
    provider = SDKTracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(_VerbosityAwareTracerProvider(provider))
