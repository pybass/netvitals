set default-list := true

# Install/update the dev environment (all extras + all dependency groups) and activate git hooks.
[group('setup')]
sync:
    uv sync --all-extras --all-groups
    uv run pre-commit install

# Run all checks; must pass before a PR or release.
[group('check')]
check: pre-commit lint test audit build

# Code checks: ruff, ruff format --check, mypy. Never modifies source files.
[group('check')]
lint:
    uv run ruff check src tests
    uv run ruff format --check src tests
    uv run mypy src tests

# Apply every autofix: ruff lint fixes + formatting, then pre-commit hooks.
[group('fix')]
fix: && pre-commit
    uv run ruff check --fix src tests
    uv run ruff format src tests

# Run the test suite in parallel (pytest -n auto).
[group('check')]
test:
    uv run pytest -n auto

# Vulnerability scan of the installed environment (pip-audit); waive an unfixed CVE with --ignore-vuln <ID>.
[group('check')]
audit:
    uv run pip-audit

# Run all pre-commit hooks against every file.
[group('check')]
pre-commit:
    uv run pre-commit run --all-files

# Rebuild the wheel into dist/ (wheel-only, --no-sources).
[group('release')]
build:
    rm -rf dist
    uv build --wheel --no-sources

# Verify the repo is release-ready: clean tree, HEAD at origin/main tip, version not yet tagged.
[private]
release-preflight:
    #!/usr/bin/env bash
    set -euo pipefail
    if [[ -n "$(git status --porcelain)" ]]; then
        echo "error: working tree has uncommitted changes; commit them before publishing." >&2
        exit 1
    fi
    # Plain `uv run` in check would silently re-lock a stale uv.lock and publish it uncommitted.
    uv lock --check
    git fetch -q origin main
    # Exact tip, not just an ancestor: catches unpushed, stale, and feature-branch HEADs alike.
    if [[ "$(git rev-parse HEAD)" != "$(git rev-parse refs/remotes/origin/main)" ]]; then
        echo "error: HEAD is not at the tip of origin/main; switch to main and pull/push first." >&2
        exit 1
    fi
    version=$(uv version --short)
    # Origin is checked too: a remote-only tag would otherwise fail the push after the wheel is uploaded.
    if git rev-parse -q --verify "refs/tags/v${version}" >/dev/null || git ls-remote --exit-code --tags origin "refs/tags/v${version}" >/dev/null 2>&1; then
        echo "error: tag v${version} already exists; bump 'version' in pyproject.toml." >&2
        exit 1
    fi

# Run the release preflight and check, publish the wheel to PyPI, then tag & push the release.
[group('release')]
release: release-preflight check
    #!/usr/bin/env bash
    set -euo pipefail
    read -rsp "PyPI token: " UV_PUBLISH_TOKEN
    echo
    if [[ -z "${UV_PUBLISH_TOKEN}" ]]; then
        echo "error: no token entered." >&2
        exit 1
    fi
    export UV_PUBLISH_TOKEN
    uv publish dist/*.whl
    version=$(uv version --short)
    git tag -a "v${version}" -m "v${version}"
    git push origin "v${version}"

# Remove caches and build artifacts.
[group('maintenance')]
clean:
    rm -rf dist .pytest_cache .mypy_cache .ruff_cache
