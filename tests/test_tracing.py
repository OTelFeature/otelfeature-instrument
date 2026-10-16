from unittest.mock import patch

import pytest
from openfeature import api as feature_api
from opentelemetry import trace
from opentelemetry.propagate import inject
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider as SDKTracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from otelfeature_instrument.tracing import _VerbosityAwareTracerProvider


@pytest.fixture(autouse=True)
def _no_flag_provider():
    """Unpatched flag reads must hit the SDK's no-op provider (FULL / enabled),
    not whatever provider another test module left registered."""
    feature_api.clear_providers()
    yield
    feature_api.clear_providers()


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
    """Suppression, not an SDK disable: the incoming parent stays current and is
    what gets propagated downstream."""
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
            assert span.get_span_context() == remote_parent.get_span_context()
            assert trace.get_current_span() is remote_parent
            with tracer.start_as_current_span("client", kind=trace.SpanKind.CLIENT):
                headers = {}
                inject(headers)
        client = tracer.start_span("client", context=context, kind=trace.SpanKind.CLIENT)
        assert client.get_span_context() == remote_parent.get_span_context()

    # The upstream caller's span id: this service drops out of the trace, but
    # the trace itself carries on downstream.
    assert headers["traceparent"] == (
        "00-00000000000000000000000000001234-0000000000005678-01"
    )

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


def test_suppressed_span_does_not_write_to_a_recording_parent():
    """If the flag flips mid-request, a suppressed child must not mutate or end
    the still-recording parent it stands in for."""
    exporter = InMemorySpanExporter()
    tracer = _wrapped_tracer(exporter)

    with tracer.start_as_current_span("server", kind=trace.SpanKind.SERVER):
        with patch("otelfeature_instrument.tracing.current_verbosity", return_value="OFF"):
            child = tracer.start_span("client", kind=trace.SpanKind.CLIENT)
            child.set_attribute("http.response.status_code", 404)
            child.set_status(trace.Status(trace.StatusCode.ERROR))
            child.end()
            with tracer.start_as_current_span("internal") as internal:
                internal.set_attribute("leaked", True)
            assert not child.is_recording()

    (server,) = exporter.get_finished_spans()
    assert dict(server.attributes) == {}
    assert server.status.status_code is trace.StatusCode.UNSET


def test_suppressed_span_restores_context_when_the_body_raises():
    exporter = InMemorySpanExporter()
    tracer = _wrapped_tracer(exporter)
    remote_parent = trace.NonRecordingSpan(
        trace.SpanContext(trace_id=0x1234, span_id=0x5678, is_remote=True)
    )
    before = trace.get_current_span()

    with patch("otelfeature_instrument.tracing.current_verbosity", return_value="OFF"):
        with pytest.raises(RuntimeError):
            with tracer.start_as_current_span(
                "server", context=trace.set_span_in_context(remote_parent)
            ):
                raise RuntimeError("boom")

    assert trace.get_current_span() is before
