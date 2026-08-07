set dotenv-load := true
# Recipes get the local file; the app itself resolves its own via PINCH_ENV
# (settings.py) — prod work exports PINCH_ENV=prod and reads .env.prod.
set dotenv-filename := ".env.local"

default:
    @just --list

claude *args:
    claude --add-dir ~/github/pinch-finance/pinch --add-dir ~/github/pinch-finance/pinch-frontend {{args}}

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
# two processes: `just api` and `just worker` — three with Plaid
# configured, since webhooks need the machine reachable: `just tunnel`.
# Run the Procrastinate worker.
worker:
    uv run python -m pinch_backend.cli.app worker

# Tunnel Plaid's doorbells to the local API (ADR 0008: webhooks are
# required, and ngrok is the documented dev path). The domain derives
# from PINCH_PLAID_WEBHOOK_URL so .env stays the single source of truth
# — no second place for the tunnel name to rot.
tunnel:
    #!/usr/bin/env sh
    set -eu
    url="${PINCH_PLAID_WEBHOOK_URL:-}"
    if [ -z "$url" ]; then
        echo "PINCH_PLAID_WEBHOOK_URL is not set (.env) — nothing to tunnel to" >&2
        exit 1
    fi
    domain="${url#*://}"
    domain="${domain%%/*}"
    exec ngrok http --url="$domain" 8000

# Probe Plaid's /item/get for a connection (or all): products, pull
# status, standing error — the PRODUCT_NOT_READY diagnostic.
plaid-item connection_id="":
    uv run python -m pinch_backend.cli.app plaid-item {{connection_id}}

# One reconcile pass now (ADR 0008): registers pre-webhook Items and
# heals rotated tunnel URLs — the deploy-day retrofit. Syncs it enqueues
# run in the worker.
reconcile:
    uv run python -m pinch_backend.cli.app reconcile

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
