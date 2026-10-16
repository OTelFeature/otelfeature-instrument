"""Feature-flag wiring that resolves every flag into one `TelemetryPolicy`.

Registering a flagd provider is the default, but it's gated by
`OTELFEATURE_FLAG_PROVIDER` rather than unconditional: this module runs from
`sitecustomize.py`, before the target application's own code is ever
imported, so there's no way to detect "did the app already configure
OpenFeature itself" - that code hasn't run yet. Setting
`OTELFEATURE_FLAG_PROVIDER=none` is how a consuming app opts out and takes
over OpenFeature configuration on its own terms (at any point - the resolver
below always reads the *current* global provider, not a snapshot taken at
startup).

Enforcement points ask one question - `current_policy()` - and get back a
`TelemetryPolicy`; how each field is derived from which flag, and what happens
when a flag can't be read, lives here and nowhere else. Every field falls back
to the value resolved from the environment at startup, so with flagd
unreachable or a flag undefined the behaviour is that of plain
`opentelemetry-instrument`.

Each flag is evaluated once while static and per span once it carries
targeting, with the classification redone on provider events. See
"Evaluating once, or evaluating always" in the README for what each resolution
reason implies and why keeping an answer is safe; the comments below cover the
local decisions only.
"""

from __future__ import annotations

import math
import os
import threading
from dataclasses import dataclass
from logging import getLogger
from typing import Mapping

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

# The selective drop: `false` drops just the spans a targeting rule matches
# (and, for a root, its whole trace). Below the global switches - it never
# re-enables what `otelEnabled`/`telemetryLevel=OFF` turned off.
RECORD_SPAN_FLAG_NAME_ENV_VAR = "OTELFEATURE_RECORD_SPAN_FLAG_NAME"


def record_span_flag_key(environ: Mapping[str, str] = os.environ) -> str:
    return environ.get(RECORD_SPAN_FLAG_NAME_ENV_VAR, "").strip() or "recordSpan"


RECORD_SPAN_FLAG_KEY = record_span_flag_key()

# Live counterpart of OTEL_TRACES_SAMPLER_ARG: probability (0.0-1.0) that a
# new trace is sampled, applied at the root and inherited downstream.
SAMPLE_RATIO_FLAG_KEY = "traceSampleRatio"

# The SDK's built-in OTEL_TRACES_SAMPLER values, as the ratio each amounts to
# (`None`: read OTEL_TRACES_SAMPLER_ARG) - the fallback for `sample_ratio`.
# Anything else is unknown to the SDK too, which then falls back to
# parentbased_always_on, i.e. 1.0.
RATIO_SAMPLERS = {
    "always_on": 1.0,
    "parentbased_always_on": 1.0,
    "always_off": 0.0,
    "parentbased_always_off": 0.0,
    "traceidratio": None,
    "parentbased_traceidratio": None,
}


@dataclass(frozen=True)
class TelemetryPolicy:
    """What the flags currently allow. One instance answers one decision point.

    Adding a flag means adding a field here and reading it at its one call
    site; failure handling stays in `_PolicySource`.
    """

    # Tracing at all. False when `otelEnabled=false` or `telemetryLevel=OFF`.
    enabled: bool = True
    # Suppress `INTERNAL` spans. True when `telemetryLevel=IO`.
    suppress_internal: bool = False
    # Record this span. False when `recordSpan` says so for it; only
    # consulted while `enabled`.
    record_span: bool = True
    # Probability that a new trace is sampled. From `traceSampleRatio`.
    sample_ratio: float = 1.0

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] = os.environ) -> TelemetryPolicy:
        """The per-field fallbacks: what the process would do with no flags at all.

        `OTEL_SDK_DISABLED` maps onto `enabled`, and `OTEL_TRACES_SAMPLER` /
        `OTEL_TRACES_SAMPLER_ARG` onto `sample_ratio` - parsed the way the SDK
        parses them. Nothing in the environment asks for `INTERNAL` spans to
        be suppressed.
        """
        disabled = environ.get("OTEL_SDK_DISABLED", "").strip().lower() == "true"
        return cls(enabled=not disabled, sample_ratio=_ratio_from_environment(environ))


def _ratio_from_environment(environ: Mapping[str, str]) -> float:
    # No strip(): the SDK doesn't either, and " always_off" must mean here
    # what it means there.
    sampler = environ.get("OTEL_TRACES_SAMPLER", "parentbased_always_on").lower()
    if sampler not in RATIO_SAMPLERS:
        return 1.0
    fixed = RATIO_SAMPLERS[sampler]
    if fixed is not None:
        return fixed
    try:
        ratio = float(environ.get("OTEL_TRACES_SAMPLER_ARG", ""))
    except ValueError:
        return 1.0  # what the SDK does with a missing/invalid argument
    return ratio if 0.0 <= ratio <= 1.0 else 1.0


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

    def __init__(self, default) -> None:
        # Served by the SDK whenever the flag can't be read (missing, provider
        # error, wrong type) - the field's startup fallback, never a constant.
        self.default = default
        # Last error code reported, so a broken flag is logged once when it
        # breaks and once when it recovers, not on every span. Span threads
        # and the event thread both report; the lock is only taken when the
        # code changes, so the steady state stays lock-free.
        self._reported_error: object | None = None
        self._report_lock = threading.Lock()
        # `None` means "not cacheable, evaluate per span". Read lock-free by
        # every span; written only from `classify()`, which `_PolicySource`
        # serialises - the SDK runs event handlers on a thread pool, and two
        # interleaved classifications could otherwise leave the older answer
        # cached until the next event. Flag values are never `None`
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

        # A missing flag was already logged by `_report()`.
        if self._cached != previous and details.error_code != ErrorCode.FLAG_NOT_FOUND:
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
        # Always reads the live global provider rather than something captured
        # at configure_feature_flags() time, so an app that registers its own
        # provider later is picked up without restarting anything.
        details = self._read(feature_api.get_client(), context)
        self._report(details)
        return self._normalise(details.value), details

    def _report(self, details: FlagEvaluationDetails) -> None:
        error = details.error_code
        if error == self._reported_error:
            return
        with self._report_lock:
            previous = self._reported_error
            if error == previous:
                return
            self._reported_error = error
        if error is None:
            _logger.info(
                "otelfeature-instrument: %s %s",
                self.flag_key,
                "is now defined" if previous == ErrorCode.FLAG_NOT_FOUND else "readable again",
            )
        elif error == ErrorCode.FLAG_NOT_FOUND:
            # The normal state of a deployment that never defined the flag.
            _logger.info(
                "otelfeature-instrument: %s is not defined - using %r",
                self.flag_key,
                self.default,
            )
        else:
            _logger.warning(
                "otelfeature-instrument: %s could not be evaluated (%s: %s) - "
                "using %r until it can",
                self.flag_key,
                error,
                details.error_message,
                self.default,
            )

    def _read(self, client, context: EvaluationContext | None) -> FlagEvaluationDetails:
        raise NotImplementedError

    def _normalise(self, value):
        return value


class _VerbosityResolver(_CachingResolver):
    """Resolves `telemetryLevel` to an upper-cased variant name."""

    flag_key = TRACE_VERBOSITY_FLAG_KEY

    def __init__(self, default: str = VERBOSITY_FULL) -> None:
        super().__init__(default)
        self._reported_unknown: str | None = None

    def _read(self, client, context: EvaluationContext | None) -> FlagEvaluationDetails:
        return client.get_string_details(self.flag_key, self.default, context)

    def _normalise(self, value) -> str:
        level = (value or self.default).strip().upper()
        if level not in _KNOWN_LEVELS and level != self._reported_unknown:
            # Logged once per unknown value; a benign race may log it twice.
            self._reported_unknown = level
            _logger.warning(
                "otelfeature-instrument: %s has unknown variant %r - "
                "falling back to the startup value",
                self.flag_key,
                level,
            )
        return level


_KNOWN_LEVELS = frozenset({VERBOSITY_FULL, VERBOSITY_IO, VERBOSITY_OFF})


class _SampleRatioResolver(_CachingResolver):
    """Resolves `traceSampleRatio`, never raising on the hot path.

    Out of range is clamped to 0.0-1.0; anything that isn't a number (NaN
    included) falls back to the startup value. A non-numeric variant never
    gets here: the SDK reports a type mismatch and serves the default.
    """

    flag_key = SAMPLE_RATIO_FLAG_KEY

    def __init__(self, default: float = 1.0) -> None:
        super().__init__(default)
        self._reported_invalid: object = None

    def _read(self, client, context: EvaluationContext | None) -> FlagEvaluationDetails:
        return client.get_float_details(self.flag_key, self.default, context)

    def _normalise(self, value) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or math.isnan(value):
            self._warn_once(value, "is not a number - using the startup value")
            return self.default
        if 0.0 <= value <= 1.0:
            return float(value)
        self._warn_once(value, "is outside 0.0-1.0 - clamping")
        return min(1.0, max(0.0, float(value)))

    def _warn_once(self, value, problem: str) -> None:
        # Once per offending value; a benign race may log it twice. Compared
        # by repr() because NaN never equals itself.
        if repr(value) == self._reported_invalid:
            return
        self._reported_invalid = repr(value)
        _logger.warning("otelfeature-instrument: %s=%r %s", self.flag_key, value, problem)


class _EnabledResolver(_CachingResolver):
    """Resolves `otelEnabled`."""

    flag_key = OTEL_ENABLED_FLAG_KEY

    def __init__(self, default: bool = True) -> None:
        super().__init__(default)

    def _read(self, client, context: EvaluationContext | None) -> FlagEvaluationDetails:
        # A missing flag, a provider error or a non-boolean variant all come
        # back from the SDK as `self.default` (reason ERROR).
        return client.get_boolean_details(self.flag_key, self.default, context)

    def _normalise(self, value) -> bool:
        return self.default if value is None else bool(value)


class _RecordSpanResolver(_EnabledResolver):
    """Resolves `recordSpan`; like `otelEnabled`, only an explicit `false` drops."""

    flag_key = RECORD_SPAN_FLAG_KEY


class _PolicySource:
    """Combines the flags into a `TelemetryPolicy`, one read per decision point."""

    def __init__(self, defaults: TelemetryPolicy | None = None) -> None:
        self.defaults = defaults or TelemetryPolicy()
        self.enabled = _EnabledResolver(self.defaults.enabled)
        self.level = _VerbosityResolver(
            VERBOSITY_IO if self.defaults.suppress_internal else VERBOSITY_FULL
        )
        self.record = _RecordSpanResolver(self.defaults.record_span)
        self.ratio = _SampleRatioResolver(self.defaults.sample_ratio)
        # Same contract as `_CachingResolver._cached`: set only while every
        # input flag is cached, so the static path stays one attribute read
        # and allocates nothing.
        self._cached: TelemetryPolicy | None = None
        # The same for spans that have a parent. `sample_ratio` only matters
        # at a root (children follow their parent's decision), so a targeted
        # `traceSampleRatio` must not cost every child span an evaluation:
        # this one is set as soon as `otelEnabled`, `telemetryLevel` and
        # `recordSpan` are cached, and carries the startup ratio, which
        # nothing reads.
        self._cached_child: TelemetryPolicy | None = None
        self._classify_lock = threading.Lock()
        # Disabled is disabled: nothing else in the policy matters then.
        self._disabled = TelemetryPolicy(
            enabled=False,
            suppress_internal=self.defaults.suppress_internal,
            sample_ratio=self.defaults.sample_ratio,
        )
        # OTEL_SDK_DISABLED=true is a hard floor: flags only ever reduce
        # telemetry, they never enable what the environment turned off.
        self._floor = not self.defaults.enabled
        if self._floor:
            self._cached = self._cached_child = self._disabled

    @property
    def cached(self) -> TelemetryPolicy | None:
        return self._cached

    @property
    def cached_child(self) -> TelemetryPolicy | None:
        return self._cached_child

    def classify(self, details: EventDetails | None = None) -> None:
        """Registered as an OpenFeature event handler - see `_RECLASSIFY_ON`.

        Serialised: the SDK dispatches events on a thread pool, and the last
        classification to *start* must be the one that ends up cached.
        """
        if self._floor:
            return
        with self._classify_lock:
            self.enabled.classify(details)
            self.level.classify(details)
            self.record.classify(details)
            self.ratio.classify(details)
            enabled, level = self.enabled.cached, self.level.cached
            record, ratio = self.record.cached, self.ratio.cached
            if enabled is None or level is None or record is None:
                self._cached = self._cached_child = None
                return
            self._cached_child = self._combine(
                enabled, level, self.defaults.sample_ratio, record
            )
            self._cached = (
                None if ratio is None else self._combine(enabled, level, ratio, record)
            )

    def current(
        self, context: EvaluationContext | None = None, *, root: bool = True
    ) -> TelemetryPolicy:
        """`root=False` for a span with a parent: `sample_ratio` is then not
        evaluated (and holds the startup value), since nothing reads it."""
        cached = self._cached if root else self._cached_child
        if cached is not None:
            return cached
        enabled = self.enabled.current(context)
        if not enabled:
            # With tracing off nothing else is evaluated at all.
            return self._disabled
        level = self.level.current(context)
        if level == VERBOSITY_OFF:
            return self._disabled
        ratio = self.ratio.current(context) if root else self.defaults.sample_ratio
        return self._combine(enabled, level, ratio, self.record.current(context))

    def _combine(
        self, enabled: bool, level: str, ratio: float, record: bool = True
    ) -> TelemetryPolicy:
        # Precedence: OTEL_SDK_DISABLED (the floor, see __init__) >
        # otelEnabled / telemetryLevel=OFF > recordSpan.
        if not enabled or level == VERBOSITY_OFF:
            return self._disabled
        # An unknown `telemetryLevel` variant is no answer at all, so the
        # field keeps its startup fallback - fail open, as before. (The
        # resolver's own default covers a *missing* flag; this covers a
        # defined flag with a value nobody knows.)
        suppress_internal = {VERBOSITY_FULL: False, VERBOSITY_IO: True}.get(
            level, self.defaults.suppress_internal
        )
        return TelemetryPolicy(
            enabled=True,
            suppress_internal=suppress_internal,
            record_span=record,
            sample_ratio=ratio,
        )


# Captured at import, which is process startup: this module is imported from
# sitecustomize.py before the application's code runs.
_policy_source = _PolicySource(TelemetryPolicy.from_environment())
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
    # what makes current_policy() a per-span-safe, no-network-call read, and
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
        feature_api.add_handler(event, _policy_source.classify)


def cached_policy(*, root: bool = True) -> TelemetryPolicy | None:
    """The policy while every flag it needs is static, else `None`.

    Lets a caller skip building an evaluation context it would not need: on
    the static path nothing is evaluated, so nothing has to be described.
    `root=False` (a span with a parent) doesn't need `traceSampleRatio`.
    """
    return _policy_source.cached if root else _policy_source.cached_child


def current_policy(
    context: EvaluationContext | None = None, *, root: bool = True
) -> TelemetryPolicy:
    """The current policy, evaluating only the flags that aren't static.

    `context` is forwarded to the provider for every flag that carries
    targeting, and ignored on the static path (where there is nothing to
    evaluate). With `root=False`, `traceSampleRatio` is not evaluated.
    """
    return _policy_source.current(context, root=root)
