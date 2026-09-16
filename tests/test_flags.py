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
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from openfeature import api as feature_api
from openfeature.contrib.provider.flagd import FlagdProvider
from openfeature.contrib.provider.flagd.config import ResolverType
from openfeature.evaluation_context import EvaluationContext
from openfeature.event import EventDetails, ProviderEvent
from openfeature.flag_evaluation import FlagEvaluationDetails, Reason

from otelfeature_instrument.flags import (
    _RECLASSIFY_ON,
    TRACE_VERBOSITY_FLAG_KEY,
    VERBOSITY_FULL,
    VERBOSITY_IO,
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
