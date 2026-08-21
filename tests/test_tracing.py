from unittest.mock import patch

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider as SDKTracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from otelfeature_instrument.tracing import _VerbosityAwareTracerProvider


def _wrapped_tracer(exporter):
    real_provider = SDKTracerProvider(resource=Resource.create())
    real_provider.add_span_processor(SimpleSpanProcessor(exporter))
    return _VerbosityAwareTracerProvider(real_provider).get_tracer("test")


def test_full_keeps_internal_spans():
    exporter = InMemorySpanExporter()
    tracer = _wrapped_tracer(exporter)

    with patch("otelfeature_instrument.tracing.current_verbosity", return_value="FULL"):
        with tracer.start_as_current_span("outer", kind=trace.SpanKind.INTERNAL):
            with tracer.start_as_current_span("inner", kind=trace.SpanKind.CLIENT):
                pass

    spans = {span.name: span for span in exporter.get_finished_spans()}
    assert set(spans) == {"outer", "inner"}
    assert spans["inner"].parent.span_id == spans["outer"].context.span_id


def test_io_suppresses_internal_spans_without_orphaning_children():
    exporter = InMemorySpanExporter()
    tracer = _wrapped_tracer(exporter)

    with patch("otelfeature_instrument.tracing.current_verbosity", return_value="IO"):
        with tracer.start_as_current_span("outer", kind=trace.SpanKind.INTERNAL):
            with tracer.start_as_current_span("inner", kind=trace.SpanKind.CLIENT):
                pass

    spans = exporter.get_finished_spans()
    assert [span.name for span in spans] == ["inner"]
    assert spans[0].parent is None


def test_io_only_suppresses_internal_kind():
    exporter = InMemorySpanExporter()
    tracer = _wrapped_tracer(exporter)

    with patch("otelfeature_instrument.tracing.current_verbosity", return_value="IO"):
        with tracer.start_as_current_span("client-span", kind=trace.SpanKind.CLIENT):
            pass

    spans = exporter.get_finished_spans()
    assert [span.name for span in spans] == ["client-span"]
