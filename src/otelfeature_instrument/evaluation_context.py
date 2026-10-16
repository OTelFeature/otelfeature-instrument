"""The OpenFeature evaluation context, built from OpenTelemetry semantic conventions.

Targeting rules are written against semconv names verbatim -
`{"var": "service.name"}` - and JsonLogic's `var` splits on `.`, so every
attribute is nested by dot segment: `service.name` is sent as
`{"service": {"name": ...}}`. A flat dotted key never matches in any flagd
evaluator (OTelFeature/poc#22), so the flat form is never sent at all.

See "The evaluation context" in the README for which keys are available.
"""

from __future__ import annotations

import threading
from logging import getLogger
from typing import Any, Mapping, Sequence

from openfeature.evaluation_context import EvaluationContext
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace.id_generator import IdGenerator
from opentelemetry.trace import SpanKind
from opentelemetry.util import types as otel_types

_logger = getLogger(__name__)

# Resource attributes worth targeting on. Deliberately a short list: a
# resource also carries process.command_args, host.*, os.* and the like,
# which no rule needs and every non-static evaluation would otherwise copy.
RESOURCE_KEYS = (
    "service.name",
    "service.namespace",
    "service.version",
    "deployment.environment.name",
    "telemetry.sdk.language",
)


# Process-wide values that change over time (e.g. the error rate), merged
# into every context. Replaced wholesale rather than mutated, so a reader
# always sees one consistent snapshot without a lock.
_live: Mapping[str, Any] = {}


def set_live_attribute(key: str, value: Any | None) -> None:
    """Publish `value` under `key` in every evaluation context; `None` removes it."""
    global _live
    live = dict(_live)
    if value is None:
        live.pop(key, None)
    else:
        live[key] = value
    _live = live


def nest(flat: Mapping[str, Any]) -> dict[str, Any]:
    """Nest dotted keys by segment: `{"a.b": 1}` becomes `{"a": {"b": 1}}`.

    A name that is both a leaf and a namespace (`db.system` next to
    `db.system.name`) cannot be both in a nested object. The namespace wins
    and the leaf is dropped: in semconv such pairs are deprecated/replacement
    pairs, and the leaf is the deprecated one. The outcome depends only on
    the set of keys, never on their order.
    """
    namespaces = {
        ".".join(segments[:i])
        for segments in (key.split(".") for key in flat)
        for i in range(1, len(segments))
    }
    nested: dict[str, Any] = {}
    for key, value in flat.items():
        if key in namespaces:
            _logger.debug(
                "otelfeature-instrument: dropping %r from the evaluation context - "
                "it is also a namespace",
                key,
            )
            continue
        *parents, leaf = key.split(".")
        node = nested
        for segment in parents:
            node = node.setdefault(segment, {})
        node[leaf] = _plain(value)
    return nested


def _plain(value: Any) -> Any:
    if isinstance(value, (str, bool, int, float)) or value is None:
        return value
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, Sequence):
        # OTel array attributes are tuples/sequences; JSON-ish lists are what
        # the evaluators expect.
        return [_plain(item) for item in value]
    return str(value)


def resource_attributes(resource: Resource | None) -> dict[str, Any]:
    if resource is None:
        return {}
    return {key: resource.attributes[key] for key in RESOURCE_KEYS if key in resource.attributes}


def span_context(
    *,
    base: Mapping[str, Any],
    scope_name: str,
    scope_version: str | None,
    name: str,
    kind: SpanKind,
    attributes: otel_types.Attributes,
    trace_id: int | None,
) -> EvaluationContext:
    """The context for one span-start decision. Only built off the static path.

    Resource keys, live keys and the keys set here win over the span's own start
    attributes: an attribute named like one of them - or one that would turn
    one of them into a namespace, such as `span.kind.detail` - is dropped
    rather than allowed to replace or hide it.
    """
    owned: dict[str, Any] = dict(base)
    owned.update(_live)
    owned["otel.scope.name"] = scope_name
    if scope_version:
        owned["otel.scope.version"] = scope_version
    owned["span.kind"] = kind.name.lower()
    owned["span.name"] = name

    flat: dict[str, Any] = {}
    for key, value in (attributes or {}).items():
        if any(_overlaps(key, own) for own in owned):
            _logger.debug(
                "otelfeature-instrument: dropping start attribute %r from the "
                "evaluation context - it clashes with a key set by the extension",
                key,
            )
            continue
        flat[key] = value
    flat.update(owned)
    return EvaluationContext(
        targeting_key=None if trace_id is None else format(trace_id, "032x"),
        attributes=nest(flat),
    )


def _overlaps(a: str, b: str) -> bool:
    return a == b or a.startswith(b + ".") or b.startswith(a + ".")


class PresetTraceIdGenerator(IdGenerator):
    """Lets a root span keep the trace id its flag evaluation was bucketed on.

    A root span's trace id is normally minted inside the SDK's `start_span`,
    after the flag has already been asked. To make `targetingKey` the trace id
    for roots too, the id is generated first, evaluated on, and then handed to
    the SDK through this generator for exactly the next `generate_trace_id()`
    on this thread.
    """

    def __init__(self, wrapped: IdGenerator) -> None:
        self._wrapped = wrapped
        self._preset = threading.local()

    def new_trace_id(self) -> int:
        return self._wrapped.generate_trace_id()

    def preset(self, trace_id: int | None) -> None:
        self._preset.trace_id = trace_id

    def generate_trace_id(self) -> int:
        trace_id = getattr(self._preset, "trace_id", None)
        if trace_id is None:
            return self._wrapped.generate_trace_id()
        self._preset.trace_id = None
        return trace_id

    def generate_span_id(self) -> int:
        return self._wrapped.generate_span_id()

    def is_trace_id_random(self) -> bool:
        return self._wrapped.is_trace_id_random()

