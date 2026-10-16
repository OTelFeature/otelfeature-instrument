from __future__ import annotations

import os
from logging import getLogger
from typing import Mapping

from opentelemetry import metrics
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource

from otelfeature_instrument.error_rate import (
    ErrorRatePublisher,
    error_rate_reader,
    interval_from_environment,
    min_requests_from_environment,
)
from otelfeature_instrument.exporters import exporter_disabled

_logger = getLogger(__name__)

METRICS_EXPORTER_ENV_VAR = "OTEL_METRICS_EXPORTER"


def metrics_disabled(environ: Mapping[str, str] = os.environ) -> bool:
    """`OTEL_METRICS_EXPORTER=none` switches metrics off (see exporters.py)."""
    return exporter_disabled(METRICS_EXPORTER_ENV_VAR, environ)


def install_meter_provider(resource: Resource) -> None:
    if metrics_disabled():
        # Metrics off means no metrics SDK at all - and so no error-rate
        # reader: `otelfeature.http.server.error_rate` stays absent and rules
        # on it never match (same as the Java extension).
        _logger.info(
            "otelfeature-instrument: %s=none - no meter provider, no error rate",
            METRICS_EXPORTER_ENV_VAR,
        )
        return
    # The error-rate reader is an extra, in-process reader with its own
    # (delta) temporality; the OTLP reader and what it exports are unchanged.
    error_reader = error_rate_reader()
    meter_provider = MeterProvider(
        resource=resource,
        metric_readers=[PeriodicExportingMetricReader(OTLPMetricExporter()), error_reader],
    )
    metrics.set_meter_provider(meter_provider)
    ErrorRatePublisher(
        error_reader, interval_from_environment(), min_requests_from_environment()
    ).start()
