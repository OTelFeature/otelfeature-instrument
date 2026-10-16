from unittest.mock import patch

import pytest

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


def _all_kinds(tracer):
    with tracer.start_as_current_span("server", kind=trace.SpanKind.SERVER):
        with tracer.start_as_current_span("internal", kind=trace.SpanKind.INTERNAL):
            with tracer.start_as_current_span("client", kind=trace.SpanKind.CLIENT):
                pass
        tracer.start_span("producer", kind=trace.SpanKind.PRODUCER).end()
        tracer.start_span("consumer", kind=trace.SpanKind.CONSUMER).end()


def test_off_suppresses_every_span_kind():
    exporter = InMemorySpanExporter()
    tracer = _wrapped_tracer(exporter)

    with patch("otelfeature_instrument.tracing.current_verbosity", return_value="OFF"):
        _all_kinds(tracer)

    assert exporter.get_finished_spans() == ()


def test_otel_enabled_false_suppresses_every_span_kind_regardless_of_level():
    exporter = InMemorySpanExporter()
    tracer = _wrapped_tracer(exporter)

    with patch("otelfeature_instrument.tracing.telemetry_enabled", return_value=False), patch(
        "otelfeature_instrument.tracing.current_verbosity", return_value="FULL"
    ):
        _all_kinds(tracer)

    assert exporter.get_finished_spans() == ()


@pytest.mark.parametrize(
    "enabled, level", [(False, "FULL"), (True, "OFF")], ids=["otelEnabled=false", "OFF"]
)
def test_off_keeps_the_incoming_parent_context(enabled, level):
    """Suppression, not an SDK disable: the propagated parent is handed straight back."""
    exporter = InMemorySpanExporter()
    tracer = _wrapped_tracer(exporter)
    remote_parent = trace.NonRecordingSpan(
        trace.SpanContext(
            trace_id=0x1234,
            span_id=0x5678,
            is_remote=True,
            trace_flags=trace.TraceFlags(trace.TraceFlags.SAMPLED),
        )
    )
    context = trace.set_span_in_context(remote_parent)

    with patch("otelfeature_instrument.tracing.telemetry_enabled", return_value=enabled), patch(
        "otelfeature_instrument.tracing.current_verbosity", return_value=level
    ):
        with tracer.start_as_current_span(
            "server", context=context, kind=trace.SpanKind.SERVER
        ) as span:
            assert span is remote_parent
            assert trace.get_current_span() is remote_parent
        assert tracer.start_span("client", context=context, kind=trace.SpanKind.CLIENT) is remote_parent

    assert exporter.get_finished_spans() == ()


def test_flipping_back_on_resumes_tracing():
    exporter = InMemorySpanExporter()
    tracer = _wrapped_tracer(exporter)

    with patch("otelfeature_instrument.tracing.telemetry_enabled", return_value=False):
        _all_kinds(tracer)
    assert exporter.get_finished_spans() == ()

    _all_kinds(tracer)  # no provider in tests: both flags fall back to on/FULL
    assert {span.name for span in exporter.get_finished_spans()} == {
        "server",
        "internal",
        "client",
        "producer",
        "consumer",
    }


@pytest.mark.parametrize("level", ["VERBOSE", "", "0"])
def test_unknown_level_fails_open_to_full(level):
    exporter = InMemorySpanExporter()
    tracer = _wrapped_tracer(exporter)

    with patch("otelfeature_instrument.tracing.current_verbosity", return_value=level):
        _all_kinds(tracer)

    assert len(exporter.get_finished_spans()) == 5
