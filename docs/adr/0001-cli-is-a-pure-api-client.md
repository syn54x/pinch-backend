# pinch-cli is a pure HTTP client of the public API

The public CLI (`pinch`, distributed as `pinch-cli`) talks to a Pinch server
exclusively through the public developer API and never imports
`pinch_backend`. `pinch-backend` itself is never published to PyPI (enforced
via the `Private :: Do Not Upload` classifier); it may carry private developer
CLIs (`pinch-dev`). We chose this over letting the CLI share backend code
because it forces the developer API to stay complete: anything the CLI can
do, any user script can do. The rule is enforced by
`tests/test_cli_client.py::test_cli_never_imports_backend`.

## Consequences

- The CLI lives as a uv workspace member (`packages/pinch-cli`) so one PR can
  change an endpoint, the client, and the CLI together — but the boundary is
  HTTP, never imports.
- Backend and CLI versions bump in lockstep via commitizen `version_files`.
