"""`OTEL_*_EXPORTER=none`: the one exporter value this launcher honours.

It always exports over OTLP/HTTP and doesn't select exporters; it only lets
`none` switch a signal's export off, parsed the way the SDK parses it
(`value.lower().strip() == "none"`).
"""

from __future__ import annotations

import os
from logging import getLogger
from typing import Mapping

_logger = getLogger(__name__)


def exporter_disabled(env_var: str, environ: Mapping[str, str] = os.environ) -> bool:
    value = environ.get(env_var, "").lower().strip()
    if value == "none":
        return True
    if "none" in {name.strip() for name in value.split(",")}:
        # The SDK rejects this; Java autoconfigure treats it as an error.
        _logger.warning(
            "otelfeature-instrument: %s=%r mixes 'none' with exporters - "
            "export stays on; set exactly 'none' to switch it off",
            env_var,
            value,
        )
    return False
