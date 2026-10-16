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
        # Not stripped, exactly like the SDK: unknown, so 1.0.
        ({"OTEL_TRACES_SAMPLER": " always_off"}, 1.0),
        ({"OTEL_TRACES_SAMPLER": "traceidratio", "OTEL_TRACES_SAMPLER_ARG": " 0.5 "}, 0.5),
    ],
)
def test_startup_ratio_comes_from_the_sdk_environment(environ, ratio):
    assert TelemetryPolicy.from_environment(environ).sample_ratio == ratio


@pytest.mark.parametrize(
    "configured, warning",
    [
        (None, None),
        ("parentbased_traceidratio", None),
        ("always_on", "applied parent-based"),
        ("traceidratio", "applied parent-based"),
        ("jaeger_remote", "custom samplers are not loaded"),
        ("", "custom samplers are not loaded"),
    ],
)
def test_the_flag_sampler_always_installs_and_says_where_it_differs(configured, warning, caplog):
    environ = {} if configured is None else {"OTEL_TRACES_SAMPLER": configured}

    with caplog.at_level("WARNING", logger=sampling.__name__):
        sampler = flag_aware_sampler(environ)

    assert isinstance(sampler, ParentBased)
    assert isinstance(sampler._root, _FlagRatioSampler)
    if warning is None:
        assert caplog.records == []
    else:
        assert warning in caplog.text


def test_install_tracer_provider_wires_the_flag_sampler(monkeypatch):
    installed = []
    monkeypatch.setattr(trace, "set_tracer_provider", installed.append)

    tracing.install_tracer_provider(Resource.create())

    (wrapper,) = installed
    assert isinstance(wrapper._wrapped.sampler._root, _FlagRatioSampler)


# --------------------------------------------------------------------------
# The resolver never raises on bad values
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value, expected",
    [
        (0.25, 0.25),
        (1, 1.0),
        (0, 0.0),
        (-0.0, 0.0),
        # above 1.0: clamped (UC-004 A1)
        (2.5, 1.0),
        (1.0000001, 1.0),
        # invalid: the startup ratio (NFR-001) - a negative is a typo, not "drop all"
        (-0.1, 0.5),
        (-1, 0.5),
        (-math.inf, 0.5),
        (math.inf, 0.5),
        (math.nan, 0.5),
        (True, 0.5),
        ("x", 0.5),
        (None, 0.5),
        (10**400, 0.5),  # an int too large for a float must not raise
    ],
)
def test_above_one_clamps_and_invalid_values_fall_back(value, expected):
    resolver = _SampleRatioResolver(default=0.5)

    class _Client:
        def get_float_details(self, key, default_value, context=None):
            return FlagEvaluationDetails(flag_key=key, value=value, reason=Reason.STATIC)

    with patch.object(feature_api, "get_client", return_value=_Client()):
        assert resolver.current() == expected


@pytest.mark.parametrize(
    "bad, message",
    [
        (3.0, "is above 1.0 - clamping to 1.0"),
        (-0.5, "is not a valid ratio"),
        (math.inf, "is not a valid ratio"),
        (math.nan, "is not a valid ratio"),
    ],
)
def test_a_bad_value_is_logged_once(caplog, bad, message):
    resolver = _SampleRatioResolver()

    class _Client:
        def get_float_details(self, key, default_value, context=None):
            return FlagEvaluationDetails(flag_key=key, value=bad, reason=Reason.TARGETING_MATCH)

    with caplog.at_level("WARNING", logger=flags.__name__), patch.object(
        feature_api, "get_client", return_value=_Client()
    ):
        for _ in range(5):
            resolver.current()

    assert len(caplog.records) == 1
    assert message in caplog.records[0].getMessage()


# --------------------------------------------------------------------------
# The sampler
# --------------------------------------------------------------------------

TRACE_IDS = [(i * 0x9E3779B97F4A7C15 + 0x1234) % (1 << 128) for i in range(1, 400)]


@pytest.mark.parametrize("ratio", [0.0, 0.1, 0.5, 1.0])
def test_root_decision_is_the_sdks_trace_id_ratio_rule(monkeypatch, ratio):
    """Same rule as TraceIdRatioBased, so every service applying the same ratio
    to the same trace agrees."""
    monkeypatch.setattr(sampling, "cached_policy", lambda **_: TelemetryPolicy(sample_ratio=ratio))
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
        sampling, "cached_policy", lambda **_: TelemetryPolicy(sample_ratio=ratio["value"])
    )
    monkeypatch.setattr(tracing, "cached_policy", lambda **_: TelemetryPolicy())
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
    monkeypatch.setattr(sampling, "cached_policy", lambda **_: TelemetryPolicy(sample_ratio=0.0))
    monkeypatch.setattr(tracing, "cached_policy", lambda **_: TelemetryPolicy())
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

    def policy(context=None, **_):
        contexts.append(context)
        return TelemetryPolicy(sample_ratio=0.0 if context is not None else 1.0)

    monkeypatch.setattr(tracing, "cached_policy", lambda **_: None)
    monkeypatch.setattr(sampling, "cached_policy", lambda **_: None)
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
    def start(flag: dict, *, startup_ratio: float = 1.0) -> None:
        monkeypatch.setattr(
            flags, "_policy_source", _PolicySource(TelemetryPolicy(sample_ratio=startup_ratio))
        )
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

    # A negative ratio falls back to the startup ratio instead of dropping
    # every trace (NFR-001, UC-004 A1).
    flagd({"defaultVariant": "typo", "variants": {"typo": -0.1}}, startup_ratio=0.25)
    assert flags._policy_source.cached.sample_ratio == 0.25

    flagd({"defaultVariant": "text", "variants": {"text": "lots"}}, startup_ratio=0.25)
    # A type mismatch: the SDK serves the startup value instead.
    assert flags._policy_source.current().sample_ratio == 0.25


def test_real_flagd_fractional_on_the_trace_id_keeps_whole_traces(flagd):
    """The poc#10 update: bucket by targetingKey (= trace id), so a trace is
    in or out as a whole - and in the bucket flagd picks for that trace id."""
    flagd(
        {
            "defaultVariant": "none",
            "variants": {"none": 0.0, "all": 1.0},
            "targeting": {"fractional": [{"var": "targetingKey"}, ["none", 50], ["all", 50]]},
        }
    )
    exporter = InMemorySpanExporter()
    tracer = _tracer(exporter)
    roots = []

    for _ in range(60):
        with tracer.start_as_current_span("root", kind=trace.SpanKind.SERVER) as root:
            roots.append(root.get_span_context().trace_id)
            tracer.start_span("child", kind=trace.SpanKind.CLIENT).end()

    by_trace: dict[int, int] = {}
    for span in exporter.get_finished_spans():
        by_trace[span.context.trace_id] = by_trace.get(span.context.trace_id, 0) + 1
    assert set(by_trace.values()) == {2}, "a trace was half sampled"
    assert 0 < len(by_trace) < 60, "fractional did not split the traces"

    client = feature_api.get_client()
    for trace_id in roots:
        ratio = client.get_float_value(
            SAMPLE_RATIO_FLAG_KEY, -1.0, EvaluationContext(targeting_key=format(trace_id, "032x"))
        )
        assert (trace_id in by_trace) == (ratio == 1.0)


def test_children_never_evaluate_a_targeted_ratio(flagd, monkeypatch):
    flagd(
        {
            "defaultVariant": "all",
            "variants": {"none": 0.0, "all": 1.0},
            "targeting": {"if": [{"==": [{"var": "service.name"}, "nobody"]}, "none", "all"]},
        }
    )
    evaluations = []
    original = flags._SampleRatioResolver._read

    def counting(self, client, context):
        evaluations.append(context)
        return original(self, client, context)

    monkeypatch.setattr(flags._SampleRatioResolver, "_read", counting)
    exporter = InMemorySpanExporter()
    tracer = _tracer(exporter)

    with tracer.start_as_current_span("root", kind=trace.SpanKind.SERVER):
        for _ in range(10):
            tracer.start_span("child").end()

    assert len(evaluations) == 1
    assert len(exporter.get_finished_spans()) == 11



SAMPLED_UPSTREAM = trace.set_span_in_context(
    trace.NonRecordingSpan(
        trace.SpanContext(
            trace_id=0x1234,
            span_id=0x5678,
            is_remote=True,
            trace_flags=trace.TraceFlags(trace.TraceFlags.SAMPLED),
        )
    )
)


@pytest.mark.parametrize(
    "configured, root_sampled, upstream_sampled",
    [
        ("always_off", False, False),  # a hard floor: nothing, ever
        ("parentbased_always_off", False, True),  # roots off; parents followed, as the SDK does
    ],
)
def test_off_samplers_are_hard_floors_the_ratio_flag_cannot_lift(
    monkeypatch, configured, root_sampled, upstream_sampled
):
    """Flags only ever reduce: traceSampleRatio=1.0 doesn't re-enable sampling."""
    monkeypatch.setattr(sampling, "cached_policy", lambda **_: TelemetryPolicy(sample_ratio=1.0))
    monkeypatch.setattr(tracing, "cached_policy", lambda **_: TelemetryPolicy(sample_ratio=1.0))
    exporter = InMemorySpanExporter()
    tracer = _tracer(exporter, sampler=flag_aware_sampler({"OTEL_TRACES_SAMPLER": configured}))

    tracer.start_span("root", kind=trace.SpanKind.SERVER).end()
    tracer.start_span("child-of-upstream", context=SAMPLED_UPSTREAM, kind=trace.SpanKind.SERVER).end()

    exported = {span.name for span in exporter.get_finished_spans()}
    assert ("root" in exported) is root_sampled
    assert ("child-of-upstream" in exported) is upstream_sampled


@pytest.mark.parametrize("configured", ["always_off", "parentbased_always_off"])
def test_off_samplers_install_without_a_warning(configured, caplog):
    with caplog.at_level("WARNING", logger=sampling.__name__):
        flag_aware_sampler({"OTEL_TRACES_SAMPLER": configured})
    assert caplog.records == []



def test_a_bad_value_logs_again_after_the_ratio_was_valid(caplog):
    """Once per state change (NFR-001): bad, valid, bad again logs twice."""
    values = iter([-0.5, -0.5, 0.3, -0.5])

    class _Client:
        def get_float_details(self, key, default_value, context=None):
            return FlagEvaluationDetails(flag_key=key, value=next(values), reason=Reason.TARGETING_MATCH)

    resolver = _SampleRatioResolver()
    with caplog.at_level("WARNING", logger=flags.__name__), patch.object(
        feature_api, "get_client", return_value=_Client()
    ):
        for _ in range(4):
            resolver.current()

    assert len([r for r in caplog.records if "not a valid ratio" in r.getMessage()]) == 2
