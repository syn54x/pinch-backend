# pinch-backend

Pinch backend: Litestar API, Penny (pydantic-ai) agents, and a private developer CLI.

## Development

```bash
uv sync
uv run prek install
just check
prek run --all-files
```

## CLI

```bash
uv run pinch-dev
```

## API

```bash
uv run litestar --app pinch_backend.api.app:app run
```
