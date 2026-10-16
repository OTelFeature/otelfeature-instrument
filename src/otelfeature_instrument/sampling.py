"""`traceSampleRatio`: a live OTEL_TRACES_SAMPLER_ARG.

The sampler is parent-based - a span with a parent follows the parent's
decision, so a trace is never half sampled - and its root delegate samples
by trace id with whatever ratio the policy carries at that moment. The ratio
check is the SDK's own `TraceIdRatioBased` rule (the low 64 bits of the
trace id against a bound), so every service that applies the same ratio to
the same trace reaches the same decision.

The policy usually reaches the sampler by hand: when a flag carries
targeting, the tracer has just evaluated it with the span's evaluation
context, and `handover()` passes that result along instead of evaluating
again without one. See "Sampling" in the README for the caveats.
"""

from __future__ import annotations

import os
import threading
from logging import getLogger
from typing import Mapping, Sequence

from opentelemetry.context import Context
from opentelemetry.sdk.trace.sampling import (
    ALWAYS_OFF,
    DEFAULT_OFF,
    Decision,
    ParentBased,
    Sampler,
    SamplingResult,
    TraceIdRatioBased,
    _get_parent_trace_state,
)
from opentelemetry.trace import Link, SpanKind
from opentelemetry.trace.span import TraceState
from opentelemetry.util.types import Attributes

from otelfeature_instrument.flags import RATIO_SAMPLERS, TelemetryPolicy, cached_policy, current_policy

_logger = getLogger(__name__)

_handover = threading.local()


def handover(policy: TelemetryPolicy | None) -> None:
    """Pass the policy the tracer just evaluated to the sampler on this thread."""
    _handover.policy = policy


def _policy() -> TelemetryPolicy:
    policy = getattr(_handover, "policy", None)
    if policy is not None:
        return policy
    policy = cached_policy()
    if policy is not None:
        return policy
    # A span started past the wrapper (straight on the SDK tracer): no
    # context to describe it with, but still the live flag.
    return current_policy()


class _FlagRatioSampler(Sampler):
    """Samples a root by trace id with the policy's current ratio."""

    def should_sample(
        self,
        parent_context: Context | None,
        trace_id: int,
        name: str,
        kind: SpanKind | None = None,
        attributes: Attributes = None,
        links: Sequence[Link] | None = None,
        trace_state: TraceState | None = None,
    ) -> SamplingResult:
        ratio = _policy().sample_ratio
        bound = TraceIdRatioBased.get_bound_for_rate(ratio)
        if trace_id & TraceIdRatioBased.TRACE_ID_LIMIT < bound:
            return SamplingResult(
                Decision.RECORD_AND_SAMPLE, attributes, _get_parent_trace_state(parent_context)
            )
        return SamplingResult(Decision.DROP, None, _get_parent_trace_state(parent_context))

    def get_description(self) -> str:
        return "FlagRatioSampler{traceSampleRatio}"


def flag_aware_sampler(environ: Mapping[str, str] = os.environ) -> Sampler:
    """The sampler `install_tracer_provider` installs - parent-based.

    OTEL_TRACES_SAMPLER supplies the fallback ratio (see
    `TelemetryPolicy.from_environment`), except for the two "off" samplers,
    which are hard floors - flags only ever reduce telemetry, they never
    enable what the environment switched off (the same rule as
    OTEL_SDK_DISABLED):

    - `always_off`: nothing is sampled - not by `traceSampleRatio`, not via
      a sampled upstream parent. The SDK's own ALWAYS_OFF is installed.
    - `parentbased_always_off`: no trace *starts* sampled here, whatever
      `traceSampleRatio` says; spans with a parent follow it, exactly as the
      SDK's sampler of that name does.

    Where the configured sampler would otherwise have behaved differently -
    not parent-based, or not a built-in at all - that is logged once, here,
    at startup.
    """
    configured = environ.get("OTEL_TRACES_SAMPLER", "parentbased_always_on").lower()
    if configured == "always_off":
        return ALWAYS_OFF
    if configured == "parentbased_always_off":
        return DEFAULT_OFF
    if configured not in RATIO_SAMPLERS:
        # The plain SDK provider never loads sampler entry points either: an
        # unknown name already meant parentbased_always_on before.
        _logger.warning(
            "otelfeature-instrument: OTEL_TRACES_SAMPLER=%r is not a built-in "
            "sampler and custom samplers are not loaded - sampling with "
            "traceSampleRatio, falling back to 1.0",
            configured,
        )
    elif not configured.startswith("parentbased_"):
        _logger.warning(
            "otelfeature-instrument: OTEL_TRACES_SAMPLER=%s is applied parent-based - "
            "a span with a sampled parent is sampled whatever the ratio",
            configured,
        )
    return ParentBased(root=_FlagRatioSampler())
