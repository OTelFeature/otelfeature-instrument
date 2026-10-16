"""Tests for `traceSampleRatio`: the sampler, its fallbacks, and real flagd."""

from __future__ import annotations

import json
import math
from pathlib import Path
from unittest.mock import patch

import pytest
from openfeature import api as feature_api
from openfeature.contrib.provider.flagd import FlagdProvider
from openfeature.contrib.provider.flagd.config import ResolverType
from openfeature.evaluation_context import EvaluationContext
from openfeature.flag_evaluation import FlagEvaluationDetails, Reason
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider as SDKTracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import Decision, ParentBased, TraceIdRatioBased

from otelfeature_instrument import flags, sampling, tracing
from otelfeature_instrument.flags import (
    SAMPLE_RATIO_FLAG_KEY,
    TelemetryPolicy,
    _PolicySource,
    _SampleRatioResolver,
)
from otelfeature_instrument.sampling import _FlagRatioSampler, flag_aware_sampler
from otelfeature_instrument.tracing import _VerbosityAwareTracerProvider

# --------------------------------------------------------------------------
# Startup fallback and when the sampler takes over
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "environ, ratio",
    [
        ({}, 1.0),
        ({"OTEL_TRACES_SAMPLER": "always_off"}, 0.0),
        ({"OTEL_TRACES_SAMPLER": "parentbased_always_off"}, 0.0),
        ({"OTEL_TRACES_SAMPLER": "parentbased_traceidratio", "OTEL_TRACES_SAMPLER_ARG": "0.25"}, 0.25),
        ({"OTEL_TRACES_SAMPLER": "TraceIdRatio", "OTEL_TRACES_SAMPLER_ARG": "0.5"}, 0.5),
        ({"OTEL_TRACES_SAMPLER": "traceidratio"}, 1.0),
        ({"OTEL_TRACES_SAMPLER": "traceidratio", "OTEL_TRACES_SAMPLER_ARG": "abc"}, 1.0),
        ({"OTEL_TRACES_SAMPLER": "traceidratio", "OTEL_TRACES_SAMPLER_ARG": "7"}, 1.0),
        ({"OTEL_TRACES_SAMPLER": "jaeger_remote"}, 1.0),
    ],
)
def test_startup_ratio_comes_from_the_sdk_environment(environ, ratio):
    assert TelemetryPolicy.from_environment(environ).sample_ratio == ratio


@pytest.mark.parametrize("configured", ["jaeger_remote", "my_custom_sampler"])
def test_a_non_ratio_sampler_is_left_in_charge(configured):
    assert flag_aware_sampler({"OTEL_TRACES_SAMPLER": configured}) is None


@pytest.mark.parametrize("configured", [None, "always_on", "parentbased_traceidratio"])
def test_ratio_samplers_are_replaced_by_the_parent_based_flag_sampler(configured):
    environ = {} if configured is None else {"OTEL_TRACES_SAMPLER": configured}
    sampler = flag_aware_sampler(environ)
    assert isinstance(sampler, ParentBased)
    assert isinstance(sampler._root, _FlagRatioSampler)


# --------------------------------------------------------------------------
# The resolver never raises on bad values
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value, expected",
    [(0.25, 0.25), (1, 1.0), (0, 0.0), (2.5, 1.0), (-0.1, 0.0), (math.nan, 0.5), (True, 0.5), ("x", 0.5)],
)
def test_out_of_range_clamps_and_non_numbers_fall_back(value, expected):
    resolver = _SampleRatioResolver(default=0.5)

    class _Client:
        def get_float_details(self, key, default_value, context=None):
            return FlagEvaluationDetails(flag_key=key, value=value, reason=Reason.STATIC)

    with patch.object(feature_api, "get_client", return_value=_Client()):
        assert resolver.current() == expected


def test_a_bad_value_is_logged_once(caplog):
    resolver = _SampleRatioResolver()

    class _Client:
        def get_float_details(self, key, default_value, context=None):
            return FlagEvaluationDetails(flag_key=key, value=3.0, reason=Reason.TARGETING_MATCH)

    with caplog.at_level("WARNING", logger=flags.__name__), patch.object(
        feature_api, "get_client", return_value=_Client()
    ):
        for _ in range(5):
            resolver.current()

    assert len([r for r in caplog.records if "outside 0.0-1.0" in r.getMessage()]) == 1


# --------------------------------------------------------------------------
# The sampler
# --------------------------------------------------------------------------

TRACE_IDS = [(i * 0x9E3779B97F4A7C15 + 0x1234) % (1 << 128) for i in range(1, 400)]


@pytest.mark.parametrize("ratio", [0.0, 0.1, 0.5, 1.0])
def test_root_decision_is_the_sdks_trace_id_ratio_rule(monkeypatch, ratio):
    """Same rule as TraceIdRatioBased, so every service applying the same ratio
    to the same trace agrees."""
    monkeypatch.setattr(sampling, "cached_policy", lambda: TelemetryPolicy(sample_ratio=ratio))
    ours, reference = _FlagRatioSampler(), TraceIdRatioBased(ratio)

    for trace_id in TRACE_IDS:
        assert (
            ours.should_sample(None, trace_id, "root").decision
            == reference.should_sample(None, trace_id, "root").decision
        )


def _tracer(exporter, sampler=None):
    provider = SDKTracerProvider(
        resource=Resource.create({"service.name": "weather"}),
        sampler=sampler or flag_aware_sampler({}),
    )
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return _VerbosityAwareTracerProvider(provider).get_tracer("test")


def test_a_trace_is_never_half_sampled_when_the_ratio_flips_mid_trace(monkeypatch):
    ratio = {"value": 1.0}
    monkeypatch.setattr(
        sampling, "cached_policy", lambda: TelemetryPolicy(sample_ratio=ratio["value"])
    )
    monkeypatch.setattr(tracing, "cached_policy", lambda: TelemetryPolicy())
    exporter = InMemorySpanExporter()
    tracer = _tracer(exporter)

    with tracer.start_as_current_span("root", kind=trace.SpanKind.SERVER):
        ratio["value"] = 0.0
        with tracer.start_as_current_span("child", kind=trace.SpanKind.CLIENT):
            pass

    assert {span.name for span in exporter.get_finished_spans()} == {"root", "child"}

    exporter.clear()
    with tracer.start_as_current_span("root", kind=trace.SpanKind.SERVER):
        ratio["value"] = 1.0
        with tracer.start_as_current_span("child", kind=trace.SpanKind.CLIENT):
            pass
    assert exporter.get_finished_spans() == ()


def test_a_downstream_service_follows_the_incoming_decision(monkeypatch):
    """The entry-point caveat: a remote parent's flag wins over the local ratio."""
    monkeypatch.setattr(sampling, "cached_policy", lambda: TelemetryPolicy(sample_ratio=0.0))
    monkeypatch.setattr(tracing, "cached_policy", lambda: TelemetryPolicy())
    exporter = InMemorySpanExporter()
    tracer = _tracer(exporter)
    sampled_upstream = trace.set_span_in_context(
        trace.NonRecordingSpan(
            trace.SpanContext(
                trace_id=0x1234,
                span_id=0x5678,
                is_remote=True,
                trace_flags=trace.TraceFlags(trace.TraceFlags.SAMPLED),
            )
        )
    )

    tracer.start_span("server", context=sampled_upstream, kind=trace.SpanKind.SERVER).end()

    assert [span.name for span in exporter.get_finished_spans()] == ["server"]


def test_the_sampler_uses_the_policy_the_tracer_evaluated(monkeypatch):
    """With targeting, the ratio comes from the span's own evaluation - not from
    a second, context-free one inside the sampler."""
    contexts: list[EvaluationContext | None] = []

    def policy(context=None):
        contexts.append(context)
        return TelemetryPolicy(sample_ratio=0.0 if context is not None else 1.0)

    monkeypatch.setattr(tracing, "cached_policy", lambda: None)
    monkeypatch.setattr(sampling, "cached_policy", lambda: None)
    monkeypatch.setattr(tracing, "current_policy", policy)
    monkeypatch.setattr(sampling, "current_policy", policy)
    exporter = InMemorySpanExporter()

    _tracer(exporter).start_span("root", kind=trace.SpanKind.SERVER).end()

    assert exporter.get_finished_spans() == ()
    assert len(contexts) == 1 and contexts[0] is not None
    assert getattr(sampling._handover, "policy", None) is None


# --------------------------------------------------------------------------
# End to end against real flagd (FILE resolver)
# --------------------------------------------------------------------------


@pytest.fixture
def flagd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(flags, "_policy_source", _PolicySource())

    def start(flag: dict) -> None:
        path = tmp_path / "flags.json"
        path.write_text(
            json.dumps({"flags": {SAMPLE_RATIO_FLAG_KEY: {"state": "ENABLED", **flag}}}),
            encoding="utf-8",
        )
        feature_api.set_provider_and_wait(
            FlagdProvider(resolver_type=ResolverType.FILE, offline_flag_source_path=str(path))
        )
        flags._policy_source.classify()

    yield start
    feature_api.clear_providers()


def _roots(tracer, n=200):
    for i in range(n):
        tracer.start_span(f"root-{i}", kind=trace.SpanKind.SERVER).end()


def test_real_flagd_static_ratio(flagd):
    flagd({"defaultVariant": "tenth", "variants": {"tenth": 0.1, "all": 1.0}})
    assert flags._policy_source.cached.sample_ratio == 0.1
    exporter = InMemorySpanExporter()

    _roots(_tracer(exporter), 1000)

    assert 50 < len(exporter.get_finished_spans()) < 160


def test_real_flagd_targets_the_ratio_by_service(flagd):
    flagd(
        {
            "defaultVariant": "all",
            "variants": {"none": 0.0, "all": 1.0},
            "targeting": {"if": [{"==": [{"var": "service.name"}, "weather"]}, "none", "all"]},
        }
    )
    exporter = InMemorySpanExporter()

    _roots(_tracer(exporter), 20)
    assert exporter.get_finished_spans() == ()

    other = SDKTracerProvider(
        resource=Resource.create({"service.name": "gateway"}), sampler=flag_aware_sampler({})
    )
    other.add_span_processor(SimpleSpanProcessor(exporter))
    _roots(_VerbosityAwareTracerProvider(other).get_tracer("test"), 20)
    assert len(exporter.get_finished_spans()) == 20


def test_real_flagd_out_of_range_and_wrong_type_never_raise(flagd):
    flagd({"defaultVariant": "big", "variants": {"big": 2.5}})
    assert flags._policy_source.cached.sample_ratio == 1.0

    flagd({"defaultVariant": "text", "variants": {"text": "lots"}})
    # A type mismatch: the SDK serves the startup value instead.
    assert flags._policy_source.current().sample_ratio == 1.0
