"""Tests for the static-vs-targeted evaluation policy in flags.py.

The end-to-end tests here drive a *real* flagd provider in `FILE` resolver
mode: same flag parser, same targeting engine and same
`PROVIDER_CONFIGURATION_CHANGED` plumbing as the `IN_PROCESS` resolver used in
production, just fed from a file on disk instead of flagd's gRPC sync stream.
That makes "is this reason STATIC or TARGETING_MATCH?" an observation rather
than an assumption.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from openfeature import api as feature_api
from openfeature.contrib.provider.flagd import FlagdProvider
from openfeature.contrib.provider.flagd.config import ResolverType
from openfeature.evaluation_context import EvaluationContext
from openfeature.event import EventDetails, ProviderEvent
from openfeature.exception import ErrorCode
from openfeature.flag_evaluation import FlagEvaluationDetails, Reason

from otelfeature_instrument import flags
from otelfeature_instrument.flags import (
    _RECLASSIFY_ON,
    OTEL_ENABLED_FLAG_KEY,
    TRACE_VERBOSITY_FLAG_KEY,
    VERBOSITY_FULL,
    VERBOSITY_IO,
    VERBOSITY_OFF,
    TelemetryPolicy,
    _EnabledResolver,
    _PolicySource,
    _VerbosityResolver,
)

STATIC_FLAGS = {
    "$schema": "https://flagd.dev/schema/v0/flags.json",
    "flags": {
        TRACE_VERBOSITY_FLAG_KEY: {
            "state": "ENABLED",
            "defaultVariant": "full",
            "variants": {"io": VERBOSITY_IO, "full": VERBOSITY_FULL},
        }
    },
}

# The same flag with a targeting rule bolted on. What it targets on doesn't
# matter here - only that flagd now reports a context-dependent reason, which
# is what has to switch caching off.
TARGETED_FLAGS = {
    "$schema": "https://flagd.dev/schema/v0/flags.json",
    "flags": {
        TRACE_VERBOSITY_FLAG_KEY: {
            "state": "ENABLED",
            "defaultVariant": "full",
            "variants": {"io": VERBOSITY_IO, "full": VERBOSITY_FULL},
            "targeting": {"if": [{"var": "suppress"}, "io", "full"]},
        }
    },
}


def _details(value: str, reason: Reason) -> FlagEvaluationDetails[str]:
    return FlagEvaluationDetails(
        flag_key=TRACE_VERBOSITY_FLAG_KEY, value=value, reason=reason
    )


class _RecordingClient:
    """Counts evaluations, so "evaluated once" is measured rather than assumed."""

    def __init__(self, *responses: FlagEvaluationDetails[str]) -> None:
        self._responses = list(responses)
        self.calls: list[EvaluationContext | None] = []

    def get_string_details(self, key, default_value, context=None):
        self.calls.append(context)
        index = min(len(self.calls) - 1, len(self._responses) - 1)
        return self._responses[index]

    get_boolean_details = get_string_details


# --------------------------------------------------------------------------
# The rule: classify once in the listener, cache only STATIC
# --------------------------------------------------------------------------


def test_static_flag_is_evaluated_once_for_every_span():
    client = _RecordingClient(_details(VERBOSITY_IO, Reason.STATIC))
    resolver = _VerbosityResolver()

    with patch.object(feature_api, "get_client", return_value=client):
        resolver.classify(EventDetails())
        values = [resolver.current() for _ in range(5)]

    assert values == [VERBOSITY_IO] * 5
    # One evaluation total: the context-free probe in the listener.
    assert len(client.calls) == 1
    assert resolver.cached == VERBOSITY_IO


@pytest.mark.parametrize(
    "reason",
    [Reason.TARGETING_MATCH, Reason.DEFAULT, Reason.SPLIT, Reason.DISABLED, Reason.ERROR],
)
def test_non_static_reasons_are_evaluated_every_time(reason):
    client = _RecordingClient(_details(VERBOSITY_IO, reason))
    resolver = _VerbosityResolver()

    with patch.object(feature_api, "get_client", return_value=client):
        resolver.classify(EventDetails())
        values = [resolver.current() for _ in range(5)]

    assert values == [VERBOSITY_IO] * 5
    # The probe, plus one evaluation per span.
    assert len(client.calls) == 6
    assert resolver.cached is None


def test_the_probe_is_context_free():
    client = _RecordingClient(_details(VERBOSITY_FULL, Reason.STATIC))
    resolver = _VerbosityResolver()

    with patch.object(feature_api, "get_client", return_value=client):
        resolver.classify(EventDetails())

    assert client.calls == [None]


def test_context_is_forwarded_on_the_per_span_path():
    """The seam for contextual evaluation, once there is something to put in it."""
    client = _RecordingClient(_details(VERBOSITY_IO, Reason.TARGETING_MATCH))
    resolver = _VerbosityResolver()
    context = EvaluationContext(attributes={"suppress": True})

    with patch.object(feature_api, "get_client", return_value=client):
        resolver.classify(EventDetails())
        resolver.current(context)

    assert client.calls == [None, context]


def test_static_path_does_not_evaluate_even_when_given_a_context():
    client = _RecordingClient(_details(VERBOSITY_FULL, Reason.STATIC))
    resolver = _VerbosityResolver()

    with patch.object(feature_api, "get_client", return_value=client):
        resolver.classify(EventDetails())
        resolver.current(EvaluationContext(attributes={"suppress": True}))

    assert len(client.calls) == 1


def test_uncached_before_the_first_event():
    """Until a provider event arrives, nothing has been classified."""
    client = _RecordingClient(_details(VERBOSITY_IO, Reason.STATIC))
    resolver = _VerbosityResolver()

    with patch.object(feature_api, "get_client", return_value=client):
        assert resolver.cached is None
        assert resolver.current() == VERBOSITY_IO

    assert len(client.calls) == 1


# --------------------------------------------------------------------------
# Re-classification
# --------------------------------------------------------------------------


def test_configuration_change_for_our_flag_reclassifies():
    client = _RecordingClient(
        _details(VERBOSITY_FULL, Reason.STATIC), _details(VERBOSITY_IO, Reason.STATIC)
    )
    resolver = _VerbosityResolver()

    with patch.object(feature_api, "get_client", return_value=client):
        resolver.classify(EventDetails())
        assert resolver.current() == VERBOSITY_FULL

        resolver.classify(EventDetails(flags_changed=[TRACE_VERBOSITY_FLAG_KEY]))
        assert resolver.current() == VERBOSITY_IO

    assert len(client.calls) == 2


def test_configuration_change_for_another_flag_is_ignored():
    client = _RecordingClient(_details(VERBOSITY_FULL, Reason.STATIC))
    resolver = _VerbosityResolver()

    with patch.object(feature_api, "get_client", return_value=client):
        resolver.classify(EventDetails())
        resolver.classify(EventDetails(flags_changed=["someUnrelatedFlag"]))
        resolver.current()

    assert len(client.calls) == 1


def test_adding_targeting_switches_caching_off():
    client = _RecordingClient(
        _details(VERBOSITY_FULL, Reason.STATIC),
        _details(VERBOSITY_IO, Reason.TARGETING_MATCH),
    )
    resolver = _VerbosityResolver()

    with patch.object(feature_api, "get_client", return_value=client):
        resolver.classify(EventDetails())
        assert resolver.cached == VERBOSITY_FULL

        resolver.classify(EventDetails(flags_changed=[TRACE_VERBOSITY_FLAG_KEY]))
        assert resolver.cached is None


def test_removing_targeting_switches_caching_back_on():
    client = _RecordingClient(
        _details(VERBOSITY_IO, Reason.TARGETING_MATCH),
        _details(VERBOSITY_FULL, Reason.STATIC),
    )
    resolver = _VerbosityResolver()

    with patch.object(feature_api, "get_client", return_value=client):
        resolver.classify(EventDetails())
        assert resolver.cached is None

        resolver.classify(EventDetails(flags_changed=[TRACE_VERBOSITY_FLAG_KEY]))
        assert resolver.cached == VERBOSITY_FULL


# --------------------------------------------------------------------------
# telemetryLevel=OFF and otelEnabled
# --------------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["OFF", "off", " Off "])
def test_off_variant_is_normalised(raw):
    client = _RecordingClient(_details(raw, Reason.STATIC))
    resolver = _VerbosityResolver()

    with patch.object(feature_api, "get_client", return_value=client):
        assert resolver.current() == VERBOSITY_OFF


def _bool_details(value, reason: Reason) -> FlagEvaluationDetails:
    return FlagEvaluationDetails(flag_key=OTEL_ENABLED_FLAG_KEY, value=value, reason=reason)


def test_static_otel_enabled_false_is_cached():
    """`False` is a real answer, not the "uncached" sentinel."""
    client = _RecordingClient(_bool_details(False, Reason.STATIC))
    resolver = _EnabledResolver()

    with patch.object(feature_api, "get_client", return_value=client):
        resolver.classify(EventDetails())
        values = [resolver.current() for _ in range(5)]

    assert values == [False] * 5
    assert len(client.calls) == 1
    assert resolver.cached is False


def test_targeted_otel_enabled_is_evaluated_per_span_with_context():
    client = _RecordingClient(_bool_details(False, Reason.TARGETING_MATCH))
    resolver = _EnabledResolver()
    context = EvaluationContext(attributes={"suppress": True})

    with patch.object(feature_api, "get_client", return_value=client):
        resolver.classify(EventDetails())
        assert resolver.current(context) is False

    assert resolver.cached is None
    assert client.calls == [None, context]


def test_otel_enabled_ignores_changes_to_other_flags():
    client = _RecordingClient(_bool_details(True, Reason.STATIC))
    resolver = _EnabledResolver()

    with patch.object(feature_api, "get_client", return_value=client):
        resolver.classify(EventDetails())
        resolver.classify(EventDetails(flags_changed=[TRACE_VERBOSITY_FLAG_KEY]))

    assert len(client.calls) == 1


@pytest.fixture
def no_provider():
    feature_api.clear_providers()
    yield
    feature_api.clear_providers()


def test_otel_enabled_with_no_provider_is_enabled(no_provider):
    """No provider registered at all: the SDK's no-op provider serves our default."""
    assert _EnabledResolver().current() is True
    assert _VerbosityResolver().current() == VERBOSITY_FULL


@pytest.mark.parametrize(
    "resolver_type, default",
    [(_EnabledResolver, True), (_VerbosityResolver, VERBOSITY_FULL)],
)
def test_a_missing_flag_caches_the_fail_open_default(resolver_type, default):
    """Deployments that never defined the flag must not pay a failing evaluation per span."""
    client = _RecordingClient(
        FlagEvaluationDetails(
            flag_key=resolver_type.flag_key,
            value=default,
            reason=Reason.ERROR,
            error_code=ErrorCode.FLAG_NOT_FOUND,
        )
    )
    resolver = resolver_type()

    with patch.object(feature_api, "get_client", return_value=client):
        resolver.classify(EventDetails())
        values = [resolver.current() for _ in range(5)]

    assert values == [default] * 5
    assert len(client.calls) == 1


def test_other_errors_are_not_cached():
    client = _RecordingClient(
        FlagEvaluationDetails(
            flag_key=OTEL_ENABLED_FLAG_KEY,
            value=True,
            reason=Reason.ERROR,
            error_code=ErrorCode.PROVIDER_NOT_READY,
        )
    )
    resolver = _EnabledResolver()

    with patch.object(feature_api, "get_client", return_value=client):
        resolver.classify(EventDetails())
        assert resolver.current() is True

    assert resolver.cached is None


# --------------------------------------------------------------------------
# End-to-end against a real flagd provider (FILE resolver)
# --------------------------------------------------------------------------


@pytest.fixture
def flag_file(tmp_path: Path) -> Path:
    path = tmp_path / "flags.json"
    path.write_text(json.dumps(STATIC_FLAGS), encoding="utf-8")
    return path


@pytest.fixture
def flagd_provider(flag_file: Path, monkeypatch: pytest.MonkeyPatch):
    # The FILE resolver re-reads the file on a timer; the 5s default would turn
    # every "did the change land?" assertion below into a five-second wait.
    monkeypatch.setenv("FLAGD_OFFLINE_POLL_MS", "50")
    feature_api.set_provider_and_wait(
        FlagdProvider(
            resolver_type=ResolverType.FILE,
            offline_flag_source_path=str(flag_file),
        )
    )
    try:
        yield
    finally:
        feature_api.clear_providers()


def _wait_for(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_real_flagd_reports_static_for_an_untargeted_flag(flagd_provider):
    details = feature_api.get_client().get_string_details(
        TRACE_VERBOSITY_FLAG_KEY, VERBOSITY_FULL
    )

    assert details.value == VERBOSITY_FULL
    assert str(details.reason) == Reason.STATIC


def test_real_flagd_reports_targeting_match_for_a_targeted_flag(
    flag_file: Path, flagd_provider
):
    flag_file.write_text(json.dumps(TARGETED_FLAGS), encoding="utf-8")

    def targeted() -> bool:
        details = feature_api.get_client().get_string_details(
            TRACE_VERBOSITY_FLAG_KEY,
            VERBOSITY_FULL,
            EvaluationContext(attributes={"suppress": True}),
        )
        return str(details.reason) == Reason.TARGETING_MATCH and details.value == VERBOSITY_IO

    assert _wait_for(targeted), "flagd never picked up the targeting rule"


def test_real_flagd_drives_the_switch_between_cached_and_per_span(
    flag_file: Path, flagd_provider
):
    """The whole feature, end to end, against a real provider."""
    resolver = _VerbosityResolver()
    # Same set of events configure_feature_flags() subscribes to. READY is what
    # classifies on an already-ready provider; CONFIGURATION_CHANGED is what
    # re-classifies when the ruleset moves underneath us.
    for event in _RECLASSIFY_ON:
        feature_api.add_handler(event, resolver.classify)
    try:
        # Subscribing to READY on an already-ready provider classifies right
        # away - though on the SDK's event executor, so it is a wait, not an
        # assertion. Untargeted, so one probe answers every span from here on.
        assert _wait_for(lambda: resolver.cached == VERBOSITY_FULL), "never classified"
        assert resolver.current() == VERBOSITY_FULL

        # Adding targeting must switch caching off, driven purely by the
        # configuration-changed event flagd emits when the file changes.
        flag_file.write_text(json.dumps(TARGETED_FLAGS), encoding="utf-8")
        assert _wait_for(lambda: resolver.cached is None), "caching never switched off"

        # ... and now the value genuinely follows the evaluation context.
        assert resolver.current(EvaluationContext(attributes={"suppress": True})) == VERBOSITY_IO
        assert resolver.current(EvaluationContext(attributes={"suppress": False})) == VERBOSITY_FULL
        assert resolver.cached is None

        # Remove the targeting again and caching resumes on its own.
        flag_file.write_text(json.dumps(STATIC_FLAGS), encoding="utf-8")
        assert _wait_for(
            lambda: resolver.cached == VERBOSITY_FULL
        ), "caching did not resume once targeting was removed"
    finally:
        for event in _RECLASSIFY_ON:
            feature_api.remove_handler(event, resolver.classify)


def test_real_flagd_classifies_on_handler_registration(flagd_provider):
    """PROVIDER_READY on an already-ready provider must reach the classifier."""
    resolver = _VerbosityResolver()
    feature_api.add_handler(ProviderEvent.PROVIDER_READY, resolver.classify)
    try:
        # Dispatched on the SDK's event executor rather than inline, so until
        # it lands `current()` simply evaluates - correct, just not yet cheap.
        assert _wait_for(lambda: resolver.cached == VERBOSITY_FULL)
    finally:
        feature_api.remove_handler(ProviderEvent.PROVIDER_READY, resolver.classify)


# --------------------------------------------------------------------------
# configure_feature_flags() itself
# --------------------------------------------------------------------------


@pytest.fixture
def registered_handlers(monkeypatch: pytest.MonkeyPatch):
    """Captures handler registration instead of touching the real event bus.

    `configure_feature_flags()` registers on the process-wide OpenFeature bus,
    which would outlive the test; it also latches `_handlers_registered`, which
    has to start false for each case.
    """
    captured: list[tuple[ProviderEvent, object]] = []
    monkeypatch.setattr(flags, "_handlers_registered", False)
    monkeypatch.setattr(
        feature_api, "add_handler", lambda event, handler: captured.append((event, handler))
    )
    return captured


def test_configure_registers_handlers_and_an_in_process_flagd_provider(
    monkeypatch: pytest.MonkeyPatch, registered_handlers
):
    monkeypatch.delenv(flags.FLAG_PROVIDER_ENV_VAR, raising=False)
    providers: list[object] = []
    monkeypatch.setattr(feature_api, "set_provider", providers.append)
    monkeypatch.setattr(flags, "FlagdProvider", lambda **kwargs: ("flagd", kwargs))

    flags.configure_feature_flags()

    # One handler per event: the policy source re-classifies every flag.
    assert registered_handlers == [
        (event, flags._policy_source.classify) for event in flags._RECLASSIFY_ON
    ]
    # in-process, not rpc: it is what delivers the configuration-changed events
    # the classification depends on.
    assert providers == [("flagd", {"resolver_type": ResolverType.IN_PROCESS})]


def test_configure_with_provider_none_still_registers_handlers(
    monkeypatch: pytest.MonkeyPatch, registered_handlers
):
    """The app owns OpenFeature, but its events still have to drive the cache."""
    monkeypatch.setenv(flags.FLAG_PROVIDER_ENV_VAR, "none")
    monkeypatch.setattr(
        feature_api, "set_provider", lambda provider: pytest.fail("must not register a provider")
    )

    flags.configure_feature_flags()

    assert [event for event, _ in registered_handlers] == list(flags._RECLASSIFY_ON)


def test_configure_rejects_an_unknown_provider(
    monkeypatch: pytest.MonkeyPatch, registered_handlers
):
    monkeypatch.setenv(flags.FLAG_PROVIDER_ENV_VAR, "launchdarkly")

    with pytest.raises(ValueError, match="OTELFEATURE_FLAG_PROVIDER"):
        flags.configure_feature_flags()

    assert registered_handlers == []


def test_configure_twice_registers_handlers_once(
    monkeypatch: pytest.MonkeyPatch, registered_handlers
):
    """Otherwise every provider event would classify twice over."""
    monkeypatch.setenv(flags.FLAG_PROVIDER_ENV_VAR, "none")

    flags.configure_feature_flags()
    flags.configure_feature_flags()

    assert len(registered_handlers) == len(flags._RECLASSIFY_ON)


def _with_otel_enabled(flag: dict) -> dict:
    return {**STATIC_FLAGS, "flags": {**STATIC_FLAGS["flags"], OTEL_ENABLED_FLAG_KEY: flag}}


def test_real_flagd_static_otel_enabled_false_is_cached(flag_file: Path, flagd_provider):
    flag_file.write_text(
        json.dumps(
            _with_otel_enabled(
                {
                    "state": "ENABLED",
                    "defaultVariant": "off",
                    "variants": {"on": True, "off": False},
                }
            )
        ),
        encoding="utf-8",
    )
    resolver = _EnabledResolver()
    feature_api.add_handler(ProviderEvent.PROVIDER_CONFIGURATION_CHANGED, resolver.classify)
    try:
        assert _wait_for(lambda: resolver.cached is False), "otelEnabled=false never cached"
        assert resolver.current() is False
    finally:
        feature_api.remove_handler(
            ProviderEvent.PROVIDER_CONFIGURATION_CHANGED, resolver.classify
        )


def test_real_flagd_non_boolean_otel_enabled_fails_open(flag_file: Path, flagd_provider):
    """A string-valued `otelEnabled` is a type mismatch: the SDK serves our `True`."""
    flag_file.write_text(
        json.dumps(
            _with_otel_enabled(
                {
                    "state": "ENABLED",
                    "defaultVariant": "off",
                    "variants": {"on": "true", "off": "false"},
                }
            )
        ),
        encoding="utf-8",
    )

    def loaded() -> bool:
        details = feature_api.get_client().get_boolean_details(OTEL_ENABLED_FLAG_KEY, True)
        return details.error_code is not None and str(details.error_code) != "FLAG_NOT_FOUND"

    assert _wait_for(loaded), "flagd never picked up the otelEnabled flag"
    assert _EnabledResolver().current() is True


def test_real_flagd_missing_otel_enabled_is_cached_until_it_is_created(
    flag_file: Path, flagd_provider
):
    """The upgrade path: no `otelEnabled` yet, then someone adds it in flagd."""
    resolver = _EnabledResolver()
    for event in _RECLASSIFY_ON:
        feature_api.add_handler(event, resolver.classify)
    try:
        assert _wait_for(lambda: resolver.cached is True), "missing flag never cached"

        flag_file.write_text(
            json.dumps(
                _with_otel_enabled(
                    {
                        "state": "ENABLED",
                        "defaultVariant": "off",
                        "variants": {"on": True, "off": False},
                    }
                )
            ),
            encoding="utf-8",
        )
        assert _wait_for(
            lambda: resolver.cached is False
        ), "creating the flag never re-classified"
    finally:
        for event in _RECLASSIFY_ON:
            feature_api.remove_handler(event, resolver.classify)



# --------------------------------------------------------------------------
# TelemetryPolicy
# --------------------------------------------------------------------------


class _FlagClient:
    """Answers each flag key from a table; a missing key is FLAG_NOT_FOUND."""

    def __init__(self, **flags_by_key) -> None:
        self.flags = flags_by_key
        self.calls = 0

    def _get(self, key, default_value, context=None):
        self.calls += 1
        if key not in self.flags:
            return FlagEvaluationDetails(
                flag_key=key,
                value=default_value,
                reason=Reason.ERROR,
                error_code=ErrorCode.FLAG_NOT_FOUND,
            )
        value, reason = self.flags[key]
        return FlagEvaluationDetails(flag_key=key, value=value, reason=reason)

    get_string_details = _get
    get_boolean_details = _get


@pytest.mark.parametrize(
    "enabled, level, expected",
    [
        (True, VERBOSITY_FULL, TelemetryPolicy(enabled=True, suppress_internal=False)),
        (True, VERBOSITY_IO, TelemetryPolicy(enabled=True, suppress_internal=True)),
        (True, VERBOSITY_OFF, TelemetryPolicy(enabled=False, suppress_internal=False)),
        (False, VERBOSITY_FULL, TelemetryPolicy(enabled=False, suppress_internal=False)),
        (False, VERBOSITY_IO, TelemetryPolicy(enabled=False, suppress_internal=False)),
        (True, "VERBOSE", TelemetryPolicy(enabled=True, suppress_internal=False)),
    ],
)
def test_both_flags_feed_one_policy(enabled, level, expected):
    client = _FlagClient(
        otelEnabled=(enabled, Reason.STATIC), telemetryLevel=(level, Reason.STATIC)
    )
    source = _PolicySource()

    with patch.object(feature_api, "get_client", return_value=client):
        assert source.current() == expected


def test_static_flags_serve_one_cached_policy():
    """The static path is one attribute read: same object, no evaluation."""
    client = _FlagClient(
        otelEnabled=(True, Reason.STATIC), telemetryLevel=(VERBOSITY_IO, Reason.STATIC)
    )
    source = _PolicySource()

    with patch.object(feature_api, "get_client", return_value=client):
        source.classify(EventDetails())
        evaluations = client.calls
        policies = [source.current() for _ in range(5)]

    assert client.calls == evaluations == 2
    assert all(policy is source.cached for policy in policies)
    assert source.cached == TelemetryPolicy(enabled=True, suppress_internal=True)


def test_one_targeted_flag_makes_the_policy_per_span():
    client = _FlagClient(
        otelEnabled=(True, Reason.STATIC),
        telemetryLevel=(VERBOSITY_IO, Reason.TARGETING_MATCH),
    )
    source = _PolicySource()

    with patch.object(feature_api, "get_client", return_value=client):
        source.classify(EventDetails())
        assert source.cached is None
        before = client.calls
        assert source.current().suppress_internal is True

    # Only the targeted flag is evaluated again; the static one stays cached.
    assert client.calls == before + 1


def test_missing_flags_fall_back_to_startup_values_per_field():
    """flagd up but neither flag defined: the environment decides, per field."""
    source = _PolicySource(TelemetryPolicy(enabled=False, suppress_internal=True))

    with patch.object(feature_api, "get_client", return_value=_FlagClient()):
        source.classify(EventDetails())

    assert source.cached == TelemetryPolicy(enabled=False, suppress_internal=True)


def test_a_defined_flag_overrides_its_startup_value():
    source = _PolicySource(TelemetryPolicy(suppress_internal=True))
    client = _FlagClient(telemetryLevel=(VERBOSITY_FULL, Reason.STATIC))

    with patch.object(feature_api, "get_client", return_value=client):
        assert source.current() == TelemetryPolicy(enabled=True, suppress_internal=False)


@pytest.mark.parametrize(
    "otel_enabled, level",
    [(True, VERBOSITY_FULL), (True, VERBOSITY_IO), (False, VERBOSITY_FULL)],
)
def test_otel_sdk_disabled_is_a_hard_floor(otel_enabled, level):
    """Flags never enable what OTEL_SDK_DISABLED turned off - and aren't even asked."""
    source = _PolicySource(TelemetryPolicy.from_environment({"OTEL_SDK_DISABLED": "true"}))
    client = _FlagClient(
        otelEnabled=(otel_enabled, Reason.STATIC), telemetryLevel=(level, Reason.STATIC)
    )

    with patch.object(feature_api, "get_client", return_value=client):
        source.classify(EventDetails())
        assert source.current().enabled is False
        assert source.cached.enabled is False

    assert client.calls == 0


def test_unknown_level_keeps_the_startup_value():
    source = _PolicySource(TelemetryPolicy(suppress_internal=True))
    client = _FlagClient(telemetryLevel=("VERBOSE", Reason.STATIC))

    with patch.object(feature_api, "get_client", return_value=client):
        assert source.current().suppress_internal is True


@pytest.mark.parametrize(
    "environ, enabled",
    [({}, True), ({"OTEL_SDK_DISABLED": "true"}, False), ({"OTEL_SDK_DISABLED": " TRUE "}, False),
     ({"OTEL_SDK_DISABLED": "false"}, True), ({"OTEL_SDK_DISABLED": "1"}, True)],
)
def test_startup_defaults_come_from_the_environment(environ, enabled):
    assert TelemetryPolicy.from_environment(environ) == TelemetryPolicy(enabled=enabled)


def test_with_no_provider_the_policy_is_the_startup_policy(no_provider):
    assert _PolicySource().current() == TelemetryPolicy()


def test_provider_errors_are_logged_once_not_per_span(caplog):
    broken = FlagEvaluationDetails(
        flag_key=OTEL_ENABLED_FLAG_KEY,
        value=True,
        reason=Reason.ERROR,
        error_code=ErrorCode.PARSE_ERROR,
        error_message="bad ruleset",
    )
    fixed = _bool_details(False, Reason.TARGETING_MATCH)
    client = _RecordingClient(*([broken] * 5 + [fixed] * 3 + [broken]))
    resolver = _EnabledResolver()

    with caplog.at_level("INFO", logger=flags.__name__), patch.object(
        feature_api, "get_client", return_value=client
    ):
        for _ in range(9):
            resolver.current()

    messages = [record.getMessage() for record in caplog.records]
    assert len([m for m in messages if "could not be evaluated" in m]) == 2
    assert len([m for m in messages if "readable again" in m]) == 1
    assert [r.levelname for r in caplog.records if "could not" in r.getMessage()] == [
        "WARNING",
        "WARNING",
    ]


def test_a_missing_flag_is_logged_once_at_info(caplog):
    with caplog.at_level("INFO", logger=flags.__name__), patch.object(
        feature_api, "get_client", return_value=_FlagClient()
    ):
        source = _PolicySource()
        for _ in range(5):
            source.current()

    not_defined = [r for r in caplog.records if "is not defined" in r.getMessage()]
    assert sorted(r.getMessage().split()[1] for r in not_defined) == [
        OTEL_ENABLED_FLAG_KEY,
        TRACE_VERBOSITY_FLAG_KEY,
    ]
    assert {r.levelname for r in not_defined} == {"INFO"}



def test_disabled_does_not_evaluate_telemetry_level():
    """As before the refactor: with otelEnabled=false the level is never asked."""
    client = _FlagClient(
        otelEnabled=(False, Reason.TARGETING_MATCH),
        telemetryLevel=(VERBOSITY_IO, Reason.TARGETING_MATCH),
    )
    asked: list[str] = []
    original = client._get

    def spy(key, default_value, context=None):
        asked.append(key)
        return original(key, default_value, context)

    client.get_string_details = client.get_boolean_details = spy
    source = _PolicySource()

    with patch.object(feature_api, "get_client", return_value=client):
        assert source.current().enabled is False

    assert asked == [OTEL_ENABLED_FLAG_KEY]


def test_a_change_to_one_flag_updates_the_cached_policy():
    client = _FlagClient(
        otelEnabled=(True, Reason.STATIC), telemetryLevel=(VERBOSITY_FULL, Reason.STATIC)
    )
    source = _PolicySource()

    with patch.object(feature_api, "get_client", return_value=client):
        source.classify(EventDetails())
        client.flags[TRACE_VERBOSITY_FLAG_KEY] = (VERBOSITY_IO, Reason.STATIC)
        source.classify(EventDetails(flags_changed=[TRACE_VERBOSITY_FLAG_KEY]))

    assert source.cached == TelemetryPolicy(enabled=True, suppress_internal=True)


def test_concurrent_classifications_leave_the_latest_answer_cached():
    """Event handlers run on a thread pool. An older classification finishing
    last must not overwrite a newer one."""
    first_evaluation = threading.Event()
    release = threading.Event()

    class _Client(_FlagClient):
        def get_string_details(self, key, default_value, context=None):
            self.calls += 1
            if self.calls == 1:
                first_evaluation.set()
                release.wait(5)
                return FlagEvaluationDetails(flag_key=key, value=VERBOSITY_IO, reason=Reason.STATIC)
            return FlagEvaluationDetails(flag_key=key, value=VERBOSITY_FULL, reason=Reason.STATIC)

        def get_boolean_details(self, key, default_value, context=None):
            return FlagEvaluationDetails(flag_key=key, value=True, reason=Reason.STATIC)

    source = _PolicySource()
    with patch.object(feature_api, "get_client", return_value=_Client()):
        older = threading.Thread(target=source.classify)
        older.start()
        assert first_evaluation.wait(5)
        # Started second, so it saw the newer ruleset (FULL).
        newer = threading.Thread(target=source.classify)
        newer.start()
        time.sleep(0.1)
        release.set()
        older.join(5)
        newer.join(5)

    assert source.cached == TelemetryPolicy(enabled=True, suppress_internal=False)


def test_unknown_level_is_logged_once(caplog):
    client = _FlagClient(telemetryLevel=("VERBOSE", Reason.TARGETING_MATCH))
    source = _PolicySource()

    with caplog.at_level("INFO", logger=flags.__name__), patch.object(
        feature_api, "get_client", return_value=client
    ):
        for _ in range(5):
            source.current()

    assert len([r for r in caplog.records if "unknown variant" in r.getMessage()]) == 1


def test_a_missing_flag_is_logged_once_even_when_classified(caplog):
    with caplog.at_level("INFO", logger=flags.__name__), patch.object(
        feature_api, "get_client", return_value=_FlagClient()
    ):
        _PolicySource().classify(EventDetails())

    messages = [r.getMessage() for r in caplog.records]
    assert len(messages) == 2  # one "is not defined" per flag, nothing else
    assert all("is not defined" in m for m in messages)


def test_a_flag_that_appears_is_reported_as_defined(caplog):
    client = _FlagClient()
    resolver = _EnabledResolver()

    with caplog.at_level("INFO", logger=flags.__name__), patch.object(
        feature_api, "get_client", return_value=client
    ):
        resolver.current()
        client.flags[OTEL_ENABLED_FLAG_KEY] = (False, Reason.STATIC)
        resolver.current()

    assert "otelEnabled is now defined" in caplog.text
    assert "readable again" not in caplog.text
