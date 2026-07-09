# pinch-cli

The `pinch` command: a pure HTTP client for the Pinch developer API.

This package never imports `pinch_backend`. It talks to a Pinch server —
hosted or self-hosted — exclusively through the public API, authenticated
with a personal access token. If the CLI can do it, any script can.

## Configuration

| Environment variable | Meaning |
|---|---|
| `PINCH_SERVER_URL` | Base URL of the Pinch server |
| `PINCH_API_TOKEN` | Personal access token |

## Usage

```bash
pinch health   # check connectivity to the configured server
```
