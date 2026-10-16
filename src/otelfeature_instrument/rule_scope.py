"""Which flags can be decided once per tracer instead of once per span.

A targeting rule that reads only resource keys (`service.name`, ...) and the
instrumentation scope (`otel.scope.name`, `otel.scope.version`) has the same
answer for every span a tracer starts: those keys are fixed per tracer, and
the extension's own values always win over span attributes. "Mute the noisy
instrumentation" is exactly such a rule. For those flags the tracer evaluates
once and keeps the answer until the next provider event, so a muted
instrumentation costs nothing per span.

Knowing what a rule reads means reading the rule. OpenFeature doesn't expose
it, so this looks into the flagd in-process evaluator (pinned exactly in
pyproject.toml). Anything it can't see or can't prove - another provider, an
unknown operator, a computed `var` path - is "per span", today's behaviour.

Two more conditions are checked per tracer and per span, not here: every key
the rule reads must actually be present in the tracer's context (an absent
resource key could still come from a span attribute), and nothing outside
the extension may add to the context (`ambient_context_is_fixed`).
"""

from __future__ import annotations

from typing import Any

from openfeature import api as feature_api
from openfeature import transaction_context
from openfeature.contrib.provider.flagd.sync_metadata_hook import SyncMetadataHook
from openfeature.transaction_context import NoOpTransactionContextPropagator

from otelfeature_instrument.evaluation_context import RESOURCE_KEYS

TRACER_KEYS = frozenset(RESOURCE_KEYS) | {"otel.scope.name", "otel.scope.version"}
# flagd's own context: `$flagd.flagKey` is a constant; `$flagd.timestamp`
# changes every second and is deliberately not here.
_CONSTANT_KEYS = frozenset({"$flagd.flagKey"})

_UNKNOWN = object()


def reads_only_tracer_keys(targeting: Any) -> bool:
    """True if every key the rule can read is fixed per tracer."""
    return tracer_keys_read(targeting) is not None


def tracer_keys_read(targeting: Any) -> frozenset[str] | None:
    """The tracer-level keys the rule reads, or `None` if it reads anything else."""
    keys = _reads(targeting)
    if keys is None or not all(_tracer_level(key) for key in keys):
        return None
    return frozenset(keys - _CONSTANT_KEYS)


def covered(keys: frozenset[str], present: frozenset[str]) -> bool:
    """Every key read is in the tracer's context (or below a key that is,
    which is always null). An absent key is not: a span attribute of that
    name would reach a per-span evaluation."""
    return all(
        key in present or any(key.startswith(own + ".") for own in present) for key in keys
    )


def ambient_context_is_fixed() -> bool:
    """Nothing but the extension's own context reaches the evaluation.

    The SDK merges the global context, a transaction context and whatever
    hooks add. Any of those could differ per request or change without a
    provider event, so their presence means per span. flagd's own sync
    metadata hook is fine: it changes only with the ruleset.
    """
    if not isinstance(
        transaction_context._evaluation_transaction_context_propagator,
        NoOpTransactionContextPropagator,
    ):
        return False
    if feature_api.get_hooks():
        return False
    ambient = feature_api.get_evaluation_context()
    if ambient.attributes or ambient.targeting_key:
        return False
    try:
        hooks = feature_api.provider_registry.get_default_provider().get_provider_hooks()
    except AttributeError:
        return False
    return all(isinstance(hook, SyncMetadataHook) for hook in hooks)


def _tracer_level(key: str) -> bool:
    # A key or something below it (`service.name.x` is always null). Never a
    # namespace above it: `{"var": "service"}` returns an object that span
    # attributes such as `service.foo` would also land in.
    return key in _CONSTANT_KEYS or any(
        key == allowed or key.startswith(allowed + ".") for allowed in TRACER_KEYS
    )


def _reads(node: Any) -> set[str] | None:
    """The context keys a JsonLogic node can read, or `None` if unknowable."""
    if isinstance(node, list):
        return _union(_reads(item) for item in node)
    if not isinstance(node, dict):
        return set()
    keys: set[str] = set()
    for op, args in node.items():
        if op == "var":
            path = args[0] if isinstance(args, list) and args else args
            if not isinstance(path, str):
                return None  # computed path: can't tell what it reads
            keys.add(path)
            if isinstance(args, list) and len(args) > 1:
                defaults = _reads(args[1:])
                if defaults is None:
                    return None
                keys |= defaults
        elif op in ("missing", "missing_some"):
            names = args
            if op == "missing_some":
                if not isinstance(args, list) or len(args) != 2:
                    return None
                # The need-count can be an expression too.
                count = _reads(args[0])
                if count is None:
                    return None
                keys |= count
                names = args[1]
            for name in names if isinstance(names, list) else [names]:
                if not isinstance(name, str):
                    return None
                keys.add(name)
        elif op == "fractional":
            items = args if isinstance(args, list) else [args]
            # flagd buckets on the first argument only if it evaluates to a
            # string; otherwise on targetingKey (+ flag key) - per span by
            # definition. Only a string literal or a plain `var` (whose key
            # must then also be present - see `covered`) is accepted.
            first = items[0] if items else None
            plain_var = (
                isinstance(first, dict)
                and set(first) == {"var"}
                and isinstance(first["var"], str)
            )
            if not (isinstance(first, str) or plain_var):
                keys.add("targetingKey")
            nested = _reads(items)
            if nested is None:
                return None
            keys |= nested
        else:
            nested = _reads(args)
            if nested is None:
                return None
            keys |= nested
    return keys


def _union(parts) -> set[str] | None:
    keys: set[str] = set()
    for part in parts:
        if part is None:
            return None
        keys |= part
    return keys


def targeting_of(flag_key: str) -> Any:
    """The flag's targeting rule as flagd holds it; `_UNKNOWN` when it can't
    be read (not the flagd in-process/file provider, or a changed internal)."""
    try:
        provider = feature_api.provider_registry.get_default_provider()
        evaluator = provider.resolver.evaluator
        with evaluator._lock:
            flag = evaluator._flag_store.get_flag(flag_key)
    except AttributeError:
        return _UNKNOWN
    return None if flag is None else flag.targeting


def tracer_keys_of(flag_key: str) -> frozenset[str] | None:
    """The keys the flag's rule reads if it can be decided per tracer, else `None`."""
    targeting = targeting_of(flag_key)
    if targeting is _UNKNOWN or targeting is None:
        return None
    return tracer_keys_read(targeting)


def is_tracer_scoped(flag_key: str) -> bool:
    return tracer_keys_of(flag_key) is not None
