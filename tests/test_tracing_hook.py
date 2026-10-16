"""Tests for the opt-in OpenFeature OpenTelemetry hook, and that the
extension never records flag evaluations on spans itself."""

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


@pytest.fixture
def flagd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def start(
        flag: dict | None, *, startup_ratio: float = 1.0, key: str = SAMPLE_RATIO_FLAG_KEY
    ) -> None:
        monkeypatch.setattr(
            flags, "_policy_source", _PolicySource(TelemetryPolicy(sample_ratio=startup_ratio))
        )
        ruleset = {"flags": {} if flag is None else {key: {"state": "ENABLED", **flag}}}
        path = tmp_path / "flags.json"
        path.write_text(json.dumps(ruleset), encoding="utf-8")
        feature_api.set_provider_and_wait(
            FlagdProvider(resolver_type=ResolverType.FILE, offline_flag_source_path=str(path))
        )
        flags._policy_source.classify()

    yield start
    feature_api.clear_providers()


def test_the_extension_records_no_flag_attributes_on_spans(flagd):
    """Which flag evaluated to what, or the applied ratio, is not the
    extension's to record - the OpenFeature OpenTelemetry hook does that."""
    flagd({"defaultVariant": "tenth", "variants": {"tenth": 0.1, "all": 1.0}})
    exporter = InMemorySpanExporter()
    provider = SDKTracerProvider(resource=Resource.create(), sampler=flag_aware_sampler({}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = _VerbosityAwareTracerProvider(provider).get_tracer("t")

    for _ in range(300):
        with tracer.start_as_current_span("root", kind=trace.SpanKind.SERVER):
            tracer.start_span("child").end()

    spans = exporter.get_finished_spans()
    assert spans, "nothing sampled at all"
    for span in spans:
        assert not any(key.startswith(("feature_flag.", "otelfeature.")) for key in span.attributes)


# --------------------------------------------------------------------------
# The OpenFeature OpenTelemetry hook: opt-in, for the extension's evaluations
# --------------------------------------------------------------------------


@pytest.fixture
def hook_options():
    yield
    flags.enable_tracing_hook({})  # back to off


@pytest.mark.parametrize("value", [None, "", "false", "1"])
def test_the_tracing_hook_is_off_by_default(hook_options, value):
    environ = {} if value is None else {flags.TRACING_HOOK_ENV_VAR: value}
    assert flags.enable_tracing_hook(environ) is False
    assert flags._hook_options == ()


def _hooked(flagd, rule_flag_key, flag):
    pytest.importorskip("openfeature.contrib.hook.opentelemetry")
    assert flags.enable_tracing_hook({flags.TRACING_HOOK_ENV_VAR: "true"}) is True
    flagd(flag, key=rule_flag_key)
    exporter = InMemorySpanExporter()
    provider = SDKTracerProvider(resource=Resource.create(), sampler=flag_aware_sampler({}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter, _VerbosityAwareTracerProvider(provider).get_tracer("t")


def _events(span):
    return [e for e in span.events if e.name == "feature_flag.evaluation"]


def test_the_hook_records_child_span_decisions_on_the_parent(flagd, hook_options):
    exporter, tracer = _hooked(
        flagd,
        flags.RECORD_SPAN_FLAG_KEY,
        {
            "defaultVariant": "on",
            "variants": {"on": True, "off": False},
            "targeting": {"if": [{"==": [{"var": "span.name"}, "never"]}, "off", None]},
        },
    )

    with tracer.start_as_current_span("request", kind=trace.SpanKind.SERVER):
        tracer.start_span("child").end()

    spans = {s.name: s for s in exporter.get_finished_spans()}
    assert [e.attributes["feature_flag.key"] for e in _events(spans["request"])] == [
        flags.RECORD_SPAN_FLAG_KEY
    ]
    assert _events(spans["child"]) == []


def test_the_hook_records_nothing_for_plain_roots(flagd, hook_options):
    """Documented: a new root has no current span to put the event on."""
    exporter, tracer = _hooked(
        flagd,
        SAMPLE_RATIO_FLAG_KEY,
        {
            "defaultVariant": "all",
            "variants": {"none": 0.0, "all": 1.0},
            "targeting": {"if": [{"==": [{"var": "url.path"}, "/never"]}, "none", "all"]},
        },
    )

    for _ in range(5):
        tracer.start_span("root", kind=trace.SpanKind.SERVER).end()

    spans = exporter.get_finished_spans()
    assert len(spans) == 5
    assert all(_events(span) == [] for span in spans)

    # Positive control: the hook is attached - a root started while another
    # span is current puts its event there.
    exporter.clear()
    with tracer.start_as_current_span("outer", kind=trace.SpanKind.SERVER):
        tracer.start_span("inner-root", context=trace.set_span_in_context(trace.INVALID_SPAN)).end()
    outer = next(s for s in exporter.get_finished_spans() if s.name == "outer")
    assert [e.attributes["feature_flag.key"] for e in _events(outer)] == [SAMPLE_RATIO_FLAG_KEY]


def test_the_hook_does_not_record_errors_as_exceptions(hook_options):
    pytest.importorskip("openfeature.contrib.hook.opentelemetry")
    flags.enable_tracing_hook({flags.TRACING_HOOK_ENV_VAR: "true"})
    (options,) = flags._hook_options
    assert options.hooks[0].exclude_exceptions is True


def test_without_the_package_opting_in_warns_and_stays_off(hook_options, monkeypatch, caplog):
    import builtins

    real_import = builtins.__import__

    def no_hook_package(name, *args, **kwargs):
        if name == "openfeature.contrib.hook.opentelemetry":
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    flags._hook_options = ("previously on",)  # must end up off
    monkeypatch.setattr(builtins, "__import__", no_hook_package)

    assert flags.enable_tracing_hook({flags.TRACING_HOOK_ENV_VAR: "true"}) is False
    assert flags._hook_options == ()
    assert "openfeature-hooks-opentelemetry is not installed" in caplog.text


def test_configure_feature_flags_reads_the_opt_in(hook_options, monkeypatch):
    pytest.importorskip("openfeature.contrib.hook.opentelemetry")
    monkeypatch.setenv(flags.TRACING_HOOK_ENV_VAR, "true")
    monkeypatch.setenv(flags.FLAG_PROVIDER_ENV_VAR, "none")
    monkeypatch.setattr(flags, "_handlers_registered", True)  # don't touch the event bus

    flags.configure_feature_flags()

    (options,) = flags._hook_options
    assert type(options.hooks[0]).__name__ == "TracingHook"
