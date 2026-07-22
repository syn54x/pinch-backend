set dotenv-load := true

default:
    @just --list

setup:
    uv sync
    uv run prek install
    uv run prek install --hook-type commit-msg

check:
    uv run ruff check .
    uv run ruff format --check .
    uv run ty check src

fix:
    uv run ruff check --fix src tests
    uv run ruff format src tests

test *args:
    uv run pytest {{args}}

# Extra args pass through (`just api --reload`, `just api --port 8100`);
# litestar defaults to :8000, the port the frontend's dev server expects.
# Run the API server against the developer .env.
api *args:
    uv run litestar --app pinch_backend.api.app:app run {{args}}

# Syncs and classification are background jobs, so a full dev stack is
# two processes: `just api` and `just worker`.
# Run the Procrastinate worker.
worker:
    uv run python -m pinch_backend.cli.app worker

# Export the OpenAPI document for typed-client generation (frontend repo:
# point openapi-typescript / @hey-api/openapi-ts at the output, or at a
# running server's /api/v1/schema/openapi.json). No database needed.
openapi out="openapi.json":
    uv run litestar --app pinch_backend.api.app:app schema openapi --output {{out}}

docs-cli:
    uv run python scripts/gen_cli_docs.py

docs-serve:
    uv run zensical serve

docs-deploy:
    gh workflow run docs.yml --ref main

release-smoke:
    rm -rf dist/
    uv build
    uv build --package pinch-cli
    uv run --with dist/pinch_backend-*.whl --no-project -- pinch-dev --help
    uv run --with dist/pinch_backend-*.whl --no-project -- python -c "import pinch_backend; print(pinch_backend.__version__)"
    uv run --with dist/pinch_cli-*.whl --no-project -- pinch --help
    uv run --with dist/pinch_cli-*.whl --no-project -- python -c "import pinch_cli; print(pinch_cli.__version__)"

release:
    #!/usr/bin/env bash
    set -euo pipefail
    branch="$(git rev-parse --abbrev-ref HEAD)"
    if [[ "${branch}" != "main" ]]; then
      echo "error: checkout main before releasing (on ${branch})" >&2
      exit 1
    fi
    if [[ -n "$(git status --porcelain)" ]]; then
      echo "error: uncommitted changes; commit or stash before releasing" >&2
      exit 1
    fi
    git fetch origin main
    if [[ "$(git rev-parse HEAD)" != "$(git rev-parse origin/main)" ]]; then
      echo "error: main is not synced with origin/main; push or pull first" >&2
      exit 1
    fi
    uv run prek run --all-files
    just release-smoke
    gh workflow run release.yml --ref main
    echo "Triggered Release workflow on main."
    echo "Watch: gh run watch --workflow release.yml"
