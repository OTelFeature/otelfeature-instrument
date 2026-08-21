"""Runs once per process, from `sitecustomize.py`, before the target
application's own code is imported - see cli.py for how that's arranged.
"""

from __future__ import annotations

from logging import getLogger

from opentelemetry.sdk.resources import Resource

from otelfeature_instrument.flags import configure_feature_flags
from otelfeature_instrument.metrics import install_meter_provider
from otelfeature_instrument.tracing import install_tracer_provider

_logger = getLogger(__name__)


def initialize(*, swallow_exceptions: bool = True) -> None:
    try:
        configure_feature_flags()

        resource = Resource.create()
        install_tracer_provider(resource)
        install_meter_provider(resource)

        # Reused rather than reimplemented: this is the same entry-point
        # discovery `opentelemetry-instrument` uses to zero-code-instrument
        # whatever instrumentation packages (fastapi, httpx, psycopg2, ...)
        # happen to be installed. It's a private module (leading underscore)
        # of `opentelemetry-instrumentation`, hence the exact version pin in
        # pyproject.toml.
        from opentelemetry.instrumentation.auto_instrumentation._load import (  # noqa: PLC0415
            _load_distro,
            _load_instrumentors,
        )

        distro = _load_distro()
        distro.configure()
        _load_instrumentors(distro)
    except Exception:
        _logger.exception("Failed to initialize otelfeature-instrument")
        if not swallow_exceptions:
            raise
