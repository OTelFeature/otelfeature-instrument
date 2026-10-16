"""Tests for `recordSpan`: dropping just the spans a rule matches."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from openfeature import api as feature_api
from openfeature.contrib.provider.flagd import FlagdProvider
from openfeature.contrib.provider.flagd.config import ResolverType
from opentelemetry import trace
from opentelemetry.propagate import inject
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider as SDKTracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from otelfeature_instrument import flags
from otelfeature_instrument.flags import (
    OTEL_ENABLED_FLAG_KEY,
    RECORD_SPAN_FLAG_KEY,
    TRACE_VERBOSITY_FLAG_KEY,
    TelemetryPolicy,
    _PolicySource,
    record_span_flag_key,
)
from otelfeature_instrument.tracing import _VerbosityAwareTracerProvider

HEALTH_CHECKS = {
    "state": "ENABLED",
    "defaultVariant": "on",
    "variants": {"on": True, "off": False},
    "targeting": {
        "if": [
            {
                "and": [
                    {"==": [{"var": "service.name"}, "geocode"]},
                    {"==": [{"var": "url.path"}, "/health"]},
                ]
            },
            "off",
            None,
        ]
    },
}


@pytest.fixture
def flagd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def start(ruleset_flags: dict, *, defaults: TelemetryPolicy | None = None) -> None:
        monkeypatch.setattr(flags, "_policy_source", _PolicySource(defaults))
        path = tmp_path / "flags.json"
        path.write_text(json.dumps({"flags": ruleset_flags}), encoding="utf-8")
        feature_api.set_provider_and_wait(
            FlagdProvider(resolver_type=ResolverType.FILE, offline_flag_source_path=str(path))
        )
        flags._policy_source.classify()

    yield start
    feature_api.clear_providers()


def _tracer(exporter, service="geocode", scope="app"):
    provider = SDKTracerProvider(resource=Resource.create({"service.name": service}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return _VerbosityAwareTracerProvider(provider).get_tracer(scope)


def _request(tracer, path):
    """A server span with what an HTTP instrumentation does inside it."""
    with tracer.start_as_current_span(
        f"GET {path}", kind=trace.SpanKind.SERVER, attributes={"url.path": path}
    ):
        with tracer.start_as_current_span("handler"):
            tracer.start_span("GET upstream", kind=trace.SpanKind.CLIENT).end()
        headers: dict[str, str] = {}
        inject(headers)
        return headers


def test_health_checks_leave_no_spans_and_other_routes_are_untouched(flagd):
    flagd({RECORD_SPAN_FLAG_KEY: HEALTH_CHECKS})
    exporter = InMemorySpanExporter()
    tracer = _tracer(exporter)

    _request(tracer, "/health")
    assert exporter.get_finished_spans() == ()

    _request(tracer, "/weather")
    assert {s.name for s in exporter.get_finished_spans()} == {
        "GET /weather",
        "handler",
        "GET upstream",
    }


def test_the_same_path_in_another_service_is_untouched(flagd):
    flagd({RECORD_SPAN_FLAG_KEY: HEALTH_CHECKS})
    exporter = InMemorySpanExporter()

    _request(_tracer(exporter, service="weather"), "/health")

    assert len(exporter.get_finished_spans()) == 3


def test_a_dropped_root_propagates_not_sampled(flagd):
    flagd({RECORD_SPAN_FLAG_KEY: HEALTH_CHECKS})
    exporter = InMemorySpanExporter()

    headers = _request(_tracer(exporter), "/health")

    # A valid traceparent with sampled=0: downstream services join the
    # dropped trace (and drop too) instead of starting traces of their own.
    version, trace_id, span_id, flags_ = headers["traceparent"].split("-")
    assert int(trace_id, 16) and int(span_id, 16)
    assert flags_ == "00"
    assert exporter.get_finished_spans() == ()


def test_a_dropped_root_started_with_start_span_is_unsampled_too(flagd):
    flagd({RECORD_SPAN_FLAG_KEY: HEALTH_CHECKS})
    exporter = InMemorySpanExporter()
    tracer = _tracer(exporter)

    root = tracer.start_span("GET /health", kind=trace.SpanKind.SERVER, attributes={"url.path": "/health"})
    with trace.use_span(root, end_on_exit=True):
        tracer.start_span("child").end()

    assert root.get_span_context().is_valid
    assert not root.get_span_context().trace_flags.sampled
    assert exporter.get_finished_spans() == ()


def test_a_dropped_scope_lets_its_children_attach_to_the_nearest_kept_ancestor(flagd):
    flagd(
        {
            RECORD_SPAN_FLAG_KEY: {
                "state": "ENABLED",
                "defaultVariant": "on",
                "variants": {"on": True, "off": False},
                "targeting": {
                    "if": [{"==": [{"var": "otel.scope.name"}, "opentelemetry.instrumentation.jdbc"]}, "off", None]
                },
            }
        }
    )
    exporter = InMemorySpanExporter()
    provider = SDKTracerProvider(resource=Resource.create({"service.name": "weather"}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    wrapper = _VerbosityAwareTracerProvider(provider)
    app, jdbc = wrapper.get_tracer("app"), wrapper.get_tracer("opentelemetry.instrumentation.jdbc")

    with app.start_as_current_span("GET /weather", kind=trace.SpanKind.SERVER):
        with jdbc.start_as_current_span("SELECT weather", kind=trace.SpanKind.CLIENT):
            app.start_span("row mapping").end()

    spans = {s.name: s for s in exporter.get_finished_spans()}
    assert set(spans) == {"GET /weather", "row mapping"}
    assert spans["row mapping"].parent.span_id == spans["GET /weather"].context.span_id


def test_static_record_span_adds_no_per_span_evaluation(flagd, monkeypatch):
    flagd({RECORD_SPAN_FLAG_KEY: {"state": "ENABLED", "defaultVariant": "on", "variants": {"on": True, "off": False}}})
    evaluations = []
    original = flags._RecordSpanResolver._read

    def counting(self, client, context):
        evaluations.append(context)
        return original(self, client, context)

    monkeypatch.setattr(flags._RecordSpanResolver, "_read", counting)
    exporter = InMemorySpanExporter()

    for _ in range(10):
        _request(_tracer(exporter), "/weather")

    assert evaluations == []
    assert len(exporter.get_finished_spans()) == 30


@pytest.mark.parametrize(
    "ruleset",
    [{}, {RECORD_SPAN_FLAG_KEY: {"state": "ENABLED", "defaultVariant": "x", "variants": {"x": "nope"}}}],
    ids=["missing", "wrong-type"],
)
def test_missing_or_broken_flag_records(flagd, ruleset):
    flagd(ruleset)
    exporter = InMemorySpanExporter()

    _request(_tracer(exporter), "/health")

    assert len(exporter.get_finished_spans()) == 3


@pytest.mark.parametrize(
    "global_flag, defaults",
    [
        ({OTEL_ENABLED_FLAG_KEY: {"state": "ENABLED", "defaultVariant": "off", "variants": {"off": False}}}, None),
        ({TRACE_VERBOSITY_FLAG_KEY: {"state": "ENABLED", "defaultVariant": "off", "variants": {"off": "OFF"}}}, None),
        ({}, TelemetryPolicy.from_environment({"OTEL_SDK_DISABLED": "true"})),
    ],
    ids=["otelEnabled=false", "telemetryLevel=OFF", "OTEL_SDK_DISABLED"],
)
def test_record_span_true_never_re_enables_what_the_global_switches_turned_off(
    flagd, global_flag, defaults
):
    flagd(
        {
            **global_flag,
            RECORD_SPAN_FLAG_KEY: {"state": "ENABLED", "defaultVariant": "on", "variants": {"on": True}},
        },
        defaults=defaults,
    )
    exporter = InMemorySpanExporter()

    _request(_tracer(exporter), "/weather")

    assert exporter.get_finished_spans() == ()


@pytest.mark.parametrize(
    "environ, key",
    [({}, "recordSpan"), ({"OTELFEATURE_RECORD_SPAN_FLAG_NAME": " dropSpan "}, "dropSpan"),
     ({"OTELFEATURE_RECORD_SPAN_FLAG_NAME": ""}, "recordSpan")],
)
def test_the_flag_key_can_be_overridden(environ, key):
    assert record_span_flag_key(environ) == key



def test_a_dropped_root_keeps_the_trace_id_it_was_evaluated_on(flagd, monkeypatch):
    from otelfeature_instrument import tracing

    flagd({RECORD_SPAN_FLAG_KEY: HEALTH_CHECKS})
    keys = []
    original = tracing.current_policy

    def spy(context=None, **kwargs):
        keys.append(context.targeting_key)
        return original(context, **kwargs)

    monkeypatch.setattr(tracing, "current_policy", spy)
    tracer = _tracer(InMemorySpanExporter())

    root = tracer.start_span("GET /health", kind=trace.SpanKind.SERVER, attributes={"url.path": "/health"})

    assert format(root.get_span_context().trace_id, "032x") == keys[0]


def test_a_static_record_span_false_drops_every_trace_unsampled(flagd):
    flagd({RECORD_SPAN_FLAG_KEY: {"state": "ENABLED", "defaultVariant": "off", "variants": {"off": False}}})
    assert flags._policy_source.cached is not None  # the static path
    exporter = InMemorySpanExporter()

    headers = _request(_tracer(exporter), "/weather")

    assert headers["traceparent"].endswith("-00")
    assert exporter.get_finished_spans() == ()


def _remote(sampled: bool = True):
    return trace.set_span_in_context(
        trace.NonRecordingSpan(
            trace.SpanContext(
                trace_id=0xABC,
                span_id=0xDEF,
                is_remote=True,
                trace_flags=trace.TraceFlags(
                    trace.TraceFlags.SAMPLED if sampled else trace.TraceFlags.DEFAULT
                ),
                trace_state=trace.TraceState([("vendor", "x")]),
            )
        )
    )


@pytest.mark.parametrize("sampled", [True, False], ids=["sampled-caller", "unsampled-caller"])
@pytest.mark.parametrize("kind", [trace.SpanKind.SERVER, trace.SpanKind.CONSUMER])
def test_a_proxied_health_check_leaves_no_spans_at_all(flagd, kind, sampled):
    """A proxy that traces its probes sends a sampled traceparent: the request
    still counts as a root here, and the whole request is dropped."""
    flagd({RECORD_SPAN_FLAG_KEY: HEALTH_CHECKS})
    exporter = InMemorySpanExporter()
    tracer = _tracer(exporter)

    with tracer.start_as_current_span(
        "GET /health", context=_remote(sampled), kind=kind, attributes={"url.path": "/health"}
    ) as dropped:
        with tracer.start_as_current_span("handler"):
            tracer.start_span("SELECT 1", kind=trace.SpanKind.CLIENT).end()
        headers: dict[str, str] = {}
        inject(headers)

    assert exporter.get_finished_spans() == ()
    # The caller's trace, unsampled from here on - the trace ends at the caller.
    context = dropped.get_span_context()
    assert context.trace_id == 0xABC and context.span_id != 0xDEF
    assert not context.trace_flags.sampled
    assert headers["traceparent"] == f"00-{0xABC:032x}-{context.span_id:016x}-00"
    assert headers["tracestate"] == "vendor=x"


@pytest.mark.parametrize("kind", [trace.SpanKind.INTERNAL, trace.SpanKind.CLIENT])
def test_internal_and_client_spans_with_a_remote_parent_stay_children(flagd, kind):
    flagd(
        {
            RECORD_SPAN_FLAG_KEY: {
                "state": "ENABLED",
                "defaultVariant": "on",
                "variants": {"on": True, "off": False},
                "targeting": {"if": [{"==": [{"var": "span.name"}, "dropped"]}, "off", None]},
            }
        }
    )
    exporter = InMemorySpanExporter()
    tracer = _tracer(exporter)

    with tracer.start_as_current_span("dropped", context=_remote(), kind=kind) as stand_in:
        tracer.start_span("kept").end()

    assert stand_in.get_span_context().span_id == 0xDEF
    (kept,) = exporter.get_finished_spans()
    assert kept.parent.span_id == 0xDEF


def test_a_provider_error_records(flagd, monkeypatch):
    from openfeature.exception import ErrorCode
    from openfeature.flag_evaluation import FlagEvaluationDetails, Reason

    flagd({RECORD_SPAN_FLAG_KEY: HEALTH_CHECKS})

    def broken(self, client, context):
        return FlagEvaluationDetails(
            flag_key=self.flag_key, value=True, reason=Reason.ERROR, error_code=ErrorCode.GENERAL
        )

    monkeypatch.setattr(flags._RecordSpanResolver, "_read", broken)
    exporter = InMemorySpanExporter()

    _request(_tracer(exporter), "/health")

    assert len(exporter.get_finished_spans()) == 3


def test_the_resolver_uses_the_configured_key():
    assert flags._RecordSpanResolver.flag_key == RECORD_SPAN_FLAG_KEY == record_span_flag_key()


def test_a_static_record_span_false_drops_proxied_requests_in_the_callers_trace(flagd):
    flagd({RECORD_SPAN_FLAG_KEY: {"state": "ENABLED", "defaultVariant": "off", "variants": {"off": False}}})
    assert flags._policy_source.cached is not None  # the static path
    exporter = InMemorySpanExporter()

    dropped = _tracer(exporter).start_span("GET /", context=_remote(), kind=trace.SpanKind.SERVER)

    assert dropped.get_span_context().trace_id == 0xABC
    assert not dropped.get_span_context().trace_flags.sampled


def test_a_remote_parent_with_an_invalid_span_id_is_a_plain_root(flagd):
    """An invalid context means no parent: a fresh, unsampled trace."""
    flagd({RECORD_SPAN_FLAG_KEY: HEALTH_CHECKS})
    invalid = trace.set_span_in_context(
        trace.NonRecordingSpan(trace.SpanContext(trace_id=0xABC, span_id=0, is_remote=True))
    )

    dropped = _tracer(InMemorySpanExporter()).start_span(
        "GET /health", context=invalid, kind=trace.SpanKind.SERVER, attributes={"url.path": "/health"}
    )

    context = dropped.get_span_context()
    assert context.is_valid and context.trace_id != 0xABC
    assert not context.trace_flags.sampled


def test_a_proxied_request_is_dropped_when_only_the_ratio_is_targeted(flagd):
    """The child-policy path (#13): recordSpan static false, traceSampleRatio
    targeted - a remote-parent SERVER span still drops as a root."""
    flagd(
        {
            RECORD_SPAN_FLAG_KEY: {"state": "ENABLED", "defaultVariant": "off", "variants": {"off": False}},
            "traceSampleRatio": {
                "state": "ENABLED",
                "defaultVariant": "all",
                "variants": {"all": 1.0},
                "targeting": {"if": [{"==": [{"var": "url.path"}, "/never"]}, "all", None]},
            },
        }
    )
    assert flags._policy_source.cached is None and flags._policy_source.cached_child is not None
    exporter = InMemorySpanExporter()

    dropped = _tracer(exporter).start_span("GET /", context=_remote(), kind=trace.SpanKind.SERVER)

    assert dropped.get_span_context().trace_id == 0xABC
    assert not dropped.get_span_context().trace_flags.sampled
