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
"""

from __future__ import annotations

from typing import Any

from openfeature import api as feature_api

from otelfeature_instrument.evaluation_context import RESOURCE_KEYS

TRACER_KEYS = frozenset(RESOURCE_KEYS) | {"otel.scope.name", "otel.scope.version"}
# flagd's own context: `$flagd.flagKey` is a constant; `$flagd.timestamp`
# changes every second and is deliberately not here.
_CONSTANT_KEYS = frozenset({"$flagd.flagKey"})

_UNKNOWN = object()


def reads_only_tracer_keys(targeting: Any) -> bool:
    """True if every key the rule can read is fixed per tracer."""
    keys = _reads(targeting)
    return keys is not None and all(_tracer_level(key) for key in keys)


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
            names = args[1] if op == "missing_some" and isinstance(args, list) and len(args) > 1 else args
            for name in names if isinstance(names, list) else [names]:
                if not isinstance(name, str):
                    return None
                keys.add(name)
        elif op == "fractional":
            items = args if isinstance(args, list) else [args]
            # Without an explicit bucketing expression first, flagd buckets
            # on targetingKey (+ flag key): per span by definition.
            if items and isinstance(items[0], list):
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


def is_tracer_scoped(flag_key: str) -> bool:
    targeting = targeting_of(flag_key)
    return targeting is not _UNKNOWN and targeting is not None and reads_only_tracer_keys(targeting)
