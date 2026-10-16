"""Tests for the semconv evaluation context: its shape, and that targeting on it
works end to end against a real flagd provider (FILE resolver)."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from openfeature import api as feature_api
from openfeature.contrib.provider.flagd import FlagdProvider
from openfeature.contrib.provider.flagd.config import ResolverType
from openfeature.evaluation_context import EvaluationContext
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider as SDKTracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from otelfeature_instrument import evaluation_context, flags, tracing
from otelfeature_instrument.evaluation_context import nest, span_context
from otelfeature_instrument.flags import TRACE_VERBOSITY_FLAG_KEY, TelemetryPolicy, _PolicySource
from otelfeature_instrument.tracing import _VerbosityAwareTracerProvider

# --------------------------------------------------------------------------
# nest()
# --------------------------------------------------------------------------


def test_nest_splits_on_every_dot():
    assert nest({"service.name": "weather", "deployment.environment.name": "prod", "x": 1}) == {
        "service": {"name": "weather"},
        "deployment": {"environment": {"name": "prod"}},
        "x": 1,
    }


@pytest.mark.parametrize(
    "flat",
    [
        {"db.system": "postgresql", "db.system.name": "postgresql"},
        {"db.system.name": "postgresql", "db.system": "postgresql"},
    ],
    ids=["leaf-first", "namespace-first"],
)
def test_the_namespace_wins_a_collision_whatever_the_order(flat, caplog):
    with caplog.at_level(logging.DEBUG, logger=evaluation_context.__name__):
        assert nest(flat) == {"db": {"system": {"name": "postgresql"}}}
    assert "'db.system'" in caplog.text


def test_array_attributes_become_lists():
    assert nest({"http.request.header.accept": ("a", "b")}) == {
        "http": {"request": {"header": {"accept": ["a", "b"]}}}
    }


def test_span_context_shape():
    context = span_context(
        base={"service.name": "weather", "telemetry.sdk.language": "python"},
        scope_name="opentelemetry.instrumentation.fastapi",
        scope_version="0.66b1",
        name="GET /health",
        kind=trace.SpanKind.SERVER,
        attributes={"url.path": "/health", "http.request.method": "GET"},
        trace_id=0xABC,
    )

    assert context.targeting_key == "00000000000000000000000000000abc"
    assert context.attributes == {
        "service": {"name": "weather"},
        "telemetry": {"sdk": {"language": "python"}},
        "otel": {"scope": {"name": "opentelemetry.instrumentation.fastapi", "version": "0.66b1"}},
        "span": {"kind": "server", "name": "GET /health"},
        "url": {"path": "/health"},
        "http": {"request": {"method": "GET"}},
    }


# --------------------------------------------------------------------------
# What the tracer hands to the flags
# --------------------------------------------------------------------------

RESOURCE = Resource.create(
    {
        "service.name": "weather",
        "service.version": "1.2.3",
        "deployment.environment.name": "demo",
        "host.name": "not-in-the-context",
    }
)


def _tracer(exporter=None):
    provider = SDKTracerProvider(resource=RESOURCE)
    if exporter is not None:
        provider.add_span_processor(SimpleSpanProcessor(exporter))
    return _VerbosityAwareTracerProvider(provider).get_tracer("my.scope", "9.9")


@pytest.fixture
def seen_contexts(monkeypatch):
    seen: list[EvaluationContext] = []

    def spy(context=None, **_):
        seen.append(context)
        return TelemetryPolicy()

    monkeypatch.setattr(tracing, "cached_policy", lambda **_: None)
    monkeypatch.setattr(tracing, "current_policy", spy)
    return seen


def test_root_span_keeps_the_trace_id_it_was_evaluated_on(seen_contexts):
    exporter = InMemorySpanExporter()
    tracer = _tracer(exporter)

    with tracer.start_as_current_span("root", kind=trace.SpanKind.SERVER) as root:
        with tracer.start_as_current_span("child", kind=trace.SpanKind.CLIENT):
            pass
    tracer.start_span("another-root").end()

    root_id = format(root.get_span_context().trace_id, "032x")
    keys = [context.targeting_key for context in seen_contexts]
    assert keys[:2] == [root_id, root_id]
    assert keys[2] != root_id
    spans = {span.name: span for span in exporter.get_finished_spans()}
    assert format(spans["another-root"].context.trace_id, "032x") == keys[2]


def test_tracer_context_carries_resource_scope_and_start_attributes(seen_contexts):
    _tracer().start_span(
        "GET /health", kind=trace.SpanKind.SERVER, attributes={"url.path": "/health"}
    ).end()

    (context,) = seen_contexts
    assert context.attributes == {
        "service": {"name": "weather", "version": "1.2.3"},
        "deployment": {"environment": {"name": "demo"}},
        "telemetry": {"sdk": {"language": "python"}},
        "otel": {"scope": {"name": "my.scope", "version": "9.9"}},
        "span": {"kind": "server", "name": "GET /health"},
        "url": {"path": "/health"},
    }


def test_a_suppressed_root_does_not_leak_its_preset_trace_id(monkeypatch):
    """The id minted for a suppressed root must not be reused by the next span."""
    decisions = iter([TelemetryPolicy(enabled=False), TelemetryPolicy()])
    keys: list[str] = []

    def policy(context=None, **_):
        keys.append(context.targeting_key)
        return next(decisions)

    monkeypatch.setattr(tracing, "cached_policy", lambda **_: None)
    monkeypatch.setattr(tracing, "current_policy", policy)
    exporter = InMemorySpanExporter()
    tracer = _tracer(exporter)

    tracer.start_span("suppressed", kind=trace.SpanKind.SERVER).end()
    tracer.start_span("kept", kind=trace.SpanKind.SERVER).end()

    (kept,) = exporter.get_finished_spans()
    assert format(kept.context.trace_id, "032x") == keys[1] != keys[0]


def test_static_path_builds_no_context(monkeypatch):
    monkeypatch.setattr(tracing, "cached_policy", lambda **_: TelemetryPolicy())
    monkeypatch.setattr(
        tracing, "span_context", lambda **_: pytest.fail("built a context on the static path")
    )
    monkeypatch.setattr(
        tracing, "current_policy", lambda *_, **__: pytest.fail("evaluated on the static path")
    )
    exporter = InMemorySpanExporter()
    tracer = _tracer(exporter)

    with tracer.start_as_current_span("root", kind=trace.SpanKind.SERVER):
        tracer.start_span("child").end()

    assert len(exporter.get_finished_spans()) == 2


# --------------------------------------------------------------------------
# End to end against real flagd (FILE resolver)
# --------------------------------------------------------------------------


def _flags(targeting: dict) -> dict:
    return {
        "$schema": "https://flagd.dev/schema/v0/flags.json",
        "flags": {
            TRACE_VERBOSITY_FLAG_KEY: {
                "state": "ENABLED",
                "defaultVariant": "full",
                "variants": {"full": "FULL", "io": "IO", "off": "OFF"},
                "targeting": targeting,
            }
        },
    }


@pytest.fixture
def flagd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Starts a real flagd FILE provider on the given ruleset, with a fresh,
    unclassified policy source so every span evaluates against it."""
    monkeypatch.setattr(flags, "_policy_source", _PolicySource())

    def start(ruleset: dict) -> None:
        path = tmp_path / "flags.json"
        path.write_text(json.dumps(ruleset), encoding="utf-8")
        feature_api.set_provider_and_wait(
            FlagdProvider(resolver_type=ResolverType.FILE, offline_flag_source_path=str(path))
        )

    yield start
    feature_api.clear_providers()


def test_real_flagd_targets_health_checks_by_service_kind_and_path(flagd):
    flagd(
        _flags(
            {
                "if": [
                    {
                        "and": [
                            {"==": [{"var": "service.name"}, "weather"]},
                            {"==": [{"var": "span.kind"}, "server"]},
                            {"==": [{"var": "url.path"}, "/health"]},
                        ]
                    },
                    "off",
                    "full",
                ]
            }
        )
    )
    exporter = InMemorySpanExporter()
    tracer = _tracer(exporter)

    for path in ("/health", "/weather"):
        tracer.start_span(
            f"GET {path}", kind=trace.SpanKind.SERVER, attributes={"url.path": path}
        ).end()
    # Same path, wrong kind: the rule must not match a client span.
    tracer.start_span(
        "GET /health", kind=trace.SpanKind.CLIENT, attributes={"url.path": "/health"}
    ).end()

    names = [(span.name, span.kind) for span in exporter.get_finished_spans()]
    assert names == [("GET /weather", trace.SpanKind.SERVER), ("GET /health", trace.SpanKind.CLIENT)]


def test_real_flagd_fractional_buckets_a_whole_trace_alike(flagd):
    flagd(_flags({"fractional": [{"var": "targetingKey"}, ["io", 50], ["full", 50]]}))
    exporter = InMemorySpanExporter()
    tracer = _tracer(exporter)

    for _ in range(40):
        with tracer.start_as_current_span("request", kind=trace.SpanKind.SERVER):
            for step in range(3):
                tracer.start_span(f"step-{step}").end()

    by_trace: dict[int, list[str]] = {}
    for span in exporter.get_finished_spans():
        by_trace.setdefault(span.context.trace_id, []).append(span.name)
    kept_steps = {len(names) - 1 for names in by_trace.values()}

    # Every SERVER span survives IO, so every trace is there; its INTERNAL
    # spans are all kept or all dropped - never some of each.
    assert len(by_trace) == 40
    assert kept_steps == {0, 3}, "a trace was split across buckets"

    # And the bucket is the one flagd picks for the trace id itself.
    client = feature_api.get_client()
    for trace_id, names in by_trace.items():
        level = client.get_string_value(
            TRACE_VERBOSITY_FLAG_KEY,
            "FULL",
            EvaluationContext(targeting_key=format(trace_id, "032x")),
        )
        assert (len(names) == 1) == (level == "IO")



def test_a_span_started_during_evaluation_does_not_take_the_roots_trace_id(monkeypatch):
    """E.g. a hook or an instrumented provider starting a span of its own."""
    provider = SDKTracerProvider(resource=RESOURCE)
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    wrapper = _VerbosityAwareTracerProvider(provider)
    tracer = wrapper.get_tracer("my.scope")
    sneaky = provider.get_tracer("hook")  # straight on the SDK, during evaluation
    keys: list[str] = []

    def policy(context=None, **_):
        keys.append(context.targeting_key)
        sneaky.start_span("during-evaluation").end()
        return TelemetryPolicy()

    monkeypatch.setattr(tracing, "cached_policy", lambda **_: None)
    monkeypatch.setattr(tracing, "current_policy", policy)

    tracer.start_span("root", kind=trace.SpanKind.SERVER).end()

    spans = {span.name: span for span in exporter.get_finished_spans()}
    assert format(spans["root"].context.trace_id, "032x") == keys[0]
    assert spans["during-evaluation"].context.trace_id != spans["root"].context.trace_id


def test_a_tracer_obtained_before_wrapping_still_keeps_the_evaluated_trace_id(seen_contexts):
    provider = SDKTracerProvider(resource=RESOURCE)
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    provider.get_tracer("lib", "1")  # cached by the SDK with the old generator

    _VerbosityAwareTracerProvider(provider).get_tracer("lib", "1").start_span("root").end()

    (root,) = exporter.get_finished_spans()
    assert format(root.context.trace_id, "032x") == seen_contexts[0].targeting_key


def test_start_attributes_cannot_replace_or_hide_extension_keys():
    context = span_context(
        base={"service.name": "weather"},
        scope_name="my.scope",
        scope_version=None,
        name="GET /",
        kind=trace.SpanKind.SERVER,
        attributes={
            "service.name": "spoofed",
            "span.kind.detail": "x",
            "otel": "leaf",
            "url.path": "/",
        },
        trace_id=None,
    )

    assert context.attributes == {
        "service": {"name": "weather"},
        "otel": {"scope": {"name": "my.scope"}},
        "span": {"kind": "server", "name": "GET /"},
        "url": {"path": "/"},
    }


def test_attribute_values_of_every_otel_type_stay_json_shaped():
    assert nest(
        {"a": b"ab", "b": (1, 2), "c": [True, False], "d": 1.5, "e": ("x", b"y")}
    ) == {"a": "ab", "b": [1, 2], "c": [True, False], "d": 1.5, "e": ["x", "y"]}
