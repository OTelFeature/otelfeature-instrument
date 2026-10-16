"""Tests for the decision attributes recorded on kept root spans."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from openfeature import api as feature_api
from openfeature.contrib.provider.flagd import FlagdProvider
from openfeature.contrib.provider.flagd.config import ResolverType
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider as SDKTracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from otelfeature_instrument import flags
from otelfeature_instrument.flags import SAMPLE_RATIO_FLAG_KEY, TelemetryPolicy, _PolicySource
from otelfeature_instrument.sampling import flag_aware_sampler
from otelfeature_instrument.tracing import _VerbosityAwareTracerProvider

DECISION_KEYS = {"feature_flag.key", "feature_flag.result.variant", "otelfeature.sample_ratio"}


@pytest.fixture
def flagd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def start(flag: dict | None, *, startup_ratio: float = 1.0) -> None:
        monkeypatch.setattr(
            flags, "_policy_source", _PolicySource(TelemetryPolicy(sample_ratio=startup_ratio))
        )
        ruleset = {"flags": {} if flag is None else {SAMPLE_RATIO_FLAG_KEY: {"state": "ENABLED", **flag}}}
        path = tmp_path / "flags.json"
        path.write_text(json.dumps(ruleset), encoding="utf-8")
        feature_api.set_provider_and_wait(
            FlagdProvider(resolver_type=ResolverType.FILE, offline_flag_source_path=str(path))
        )
        flags._policy_source.classify()

    yield start
    feature_api.clear_providers()


def _traced(n: int = 300):
    exporter = InMemorySpanExporter()
    provider = SDKTracerProvider(resource=Resource.create(), sampler=flag_aware_sampler({}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = _VerbosityAwareTracerProvider(provider).get_tracer("t")
    for _ in range(n):
        with tracer.start_as_current_span(
            "GET /", kind=trace.SpanKind.SERVER, attributes={"url.path": "/"}
        ):
            tracer.start_span("child", kind=trace.SpanKind.CLIENT).end()
    return exporter.get_finished_spans()


def test_a_sampled_root_after_a_ratio_of_a_tenth_carries_the_decision(flagd):
    flagd({"defaultVariant": "tenth", "variants": {"tenth": 0.1, "all": 1.0}})

    spans = _traced()
    roots = [s for s in spans if s.parent is None]

    assert roots, "nothing sampled at all"
    for root in roots:
        assert root.attributes["feature_flag.key"] == SAMPLE_RATIO_FLAG_KEY
        assert root.attributes["feature_flag.result.variant"] == "tenth"
        assert root.attributes["otelfeature.sample_ratio"] == 0.1
        assert root.attributes["url.path"] == "/"  # start attributes kept


def test_only_roots_carry_it(flagd):
    flagd({"defaultVariant": "half", "variants": {"half": 0.5}})

    children = [s for s in _traced() if s.parent is not None]

    assert children
    assert all(DECISION_KEYS.isdisjoint(child.attributes) for child in children)


@pytest.mark.parametrize(
    "flag, startup_ratio",
    [
        (None, 1.0),  # no flag at all
        ({"defaultVariant": "all", "variants": {"all": 1.0}}, 1.0),  # flag = startup value
        ({"defaultVariant": "text", "variants": {"text": "lots"}}, 0.5),  # unreadable: fallback
    ],
    ids=["no-flag", "flag-at-default", "fallback"],
)
def test_nothing_is_recorded_at_defaults(flagd, flag, startup_ratio):
    flagd(flag, startup_ratio=startup_ratio)

    spans = _traced()

    assert spans
    assert all(DECISION_KEYS.isdisjoint(span.attributes) for span in spans)


def test_a_targeted_ratio_records_the_variant_that_decided(flagd):
    flagd(
        {
            "defaultVariant": "low",
            "variants": {"low": 0.2, "high": 0.9},
            "targeting": {"fractional": [{"var": "targetingKey"}, ["low", 50], ["high", 50]]},
        }
    )

    roots = [s for s in _traced(400) if s.parent is None]
    variants = {root.attributes["feature_flag.result.variant"] for root in roots}
    ratios = {
        root.attributes["feature_flag.result.variant"]: root.attributes["otelfeature.sample_ratio"]
        for root in roots
    }

    assert variants == {"low", "high"}
    assert ratios == {"low": 0.2, "high": 0.9}


def test_the_static_path_builds_the_attributes_once(flagd):
    flagd({"defaultVariant": "quarter", "variants": {"quarter": 0.25}})
    first = flags._policy_source.cached.decision_attributes

    _traced(50)

    assert flags._policy_source.cached.decision_attributes is first
    assert TelemetryPolicy().decision_attributes is None


def test_the_shared_attributes_are_read_only():
    policy = _PolicySource()._combine(True, "FULL", (0.25, True, "quarter"))

    with pytest.raises(TypeError):
        policy.decision_attributes["feature_flag.key"] = "tampered"


class _NoVariantClient:
    """A provider that reports values but no variants (they're optional)."""

    def __init__(self, ratio):
        self.ratio = ratio

    def _get(self, key, default_value, context=None):
        from openfeature.flag_evaluation import FlagEvaluationDetails, Reason

        value = self.ratio if key == SAMPLE_RATIO_FLAG_KEY else default_value
        return FlagEvaluationDetails(flag_key=key, value=value, reason=Reason.STATIC)

    get_string_details = get_boolean_details = get_float_details = _get


def test_a_provider_without_variants_still_records_the_decision(monkeypatch):
    monkeypatch.setattr(flags, "_policy_source", _PolicySource())
    monkeypatch.setattr(feature_api, "get_client", lambda: _NoVariantClient(0.5))
    flags._policy_source.classify()

    roots = [s for s in _traced(100) if s.parent is None]

    assert roots
    for root in roots:
        assert root.attributes["feature_flag.key"] == SAMPLE_RATIO_FLAG_KEY
        assert root.attributes["otelfeature.sample_ratio"] == 0.5
        assert "feature_flag.result.variant" not in root.attributes


@pytest.mark.parametrize(
    "variant_value, startup_ratio, recorded",
    [
        (5, 0.5, 1.0),  # clamped to 1.0, which differs from the startup 0.5
        (5, 1.0, None),  # clamped to exactly the startup value: no decision
        (1, 1.0, None),  # an int equal to the startup float
    ],
)
def test_clamped_and_int_values(flagd, variant_value, startup_ratio, recorded):
    flagd({"defaultVariant": "v", "variants": {"v": variant_value}}, startup_ratio=startup_ratio)

    roots = [s for s in _traced(50) if s.parent is None]

    assert roots
    assert {root.attributes.get("otelfeature.sample_ratio") for root in roots} == {recorded}


def test_the_applications_own_attributes_win(flagd):
    flagd({"defaultVariant": "all", "variants": {"all": 1.0}}, startup_ratio=0.5)
    exporter = InMemorySpanExporter()
    provider = SDKTracerProvider(resource=Resource.create(), sampler=flag_aware_sampler({}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = _VerbosityAwareTracerProvider(provider).get_tracer("t")

    tracer.start_span("root", attributes={"feature_flag.key": "appFlag"}).end()

    (root,) = exporter.get_finished_spans()
    assert root.attributes["feature_flag.key"] == "appFlag"
    assert root.attributes["otelfeature.sample_ratio"] == 1.0
