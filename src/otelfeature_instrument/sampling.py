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


def flag_aware_sampler(environ: Mapping[str, str] = os.environ) -> Sampler | None:
    """The sampler to install, or `None` to leave OTEL_TRACES_SAMPLER in charge.

    Takes over whenever the configured sampler is one of the SDK's built-in
    ratio-shaped ones (or none is configured); its ratio becomes the flag's
    fallback. A custom or `jaeger_remote` sampler is left alone.
    """
    configured = environ.get("OTEL_TRACES_SAMPLER", "parentbased_always_on").strip().lower()
    if configured not in RATIO_SAMPLERS:
        _logger.info(
            "otelfeature-instrument: OTEL_TRACES_SAMPLER=%s is not a ratio sampler - "
            "traceSampleRatio does not apply",
            configured,
        )
        return None
    return ParentBased(root=_FlagRatioSampler())
