"""Tests for the error rate published into the evaluation context."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from openfeature import api as feature_api
from openfeature.contrib.provider.flagd import FlagdProvider
from openfeature.contrib.provider.flagd.config import ResolverType
from opentelemetry import trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import AggregationTemporality, InMemoryMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider as SDKTracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from otelfeature_instrument import error_rate, evaluation_context, flags, metrics
from otelfeature_instrument.error_rate import (
    ERROR_RATE_KEY,
    ErrorRatePublisher,
    error_rate_reader,
    interval_from_environment,
    is_error,
    min_requests_from_environment,
)
from otelfeature_instrument.evaluation_context import span_context
from otelfeature_instrument.flags import SAMPLE_RATIO_FLAG_KEY, TelemetryPolicy, _PolicySource
from otelfeature_instrument.sampling import flag_aware_sampler
from otelfeature_instrument.tracing import _VerbosityAwareTracerProvider


@pytest.fixture(autouse=True)
def _no_live_attributes(monkeypatch):
    monkeypatch.setattr(evaluation_context, "_live", {})


class _Service:
    """Records what the HTTP server instrumentation would, per request."""

    def __init__(self, *readers, stable: bool = True) -> None:
        self.provider = MeterProvider(metric_readers=list(readers))
        name = "http.server.request.duration" if stable else "http.server.duration"
        self.histogram = self.provider.get_meter("opentelemetry.instrumentation.asgi").create_histogram(name)
        self.stable = stable

    def request(self, status: int) -> None:
        if self.stable:
            attributes = {"http.request.method": "GET", "http.response.status_code": status}
            if status >= 500:
                attributes["error.type"] = str(status)
        else:
            attributes = {"http.method": "GET", "http.status_code": status}
        self.histogram.record(0.01, attributes)


def _live_rate():
    return evaluation_context._live.get(ERROR_RATE_KEY)


def test_k_of_n_failing_requests_publish_k_over_n():
    reader = error_rate_reader()
    service = _Service(reader)
    publisher = ErrorRatePublisher(reader, interval=10, min_requests=1)

    for i in range(20):
        service.request(503 if i < 3 else 200)
    publisher.collect_once()

    assert _live_rate() == pytest.approx(3 / 20)


def test_each_collection_is_its_own_window():
    """Delta temporality: the next collection only sees the requests since."""
    reader = error_rate_reader()
    service = _Service(reader)
    publisher = ErrorRatePublisher(reader, interval=10, min_requests=1)

    for _ in range(10):
        service.request(500)
    publisher.collect_once()
    for _ in range(10):
        service.request(200)
    publisher.collect_once()

    assert _live_rate() == 0.0


def test_no_requests_leaves_the_key_out():
    reader = error_rate_reader()
    service = _Service(reader)
    publisher = ErrorRatePublisher(reader, interval=10, min_requests=1)
    service.request(500)
    publisher.collect_once()
    assert _live_rate() == 1.0

    publisher.collect_once()  # nothing recorded since

    assert ERROR_RATE_KEY not in evaluation_context._live


def test_pre_stable_metric_falls_back_to_the_status_code():
    """Without OTEL_SEMCONV_STABILITY_OPT_IN=http there is no error.type."""
    reader = error_rate_reader()
    service = _Service(reader, stable=False)
    publisher = ErrorRatePublisher(reader, interval=10, min_requests=1)

    for status in (200, 404, 500, 502):
        service.request(status)
    publisher.collect_once()

    assert _live_rate() == 0.5


def test_dup_opt_in_counts_each_request_once():
    """http/dup records both metrics for the same requests; only the stable one counts."""
    reader = error_rate_reader()
    stable = _Service(reader)
    old_histogram = stable.provider.get_meter("dup").create_histogram("http.server.duration")
    publisher = ErrorRatePublisher(reader, interval=10, min_requests=1)

    for status in (200, 200, 200, 500):
        stable.request(status)
        old_histogram.record(0.01, {"http.status_code": 200})  # disagrees on purpose

    publisher.collect_once()

    assert _live_rate() == 0.25


@pytest.mark.parametrize(
    "attributes, expected",
    [
        ({"error.type": "timeout"}, True),
        ({"error.type": "500", "http.response.status_code": 500}, True),
        ({"http.response.status_code": 499}, False),
        ({"http.status_code": "503"}, True),
        ({"http.status_code": "n/a"}, False),
        ({}, False),
    ],
)
def test_is_error(attributes, expected):
    assert is_error(attributes) is expected


def test_the_otlp_view_is_unchanged_by_the_extra_reader():
    """On the same provider, a cumulative reader standing in for the OTLP one
    keeps cumulative totals, however often the delta reader collects."""
    otlp, ours = InMemoryMetricReader(), error_rate_reader()
    service = _Service(otlp, ours)
    publisher = ErrorRatePublisher(ours, interval=10, min_requests=1)

    for _ in range(3):
        for i in range(5):
            service.request(500 if i == 0 else 200)
        publisher.collect_once()

    (resource_metrics,) = otlp.get_metrics_data().resource_metrics
    (metric,) = resource_metrics.scope_metrics[0].metrics
    assert metric.data.aggregation_temporality is AggregationTemporality.CUMULATIVE
    counts = {p.attributes["http.response.status_code"]: p.count for p in metric.data.data_points}
    assert counts == {500: 3, 200: 12}


def test_the_timer_publishes_without_anyone_calling_it():
    reader = error_rate_reader()
    service = _Service(reader)
    service.request(500)
    service.request(200)

    publisher = ErrorRatePublisher(reader, interval=0.02, min_requests=1)
    thread = publisher.start()
    try:
        deadline = time.monotonic() + 5
        while _live_rate() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert _live_rate() == 0.5
    finally:
        publisher.stop()
        thread.join(5)
    assert not thread.is_alive()


def test_span_sampling_does_not_change_the_measured_rate(monkeypatch):
    """Spans sampled down to 0: the rate comes from metrics, so it is unchanged."""
    reader = error_rate_reader()
    service = _Service(reader)
    publisher = ErrorRatePublisher(reader, interval=10, min_requests=1)
    exporter = InMemorySpanExporter()
    monkeypatch.setattr(
        "otelfeature_instrument.sampling.cached_policy",
        lambda **_: TelemetryPolicy(sample_ratio=0.0),
    )
    provider = SDKTracerProvider(sampler=flag_aware_sampler({}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("opentelemetry.instrumentation.asgi")

    for i in range(10):
        with tracer.start_as_current_span("GET /", kind=trace.SpanKind.SERVER):
            service.request(500 if i < 4 else 200)
    publisher.collect_once()

    assert exporter.get_finished_spans() == ()
    assert _live_rate() == 0.4


def test_the_rate_is_in_every_evaluation_context_nested():
    evaluation_context.set_live_attribute(ERROR_RATE_KEY, 0.25)

    context = span_context(
        base={"service.name": "weather"},
        scope_name="s",
        scope_version=None,
        name="GET /",
        kind=trace.SpanKind.SERVER,
        # A start attribute must not be able to fake it.
        attributes={ERROR_RATE_KEY: 0.0},
        trace_id=None,
    )

    assert context.attributes["otelfeature"] == {"http": {"server": {"error_rate": 0.25}}}


@pytest.mark.parametrize(
    "attribute", [ERROR_RATE_KEY, "otelfeature.anything", "otelfeature"]
)
def test_the_otelfeature_namespace_cannot_be_faked_while_the_rate_is_absent(attribute):
    """No requests yet (or a failed collection): the key is absent, and a
    start attribute must still not be able to supply it."""
    assert ERROR_RATE_KEY not in evaluation_context._live

    context = span_context(
        base={},
        scope_name="s",
        scope_version=None,
        name="GET /",
        kind=trace.SpanKind.SERVER,
        attributes={attribute: 0.99, "url.path": "/"},
        trace_id=None,
    )

    assert "otelfeature" not in context.attributes
    assert context.attributes["url"] == {"path": "/"}


def test_a_failing_collection_keeps_the_loop_alive(monkeypatch, caplog):
    reader = error_rate_reader()
    publisher = ErrorRatePublisher(reader, interval=10, min_requests=1)
    evaluation_context.set_live_attribute(ERROR_RATE_KEY, 0.9)
    monkeypatch.setattr(reader, "get_metrics_data", lambda: 1 / 0)

    assert publisher.collect_once() is None
    assert ERROR_RATE_KEY not in evaluation_context._live
    assert "collection failed" in caplog.text


@pytest.mark.parametrize(
    "raw, expected", [(None, 10.0), ("", 10.0), ("2.5", 2.5), ("0", 10.0), ("-1", 10.0), ("x", 10.0)]
)
def test_interval_from_environment(raw, expected):
    environ = {} if raw is None else {error_rate.INTERVAL_ENV_VAR: raw}
    assert interval_from_environment(environ) == expected


def test_install_meter_provider_wires_the_minimum_from_the_environment(monkeypatch):
    built = []
    monkeypatch.setenv(error_rate.MIN_REQUESTS_ENV_VAR, "3")
    monkeypatch.setattr(metrics, "MeterProvider", lambda **kwargs: built.append(kwargs))
    monkeypatch.setattr(metrics.metrics, "set_meter_provider", lambda provider: None)
    started = []
    monkeypatch.setattr(ErrorRatePublisher, "start", lambda self: started.append(self))

    metrics.install_meter_provider(Resource.create())

    (publisher,) = started
    assert publisher._min_requests == 3
    built[0]["metric_readers"][0].shutdown()


def test_install_meter_provider_adds_the_reader_next_to_otlp(monkeypatch):
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

    built, started = [], []
    monkeypatch.setattr(metrics, "MeterProvider", lambda **kwargs: built.append(kwargs))
    monkeypatch.setattr(metrics.metrics, "set_meter_provider", lambda provider: None)
    monkeypatch.setattr(ErrorRatePublisher, "start", lambda self: started.append(self))

    metrics.install_meter_provider(Resource.create())

    (kwargs,) = built
    readers = kwargs["metric_readers"]
    assert [type(r) for r in readers] == [PeriodicExportingMetricReader, InMemoryMetricReader]
    assert len(started) == 1
    readers[0].shutdown()


# --------------------------------------------------------------------------
# D5 end to end: a flagd rule turns sampling up when errors go up
# --------------------------------------------------------------------------


def test_real_flagd_rule_raises_sampling_when_the_error_rate_crosses_a_threshold(
    tmp_path: Path, monkeypatch
):
    path = tmp_path / "flags.json"
    path.write_text(
        json.dumps(
            {
                "flags": {
                    SAMPLE_RATIO_FLAG_KEY: {
                        "state": "ENABLED",
                        "defaultVariant": "low",
                        "variants": {"low": 0.0, "all": 1.0},
                        "targeting": {
                            "if": [
                                {">": [{"var": "otelfeature.http.server.error_rate"}, 0.05]},
                                "all",
                                "low",
                            ]
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(flags, "_policy_source", _PolicySource())
    feature_api.set_provider_and_wait(
        FlagdProvider(resolver_type=ResolverType.FILE, offline_flag_source_path=str(path))
    )
    try:
        flags._policy_source.classify()
        reader = error_rate_reader()
        service = _Service(reader)
        publisher = ErrorRatePublisher(reader, interval=10, min_requests=1)
        exporter = InMemorySpanExporter()
        provider = SDKTracerProvider(resource=Resource.create(), sampler=flag_aware_sampler({}))
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        tracer = _VerbosityAwareTracerProvider(provider).get_tracer("t")

        def window(failures: int) -> int:
            exporter.clear()
            for i in range(20):
                tracer.start_span("GET /", kind=trace.SpanKind.SERVER).end()
                service.request(500 if i < failures else 200)
            publisher.collect_once()
            return len(exporter.get_finished_spans())

        assert window(0) == 0  # no rate yet: rule doesn't match, ratio 0.0
        assert window(5) == 0  # still the previous window's rate (0.0)
        assert window(0) == 20  # previous window was 25% errors: sample everything
        assert window(0) == 0  # healthy again
    finally:
        feature_api.clear_providers()


def test_below_the_minimum_request_count_no_rate_is_published():
    """One failure in one request must not publish 1.0 and flip the loop."""
    reader = error_rate_reader()
    service = _Service(reader)
    publisher = ErrorRatePublisher(reader, interval=10)  # default minimum: 10

    service.request(500)
    publisher.collect_once()
    assert ERROR_RATE_KEY not in evaluation_context._live

    for i in range(10):
        service.request(500 if i < 2 else 200)
    publisher.collect_once()
    assert _live_rate() == pytest.approx(0.2)


def test_dropping_below_the_minimum_removes_a_previous_rate():
    reader = error_rate_reader()
    service = _Service(reader)
    publisher = ErrorRatePublisher(reader, interval=10, min_requests=5)
    for _ in range(5):
        service.request(500)
    publisher.collect_once()
    assert _live_rate() == 1.0

    service.request(200)
    publisher.collect_once()

    assert ERROR_RATE_KEY not in evaluation_context._live


@pytest.mark.parametrize(
    "raw, expected", [(None, 10), ("", 10), ("25", 25), ("1", 1), ("0", 10), ("-3", 10), ("2.5", 10), ("x", 10)]
)
def test_min_requests_from_environment(raw, expected):
    environ = {} if raw is None else {error_rate.MIN_REQUESTS_ENV_VAR: raw}
    assert min_requests_from_environment(environ) == expected
