"""Tests for honouring OTEL_TRACES_EXPORTER=none / OTEL_METRICS_EXPORTER=none."""

from __future__ import annotations

import pytest
from opentelemetry import trace
from opentelemetry.propagate import inject
from opentelemetry.sdk.resources import Resource

from otelfeature_instrument import exporters, tracing
from otelfeature_instrument.exporters import exporter_disabled
from otelfeature_instrument.tracing import _VerbosityAwareTracerProvider


@pytest.mark.parametrize(
    "value, disabled",
    [
        (None, False),
        ("", False),
        ("otlp", False),
        ("console", False),
        ("none", True),
        (" NONE ", True),
        ("None", True),
        ("none,", False),  # as the SDK: not exactly "none"
        ("otlp,none", False),
    ],
)
def test_exporter_disabled_parses_like_the_sdk(value, disabled):
    environ = {} if value is None else {"OTEL_TRACES_EXPORTER": value}
    assert exporter_disabled("OTEL_TRACES_EXPORTER", environ) is disabled


def test_none_mixed_with_exporters_warns(caplog):
    with caplog.at_level("WARNING", logger=exporters.__name__):
        assert exporter_disabled("OTEL_TRACES_EXPORTER", {"OTEL_TRACES_EXPORTER": "otlp, none"}) is False
    assert "mixes 'none' with exporters" in caplog.text


@pytest.fixture
def installed(monkeypatch):
    providers = []
    monkeypatch.setattr(trace, "set_tracer_provider", providers.append)
    processors = []
    monkeypatch.setattr(
        tracing.SDKTracerProvider, "add_span_processor", lambda self, p: processors.append(p)
    )
    return providers, processors


def test_traces_exporter_none_exports_nothing_but_keeps_the_provider(monkeypatch, installed, caplog):
    providers, processors = installed
    monkeypatch.setenv("OTEL_TRACES_EXPORTER", "none")
    monkeypatch.setattr(tracing, "OTLPSpanExporter", lambda: pytest.fail("built an OTLP exporter"))

    with caplog.at_level("INFO", logger=tracing.__name__):
        tracing.install_tracer_provider(Resource.create())

    assert processors == []
    (provider,) = providers
    assert isinstance(provider, _VerbosityAwareTracerProvider)
    assert "spans are not exported" in caplog.text


@pytest.mark.parametrize("value", [None, "otlp", "otlp,none"])
def test_otherwise_spans_are_exported_over_otlp(monkeypatch, installed, value):
    providers, processors = installed
    if value is None:
        monkeypatch.delenv("OTEL_TRACES_EXPORTER", raising=False)
    else:
        monkeypatch.setenv("OTEL_TRACES_EXPORTER", value)

    tracing.install_tracer_provider(Resource.create())

    (processor,) = processors
    assert type(processor).__name__ == "BatchSpanProcessor"
    processor.shutdown()


def test_with_export_off_context_still_propagates(monkeypatch):
    """No exporter, but spans still start: traceparent flows downstream."""
    providers = []
    monkeypatch.setattr(trace, "set_tracer_provider", providers.append)
    monkeypatch.setenv("OTEL_TRACES_EXPORTER", "none")
    tracing.install_tracer_provider(Resource.create())
    tracer = providers[0].get_tracer("app")

    with tracer.start_as_current_span("request", kind=trace.SpanKind.SERVER) as span:
        headers: dict[str, str] = {}
        inject(headers)

    assert span.get_span_context().is_valid
    assert headers["traceparent"].split("-")[1] == f"{span.get_span_context().trace_id:032x}"
