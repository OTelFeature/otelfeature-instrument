"""Tests for deciding once per tracer when a rule reads only tracer-level keys."""

from __future__ import annotations

import json
import time
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

from openfeature.evaluation_context import EvaluationContext
from openfeature.hook import Hook
from openfeature.transaction_context import ContextVarsTransactionContextPropagator

from otelfeature_instrument import flags, rule_scope, tracing
from otelfeature_instrument.flags import (
    _RECLASSIFY_ON,
    SAMPLE_RATIO_FLAG_KEY,
    TRACE_VERBOSITY_FLAG_KEY,
    _PolicySource,
)
from otelfeature_instrument.rule_scope import reads_only_tracer_keys
from otelfeature_instrument.sampling import flag_aware_sampler
from otelfeature_instrument.tracing import _VerbosityAwareTracerProvider


def _if(condition):
    return {"if": [condition, "off", "full"]}


def _eq(key, value):
    return {"==": [{"var": key}, value]}


@pytest.mark.parametrize(
    "rule, tracer_level",
    [
        (_if(_eq("otel.scope.name", "opentelemetry.instrumentation.httpx")), True),
        (_if({"and": [_eq("service.name", "weather"), _eq("otel.scope.name", "x")]}), True),
        (_if({"in": [{"var": "otel.scope.name"}, ["a", "b"]]}), True),
        (_if(_eq("deployment.environment.name", "prod")), True),
        (_if({"missing": ["service.name"]}), True),
        (_if(_eq("$flagd.flagKey", "x")), True),
        (_if({"==": [{"var": ["service.name", "unknown"]}, "weather"]}), True),
        ({"fractional": [{"var": "service.name"}, ["off", 50], ["full", 50]]}, True),
        # span-level, time-dependent or unknowable: per span, as today
        (_if(_eq("span.kind", "server")), False),
        (_if(_eq("url.path", "/health")), False),
        (_if(_eq("otelfeature.http.server.error_rate", 0.1)), False),
        (_if(_eq("targetingKey", "abc")), False),
        (_if({">": [{"var": "$flagd.timestamp"}, 0]}), False),
        ({"fractional": [["off", 50], ["full", 50]]}, False),  # implicit targetingKey
        (_if({"==": [{"var": "service"}, {}]}), False),  # a namespace span attrs can join
        (_if({"==": [{"var": {"cat": ["service", ".name"]}}, "weather"]}), False),
        (_if({"missing_some": [1, ["service.name", "url.path"]]}), False),
        (_if({"missing_some": [{"var": "http.n"}, ["service.name"]]}), False),
        (_if({"missing_some": [1, ["service.name", "service.namespace"]]}), True),
        # fractional buckets on targetingKey unless its first argument is a
        # string - a computed one may not be
        ({"fractional": [{"merge": [["off", 50]]}, ["full", 50]]}, False),
        ({"fractional": ["constant", ["off", 50], ["full", 50]]}, True),
    ],
)
def test_which_rules_are_decided_per_tracer(rule, tracer_level):
    assert reads_only_tracer_keys(rule) is tracer_level


def test_a_provider_whose_rules_cannot_be_read_stays_per_span():
    feature_api.clear_providers()  # the no-op provider has no flagd evaluator
    assert rule_scope.targeting_of(TRACE_VERBOSITY_FLAG_KEY) is rule_scope._UNKNOWN
    assert rule_scope.is_tracer_scoped(TRACE_VERBOSITY_FLAG_KEY) is False


# --------------------------------------------------------------------------
# End to end against real flagd (FILE resolver)
# --------------------------------------------------------------------------


def _ruleset(level_targeting=None, ratio=None):
    level = {"state": "ENABLED", "defaultVariant": "full", "variants": {"full": "FULL", "off": "OFF"}}
    if level_targeting is not None:
        level["targeting"] = level_targeting
    ruleset = {"flags": {TRACE_VERBOSITY_FLAG_KEY: level}}
    if ratio is not None:
        ruleset["flags"][SAMPLE_RATIO_FLAG_KEY] = {"state": "ENABLED", **ratio}
    return ruleset


@pytest.fixture
def flagd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A real flagd FILE provider whose events drive a fresh policy source."""
    monkeypatch.setenv("FLAGD_OFFLINE_POLL_MS", "50")
    source = _PolicySource()
    monkeypatch.setattr(flags, "_policy_source", source)
    path = tmp_path / "flags.json"

    def start(ruleset: dict) -> Path:
        path.write_text(json.dumps(ruleset), encoding="utf-8")
        feature_api.set_provider_and_wait(
            FlagdProvider(resolver_type=ResolverType.FILE, offline_flag_source_path=str(path))
        )
        for event in _RECLASSIFY_ON:
            feature_api.add_handler(event, source.classify)
        deadline = time.monotonic() + 5
        while source.generation == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        return path

    yield start
    for event in _RECLASSIFY_ON:
        feature_api.remove_handler(event, source.classify)
    feature_api.clear_providers()


@pytest.fixture
def level_evaluations(monkeypatch):
    seen = []
    original = flags._VerbosityResolver._read

    def counting(self, client, context):
        seen.append(context)
        return original(self, client, context)

    monkeypatch.setattr(flags._VerbosityResolver, "_read", counting)
    return seen


def _provider(exporter, service="weather"):
    provider = SDKTracerProvider(
        resource=Resource.create({"service.name": service}), sampler=flag_aware_sampler({})
    )
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return _VerbosityAwareTracerProvider(provider)


def test_a_muted_instrumentation_is_decided_once_per_tracer(flagd, level_evaluations):
    flagd(_ruleset(_if(_eq("otel.scope.name", "noisy.instrumentation"))))
    exporter = InMemorySpanExporter()
    provider = _provider(exporter)
    noisy, quiet = provider.get_tracer("noisy.instrumentation"), provider.get_tracer("app")
    level_evaluations.clear()  # ignore the classification probes

    for _ in range(50):
        with quiet.start_as_current_span("request", kind=trace.SpanKind.SERVER):
            noisy.start_span("chatter", kind=trace.SpanKind.CLIENT).end()

    assert {span.name for span in exporter.get_finished_spans()} == {"request"}
    # Once per tracer and kind of span it starts (quiet: roots, noisy:
    # children), never per span.
    assert len(level_evaluations) == 2
    assert all(context.targeting_key is None for context in level_evaluations)


def test_a_ruleset_change_re_decides_every_tracer(flagd, level_evaluations):
    path = flagd(_ruleset(_if(_eq("otel.scope.name", "noisy.instrumentation"))))
    exporter = InMemorySpanExporter()
    noisy = _provider(exporter).get_tracer("noisy.instrumentation")
    noisy.start_span("before").end()
    assert exporter.get_finished_spans() == ()

    generation = flags._policy_source.generation
    path.write_text(json.dumps(_ruleset(_if(_eq("otel.scope.name", "something.else")))), encoding="utf-8")
    deadline = time.monotonic() + 10
    while flags._policy_source.generation == generation and time.monotonic() < deadline:
        time.sleep(0.02)

    noisy.start_span("after").end()
    assert [span.name for span in exporter.get_finished_spans()] == ["after"]


def test_span_level_rules_are_still_evaluated_per_span(flagd, level_evaluations):
    flagd(_ruleset(_if(_eq("url.path", "/health"))))
    exporter = InMemorySpanExporter()
    tracer = _provider(exporter).get_tracer("app")
    level_evaluations.clear()

    for path in ["/health", "/weather"] * 10:
        tracer.start_span(path, kind=trace.SpanKind.SERVER, attributes={"url.path": path}).end()

    assert len(level_evaluations) == 20
    assert {span.name for span in exporter.get_finished_spans()} == {"/weather"}


def test_a_ratio_by_service_is_decided_per_tracer_and_reaches_the_sampler(flagd):
    flagd(
        _ruleset(
            ratio={
                "defaultVariant": "all",
                "variants": {"none": 0.0, "all": 1.0},
                "targeting": {"if": [_eq("service.name", "weather"), "none", "all"]},
            }
        )
    )
    exporter = InMemorySpanExporter()

    for _ in range(20):
        _provider(exporter, "weather").get_tracer("app").start_span("w").end()
        _provider(exporter, "gateway").get_tracer("app").start_span("g").end()

    assert {span.name for span in exporter.get_finished_spans()} == {"g"}
    assert len(exporter.get_finished_spans()) == 20



# --------------------------------------------------------------------------
# Per tracer must never differ from per span
# --------------------------------------------------------------------------

SPANS = [
    # (resource, scope, scope version, start attributes)
    ({"service.name": "weather"}, "app", None, {}),
    ({"service.name": "weather"}, "app", None, {"deployment.environment.name": "prod"}),
    ({"service.name": "weather"}, "app", None, {"otel.scope.version": "2"}),
    ({"service.name": "weather", "deployment.environment.name": "prod"}, "app", "1", {}),
    ({"service.name": "gateway"}, "noisy", "1", {"service.namespace": "x"}),
]

RULES = [
    _if(_eq("otel.scope.name", "noisy")),
    _if(_eq("deployment.environment.name", "prod")),  # absent on most resources
    _if(_eq("otel.scope.version", "2")),  # absent on version-less tracers
    _if(_eq("service.namespace", "x")),
    _if({"missing": ["deployment.environment.name"]}),
    # (A fractional on targetingKey is per span by classification, and its
    # outcome depends on random trace ids, so two runs can't be compared.)
    {"fractional": [{"var": "service.name"}, ["off", 50], ["full", 50]]},
]


def _exported() -> list[tuple[int, int]]:
    out = []
    for i, (resource, scope, version, attributes) in enumerate(SPANS):
        exporter = InMemorySpanExporter()
        provider = SDKTracerProvider(resource=Resource.create(resource), sampler=flag_aware_sampler({}))
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        tracer = _VerbosityAwareTracerProvider(provider).get_tracer(scope, version)
        for j in range(8):
            tracer.start_span(f"{i}-{j}", attributes=attributes).end()
        out.append((i, len(exporter.get_finished_spans())))
    return out


@pytest.mark.parametrize("rule", RULES, ids=[json.dumps(r)[:60] for r in RULES])
def test_per_tracer_and_per_span_always_agree(flagd, rule):
    flagd(_ruleset(rule))
    per_tracer = _exported()
    with pytest.MonkeyPatch.context() as forced:
        forced.setattr(tracing._VerbosityAwareTracer, "_tracer_policy", lambda self, root: None)
        per_span = _exported()

    assert per_tracer == per_span


def test_a_rule_on_a_key_the_resource_lacks_stays_per_span(flagd, level_evaluations):
    flagd(_ruleset(_if(_eq("deployment.environment.name", "prod"))))
    exporter = InMemorySpanExporter()
    tracer = _provider(exporter).get_tracer("app")  # resource has no environment
    level_evaluations.clear()

    tracer.start_span("a", attributes={"deployment.environment.name": "prod"}).end()
    tracer.start_span("b").end()

    assert len(level_evaluations) == 2
    assert [span.name for span in exporter.get_finished_spans()] == ["b"]


def test_a_transaction_context_keeps_evaluation_per_span(flagd, level_evaluations):
    """The SDK merges the transaction context in; it can differ per request."""
    flagd(_ruleset(_if(_eq("otel.scope.name", "noisy.instrumentation"))))
    feature_api.set_transaction_context_propagator(ContextVarsTransactionContextPropagator())
    try:
        exporter = InMemorySpanExporter()
        tracer = _provider(exporter).get_tracer("app")
        level_evaluations.clear()
        for _ in range(5):
            tracer.start_span("x").end()
        assert len(level_evaluations) == 5
    finally:
        feature_api.clear_transaction_context_propagator()


@pytest.mark.parametrize("ambient", ["global context", "api hook"])
def test_ambient_context_sources_keep_evaluation_per_span(flagd, level_evaluations, ambient):
    flagd(_ruleset(_if(_eq("otel.scope.name", "noisy.instrumentation"))))
    if ambient == "global context":
        feature_api.set_evaluation_context(EvaluationContext(attributes={"region": "eu"}))
    else:
        feature_api.add_hooks([Hook()])
    try:
        assert rule_scope.ambient_context_is_fixed() is False
        tracer = _provider(InMemorySpanExporter()).get_tracer("app")
        level_evaluations.clear()
        for _ in range(3):
            tracer.start_span("x").end()
        assert len(level_evaluations) == 3
    finally:
        feature_api.clear_evaluation_context()
        feature_api.clear_hooks()


def test_flagds_own_sync_metadata_hook_is_not_ambient(flagd):
    flagd(_ruleset())
    assert rule_scope.ambient_context_is_fixed() is True
