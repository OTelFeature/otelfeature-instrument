# otelfeature-instrument

A zero-code OpenTelemetry launcher, like `opentelemetry-instrument`, with one
addition: [OpenFeature](https://openfeature.dev/) flags decide, live and
without a restart, which spans get created and which traces are sampled.

```sh
otelfeature-instrument uvicorn main:app --host 0.0.0.0 --port 8000
```

Everything else works as with `opentelemetry-instrument`: resource
detection, OTLP export, zero-code instrumentation of the libraries whose
`opentelemetry-instrumentation-*` packages are installed. No code changes in the application. Manual spans the application
creates are covered too.

> From the KubeCon + CloudNativeCon North America 2026 talk
> **OpenFeature as a Control Plane for OpenTelemetry** by Simon Schrottner
> ([Flagsmith](https://www.flagsmith.com/?utm_source=github&utm_medium=readme&utm_campaign=otelfeature&utm_content=otelfeature-instrument)) and Juliano Costa
> ([Dash0](https://www.dash0.com/?utm_source=github&utm_medium=readme&utm_campaign=otelfeature&utm_content=otelfeature-instrument)). The talk page has how to
> reach us, and the slides and recording once they're out:
> **[otelfeature.github.io/presentation](https://otelfeature.github.io/presentation/)**

## Usage

There is no PyPI package yet. Install the wheel of a
[GitHub release](https://github.com/OTelFeature/otelfeature-instrument/releases):

```sh
pip install https://github.com/OTelFeature/otelfeature-instrument/releases/download/v0.4.0/otelfeature_instrument-0.4.0-py3-none-any.whl
```

or depend on its git tag with uv, which writes both the dependency and the
`[tool.uv.sources]` entry (a git source needs `git` in the build image):

```sh
uv add git+https://github.com/OTelFeature/otelfeature-instrument --tag v0.4.0
```

Also install the instrumentation packages for your libraries, e.g.
`opentelemetry-instrumentation-fastapi`, at the version of the pinned
`opentelemetry-instrumentation` (`0.66b1` in v0.4.0).

Then run the application through the launcher instead of directly (or
instead of `opentelemetry-instrument`):

```dockerfile
CMD ["sh", "-c", "exec otelfeature-instrument uvicorn main:app --host 0.0.0.0 --port ${PORT}"]
```

## Flags

| Flag               | Value         | Effect on spans                                           |
|--------------------|---------------|-----------------------------------------------------------|
| `telemetryLevel`   | `FULL`        | everything is traced (default)                            |
| `telemetryLevel`   | `IO`          | `INTERNAL` spans suppressed; `SERVER`/`CLIENT`/`PRODUCER`/`CONSUMER` kept |
| `telemetryLevel`   | `OFF`         | every span suppressed                                     |
| `otelEnabled`      | `true`/`false`| `false` is the same as `telemetryLevel=OFF` (default `true`) |
| `recordSpan`       | `true`/`false`| `false` drops the span a targeting rule matches (see [below](#dropping-selected-spans-recordspan)) |
| `traceSampleRatio` | `0.0`-`1.0`   | probability a new trace is sampled, a live `OTEL_TRACES_SAMPLER_ARG` (see [Sampling](#sampling-tracesampleratio)) |

All flags **fail open**: a missing flag, a provider error, an unknown
`telemetryLevel` value or a value of the wrong type falls back to what the
process would do without OTelFeature (see
[One policy, one read](#one-policy-one-read)). The flags cover **traces
only**. Metrics keep flowing either way.

An example flag definition is in
[poc/src/flagd](https://github.com/OTelFeature/poc/tree/main/src/flagd).

## How it works

[cli.py](src/otelfeature_instrument/cli.py) works like
`opentelemetry-instrument`: it prepends this package's directory to
`PYTHONPATH` and `exec`s the command. Python imports
[sitecustomize.py](src/otelfeature_instrument/sitecustomize.py) at startup,
so [bootstrap.py](src/otelfeature_instrument/bootstrap.py) runs before the
application's code, and it instruments the installed libraries through
`opentelemetry-instrumentation`'s own entry-point discovery.

[tracing.py](src/otelfeature_instrument/tracing.py) wraps the real
`Tracer`. A suppressed span is never created: `start_span` and
`start_as_current_span` hand back the current (or explicitly passed) parent.
Its children therefore attach to the real parent and are not orphaned. A
`Sampler` can't do this: by the time it runs, the span's context already
exists, and dropping the span leaves its children pointing at a parent that
is never exported. The decision is made once, when the span starts, so a
flag flip mid-request does not change it. With `telemetryLevel=IO`, a
`POST /geocode` (`SERVER`) span with `INTERNAL` children and an httpx `GET`
(`CLIENT`) below them is exported as just `POST /geocode` with `GET` as its
direct child.

`OFF` and `otelEnabled=false` suppress spans; they don't disable the SDK the
way `OTEL_SDK_DISABLED` does at startup. Context propagation keeps working
(see [Known limitations](#off-leaves-gaps-in-traces)), and the SDK, its
exporters and the instrumentation stay installed. No SDK span is created,
recorded or exported, so most of the CPU cost goes away. What remains is
instrumentation glue and the per-span flag lookups.

Flags come from a [flagd](https://flagd.dev/) provider in
[in-process mode](https://flagd.dev/reference/specifications/in-process-providers/):
the ruleset is synced over a gRPC stream and evaluated in memory, with no
network call per span. A flag change applies to the next span.

### One policy, one read

[flags.py](src/otelfeature_instrument/flags.py) resolves all flags into one
`TelemetryPolicy` per decision (`enabled`, `suppress_internal`,
`record_span`, `sample_ratio`). Each field falls back to its startup value,
read from the environment once. With flagd unreachable or a flag undefined,
the behaviour is that of plain `opentelemetry-instrument`.

Two env vars are **hard floors** that no flag overrides:
`OTEL_SDK_DISABLED=true` (no flag is evaluated at all) and
`OTEL_TRACES_SAMPLER=always_off`. Precedence: hard floors >
`otelEnabled`/`telemetryLevel=OFF` > `recordSpan` > `traceSampleRatio`. The
field table is in the spec:
[policy field resolution](https://github.com/OTelFeature/poc/blob/main/docs/spec/entity_model.md#policy-field-resolution)
and [hard floors](https://github.com/OTelFeature/poc/blob/main/docs/spec/entity_model.md#hard-floors).

Provider errors and missing flags are logged once when they start (a missing
flag at `INFO`, anything else at `WARNING`) and once when the flag is
readable again, never per span.

### Dropping selected spans: `recordSpan`

`recordSpan` (default `true`) drops only the spans a targeting rule matches,
for example `service.name == "geocode"` and `url.path == "/health"` serving
the `false` variant.

- **A non-root span** is suppressed like an `IO` span. Its children attach
  to the nearest kept ancestor.
- **A root span drops its whole trace.** It gets a valid, non-sampled
  context, so its children aren't sampled and outgoing calls carry
  `sampled=0`. Downstream services drop the trace too.
- **An incoming request counts as a root.** A `SERVER` or `CONSUMER` span
  with a remote parent drops the request here and sends `sampled=0`
  downstream, so the trace ends at the caller.
- Downstream services need a **parent-based sampler** (the SDK default).
  A downstream service with `always_on` or `traceidratio` samples the
  children anyway, parented to a span that is never exported. This launcher
  always applies its sampler parent-based.
- `recordSpan=true` never re-enables what `OFF` or `otelEnabled=false`
  turned off. The key can be renamed with `OTELFEATURE_RECORD_SPAN_FLAG_NAME`.

### Sampling: `traceSampleRatio`

`traceSampleRatio` (a float, `0.0`-`1.0`) is the live version of
`OTEL_TRACES_SAMPLER_ARG`:

- **Parent-based.** Only a root asks the flag; a span with a parent follows
  the parent. A trace is never half sampled.
- `0.0` samples nothing and `1.0` everything. A value above 1.0 is clamped
  to 1.0. A negative value, NaN, infinity or a non-number falls back to the
  startup ratio, logged once.
- **Startup ratio** comes from `OTEL_TRACES_SAMPLER`/`_ARG`, parsed as the
  SDK parses them. `always_off` is a hard floor. With
  `parentbased_always_off` no trace starts sampled here, but sampled parents
  are followed. With a non-parent-based `always_on` or `traceidratio`, a
  span with a sampled parent is sampled whatever the ratio (a warning is
  logged at startup). Custom samplers such as `jaeger_remote` are not
  loaded; an unknown name falls back to `1.0`, also with a warning.
- **Targetable** with the [evaluation context](#the-evaluation-context),
  e.g. a ratio per `service.name`. It costs one evaluation per trace, not
  per span.

> **Flip it at the entry point.** With parent-based sampling, the service
> that starts the trace decides, and every downstream service follows the
> `traceparent` it receives. Raising the ratio on a downstream service only
> affects traces that start there.

**Java and Python can disagree at the same ratio.** This package uses the
Python SDK's `TraceIdRatioBased` rule (low 64 bits of the trace id,
unsigned, against `ratio * 2^64`). The Java SDK, and so
`otelfeature-java-extension`, compares them signed against
`ratio * Long.MAX_VALUE`. Python services with the same ratio agree on a
trace id. With parent-based sampling a cross-language difference doesn't
matter, because only the entry service decides. Where entry services in
different languages must agree, use a flagd `fractional` rule on
`targetingKey` (the trace id) and serve `0.0`/`1.0` per bucket: every flagd
evaluator buckets the same way. Worked example:
[ratio-parity.md](https://github.com/OTelFeature/poc/blob/main/docs/spec/ratio-parity.md).

**Compared with `jaeger_remote`.** OpenTelemetry's `jaeger_remote` sampler
polls a Jaeger-compatible endpoint for sampling strategies and is the right
choice if you run that infrastructure. `traceSampleRatio` reuses the flag
infrastructure you already run (flagd, OpenFeature), with its targeting,
percentage rollouts, audit trail and tooling. OpenTelemetry Python ships no
`jaeger_remote` sampler, so the comparison is with the other SDKs and the
Collector.

### The evaluation context

When a flag has `targeting`, each span start is described in OpenTelemetry
semantic-convention terms:

| Source                         | Keys |
|--------------------------------|------|
| Resource                       | `service.name`, `service.namespace`, `service.version`, `deployment.environment.name`, `telemetry.sdk.language` |
| Instrumentation scope          | `otel.scope.name`, `otel.scope.version` |
| Span at start                  | `span.kind` (lower case: `server`, `client`, ...), `span.name`, and every attribute the span starts with, typically `url.path`, `http.request.method`, `server.address`, `db.system.name`, `db.operation.name`, `rpc.service` |
| `targetingKey`                 | the **trace id** (32 hex digits) |
| Per process                    | `otelfeature.http.server.error_rate`, see [Error rate](#error-rate-in-the-context) |

- **Only what exists when the span starts.** `http.route` and
  `http.response.status_code` are not available, because the
  instrumentation sets them later. Target `url.path` instead of the route.
- When a start attribute clashes with a key above, the key wins. When
  `db.system` and `db.system.name` both exist, the namespace wins.
  Attributes in the reserved `otelfeature.*` namespace are dropped.
- For a root span the trace id is generated before evaluation and kept by
  the span, so `targetingKey` matches the exported trace id. This needs the
  SDK's `TracerProvider`, which the launcher installs.

The full contract is in the spec:
[evaluation-context contract](https://github.com/OTelFeature/poc/blob/main/docs/spec/entity_model.md#evaluation-context-contract).

### Decision cost

A flag without `targeting` (flagd reason `STATIC`) or a missing flag is
evaluated once and answered from memory until the ruleset changes. A flag
with targeting is evaluated per span. Adding or removing targeting switches
between the two, with no restart. With `OTELFEATURE_FLAG_PROVIDER=none`, your
provider must emit `PROVIDER_CONFIGURATION_CHANGED` when flags change, or
static flags keep their first value.

Rough per-span cost, from `uv run python benchmarks/span_cost.py` (one
`SERVER` span with two attributes and one `INTERNAL` child, no export;
Python 3.12, a laptop Intel CPU; orders of magnitude, not a benchmark):

| Setup                                         | per span |
|-----------------------------------------------|----------|
| plain SDK tracer                              | ~21 µs   |
| OTelFeature, flags static (cached policy)     | ~26 µs   |
| OTelFeature, `telemetryLevel` targeted (FILE/in-process flagd) | ~75-105 µs |

Use targeting while you need it, not permanently.

### Muting an instrumentation, once per tracer

To mute one instrumentation, target its scope:

```json
"telemetryLevel": {
  "state": "ENABLED", "defaultVariant": "full",
  "variants": { "full": "FULL", "off": "OFF" },
  "targeting": { "if": [
    { "==": [ { "var": "otel.scope.name" }, "opentelemetry.instrumentation.httpx" ] }, "off", "full"
  ] }
}
```

A rule that reads only resource and scope keys (`service.*`,
`deployment.environment.name`, `telemetry.sdk.language`, `otel.scope.*`),
and only ones the tracer has, is evaluated **once per tracer** and kept
until the next ruleset change. The muted instrumentation's spans are then
never created, at no per-span cost. The same holds for `traceSampleRatio`
by `service.name`. Per-span evaluation stays for any other key (span keys,
`targetingKey`, also via `fractional` without a bucketing key,
`otelfeature.*`, `$flagd.timestamp`, computed paths), for providers other
than flagd in-process or file, and when anything else adds to the
evaluation context (a global context, a transaction context propagator,
API-level hooks).

This is scope filtering, not `OTEL_INSTRUMENTATION_*_ENABLED`: the
instrumentation stays installed and runs its hooks; only its spans are not
started.

The scope name is the instrumentation's module, e.g.
`opentelemetry.instrumentation.fastapi`. Exported spans show it as their
instrumentation scope. DEBUG logging for `otelfeature_instrument` logs every
scope a tracer is created for:

```python
logging.basicConfig()
logging.getLogger("otelfeature_instrument").setLevel(logging.DEBUG)
# otelfeature-instrument: tracer for otel.scope.name='opentelemetry.instrumentation.fastapi' ...
```

### Error rate in the context

`otelfeature.http.server.error_rate` (0.0-1.0) is the service's HTTP server
error rate over the last collection interval. A `traceSampleRatio` rule
such as `{ ">": [ { "var": "otelfeature.http.server.error_rate" }, 0.05 ] }`
can raise sampling when errors go up.

- It is computed from the `http.server.request.duration` metric, not from
  spans, so lower sampling doesn't hide errors: requests with `error.type` /
  all requests, every `OTELFEATURE_ERROR_RATE_INTERVAL` seconds (default
  `10`), no smoothing. OTLP export is unchanged.
- **It lags.** A rule sees the previous interval, so it reacts one to two
  intervals after errors start.
- **Minimum requests.** Below `OTELFEATURE_ERROR_RATE_MIN_REQUESTS` (default
  `10`) requests in an interval, the key is left out and a rule on it sees
  `null`. A service with fewer requests than that (under 1 req/s at the
  defaults) never gets a rate, even in a total outage. For quiet services,
  lower the minimum or lengthen the interval.
- Each process publishes its own rate. With `OTEL_METRICS_EXPORTER=none`
  there is no rate. The key is only in the contexts the extension builds for
  span decisions, not in the application's own evaluations.

> **Set `OTEL_SEMCONV_STABILITY_OPT_IN=http`.** Only then do the Python HTTP
> instrumentations emit `http.server.request.duration` with `error.type`.
> Without it the rate counts `http.status_code >= 500` from the older
> `http.server.duration`. With `http/dup` only the stable metric is counted.
> An unhandled exception in a bare ASGI app has neither and is not counted;
> FastAPI/Starlette turn it into a `500` first. The launcher doesn't set the
> opt-in, because it renames metrics and attributes your dashboards use.

### Flag evaluations as span events (opt-in)

The extension writes nothing about its decisions onto spans. For debugging
a rule, `OTELFEATURE_OPENFEATURE_TRACING_HOOK=true` (install
`otelfeature-instrument[tracing-hook]`) records the extension's own
evaluations through the
[OpenFeature OpenTelemetry hook](https://github.com/open-feature/python-sdk-contrib/tree/main/hooks/openfeature-hooks-opentelemetry)
as `feature_flag.evaluation` events. It is off by default: every
non-cached evaluation adds an event. The events land on the span current at
evaluation time, i.e. the **parent** of the span being decided; decisions
for new roots, including every `traceSampleRatio` decision, record nothing.
Evaluation errors are not recorded as exceptions.

## Configuration

| Environment variable         | Default    | Description                                                    |
|-------------------------------|------------|------------------------------------------------------------------|
| `OTEL_SERVICE_NAME`           | -          | Service name reported in telemetry                              |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | -          | OTLP/HTTP collector endpoint for traces and metrics              |
| `FLAGD_HOST`                  | `localhost`| flagd host                                                       |
| `FLAGD_PORT`                  | `8015`     | flagd gRPC sync port (in-process resolver)                       |
| `OTELFEATURE_FLAG_PROVIDER`   | `flagd`    | `none` to manage OpenFeature yourself (see [flags.py](src/otelfeature_instrument/flags.py)) |
| `OTELFEATURE_RECORD_SPAN_FLAG_NAME` | `recordSpan` | Key of the selective-drop flag |
| `OTELFEATURE_ERROR_RATE_INTERVAL` | `10`  | Seconds between error-rate collections |
| `OTELFEATURE_ERROR_RATE_MIN_REQUESTS` | `10` | Minimum requests per interval before an error rate is published |
| `OTELFEATURE_OPENFEATURE_TRACING_HOOK` | `false` | `true` records the extension's own evaluations as span events (needs the `tracing-hook` extra) |
| `OTEL_SDK_DISABLED`           | `false`    | `true` is a hard floor no flag overrides |
| `OTEL_TRACES_SAMPLER` / `OTEL_TRACES_SAMPLER_ARG` | `parentbased_always_on` | Startup ratio for `traceSampleRatio`; `always_off` is a hard floor (see [Sampling](#sampling-tracesampleratio)) |
| `OTEL_TRACES_EXPORTER`        | `otlp`     | `none` exports no spans; spans are still started and recorded, and `traceparent` still flows. Any other value exports via OTLP/HTTP |
| `OTEL_METRICS_EXPORTER`       | `otlp`     | `none` installs no meter provider, and so no error rate. Any other value exports via OTLP/HTTP |
| `OTEL_SEMCONV_STABILITY_OPT_IN` | -       | Set to `http` for the error rate (see [above](#error-rate-in-the-context)) |

Which `OTEL_*` env vars can be live flags, and why the others can't,
is in the spec:
[`OTEL_*` env vars and live flags](https://github.com/OTelFeature/poc/blob/main/docs/spec/entity_model.md#otel_-env-vars-and-live-flags).

## Development

Everything runs in the toolchain image built from
[`ci/Dockerfile`](ci/Dockerfile). The only host requirement is Docker (on
Windows: run `make` in WSL). CI runs the same image.

```sh
make test    # build the image (cached), run the test suite in it
make shell   # an interactive shell in the image, with the checkout mounted
```

- The checkout is mounted and imported from `src/`, so edits need no
  rebuild. The project itself isn't installed, so the
  `otelfeature-instrument` command isn't available in `make shell`.
- The container runs as root; commands in `make shell` can leave root-owned
  files in the checkout.
- Only Python 3.12 is tested, although `requires-python` is `>=3.10`.

## Releases

After the version bump is merged, a maintainer pushes `v<version>-rc.1`
(a pre-release), checks it, then pushes `v<version>`.
[`release.yml`](.github/workflows/release.yml) checks the tag against
`version` in `pyproject.toml` and `__version__`, runs the tests, and
attaches the wheel and sdist to a GitHub Release. Check the generated notes
of a final release: GitHub may compute them against the rc. Locally,
`make dist` builds into `dist/` and `make check-version TAG=v0.4.0` runs the
tag check.

## Known limitations

### `OFF` leaves gaps in traces

While `OFF` or `otelEnabled=false`, trace context keeps propagating: the
tracer hands back the current span instead of creating one. Services that
still trace can therefore produce traces with gaps or missing parents:

- **Request with a `traceparent`:** downstream spans join the trace but
  attach to the upstream caller's span, so this service is a gap.
- **Request without one** (this service is the entry point): no context is
  propagated, so each traced downstream call starts its own trace.
- **Flag flipped mid-request:** after a flip back on, new spans attach to
  whatever the suppressed span stood in for (without an incoming parent,
  each becomes its own trace). After a flip off, spans already started are
  exported but their later children are missing.

### Other limitations

- `bootstrap.py` uses a private module of `opentelemetry-instrumentation`
  (`opentelemetry.instrumentation.auto_instrumentation._load`), hence the
  exact version pin in [pyproject.toml](pyproject.toml).
- The flag keys `telemetryLevel`, `otelEnabled` and `traceSampleRatio` and
  the variants `FULL`/`IO`/`OFF` are not configurable.
- Only traces and metrics are set up; no logs.

## Talk to us

This is a prototype with open questions, and we'd like to hear how it
fits your setup:

- [Open an issue](https://github.com/OTelFeature/otelfeature-instrument/issues/new)
- CNCF Slack ([invite](https://slack.cncf.io)): `#openfeature` and `#opentelemetry`
- The speakers' websites: [Simon Schrottner](https://schrottner.at) and
  [Juliano Costa](https://jcosta.dev)
