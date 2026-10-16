# Everything runs in the toolchain image (ci/Dockerfile); the only host
# requirement is Docker. CI uses the same targets - see .github/workflows/ci.yml.

IMAGE ?= otelfeature-instrument-ci
DOCKER_RUN = docker run --rm -v "$(CURDIR)":/workspace -w /workspace
# --no-sync: dependencies come from the image, the project from src/.
# no:cacheprovider: don't leave a root-owned .pytest_cache in the checkout.
TEST_CMD = uv run --frozen --no-sync pytest -q -p no:cacheprovider

.PHONY: image test test-only shell dist check-version

image:
	docker build -f ci/Dockerfile -t $(IMAGE) .

test: image
	$(MAKE) test-only

# Run the tests in an already built image (CI builds it with layer caching).
test-only:
	$(DOCKER_RUN) $(IMAGE) $(TEST_CMD)

shell: image
	$(DOCKER_RUN) -it $(IMAGE) bash

# Wheel and sdist into dist/ (what the release workflow attaches).
dist: image
	$(DOCKER_RUN) $(IMAGE) uv build --out-dir dist

# Fail unless TAG (e.g. v0.3.0) matches the version in pyproject.toml.
check-version: image
	$(DOCKER_RUN) $(IMAGE) python ci/check_tag_version.py "$(TAG)"
