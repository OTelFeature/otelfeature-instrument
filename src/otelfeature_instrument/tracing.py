"""The verbosity-aware TracerProvider/Tracer.

When verbosity is `IO`, spans of kind `INTERNAL` are never created and never
attached to context - `start_span`/`start_as_current_span` just hand back
whatever span was already current. Anything created "under" a suppressed
span therefore attaches directly to its real parent instead of to the
suppressed span, so no orphaned parent references are produced.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider as SDKTracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Link, Span, SpanKind, Tracer, TracerProvider
from opentelemetry.util import types as otel_types

from otelfeature_instrument.flags import VERBOSITY_IO, current_verbosity


class _VerbosityAwareTracer(Tracer):
    def __init__(self, wrapped: Tracer) -> None:
        self._wrapped = wrapped

    @staticmethod
    def _suppress(kind: SpanKind) -> bool:
        return kind is SpanKind.INTERNAL and current_verbosity() == VERBOSITY_IO

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
            return trace.get_current_span(context)
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
            yield trace.get_current_span(context)
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
