# otelfeature-instrument

A zero-code OpenTelemetry launcher, like `opentelemetry-instrument`, but with
one addition: **`INTERNAL` spans - or all tracing - can be suppressed live,
via a feature flag, with no application restart.**

```sh
otelfeature-instrument uvicorn main:app --host 0.0.0.0 --port 8000
```

Add it as a dependency, change your `CMD`/entrypoint to run through
`otelfeature-instrument` instead of your app directly (or instead of
`opentelemetry-instrument`), and everything else - resource detection, OTLP
export, zero-code instrumentation of installed libraries - works the same as
`opentelemetry-instrument`. The one difference: two
[OpenFeature](https://openfeature.dev/) flags decide which spans get created,
re-read live:

| Flag             | Value   | Effect on spans                                           |
|------------------|---------|-----------------------------------------------------------|
| `telemetryLevel` | `FULL`  | everything is traced (default)                            |
| `telemetryLevel` | `IO`    | `INTERNAL` spans suppressed; `SERVER`/`CLIENT`/`PRODUCER`/`CONSUMER` kept |
| `telemetryLevel` | `OFF`   | every span suppressed                                     |
| `otelEnabled`    | `true`  | no effect, `telemetryLevel` decides (default)             |
| `otelEnabled`    | `false` | every span suppressed - identical to `telemetryLevel=OFF` |
| `recordSpan`     | `false` | the span is dropped - per span when a targeting rule decides it (see [Dropping selected spans](#dropping-selected-spans-recordspan)) |
| `traceSampleRatio` | `0.0`-`1.0` | probability a new trace is sampled - a live `OTEL_TRACES_SAMPLER_ARG` (see [Sampling](#sampling-tracesampleratio)) |

`otelEnabled` is the plain on/off switch; `OFF` is the same thing for
operators who already manage `telemetryLevel`. Both **fail open**: a missing
flag, a provider error, an unknown `telemetryLevel` variant or a non-boolean
`otelEnabled` all fall back to what the process would do without
OTelFeature - `FULL`/`true` unless the environment says otherwise (see
[One policy, one read](#one-policy-one-read)). Both cover **traces only** -
metrics keep flowing either way, on purpose.

Suppressed spans' children reattach to the real parent instead of being
orphaned. See [How it works](#how-it-works) for why that's not simply a
`Sampler`.

## Why this exists

This started as a prototype directly inside one service (see
[poc-otelfeature](https://github.com/OTelFeature/poc)): most `INTERNAL`
spans are connective tissue between the actual I/O boundaries (`SERVER`,
`CLIENT`, `PRODUCER`, `CONSUMER`) a trace cares about - useful in detail, but
frequently just noise once a system is understood. This package makes that
verbosity control a reusable, install-and-go capability instead of
per-service boilerplate.

## Usage

```sh
uv add otelfeature-instrument
```

```dockerfile
CMD ["sh", "-c", "exec otelfeature-instrument uvicorn main:app --host 0.0.0.0 --port ${PORT}"]
```

No code changes required in the target application - the same zero-code
promise `opentelemetry-instrument` makes. If the application creates its own
manual spans (e.g. `tracer.start_as_current_span("some.internal.op",
kind=SpanKind.INTERNAL)`), those are covered too, not just spans created by
auto-instrumentation.

## How it works

### The launcher mechanism

Identical to `opentelemetry-instrument`: [cli.py](src/otelfeature_instrument/cli.py)
prepends this package's own directory to `PYTHONPATH` (where
[sitecustomize.py](src/otelfeature_instrument/sitecustomize.py) lives) and
`exec`s the target command. Python's `site` module auto-imports
`sitecustomize` at interpreter startup, so
[bootstrap.py](src/otelfeature_instrument/bootstrap.py)'s `initialize()` runs
*before* the target application's own code is ever imported. `initialize()`
then reuses `opentelemetry-instrumentation`'s own entry-point discovery
(`_load_distro`/`_load_instrumentors`) to zero-code-instrument whatever
instrumentation packages (`opentelemetry-instrumentation-fastapi`, etc.) are
installed - the exact same mechanism `opentelemetry-instrument` uses for
that part.

### Suppressing `INTERNAL` spans without orphaning children

A `Sampler`-based `DROP` decision happens too late: by the time a `Sampler`
runs, the span's context has already been minted and handed to its children
as `parent_span_id`. Dropping it after the fact leaves those children
pointing at a parent that was never exported - an orphan.

Instead, [tracing.py](src/otelfeature_instrument/tracing.py) wraps the real
`Tracer`: when verbosity is `IO` and a span's kind is `INTERNAL` (or, under
`OFF`/`otelEnabled=false`, for every kind), no span is created and context is
never advanced past the real parent (`start_span`/`start_as_current_span`
just hand back the current - or explicitly passed - parent unchanged).
Anything created afterward - however deep in the call stack - inherits that
same untouched parent, so it attaches directly to it instead of to the
(nonexistent) suppressed span. The decision
is made once, at the suppressed span's own creation, so it's stable for that
span's entire lifetime even if the flag flips mid-request.

Example - a request that would otherwise produce:

```
POST /geocode                    (SERVER)
├─ http receive / http send × N  (INTERNAL, from ASGI instrumentation)
└─ geocode.resolve                (INTERNAL, application code)
   └─ GET                         (CLIENT, httpx)
```

produces, with `telemetryLevel=IO`:

```
POST /geocode      (SERVER)
└─ GET              (CLIENT, httpx)
```

### `OFF` is suppression, not an SDK disable

OpenTelemetry's own kill switch, `OTEL_SDK_DISABLED`, is read once at process
start; `OFF` and `otelEnabled=false` flip at runtime instead, but they are
not the same thing:

- **Context propagation keeps working.** A suppressed span stands in for
  the incoming parent (as a non-recording span with the same context) and
  keeps that parent current, so an incoming `traceparent` is still
  forwarded downstream and a trace resumes coherently the moment the flag
  flips back. For an incident-response kill switch that is a feature - but
  it does leave gaps; see
  [`OFF` leaves gaps in traces](#off-leaves-gaps-in-traces).
- **Most of the CPU is saved too, in Python.** Suppression happens at span
  creation: no SDK span is created, recorded, processed or exported. (This
  differs from span-processor-based implementations, which still pay for
  creating the span before dropping it.) What remains is instrumentation
  glue - extracting headers, building attribute dicts that are written to a
  no-op span - plus the per-span flag lookups, which are answered from
  memory while the flags are static or absent.
- The SDK, its exporters and the instrumentation stay installed.

### The feature flag

[flags.py](src/otelfeature_instrument/flags.py) registers a
[flagd](https://flagd.dev/) OpenFeature provider in
["in-process" resolver mode](https://flagd.dev/reference/specifications/in-process-providers/):
the flag ruleset is synced from flagd once (and kept updated in the
background over a gRPC stream) and evaluated in memory - no network call on
the per-span hot path. Editing the flag in flagd takes effect on the very
next span, with no restart of the instrumented application.

### One policy, one read

Enforcement points never read a flag directly. `current_policy()` in
[flags.py](src/otelfeature_instrument/flags.py) resolves every flag into one
`TelemetryPolicy` value per decision point:

| Field               | From                                          | Fallback                          |
|---------------------|-----------------------------------------------|-----------------------------------|
| `enabled`           | `otelEnabled` and `telemetryLevel != OFF`      | `OTEL_SDK_DISABLED` is not `true` (a hard floor) |
| `suppress_internal` | `telemetryLevel == IO`                        | `false`                           |
| `record_span`       | `recordSpan` (only while `enabled`)           | `true`                            |
| `sample_ratio`      | `traceSampleRatio`, clamped to 0.0-1.0        | `OTEL_TRACES_SAMPLER` / `_ARG`, as the SDK reads them (`1.0` by default) |

**Every field falls back to its startup value**, resolved from the
environment once when the process starts. With flagd unreachable or a flag
undefined, behaviour is that of plain `opentelemetry-instrument`; flags are a
live override, never a replacement.

`OTEL_SDK_DISABLED=true` is the one exception, and a hard floor: flags never
enable what `OTEL_SDK_DISABLED` turned off. `enabled` stays `false` whatever
`otelEnabled` or `telemetryLevel` say, and no flag is evaluated at all.

Provider errors and missing flags are logged once when they start - a missing
flag at `INFO`, anything else at `WARNING` - and once when the flag becomes
readable again, never per span.

### Evaluating once, or evaluating always

`telemetryLevel` and `otelEnabled` are each asked about on every single span,
but most of the time the answer cannot have changed since the last one: a
flag with no `targeting` block is one `defaultVariant` for the whole world. flagd says so in the
resolution `reason`, and that reason is the entire mechanism:

| `reason`          | flagd produced it because                        | same for every span? |
|-------------------|--------------------------------------------------|----------------------|
| `STATIC`          | flag has no `targeting`, served `defaultVariant`  | yes                  |
| `TARGETING_MATCH` | `targeting` rules ran and picked a variant        | no                   |
| `DEFAULT`         | `targeting` rules ran and matched nothing         | no                   |
| `DISABLED`        | flag `state` is `DISABLED`, served our default    | not worth assuming   |
| `ERROR`           | flag missing, provider not ready, ...             | not yet knowable     |

So the flag is evaluated **once, from the provider's event handler, without
an evaluation context**, purely to read the reason back:

- `STATIC` is kept. Every subsequent span is answered from memory,
  evaluating nothing and running no hooks.
- So is an `ERROR` whose code is `FLAG_NOT_FOUND`: a deployment that never
  defined the flag gets the startup fallback from memory instead of a
  failing evaluation per span. Creating the flag later lists it in the
  configuration-changed event, which re-classifies.
- Anything else means the value can depend on who's asking, so every span
  evaluates for itself.

Each flag is classified independently; while every flag is cached, so is
the combined `TelemetryPolicy`, and a span costs one attribute read. That
classification is redone on every `PROVIDER_CONFIGURATION_CHANGED`, so
attaching a `targeting` block to `telemetryLevel` (or `otelEnabled`) in flagd
makes the next probe report `TARGETING_MATCH`, caching switches itself off
for that flag, and spans start being evaluated individually. Remove the
targeting and caching resumes by itself. No configuration, no restart, no
code change here.

The event handler is what makes caching safe in the first place: flagd's
in-process resolver pushes a changed ruleset over its gRPC sync stream, the
SDK emits the event, and the kept answer is replaced - which is why the
`rpc`/OFREP resolvers would not do. Handlers run on the SDK's own executor,
so for a brief moment after startup spans fall through to a direct
evaluation: the right answer, just not yet the cheap one.

### Dropping selected spans: `recordSpan`

`otelEnabled` and `telemetryLevel=OFF` drop everything. To drop only the
spans a rule matches - health checks, one noisy instrumentation - use the
boolean flag `recordSpan` (default `true`), evaluated against the
[evaluation context](#the-evaluation-context) of each span:

```json
"recordSpan": {
  "state": "ENABLED", "defaultVariant": "on",
  "variants": { "on": true, "off": false },
  "targeting": { "if": [ { "and": [
      { "==": [ { "var": "service.name" }, "geocode" ] },
      { "==": [ { "var": "url.path" }, "/health" ] } ] }, "off", null ] }
}
```

- **A non-root span** with `recordSpan=false` is suppressed like an `IO`
  span: never created, its children attach to the nearest kept ancestor.
- **A root span** (no valid parent, or an incoming request - see below)
  with `recordSpan=false` drops its
  **whole trace**: it gets a valid but *non-sampled* span context, so its
  children aren't sampled either and outgoing calls carry `traceparent`
  with `sampled=0` - downstream services drop that trace too, instead of
  each starting a new one (unlike `OFF`, see
  [Known limitations](#off-leaves-gaps-in-traces)). This relies on
  **parent-based sampling**, here and downstream: the SDK default
  (`parentbased_always_on`), and what `otelfeature-instrument` installs. A
  non-parent-based sampler (`OTEL_TRACES_SAMPLER=always_on` or
  `traceidratio`) would sample the children anyway, parented to a span that
  was never exported.
- **An incoming request counts as a root**, even with a remote parent: a
  `SERVER` or `CONSUMER` span whose `traceparent` came from another service
  drops the whole request here, exactly like a root - it gets a non-sampled
  context in the *caller's* trace, so nothing below it is recorded and
  outgoing calls carry `sampled=0`. A health check sent by a proxy or
  gateway that traces its probes therefore leaves no spans in this service.
  The trade-off: the trace ends at the caller - downstream services see
  `sampled=0` and record nothing for it either. `INTERNAL` and `CLIENT`
  spans with a remote parent are suppressed as children; their children
  attach to the remote parent.
- **Precedence:** `OTEL_SDK_DISABLED` (hard floor) > `otelEnabled` /
  `telemetryLevel=OFF` > `recordSpan`. `recordSpan=true` never re-enables
  anything the global switches turned off.
- **Fails open:** a missing flag, a provider error or a non-boolean value
  records the span.
- **Static costs nothing:** without targeting, `recordSpan` is cached like
  the other flags; with targeting it is evaluated per span.
- The key can be renamed with `OTELFEATURE_RECORD_SPAN_FLAG_NAME`.

`OTEL_TRACES_SAMPLER_ARG` is read once, when the SDK starts. "Turn sampling up
to 100% for this service for ten minutes" needs it live, so
`traceSampleRatio` (a float flag, `0.0`-`1.0`) backs the sampler instead:

- **Parent-based.** A span with a parent follows the parent's decision; only
  a root asks the flag. A trace is never half sampled, even if the flag
  flips while it is in flight.
- **Same rule as the Python SDK's `TraceIdRatioBased`:** the low 64 bits
  of the trace id, unsigned, against `ratio * 2^64`. Every Python service
  that applies the same ratio to the same trace makes the same call.
  **Not across languages:** the Java SDK (and so
  `otelfeature-java-extension`) compares the low 64 bits as a *signed*
  long against `ratio * Long.MAX_VALUE`, so at the same ratio Java and
  Python can disagree on a given trace id. With parent-based sampling this
  doesn't matter - only the entry service decides, everyone downstream
  follows its `traceparent`. Where several entry services in different
  languages must agree on the same traces, bucket with a flagd
  `fractional` rule on `targetingKey` (the trace id), which every flagd
  evaluator computes identically, and serve `0.0`/`1.0` per bucket.
- **`0.0` is `always_off`**, `1.0` is `always_on` - no separate flag needed.
- **Never throws on the hot path.** Out of range is clamped; a non-numeric
  or NaN value falls back to the startup value (logged once).
- **Startup fallback** is the ratio `OTEL_TRACES_SAMPLER`/`_ARG` amount to,
  parsed as the SDK parses them (`traceidratio` its argument, anything else
  `1.0`). The sampler itself is parent-based: with a non-parent-based
  `always_on` or `traceidratio`, a span with a sampled parent is sampled
  whatever the ratio - logged as a warning at startup.
- **`always_off` is a hard floor**, like `OTEL_SDK_DISABLED`: flags never
  enable what the env var switched off. With `OTEL_TRACES_SAMPLER=always_off`
  nothing is sampled - not by `traceSampleRatio`, not via a sampled upstream
  parent. With `parentbased_always_off` no trace starts sampled in this
  service, whatever the flag says; spans with a parent follow it, as the
  SDK's sampler of that name does. Custom samplers (entry points such
  as `jaeger_remote`) are not loaded by `otelfeature-instrument` - an
  unknown name falls back to `1.0`, as the plain SDK provider would.
- **Only roots evaluate it.** A span with a parent never asks
  `traceSampleRatio`, so targeting on the ratio costs one evaluation per
  trace in each service, not one per span.
- **Targetable** with the [evaluation context](#the-evaluation-context): a
  ratio per `service.name` or `deployment.environment.name`, or a
  `fractional` rollout on `targetingKey` - the trace id, so a trace lands in
  the same bucket in every service.

> **Flip it at the entry point.** Sampling decisions do not apply
> retroactively or mid-trace: with parent-based sampling, the service that
> starts the trace decides, and every downstream service follows the
> decision it receives in `traceparent`. Raising `traceSampleRatio` on a
> downstream service changes almost nothing - only traces that *start*
> there. Target the edge service (or use the same rule everywhere).

**Compared with `jaeger_remote`.** OpenTelemetry already has a remotely
controlled sampler, `jaeger_remote`, which polls a Jaeger-compatible
sampling endpoint for per-service and per-operation strategies. It is the
right choice if you run that infrastructure. `traceSampleRatio` reuses the
flag infrastructure (flagd, OpenFeature) the organisation already operates
for everything else, and gets OpenFeature targeting with it: ratios by any
key in the evaluation context, percentage rollouts, and the same audit trail
and tooling as every other flag. (OpenTelemetry Python ships no
`jaeger_remote` sampler, and `otelfeature-instrument` doesn't load sampler
entry points; the comparison is with the other SDKs and the Collector.)

### The evaluation context

When a flag carries `targeting`, each span start is described to it in
OpenTelemetry semantic-convention terms - so a targeting rule is written
against the same names as the telemetry itself:

| Source                         | Keys |
|--------------------------------|------|
| Resource (once per process)    | `service.name`, `service.namespace`, `service.version`, `deployment.environment.name`, `telemetry.sdk.language` |
| Instrumentation scope          | `otel.scope.name`, `otel.scope.version` |
| Span at start                  | `span.kind` (lower-case: `server`, `client`, `internal`, `producer`, `consumer`), `span.name`, plus every attribute the span is started with - typically `url.path`, `http.request.method`, `server.address`, `db.system.name`, `db.operation.name`, `rpc.service` |
| `targetingKey`                 | the **trace id** (32 hex digits), so `fractional` buckets a whole trace the same way in every service |
| Live, per process              | `otelfeature.http.server.error_rate` - see [Error rate](#error-rate-in-the-context) |

```json
"targeting": {
  "if": [
    { "and": [
      { "==": [ { "var": "service.name" }, "weather" ] },
      { "==": [ { "var": "span.kind" }, "server" ] },
      { "==": [ { "var": "url.path" }, "/health" ] }
    ] },
    "off", "full"
  ]
}
```

- **Keys are nested by dot segment.** `service.name` is sent as
  `{"service": {"name": ...}}`, because JsonLogic's `var` splits on `.` and a
  flat dotted key never matches in any flagd evaluator
  ([OTelFeature/poc#22](https://github.com/OTelFeature/poc/issues/22)). Rules
  keep using the semconv name verbatim.
- **A name that is both a leaf and a namespace** (a deprecated/replacement
  pair such as `db.system` and `db.system.name`): the namespace wins and the
  leaf is dropped, logged at `DEBUG`. Target the current name.
- **Only what exists when the span starts.** `http.route` and
  `http.response.status_code` are *not* available: the instrumentation sets
  them later. Target `url.path` instead of the route.
- **Extension keys win over start attributes.** A start attribute named
  like a resource key or one of the keys above (`span.kind`,
  `otel.scope.name`, ...) - or one that would turn such a key into a
  namespace, like `span.kind.detail` - is dropped, logged at `DEBUG`.
- **Root spans keep the trace id they were evaluated on.** For a span with no
  parent, the trace id is generated before the flags are asked and handed to
  the SDK, so `targetingKey` and the exported trace id are the same. This
  needs the OpenTelemetry SDK's `TracerProvider` (which
  `otelfeature-instrument` always installs); wrapped around any other
  provider, roots are evaluated without a `targetingKey`, and `fractional`
  then puts every root in the same bucket.
- **Built only off the static path.** While every flag is static, no context
  is built at all.

Rough per-span cost (one `SERVER` span with two attributes and one `INTERNAL`
child, no export; Python 3.12, a laptop Intel CPU - orders of magnitude, not
a benchmark; reproduce with `uv run python benchmarks/span_cost.py`):

| Setup                                         | per span |
|-----------------------------------------------|----------|
| plain SDK tracer                              | ~21 µs   |
| OTelFeature, flags static (cached policy)     | ~26 µs   |
| OTelFeature, `telemetryLevel` targeted (FILE/in-process flagd) | ~75-105 µs |

Targeting costs one in-process flag evaluation per span, so it belongs on a
flag while it is being used, not left on permanently.

### Error rate in the context

`otelfeature.http.server.error_rate` (a number, 0.0-1.0) is the service's
HTTP server error rate over the last collection interval, so a flagd rule
can close the loop on its own - no outside script, no new metric:

```json
"traceSampleRatio": {
  "state": "ENABLED", "defaultVariant": "low",
  "variants": { "low": 0.1, "all": 1.0 },
  "targeting": { "if": [
    { ">": [ { "var": "otelfeature.http.server.error_rate" }, 0.05 ] }, "all", "low"
  ] }
}
```

- **Input:** the `http.server.request.duration` histogram the HTTP server
  instrumentation already records - not spans, because a sampled-out span
  never records a status, while metrics are recorded whatever the sampling
  does. Turning sampling down does not blind the loop.
- **How:** an extra in-process reader with *delta* temporality sits next to
  the OTLP reader and is collected every `OTELFEATURE_ERROR_RATE_INTERVAL`
  seconds (default `10`). Each collection is its own window:
  `requests with error.type / all requests`. No smoothing. The OTLP reader,
  its temporality and what it exports are unchanged.
- **No requests in the interval:** the key is left out of the context (a
  rule comparing it then sees `null`, which is not `> 0.05`).
- **Per service:** each process measures and publishes its own rate, so a
  rule raises sampling for the service whose errors went up.
- **Lags by one interval:** the rate a rule sees describes the *previous*
  interval.

> **Set `OTEL_SEMCONV_STABILITY_OPT_IN=http`.** The Python HTTP
> instrumentations only emit `http.server.request.duration` (with
> `error.type`) with the stable HTTP semantic conventions opted in.
> Without it they emit the older `http.server.duration`, which has no
> `error.type`; the rate then falls back to counting
> `http.status_code >= 500`, which misses errors that never produced a
> response. With `http/dup` both metrics are emitted; only the stable one
> is counted. `otelfeature-instrument` does not set the opt-in itself,
> because it also renames the metrics and attributes your dashboards see.

## Configuration

| Environment variable         | Default    | Description                                                    |
|-------------------------------|------------|------------------------------------------------------------------|
| `OTEL_SERVICE_NAME`           | -          | Service name reported in telemetry                              |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | -          | OTLP/HTTP collector endpoint for traces and metrics              |
| `FLAGD_HOST`                  | `localhost`| flagd host                                                       |
| `FLAGD_PORT`                  | `8015`     | flagd gRPC sync port (in-process resolver)                       |
| `OTELFEATURE_RECORD_SPAN_FLAG_NAME` | `recordSpan` | flagd key of the selective-drop flag |
| `OTELFEATURE_FLAG_PROVIDER`   | `flagd`    | `flagd` to auto-register a flagd provider, `none` to manage OpenFeature yourself (see [flags.py](src/otelfeature_instrument/flags.py)) |
| `OTEL_SDK_DISABLED`           | `false`    | Standard OTel switch; `true` is a hard floor for `TelemetryPolicy.enabled` that no flag overrides |
| `OTELFEATURE_ERROR_RATE_INTERVAL` | `10`  | Seconds between error-rate collections (see [Error rate](#error-rate-in-the-context)) |
| `OTEL_SEMCONV_STABILITY_OPT_IN` | -       | Set to `http` so the HTTP instrumentation emits `http.server.request.duration` with `error.type` |
| `OTEL_TRACES_SAMPLER` / `OTEL_TRACES_SAMPLER_ARG` | `parentbased_always_on` | Fallback ratio for `traceSampleRatio`; `always_off` / `parentbased_always_off` are hard floors no flag overrides; the sampler is parent-based and custom samplers are not loaded |

The `telemetryLevel` (variants `FULL`/`IO`/`OFF`) and `otelEnabled` (boolean)
flags should exist in whatever flagd instance `FLAGD_HOST`/`FLAGD_PORT` point
at - a missing one simply falls back to its startup value. See
[poc-otelfeature/src/flagd](https://github.com/OTelFeature/poc/tree/main/src/flagd)
for an example flag definition.

## Known limitations

### `OFF` leaves gaps in traces

`OFF` and `otelEnabled=false` are kept for completeness, but they are known
to produce incomplete traces. While tracing is suppressed, the tracer hands
back the span that is already current instead of creating one. What
downstream services see depends on whether a request arrived with a parent:

- **Request arrived with a `traceparent`:** the incoming context is still
  forwarded. Downstream spans join the same trace but attach directly to
  the upstream caller's span, so this service is a gap in the middle of
  the trace.
- **Request arrived without one** (this service is the entry point): there
  is no current span, so no context is propagated. Every traced downstream
  service starts its own root trace for each call, and one request shows up
  as several unrelated traces.
- **Flag flipped back on mid-request:** spans created after the flip
  attach to whatever the suppressed span stood in for - the upstream caller,
  or nothing. Without an incoming parent, every such span becomes its own
  root trace.
- **Flag flipped off mid-request:** spans already started finish and are
  exported, but their children from after the flip are missing.

In short: while `OFF`, this service's part of a trace is missing, and
without an incoming parent the trace is also split downstream.

### Other limitations

- `bootstrap.py` reaches into `opentelemetry.instrumentation.auto_instrumentation._load`,
  a private module of `opentelemetry-instrumentation` - hence the exact
  version pin in [pyproject.toml](pyproject.toml). A future
  `opentelemetry-instrumentation` release could change or remove it.
- The flag keys (`telemetryLevel`, `otelEnabled`, `traceSampleRatio`) and
  variants (`FULL`/`IO`/`OFF`) are hardcoded for now, not yet configurable
  per consuming application.
- Only traces and metrics are bootstrapped (matching
  [poc-otelfeature](https://github.com/OTelFeature/poc)'s current scope) -
  no log correlation setup.
