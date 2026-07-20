set default-list := true


# ============================================================================
#  Environment
# ============================================================================

# Install/update the dev environment (all extras + all dependency groups) and activate git hooks.
[group('env')]
sync:
    uv sync --all-extras --all-groups
    uv run --no-sync pre-commit install

# Bring .venv to the exact locked state — the single gate every env-consuming recipe depends on.
# `uv sync` is exact: anything installed ad hoc (`uv pip install`) is removed here, so every
# recipe behaves identically in a fresh clone and a long-lived working copy.
[private]
env: lock-check
    uv sync --locked --all-extras --all-groups


# ============================================================================
#  Checks
# ============================================================================

# Canonical full gate - run before a PR or release.
[group('check')]
check: pre-commit lint test audit build

# Verify uv.lock is in sync with pyproject.toml; fails instead of silently re-locking.
[group('check')]
lock-check:
    uv lock --check

# Code checks: ruff, ruff format --check, mypy. Never modifies files.
[group('check')]
lint: env
    uv run --no-sync ruff check src tests
    uv run --no-sync ruff format --check src tests
    uv run --no-sync mypy src tests

# Run the test suite in parallel (pytest -n auto).
[group('check')]
test: env
    uv run --no-sync pytest -n auto

# Run tests with branch coverage; prints uncovered lines.
[group('check')]
coverage: env
    uv run --no-sync pytest -n auto --cov --cov-report=term-missing

# Code-level security checks live in `just lint` (ruff S rules), not here.
# Dependency gate: vulnerability scan (pip-audit) + imports vs declared deps (deptry).
[group('check')]
audit: env
    #!/usr/bin/env bash
    set -euo pipefail
    req=$(mktemp -t audit-requirements.XXXXXX)
    trap 'rm -f "$req"' EXIT
    uv export --locked --no-dev --all-extras --no-emit-project --no-emit-workspace --format requirements-txt > "$req"
    uv run --no-sync pip-audit -r "$req" --disable-pip --require-hashes
    uv run --no-sync deptry src

# Run all pre-commit hooks against every file.
[group('check')]
pre-commit: env
    uv run --no-sync pre-commit run --all-files


# ============================================================================
#  Fix
# ============================================================================

# Auto-fix lint issues and reformat src/ and tests/ (ruff).
[group('fix')]
format: env
    uv run --no-sync ruff check --fix src tests
    uv run --no-sync ruff format src tests

# Apply every autofix: ruff lint fixes + formatting, then pre-commit hooks.
[group('fix')]
fix: format pre-commit


# ============================================================================
#  Release
# ============================================================================

# Remove stale build artifacts so a release can't pick up an old distribution.
[private]
clean-dist:
    rm -rf dist

# Deliberately depends on `clean-dist`, not `clean`: wiping lint/type caches would make every post-`check` run cold.
# Build the wheel and source distribution into dist/ without uv-specific sources (PEP 517-clean, per uv's publishing guide).
[group('release')]
build: clean-dist
    uv build --no-sources

# Verify the repo is release-ready: clean tree, HEAD at origin/main tip, version not yet tagged.
[group('release')]
release-preflight:
    #!/usr/bin/env bash
    set -euo pipefail
    # The wheel is built from the working tree, not a commit — a dirty tree would publish code that exists nowhere in git.
    if [[ -n "$(git status --porcelain)" ]]; then
        echo "error: working tree has uncommitted changes; commit them before publishing." >&2
        exit 1
    fi
    # --tags brings remote tags local, so the single tag check below covers both local and origin.
    git fetch -q --tags origin main
    # Exact tip of pushed main: an unpushed, stale, or feature-branch HEAD would tag code that public main doesn't have.
    if [[ "$(git rev-parse HEAD)" != "$(git rev-parse refs/remotes/origin/main)" ]]; then
        echo "error: HEAD is not exactly at origin/main; pull or push first." >&2
        exit 1
    fi
    version=$(uv version --short)
    if git rev-parse -q --verify "refs/tags/v${version}" >/dev/null; then
        echo "error: tag v${version} already exists; bump 'version' in pyproject.toml." >&2
        exit 1
    fi

# Run check, publish the distributions to PyPI, then tag & push the release.
[group('release')]
release: release-preflight check
    #!/usr/bin/env bash
    set -euo pipefail
    read -rsp "PyPI token: " UV_PUBLISH_TOKEN
    echo
    export UV_PUBLISH_TOKEN
    uv publish --publish-url https://upload.pypi.org/legacy/ --check-url https://pypi.org/simple/ dist/*
    version=$(uv version --short)
    git tag -a "v${version}" -m "v${version}"
    git push origin "v${version}"


# ============================================================================
#  Maintenance
# ============================================================================

# Remove caches and build artifacts.
[group('maintenance')]
clean: clean-dist
    rm -rf .pytest_cache .mypy_cache .ruff_cache .coverage* htmlcov
