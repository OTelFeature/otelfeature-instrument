"""Rough per-span cost: plain SDK vs OTelFeature static vs targeted (real flagd FILE).

    uv run python benchmarks/span_cost.py

Orders of magnitude, not a benchmark suite - the numbers in the README come
from one run of this on a laptop.
"""
import json
import tempfile
import time
import timeit
from pathlib import Path

from openfeature import api as feature_api
from openfeature.contrib.provider.flagd import FlagdProvider
from openfeature.contrib.provider.flagd.config import ResolverType
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider

from otelfeature_instrument import flags
from otelfeature_instrument.tracing import _VerbosityAwareTracerProvider

RES = Resource.create({"service.name": "weather"})
N = 20000


def run(tracer, label):
    attrs = {"url.path": "/weather", "http.request.method": "GET"}

    def one():
        with tracer.start_as_current_span("GET /weather", kind=trace.SpanKind.SERVER, attributes=attrs):
            tracer.start_span("internal").end()

    one()
    best = min(timeit.repeat(one, number=N, repeat=5)) / N / 2 * 1e6
    print(f"{label:<40} {best:6.2f} us/span")


run(TracerProvider(resource=RES).get_tracer("t"), "plain SDK tracer")

tmp = Path(tempfile.mkdtemp(prefix="otelfeature-bench-"))


def provider(ruleset):
    p = tmp / f"f{time.time_ns()}.json"
    p.write_text(json.dumps(ruleset))
    feature_api.set_provider_and_wait(FlagdProvider(resolver_type=ResolverType.FILE, offline_flag_source_path=str(p)))


def ruleset(targeting=None):
    flag = {"state": "ENABLED", "defaultVariant": "full", "variants": {"full": "FULL", "io": "IO", "off": "OFF"}}
    if targeting:
        flag["targeting"] = targeting
    return {"flags": {"telemetryLevel": flag, "otelEnabled": {"state": "ENABLED", "defaultVariant": "on", "variants": {"on": True, "off": False}}}}


provider(ruleset())
flags._policy_source = flags._PolicySource()
flags._policy_source.classify()
assert flags._policy_source.cached is not None
run(_VerbosityAwareTracerProvider(TracerProvider(resource=RES)).get_tracer("t"), "otelfeature, static flags (cached)")

provider(ruleset({"if": [{"==": [{"var": "url.path"}, "/health"]}, "off", "full"]}))
flags._policy_source = flags._PolicySource()
flags._policy_source.classify()
assert flags._policy_source.cached is None
run(_VerbosityAwareTracerProvider(TracerProvider(resource=RES)).get_tracer("t"), "otelfeature, telemetryLevel targeted")
feature_api.clear_providers()
