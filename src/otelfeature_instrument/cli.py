"""The `otelfeature-instrument` console script.

Mirrors the mechanism `opentelemetry-instrument` itself uses (see
`opentelemetry.instrumentation.auto_instrumentation.run`): prepend this
package's own directory to `PYTHONPATH` - which is where `sitecustomize.py`
lives - then exec the target command. Python's `site` module auto-imports
`sitecustomize` at interpreter startup, so `bootstrap.initialize()` runs
before the target command's own code, with no code changes required in the
target application.

Unlike `opentelemetry-instrument`, this doesn't translate `OTEL_*` CLI flags
into environment variables - only the exec-with-PYTHONPATH mechanism is
reused. Configuration is environment-variable-only (`FLAGD_HOST`,
`OTEL_EXPORTER_OTLP_ENDPOINT`, `OTELFEATURE_FLAG_PROVIDER`, ...).
"""

from __future__ import annotations

import sys
from os import environ, execvp
from os.path import abspath, dirname, pathsep


def run() -> None:
    if len(sys.argv) < 2:
        print("usage: otelfeature-instrument <command> [args...]", file=sys.stderr)
        raise SystemExit(1)

    command, command_args = sys.argv[1], sys.argv[1:]

    filedir_path = dirname(abspath(__file__))
    python_path = environ.get("PYTHONPATH")
    python_path = python_path.split(pathsep) if python_path else []
    if filedir_path not in python_path:
        python_path.insert(0, filedir_path)
    environ["PYTHONPATH"] = pathsep.join(python_path)

    execvp(command, command_args)
