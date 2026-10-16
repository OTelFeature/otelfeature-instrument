# syntax=docker/dockerfile:1
#
# Toolchain image for otelfeature-instrument: Python + uv + every locked
# dependency, nothing else. `make test` / `make shell` and CI
# (.github/workflows/ci.yml) all run in this image with the repository
# mounted at /workspace, so local and CI runs cannot drift.
#
# Base image pinned by tag and digest; Dependabot (docker ecosystem) bumps
# both. Python 3.12 matches what the project is developed and tested with
# (`requires-python = ">=3.10"`).
FROM ghcr.io/astral-sh/uv:0.9.30-python3.12-bookworm-slim@sha256:e5b65587bce7de595f299855d7385fe7fca39b8a74baa261ba1b7147afa78e58

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH=/opt/venv/bin:$PATH

WORKDIR /workspace

# The dependency layer is keyed on the lock files only: editing sources never
# invalidates it. The project itself is not installed here - tests import it
# from the mounted src/ (PYTHONPATH), so a run needs no network.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --all-extras --dev --no-install-project

ENV PYTHONPATH=/workspace/src
