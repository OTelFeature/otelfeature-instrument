# otelfeature-instrument

A zero-code OpenTelemetry launcher, like `opentelemetry-instrument`, but with
one addition: **`INTERNAL` spans can be suppressed live, via a feature flag,
with no application restart.**

```sh
otelfeature-instrument uvicorn main:app --host 0.0.0.0 --port 8000
```

Add it as a dependency, change your `CMD`/entrypoint to run through
`otelfeature-instrument` instead of your app directly (or instead of
`opentelemetry-instrument`), and everything else - resource detection, OTLP
export, zero-code instrumentation of installed libraries - works the same as
`opentelemetry-instrument`. The one difference: any span of kind `INTERNAL`
is suppressed while the `telemetryLevel` [OpenFeature](https://openfeature.dev/)
flag evaluates to `IO`, with its children reattaching to the real parent
instead of being orphaned. See [How it works](#how-it-works) for why that's
not simply a `Sampler`.

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
`Tracer`: when verbosity is `IO` and a span's kind is `INTERNAL`, no span is
created and context is never advanced past the real parent
(`start_span`/`start_as_current_span` just hand back
`trace.get_current_span()` unchanged). Anything created afterward - however
deep in the call stack - inherits that same untouched parent, so it attaches
directly to it instead of to the (nonexistent) suppressed span. The decision
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

### The feature flag

[flags.py](src/otelfeature_instrument/flags.py) registers a
[flagd](https://flagd.dev/) OpenFeature provider in
["in-process" resolver mode](https://flagd.dev/reference/specifications/in-process-providers/):
the flag ruleset is synced from flagd once (and kept updated in the
background over a gRPC stream) and evaluated in memory - no network call on
the per-span hot path. Editing the flag in flagd takes effect on the very
next span, with no restart of the instrumented application.

### Evaluating once, or evaluating always

`telemetryLevel` is asked about on every single span, but most of the time
the answer cannot have changed since the last one: a flag with no `targeting`
block is one `defaultVariant` for the whole world. flagd says so in the
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

- `STATIC` - and only `STATIC` - is kept. Every subsequent span is answered
  from memory, evaluating nothing and running no hooks.
- Anything else means the value can depend on who's asking, so every span
  evaluates for itself.

That classification is redone on every `PROVIDER_CONFIGURATION_CHANGED`, so
attaching a `targeting` block to `telemetryLevel` in flagd makes the next
probe report `TARGETING_MATCH`, caching switches itself off, and spans start
being evaluated individually. Remove the targeting and caching resumes by
itself. No configuration, no restart, no code change here.

The event handler is what makes caching safe in the first place: flagd's
in-process resolver pushes a changed ruleset over its gRPC sync stream, the
SDK emits the event, and the kept answer is replaced - which is why the
`rpc`/OFREP resolvers would not do. Handlers run on the SDK's own executor,
so for a brief moment after startup spans fall through to a direct
evaluation: the right answer, just not yet the cheap one.

## Configuration

| Environment variable         | Default    | Description                                                    |
|-------------------------------|------------|------------------------------------------------------------------|
| `OTEL_SERVICE_NAME`           | -          | Service name reported in telemetry                              |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | -          | OTLP/HTTP collector endpoint for traces and metrics              |
| `FLAGD_HOST`                  | `localhost`| flagd host                                                       |
| `FLAGD_PORT`                  | `8015`     | flagd gRPC sync port (in-process resolver)                       |
| `OTELFEATURE_FLAG_PROVIDER`   | `flagd`    | `flagd` to auto-register a flagd provider, `none` to manage OpenFeature yourself (see [flags.py](src/otelfeature_instrument/flags.py)) |

The `telemetryLevel` flag itself (variants `FULL`/`IO`) needs to exist in
whatever flagd instance `FLAGD_HOST`/`FLAGD_PORT` point at - see
[poc-otelfeature/src/flagd](https://github.com/OTelFeature/poc/tree/main/src/flagd)
for an example flag definition.

## Known limitations

- `bootstrap.py` reaches into `opentelemetry.instrumentation.auto_instrumentation._load`,
  a private module of `opentelemetry-instrumentation` - hence the exact
  version pin in [pyproject.toml](pyproject.toml). A future
  `opentelemetry-instrumentation` release could change or remove it.
- The flag key (`telemetryLevel`) and its variants (`FULL`/`IO`) are
  hardcoded for now, not yet configurable per consuming application.
- Only traces and metrics are bootstrapped (matching
  [poc-otelfeature](https://github.com/OTelFeature/poc)'s current scope) -
  no log correlation setup.
