# SPEC: claude-rotate

The design and constraints of claude-rotate. `README.md` is the contract for
users. `AGENTS.md` holds working conventions.

## 1. Purpose and architecture

claude-rotate is a self-hosted proxy that lets Claude Code use several Claude
subscription accounts. It forwards requests to `api.anthropic.com` and switches
accounts when one reaches its rate limit.

Tracked components:

- `rotator.py`: the proxy and rotation logic.
- `oai_compat.py`: the OpenAI-compatible endpoint.
- `setup.sh`: adds accounts and devices to `config.json`.
- `install.sh` and `uninstall.sh`: install and remove the service.
- `deploy/claude_rotate_admin.py`: the admin CLI.
- `deploy/systemd_runner.py`: the systemd entry point.
- `Dockerfile`: the container image.
- `phase0_proxy.py`: a standalone logging passthrough for checking header
  behaviour. It is not part of the service.

### 1.1 Non-goals

See "Limitations" in `README.md`. This file does not repeat it.

## 2. Interface

`README.md` is the user contract for keys, defaults and endpoints. See
"Configuration (`config.json`)", "Analytics panel", "OpenAI-compatible
endpoint" and "Operations". A change to any of them updates `README.md` first.

## 3. Data model

The Configuration table in `README.md` is authoritative for `config.json`.

The Anthropic quota headers (section 4, fact 3) are an external contract. This
repo cannot change them. Re-verify them with `phase0_proxy.py` after an
Anthropic API change.

## 4. Cross-cutting conventions

Design constraints:

- Keep one `rotator.py` until that genuinely hurts.
- Use only fastapi, httpx and uvicorn as dependencies.

Four facts about the proxy were each verified against the live API. Keep them
true when you change `rotator.py`.

1. **Transparent passthrough.** The proxy forwards every path and method to
   `api.anthropic.com` and replaces only the `Authorization` header. Claude
   Code sends its own headers, such as `anthropic-beta: oauth-2025-04-20`.
   Do not strip or reorder them.
2. **Encoding trick.** Forwarded requests force `accept-encoding: identity`,
   and the proxy drops the response's `content-encoding` header. Without both,
   httpx decompresses the body while the original header survives, and the
   client fails with Zlib or Brotli errors.
3. **Quota telemetry.** Quota state comes from the response headers
   `anthropic-ratelimit-unified-5h-utilization`, `-7d-utilization`,
   `-5h-reset` and `-status`. Max subscription traffic carries them. They are
   undocumented, so re-verify them after Anthropic API changes.
4. **SSE usage capture.** The proxy parses `data:` lines that contain `"usage"`
   while it streams chunks through untouched. It writes the audit record in
   the stream's `finally` block.

## 5. Testing strategy

`tests/run_all.py` runs every test file against a fixture config. `tests.yml`
runs it on Python 3.10, 3.12 and 3.14 for each pull request and each push to a
branch other than `main`, and `image.yml` runs it before it publishes an image. The fixture steps are in `AGENTS.md` under
"Build and test".

## 6. Acceptance criteria

The tests pass in CI on every push to `main`.
