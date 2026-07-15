# pinch-backend

Pinch backend: Litestar API, Penny (pydantic-ai) agents, and a private developer CLI.

This distribution (`pinch-backend`) is **not published to PyPI** (enforced via the
`Private :: Do Not Upload` classifier). The published package is
[`pinch-cli`](packages/pinch-cli) — a pure HTTP client of the public API.

## Development

Development runs against a local Postgres (`docker run -d --name local-pg
-e POSTGRES_PASSWORD=password -p 5432:5432 postgres:17`); tests isolate
themselves in throwaway schemas.

```bash
uv sync
uv run prek install
uv run prek install --hook-type commit-msg
prek run --all-files   # full CI parity
just check             # fast lint + types
uv run pytest
```

## Release

```bash
just release           # local gate + trigger release workflow
just docs-deploy       # deploy docs without releasing
```

## Release setup

Configure in GitHub before the first release:

1. **Pages** — Settings → Pages → Source: GitHub Actions
2. **PyPI** — Trusted publisher for `release.yml` on environment `pypi`
3. **GitHub App** — `RELEASE_APP_ID` variable + `RELEASE_APP_PRIVATE_KEY` secret
4. **Environments** — `github-pages`, `pypi`

PyPI project URL (CLI): https://pypi.org/p/pinch-cli
