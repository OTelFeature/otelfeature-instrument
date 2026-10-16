from __future__ import annotations

from opentelemetry import metrics
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource

from otelfeature_instrument.error_rate import (
    ErrorRatePublisher,
    error_rate_reader,
    interval_from_environment,
)


def install_meter_provider(resource: Resource) -> None:
    # The error-rate reader is an extra, in-process reader with its own
    # (delta) temporality; the OTLP reader and what it exports are unchanged.
    error_reader = error_rate_reader()
    meter_provider = MeterProvider(
        resource=resource,
        metric_readers=[PeriodicExportingMetricReader(OTLPMetricExporter()), error_reader],
    )
    metrics.set_meter_provider(meter_provider)
    ErrorRatePublisher(error_reader, interval_from_environment()).start()
