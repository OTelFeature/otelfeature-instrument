"""Feature-flag wiring backing the `otelEnabled` and `telemetryLevel` checks.

Registering a flagd provider is the default, but it's gated by
`OTELFEATURE_FLAG_PROVIDER` rather than unconditional: this module runs from
`sitecustomize.py`, before the target application's own code is ever
imported, so there's no way to detect "did the app already configure
OpenFeature itself" - that code hasn't run yet. Setting
`OTELFEATURE_FLAG_PROVIDER=none` is how a consuming app opts out and takes
over OpenFeature configuration on its own terms (at any point - the resolver
below always reads the *current* global provider, not a snapshot taken at
startup).

Both flags are evaluated once while static and per span once they carry
targeting, with the classification redone on provider events. See
"Evaluating once, or evaluating always" in the README for what each resolution
reason implies and why keeping an answer is safe; the comments below cover the
local decisions only.
"""

from __future__ import annotations

import os
from logging import getLogger

from openfeature import api as feature_api
from openfeature.contrib.provider.flagd import FlagdProvider
from openfeature.contrib.provider.flagd.config import ResolverType
from openfeature.evaluation_context import EvaluationContext
from openfeature.event import EventDetails, ProviderEvent
from openfeature.exception import ErrorCode
from openfeature.flag_evaluation import FlagEvaluationDetails, Reason

_logger = getLogger(__name__)

FLAG_PROVIDER_ENV_VAR = "OTELFEATURE_FLAG_PROVIDER"
FLAG_PROVIDER_FLAGD = "flagd"
FLAG_PROVIDER_NONE = "none"

TRACE_VERBOSITY_FLAG_KEY = "telemetryLevel"
VERBOSITY_FULL = "FULL"
VERBOSITY_IO = "IO"
VERBOSITY_OFF = "OFF"

# The on-stage kill switch. `false` means exactly what `telemetryLevel=OFF`
# means; it exists so the switch can be a plain boolean.
OTEL_ENABLED_FLAG_KEY = "otelEnabled"

# Every provider event is a reason to re-classify: READY is the first chance to
# ask at all, CONFIGURATION_CHANGED means the ruleset moved, and ERROR/STALE
# mean whatever was cached may no longer reflect the flag.
_RECLASSIFY_ON = (
    ProviderEvent.PROVIDER_READY,
    ProviderEvent.PROVIDER_CONFIGURATION_CHANGED,
    ProviderEvent.PROVIDER_ERROR,
    ProviderEvent.PROVIDER_STALE,
)


class _CachingResolver:
    """Resolves one flag, evaluating once when the flag is static."""

    flag_key: str

    def __init__(self) -> None:
        # `None` means "not cacheable, evaluate per span". A plain attribute
        # rather than a lock: it's written by the provider's event thread and
        # read by every span, and a torn read is not possible - the worst a
        # race can do is serve the previous classification for one more span,
        # which the next event corrects. Flag values are never `None`
        # themselves, so a cached `False` is still a cached answer.
        self._cached: object | None = None

    @property
    def cached(self):
        return self._cached

    def classify(self, details: EventDetails | None = None) -> None:
        """Evaluate once, context-free, and decide whether to keep the answer.

        Registered as an OpenFeature event handler - see `_RECLASSIFY_ON`.
        """
        # `flags_changed` is populated by flagd's in-process resolver; when a
        # provider reports which keys moved and ours isn't among them, the
        # classification cannot have changed. Other events (READY, ERROR,
        # STALE) carry no key list and always re-classify.
        changed = getattr(details, "flags_changed", None)
        if changed and self.flag_key not in changed:
            return

        value, details = self._evaluate()
        reason = details.reason
        previous = self._cached
        # Deliberately `STATIC` only. TARGETING_MATCH and DEFAULT both mean a
        # targeting block ran, so the answer belongs to one evaluation context
        # and not to the process; DISABLED and other errors are not worth
        # betting on. The one exception is a flag that doesn't exist: that is
        # the normal state of a deployment that never defined it, the fail-open
        # default is the whole answer, and creating the flag later lists it in
        # `flags_changed`, which re-classifies.
        cacheable = reason == Reason.STATIC or details.error_code == ErrorCode.FLAG_NOT_FOUND
        self._cached = value if cacheable else None

        if self._cached != previous:
            if self._cached is None:
                _logger.info(
                    "otelfeature-instrument: %s resolves with reason=%s - "
                    "evaluating per span",
                    self.flag_key,
                    reason,
                )
            else:
                _logger.info(
                    "otelfeature-instrument: %s is static (%s) - serving %s "
                    "without further evaluation",
                    self.flag_key,
                    reason,
                    self._cached,
                )

    def current(self, context: EvaluationContext | None = None):
        cached = self._cached
        if cached is not None:
            # The point of the whole exercise: on the static path no
            # evaluation runs and no hook chain is walked.
            return cached
        return self._evaluate(context)[0]

    def _evaluate(
        self, context: EvaluationContext | None = None
    ) -> tuple[object, FlagEvaluationDetails]:
        """Return the normalised value and the raw evaluation details.

        Implementations always read the live global provider rather than
        something captured at configure_feature_flags() time, so an app that
        registers its own provider later is picked up without restarting
        anything.
        """
        raise NotImplementedError


class _VerbosityResolver(_CachingResolver):
    """Resolves `telemetryLevel`. Anything but `IO`/`OFF` behaves as `FULL`."""

    flag_key = TRACE_VERBOSITY_FLAG_KEY

    def _evaluate(
        self, context: EvaluationContext | None = None
    ) -> tuple[str, FlagEvaluationDetails]:
        details = feature_api.get_client().get_string_details(
            self.flag_key, VERBOSITY_FULL, context
        )
        return (details.value or VERBOSITY_FULL).strip().upper(), details


class _EnabledResolver(_CachingResolver):
    """Resolves `otelEnabled`. Fails open: only an explicit `false` disables."""

    flag_key = OTEL_ENABLED_FLAG_KEY

    def _evaluate(
        self, context: EvaluationContext | None = None
    ) -> tuple[bool, FlagEvaluationDetails]:
        # A missing flag, a provider error or a non-boolean variant all come
        # back from the SDK as our default `True` (reason ERROR).
        details = feature_api.get_client().get_boolean_details(
            self.flag_key, True, context
        )
        return details.value is not False, details


_resolver = _VerbosityResolver()
_enabled_resolver = _EnabledResolver()
_handlers_registered = False


def configure_feature_flags() -> None:
    provider = os.environ.get(FLAG_PROVIDER_ENV_VAR, FLAG_PROVIDER_FLAGD).strip().lower()
    if provider == FLAG_PROVIDER_NONE:
        # Still worth listening: the application may register its own provider
        # later, and its READY/CONFIGURATION_CHANGED events have to drive the
        # classification just the same.
        _register_event_handlers()
        return
    if provider != FLAG_PROVIDER_FLAGD:
        raise ValueError(
            f"Unknown {FLAG_PROVIDER_ENV_VAR}={provider!r}; expected "
            f"{FLAG_PROVIDER_FLAGD!r} or {FLAG_PROVIDER_NONE!r}"
        )

    _register_event_handlers()
    # in-process is a correctness requirement, not a deployment choice: it's
    # what makes current_verbosity() a per-span-safe, no-network-call read, and
    # what delivers the configuration-changed events the classification needs.
    feature_api.set_provider(FlagdProvider(resolver_type=ResolverType.IN_PROCESS))


def _register_event_handlers() -> None:
    # add_handler() appends blindly, so a second configure_feature_flags()
    # would classify twice per event: same outcome, twice the evaluations.
    global _handlers_registered
    if _handlers_registered:
        return
    _handlers_registered = True

    for event in _RECLASSIFY_ON:
        # The SDK runs a handler immediately if the provider is already in the
        # matching state (see openfeature/_event_support.py), so registering
        # before or after set_provider() both end up classifying.
        for resolver in (_enabled_resolver, _resolver):
            feature_api.add_handler(event, resolver.classify)


def current_verbosity(context: EvaluationContext | None = None) -> str:
    """The current verbosity, evaluating only when the flag isn't static.

    `context` is the seam for contextual evaluation: it's forwarded to the
    provider on the non-static path, and ignored on the static path (where
    there is nothing to evaluate). Callers pass nothing today; describing the
    span - and eventually its OTel attributes - to a targeting rule goes here.
    """
    return _resolver.current(context)


def telemetry_enabled(context: EvaluationContext | None = None) -> bool:
    """Whether `otelEnabled` allows tracing at all; `True` unless explicitly off.

    Cached and context-forwarded exactly like `current_verbosity()`.
    """
    return _enabled_resolver.current(context)
