"""Feature-flag wiring backing the `telemetryLevel` verbosity check.

Registering a flagd provider is the default, but it's gated by
`OTELFEATURE_FLAG_PROVIDER` rather than unconditional: this module runs from
`sitecustomize.py`, before the target application's own code is ever
imported, so there's no way to detect "did the app already configure
OpenFeature itself" - that code hasn't run yet. Setting
`OTELFEATURE_FLAG_PROVIDER=none` is how a consuming app opts out and takes
over OpenFeature configuration on its own terms (at any point - `_verbosity`
below always reads the *current* global provider, not a snapshot taken at
startup).
"""

from __future__ import annotations

import os

from openfeature import api as feature_api
from openfeature.contrib.provider.flagd import FlagdProvider
from openfeature.contrib.provider.flagd.config import ResolverType

FLAG_PROVIDER_ENV_VAR = "OTELFEATURE_FLAG_PROVIDER"
FLAG_PROVIDER_FLAGD = "flagd"
FLAG_PROVIDER_NONE = "none"

TRACE_VERBOSITY_FLAG_KEY = "telemetryLevel"
VERBOSITY_FULL = "FULL"
VERBOSITY_IO = "IO"


def configure_feature_flags() -> None:
    provider = os.environ.get(FLAG_PROVIDER_ENV_VAR, FLAG_PROVIDER_FLAGD).strip().lower()
    if provider == FLAG_PROVIDER_NONE:
        return
    if provider != FLAG_PROVIDER_FLAGD:
        raise ValueError(
            f"Unknown {FLAG_PROVIDER_ENV_VAR}={provider!r}; expected "
            f"{FLAG_PROVIDER_FLAGD!r} or {FLAG_PROVIDER_NONE!r}"
        )
    # in-process is a correctness requirement, not a deployment choice: it's
    # what makes _current_verbosity() a per-span-safe, no-network-call read.
    feature_api.set_provider(FlagdProvider(resolver_type=ResolverType.IN_PROCESS))


def current_verbosity() -> str:
    # Evaluated fresh on every span - see the module docstring for why this
    # always reads the live global provider rather than something cached at
    # configure_feature_flags() time.
    client = feature_api.get_client()
    return client.get_string_value(TRACE_VERBOSITY_FLAG_KEY, VERBOSITY_FULL).strip().upper()
