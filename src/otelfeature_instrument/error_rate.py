"""The service's HTTP server error rate, as an evaluation-context key.

The input is the metric the HTTP instrumentation already records,
`http.server.request.duration`, not spans: a sampled-out span never records
a status, while a metric is recorded whatever the sampling does. An extra
in-process reader with *delta* temporality sits next to the OTLP one, so
each collection covers exactly the interval since the last - no window
bookkeeping, and nothing changes for what the OTLP reader exports.

Every `OTELFEATURE_ERROR_RATE_INTERVAL` seconds:

    error_rate = requests with error.type / all requests

published as `otelfeature.http.server.error_rate` (absent while there were
no requests). See "Error rate" in the README for the opt-in it relies on.
"""

from __future__ import annotations

import os
import threading
from logging import getLogger
from typing import Any, Mapping

from opentelemetry.sdk.metrics import Histogram
from opentelemetry.sdk.metrics.export import (
    AggregationTemporality,
    HistogramDataPoint,
    InMemoryMetricReader,
    MetricsData,
)

from otelfeature_instrument.evaluation_context import set_live_attribute

_logger = getLogger(__name__)

ERROR_RATE_KEY = "otelfeature.http.server.error_rate"
INTERVAL_ENV_VAR = "OTELFEATURE_ERROR_RATE_INTERVAL"
DEFAULT_INTERVAL = 10.0

# Stable HTTP semconv first; the pre-stable name is what the Python HTTP
# instrumentations emit without OTEL_SEMCONV_STABILITY_OPT_IN=http.
_METRICS = ("http.server.request.duration", "http.server.duration")
_STATUS_KEYS = ("http.response.status_code", "http.status_code")


def error_rate_reader() -> InMemoryMetricReader:
    """The reader to add to the MeterProvider: delta, so a collection is a window."""
    return InMemoryMetricReader(preferred_temporality={Histogram: AggregationTemporality.DELTA})


def is_error(attributes: Mapping[str, Any]) -> bool:
    if attributes.get("error.type"):
        return True
    # Fallback for points without error.type (the pre-stable metric).
    for key in _STATUS_KEYS:
        status = attributes.get(key)
        if status is not None:
            try:
                return int(status) >= 500
            except (TypeError, ValueError):
                return False
    return False


def error_rate(data: MetricsData | None) -> float | None:
    """Σcount(error points) / Σcount(all points) over the HTTP server duration
    histogram; `None` when there were no requests.

    With OTEL_SEMCONV_STABILITY_OPT_IN=http/dup both metrics are recorded for
    the same requests, so only the first name in `_METRICS` that has data
    counts.
    """
    counts = {name: [0, 0] for name in _METRICS}  # name -> [errors, total]
    for resource_metrics in data.resource_metrics if data else ():
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                if metric.name not in counts:
                    continue
                tally = counts[metric.name]
                for point in metric.data.data_points:
                    if not isinstance(point, HistogramDataPoint):
                        continue
                    tally[1] += point.count
                    if is_error(point.attributes or {}):
                        tally[0] += point.count
    for errors, total in counts.values():
        if total:
            return errors / total
    return None


class ErrorRatePublisher:
    """Collects the reader on a timer and publishes the rate into the context."""

    def __init__(self, reader: InMemoryMetricReader, interval: float) -> None:
        self._reader = reader
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def collect_once(self) -> float | None:
        try:
            rate = error_rate(self._reader.get_metrics_data())
        except Exception:  # noqa: BLE001 - never let the loop die
            _logger.exception("otelfeature-instrument: error-rate collection failed")
            rate = None
        set_live_attribute(ERROR_RATE_KEY, rate)
        return rate

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name="otelfeature-error-rate", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            self.collect_once()


def interval_from_environment(environ: Mapping[str, str] = os.environ) -> float:
    raw = environ.get(INTERVAL_ENV_VAR, "")
    if not raw.strip():
        return DEFAULT_INTERVAL
    try:
        interval = float(raw)
    except ValueError:
        interval = 0.0
    if interval > 0:
        return interval
    _logger.warning(
        "otelfeature-instrument: %s=%r is not a positive number of seconds - using %s",
        INTERVAL_ENV_VAR,
        raw,
        DEFAULT_INTERVAL,
    )
    return DEFAULT_INTERVAL
