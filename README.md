# claude-rotate

![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)
![License: MIT](https://img.shields.io/badge/license-MIT-green)
[![tests](https://github.com/mrglrlgrlgrl/claude-rotate/actions/workflows/tests.yml/badge.svg)](https://github.com/mrglrlgrlgrl/claude-rotate/actions/workflows/tests.yml)
![Herd-safe](https://img.shields.io/badge/herd--safe-100%20racers%20%E2%86%92%201%20switch-orange)
![Dependencies](https://img.shields.io/badge/deps-fastapi%20%7C%20httpx%20%7C%20uvicorn-lightgrey)

A tiny self-hosted proxy that lets **Claude Code** ride multiple Claude
Max/Pro subscriptions and switches accounts automatically when one hits its
rate-limit window, with a built-in analytics panel showing quota gauges,
per-device consumption, and cost-equivalent dollars.

> **Fork lineage:** this repository is a fork of
> [doxaras/claude-rotate](https://github.com/doxaras/claude-rotate), the
> original, and includes the changes of
> [clement-z/claude-rotate](https://github.com/clement-z/claude-rotate) by
> Clément Zrounba up to `56ef19e`.
> See [Fork changes](#fork-changes) for what each fork added.

One process, one config file, no database. Built for individual developers who
own more than one subscription and are tired of "You've hit your session
limit".

Before deployment, read [Security and Tailscale setup](SECURITY.md) for credential
handling, private access, request limits, and the shared-device trust model.

**Herd-safe, and tested:** when your whole fleet slams into a rate
limit at the same instant, exactly *one* rotation decision happens: 100
in-flight requests, 1 switch, and every request recovers with a single retry
onto the new account. Reproducible numbers in
[Performance](#performance-herd-safe).

> **Terms-of-Service note.** This tool automates switching between accounts
> *you personally own and pay for*. Each account's limits are still fully
> enforced by Anthropic. Rotating accounts to work past limits is a gray area
> under Anthropic's consumer terms, use at your own risk, and do **not** use
> consumer subscriptions to back a shared/commercial service.

**Contents:**
[How it works](#how-it-works) ·
[Performance](#performance-herd-safe) ·
[Quickstart: server](#quickstart-server) ·
[Generating tokens](#generating-account-tokens-claude-setup-token) ·
[Quickstart: devices](#quickstart-each-device) ·
[Networking](#connecting-distributed-devices) ·
[Analytics panel](#analytics-panel) ·
[Configuration](#configuration-configjson) ·
[Run as a service](#run-as-a-service) ·
[Troubleshooting](#troubleshooting) ·
[Limitations](#limitations) ·
[Operations](#operations) ·
[Comparison](#comparison-with-similar-projects) ·
[Security](#security-notes) ·
[Fork changes](#fork-changes) ·
[License](#license)

## How it works

![claude-rotate architecture: devices authenticate with device keys, the proxy swaps in the active account's setup-token, forwards to api.anthropic.com, reads exact quota telemetry back, and rotates consume-first](screenshots/architecture.png)

Per request the proxy: authenticates the device (device key → device name),
swaps in the active account's long-lived setup-token, forwards transparently,
reads the exact `anthropic-ratelimit-unified-*` quota headers off the
response, applies the rotation policy (consume-first; quota 429 → rotate,
burst 429 → pace, all spent → hold), and logs usage per device/model.
Diagram regenerates with `python3 screenshots/architecture.py`
(needs `pip install diagrams` + graphviz).

Claude Code respects `ANTHROPIC_BASE_URL`, and `claude
setup-token` mints a ~1-year OAuth token per account. The proxy holds those
tokens; devices only ever hold an internal *device key*. Every response from
Anthropic carries exact quota telemetry headers, so switching at 80% is
measured, not guessed.

## Performance (herd-safe)

The point of a *server* proxy is that a whole fleet leans on it at once, so
high-concurrency behavior is tested, not assumed. All numbers from the
reproducible stress suite (`python3 tests/test_stress.py`, mocked upstream,
Apple-silicon dev machine, run it on yours):

| Scenario | Result |
|---|---|
| 300 concurrent requests | every one served, audit complete, **~3,500 req/s** proxy overhead ceiling |
| Quota limit hit with **100 requests in flight** (50 ms simulated upstream RTT) | **exactly 1 switch**: the in-flight herd hits the dying account once each (they were already on the wire), then every request retries **once** onto the survivor; all 100 recover |
| 50 burst-429s across 100 concurrent requests | **0 rotations**: paced on the same account, prompt caches kept warm |
| All accounts spent, 20 requests arrive | all park via hold-until-reset and release **together** at the window reset |
| 50 parallel SSE streams | byte-identical relay, usage audited on every stream |
| 50,000-row audit log | analytics rollup in **0.42 s** |

Under load, the failure mode of naive rotators
is the *switch* stampede: N in-flight requests each trigger their own switch,
the account pool burns down in seconds, and every device's prompt cache is
thrown away N times. Here a single lock serializes the decision: one switch,
everyone else rides it. To be precise about what the lock can and cannot do:
requests already on the wire when the quota hits still reach the dying account
once each (nothing can un-send them), but there is exactly one switch and
exactly one retry per request, and both counts are asserted in the test.

Scope: single process by design (state lives in one place; don't run
uvicorn workers), and the upstream is mocked (the race test adds 50 ms of
simulated RTT), so figures measure the proxy's own overhead and race behavior,
not Anthropic latency.

## Quickstart (server)

Requires Python 3.10+ and three packages: `fastapi`, `httpx`, `uvicorn`
(pinned in `requirements.txt`). Any always-on box works: a Mac mini, a
home-lab Linux machine, a small VPS.

```bash
git clone <this repo> && cd claude-rotate
pip install -r requirements.txt
./setup.sh                      # creates config.json, tokens/, logs/
./setup.sh add-account acct-1   # paste token from: claude setup-token  (account 1's browser)
./setup.sh add-account acct-2   # …account 2
./setup.sh add-device my-laptop # prints the env vars for that device
python3 rotator.py
```

That is the standalone, run-it-yourself layout. On a Debian 12 server, install
it as a hardened systemd service instead and administer it from any directory:

```bash
git clone <this repo> && cd claude-rotate
sudo ./install.sh                               # leaves the service stopped until configured
sudo claude-rotate-admin add-token acct-1       # paste token from: claude setup-token
sudo claude-rotate-admin add-user my-laptop     # prints the env vars for that device; starts the service
sudo claude-rotate-admin status
```

`sudo ./uninstall.sh` removes the service again and keeps your config and tokens
(`--purge` deletes them). See [OPERATIONS.md](OPERATIONS.md) for the admin
commands, including `rotate`, and [deploy/README.md](deploy/README.md) for what
the installer does.

## Generating account tokens (`claude setup-token`)

Each subscription account contributes one long-lived OAuth token. Facts below
are from the [official authentication docs](https://code.claude.com/docs/en/authentication.md)
unless flagged otherwise.

**Minting a token:**

```bash
claude setup-token
```

- Opens the same browser OAuth flow as `/login`; if no browser can reach the
  local callback (headless server, SSH session), it falls back to a paste-a-code
  flow, so you can mint tokens on the proxy server itself.
- Prints a token starting `sk-ant-oat01-…` to the terminal and does **not**
  save it anywhere; copy it straight into `./setup.sh add-account <name>`
  (standalone: stores it under `tokens/`, chmod 600, gitignored) or into
  `sudo claude-rotate-admin add-token <name>` (Debian service: stores it under
  `/etc/claude-rotate/tokens/`, readable by root and the service only).
- Valid for **about one year**. Requires a Claude subscription (Pro, Max,
  Team, or Enterprise).

**Minting for multiple accounts from one machine.** The account that completes
the browser OAuth is the account the token belongs to; the CLI's current
login doesn't decide it. (Officially undocumented; this is the behavior in
practice.) Two workable recipes:

1. **Browser profiles**: keep one browser profile (or incognito window) logged
   into claude.ai per account. Run `claude setup-token`, and complete the OAuth
   in the profile of the account you're minting for; copy the URL into that
   profile if the wrong one opens.
2. **Config-dir isolation**: `CLAUDE_CONFIG_DIR=~/.claude-acct2 claude
   setup-token` keeps a fully separate CLI login per directory, useful if you
   also want to *run* Claude Code as different accounts.

After adding an account, send one request through the proxy and check
`/rotate/status`; the unified utilization headers it reports are per-account,
so a token minted against the wrong account shows up immediately as the wrong
gauge moving.

**Lifetime & revocation:**

- There is currently **no CLI command to list or revoke** setup tokens
  ([open feature request](https://github.com/anthropics/claude-code/issues/57400));
  revoke manually from claude.ai settings. `/logout` only revokes the CLI's
  *active* session credential, not previously minted setup tokens.
- Treat each token file as a year-long bearer credential for that Anthropic
  account (see [Security notes](#security-notes)). When a token dies early, the
  proxy's upstream calls start returning 401 for that account; the panel
  then shows a warning alert for it (see Troubleshooting).
- Scope note: subscription OAuth tokens are for Claude Code traffic, which is
  all this proxy forwards, and per the 2026 docs they are rejected by the raw
  Messages API.

## Quickstart (each device)

Two env vars in your shell profile are the whole client install:

```bash
export ANTHROPIC_BASE_URL=http://<server>:8484
export CLAUDE_CODE_OAUTH_TOKEN=<device key printed by add-device>
```

Run `claude` as usual. When account 1's 5-hour window fills, the next request
rides account 2. Use a VPN/overlay like Tailscale between devices and server,
the proxy speaks plain HTTP and device keys are bearer secrets. See
[Connecting distributed devices](#connecting-distributed-devices) for setups.

## Connecting distributed devices

The proxy is plain HTTP and device keys are bearer secrets, so the transport
between devices and server must be private. Pick one:

### Same LAN / subnet (nothing to install)

If every device lives on the same trusted network as the server (home lab,
office LAN), you don't need Tailscale or any overlay at all:

```bash
# on the server: find its LAN address
ipconfig getifaddr en0        # macOS (en0 = Ethernet/Wi-Fi)
hostname -I                   # Linux

# on each device:
export ANTHROPIC_BASE_URL=http://192.168.1.42:8484
```

- **macOS servers get a free stable name** via Bonjour/mDNS:
  `http://<hostname>.local:8484` (e.g. `http://mac-mini.local:8484`) works from
  Macs, iPhones, and most Linux devices (with `avahi-daemon`), no IP to
  remember. Otherwise give the server a **DHCP reservation** in your router so
  its IP never changes under the devices pointing at it.
- Set `bind` in `config.json` to the LAN IP (or keep `0.0.0.0` if the box has
  only one network). Verify from a device: `curl http://<server>:8484/rotate/status?key=<device key>`.
- **Do not port-forward 8484 on your router.** LAN-only means the firewall
  boundary is your router's NAT; forwarding the port turns this into the
  internet-exposed scenario below.

The trust caveat: traffic is plain HTTP, so anyone on the same subnet can read
device keys off the wire. Fine for a home network you control; on a shared
office network or anywhere with guests, prefer one of the encrypted options
below. And a hybrid is normal: LAN for the desktop next to the server,
Tailscale for the laptop that leaves the house.

### Tailscale (recommended)

Zero-config WireGuard mesh; free tier covers personal use easily.

```bash
# on the server AND every device:
#   macOS:  brew install tailscale && brew services start tailscale
#   Linux:  curl -fsSL https://tailscale.com/install.sh | sh
tailscale up                      # login once per machine, same tailnet
tailscale status                  # note the server's name / 100.x.y.z address
```

With MagicDNS on (default on new tailnets), devices reach the server by name:

```bash
export ANTHROPIC_BASE_URL=http://<server-hostname>:8484   # e.g. http://my-server:8484
export CLAUDE_CODE_OAUTH_TOKEN=<device key>
```

Tighten the listener so the proxy is *only* reachable over the tailnet: set
`bind` in `config.json` to the server's Tailscale IP instead of `0.0.0.0`:

```json
{ "bind": "100.x.y.z", "port": 8484 }
```

Optional TLS: `tailscale serve --bg http://127.0.0.1:8484` publishes the proxy
as `https://<server>.<tailnet>.ts.net` (valid cert, tailnet-only). Then use
that URL as `ANTHROPIC_BASE_URL` and set `bind` to `127.0.0.1`.

CI runners and containers work too: ephemeral auth keys
(`tailscale up --auth-key=tskey-...`) join a runner to the tailnet for the
duration of a job; there's a ready-made GitHub Action (`tailscale/github-action`).

### Plain WireGuard

Same effect, no third party. Sketch: generate a keypair per machine
(`wg genkey | tee private.key | wg pubkey > public.key`), give the server a
`wg0` with an internal subnet (e.g. `10.84.0.1/24`), add each device as a
`[Peer]` with `AllowedIPs = 10.84.0.X/32`, and point devices at
`http://10.84.0.1:8484`. Set `bind` to `10.84.0.1`. More manual than
Tailscale (key distribution, NAT traversal is on you), but fully self-hosted.

### SSH tunnel (zero install)

Any device that can SSH to the server needs nothing else:

```bash
ssh -N -L 8484:127.0.0.1:8484 user@server &
export ANTHROPIC_BASE_URL=http://127.0.0.1:8484
```

With `bind: 127.0.0.1` on the server, this is the tightest setup: the proxy
never listens on a network interface at all. Use `autossh` (or
`ServerAliveInterval 30` in `~/.ssh/config`) to keep the tunnel up; fine for a
laptop or a single CI box, tedious beyond a few devices.

### Cloudflare Tunnel (device without VPN access)

For a device that can't join your tailnet (locked-down corp machine, hosted
CI you can't install agents on), `cloudflared` can expose the proxy through
Cloudflare without opening ports:

```bash
# server:
cloudflared tunnel login
cloudflared tunnel create claude-rotate
cloudflared tunnel route dns claude-rotate rotate.example.com
cloudflared tunnel run --url http://127.0.0.1:8484 claude-rotate
# device:
export ANTHROPIC_BASE_URL=https://rotate.example.com
```

**This makes the proxy internet-reachable**: the only thing between the
world and your Anthropic tokens is the device-key check. If you go this
route, put [Cloudflare Access](https://developers.cloudflare.com/cloudflare-one/)
(a Zero Trust service-token policy) in front so unauthenticated requests never
reach the proxy, and treat device keys as revocable: delete a leaked one from
`config.json` (the proxy drops it at its next reload). Prefer any of the VPN
options above when possible.

### Other overlays

ZeroTier and NetBird work identically to Tailscale for this purpose (private
overlay IP + `bind` to it); use whichever your fleet already runs. Whatever
the transport, the checklist is the same: proxy bound to a private interface,
HTTP never exposed publicly, one device key per machine so any single machine
can be revoked alone.

## Analytics panel

Open `http://<server>:8484/rotate/panel?key=<any device key>`. The page keeps
the key in the tab's session storage and removes it from the address bar, so a
reload works. Open the link once per browser tab:

![claude-rotate analytics panel: devices, account gauges, consumption and cost-equivalent tables](screenshots/panel.png)

Live 5-hour and weekly gauges per account, straight from Anthropic's own
rate-limit headers:

![account quota gauges with reset countdowns](screenshots/accounts.png)

- **Headline**: time until every account is spent, with when it happens and
  which reset brings quota back; tiles for weekly quota left, burn rate,
  requests in the last 24 h and the 7-day cost-equivalent. Alerts (device over
  N tokens/hour, expensive-model usage, account near the switch threshold;
  thresholds in `config.json`) sit in a banner above it. An account whose
  token upstream refused with a 401 in the last hour also raises a warning
  naming it; the proxy does not rotate away from it. Set `alerts.webhook_url`
  to also receive each alert as a JSON POST.
- **Accounts**: live 5-hour and weekly meters (from Anthropic's own headers)
  with a tick at the switch threshold, reset countdowns, upstream status and
  the active account. Hover a reset countdown for the exact reset time in your
  browser's time zone and in UTC. A private account carries a "private to
  <device>" badge. An account with `weekly_cap` or `weekly_pacing` shows its
  limit on the weekly meter: a red line at `weekly_cap` with the part after it
  hatched, as it is not available, and for a paced account a marker at the
  allowance now, which moves along the bar through the week and carries a
  small tag with the allowance percentage. Hover the line or the marker for
  its full value. Once weekly use is at or over the allowance, the meter says
  `weekly cap reached` or `paced allowance reached`.
- **Weekly quota, all accounts**: the enabled accounts' current weekly
  usage, summed (blue), against their combined capacity (orange: 100% per
  account, scaled by `accounts[].weight`). Usage drops at each account's weekly
  reset by what that week used. A 7.5-day projection spends the last 24 h rate
  consume-first, pauses usage while every account is spent, and drops it at
  each projected reset; it states when every account first reaches 100% and
  when the next reset brings quota back. The rate counts only rises in usage,
  so a reset does not cancel consumption. Periods when every account was or
  will be at its limit are shaded. The chart opens on 7.5 days either side of
  now (3.5 days on a phone) and scrolls back to the first sample. Hover or tap
  for values; **Table** shows the same data as a table. History is sampled
  every 30 min into `usage_history.json` next to `state.json` and kept 14 days.
  One line under the chart gives the weekly demand: the rate over the last
  7 days of history (all of it while there is less), as a share of one weekly
  quota per week, and the number of weight-1 accounts that covers it next to
  your combined capacity. A 30-min stretch counts only when an account was
  available at its start, meaning under both its weekly allowance at that time
  (`weekly_cap` and `weekly_pacing`) and `hard_limit`, so time when every
  account had run out does not understate demand. Accounts are counted as
  weight-1 because weight is relative. With under 6 counted hours the line
  gives a note instead. Limit: the history holds no 5h data, so time when
  every account was 5h-limited still counts as available.
- **Devices**: every registered device (online status, last seen, last model)
  and every removed device that still has history, with usage over 24 h, 7 d,
  30 d and all time plus a 30-day cost trend. 7 d and 30 d are calendar days,
  today included.
- **By model** and **by account**, each with its own 24 h / 7 d / 30 d / all
  time selector (by account starts at 24 h; account history beyond 24 h
  starts when the account totals were first recorded, shown as "since" in its
  heading), requests, tokens, the share of prompt tokens served from cache, and a
  *cost-equivalent* column: what the usage would cost at API list prices (incl.
  cache read 0.1× / write 1.25×), i.e. what the subscriptions are saving you.
  Under each table a pie chart shows each row's share of that
  cost-equivalent, in table order: the top six rows get a slice each and the
  rest are summed as "other". Each pie follows its card's period selector and
  sits centred in its card, as does a last account card left alone in a
  two-column row. Without usage the pie gives way to "no usage".
- **Recent switches**: the latest account switches and their reasons, in
  their own full-width card below the two tables.
- **Footer**: the running version and commit, such as `claude-rotate edge ·
  0123abc`. The Docker image takes them from its build: `edge` for an image
  built from `main`, `X.Y.Z` for one built from a release tag, and the first 7
  characters of the commit. Outside Docker (`install.sh`, systemd, launchd)
  the files are copied without git history, so it shows `dev` and no commit.

The 24 h figures come from the audit log. Longer periods come from
`device_usage.json` next to `state.json`: daily totals per device, model and account for
31 days plus all-time totals, updated with every request and saved every
minute. On first start it is rebuilt once from `audit.jsonl` and its rotated
copies, so all time reaches back as far as those logs do.

JSON endpoints: `/rotate/status` (accounts, each with its `private_to` owners as a list or null, its `weekly_cap`, `weekly_pacing` and `weekly_allowance` (the weekly use allowed now, or null with neither setting), + switch events, + `weekly.demand`, the weekly demand figures, + `version`, the running `version` and `revision`) and
`/rotate/stats` (rollups, alerts and the longer `periods`), same `?key=` or `Authorization: Bearer`
auth. Audit trail: `logs/audit.jsonl`, one JSON record per request.

## OpenAI-compatible endpoint

`POST /v1/chat/completions` (same device-key auth) accepts OpenAI-format chat
requests, streaming included, and rides the same rotated accounts, so any
OpenAI-format app or LLM router can use the capacity, not just Claude Code:

```bash
curl http://<server>:8484/v1/chat/completions \
  -H "Authorization: Bearer <device key>" -H "content-type: application/json" \
  -d '{"model":"claude-sonnet-5","max_tokens":256,
       "messages":[{"role":"user","content":"hello"}]}'
```

Text conversations only (tool use or a non-text content part → 400). When every account's window is spent
it returns `503` + `Retry-After: <seconds until an account this device may
use can serve again>` (a window reset, or a weekly cap lifting), point a
router's circuit breaker at that. Responses carry `x-rotate-account` and the
upstream `anthropic-ratelimit-unified-*` headers for per-request telemetry.
A failure mid-stream, or a stream cut short, arrives as an OpenAI error chunk
and `[DONE]`, not as a normal finish.

## Configuration (`config.json`)

| Field | Meaning |
|---|---|
| `accounts[]` | `{name, token_file}`: one `claude setup-token` per subscription, stored under `tokens/` (chmod 600) |
| `devices{}` | `name → device key`; key is what the device puts in `CLAUDE_CODE_OAUTH_TOKEN` |
| `accounts[].priority` | lower = preferred; accounts form tiers, a backup tier is used once every preferred account is past its switch thresholds or weekly cap, not at 100% (default 100) |
| `accounts[].disabled` | `true` benches an account: never rotated onto, still shown in the panel |
| `accounts[].private_to` | a device name, or a list of device names: the account is served *only* to those devices; every other device never touches it. Within its priority tier its owners use it first. `--private-to` writes `priority: 1` for a new account. See "Preferring one account over another". |
| `accounts[].weight` | plan size relative to the other accounts (default 1): scales the account in the panel's weekly chart and run-out estimate, and in the `weekly-pace` score. Max 20x is 4 times Max 5x: give a 5x `0.25` next to 20x accounts (or a 20x `4` next to 5x accounts, if you leave 5x at 1). |
| `accounts[].weekly_cap` | fraction of the weekly window, 0 to 1, that this account may use: a flat maximum (default: no cap). With `weekly_pacing: true` the allowance ramps up to it through the week. See "Rotation policy". |
| `accounts[].weekly_pacing` | `true` or `false` (default `false`): ramp this account's weekly allowance up through the week, to `weekly_cap` or to the full week when no cap is set, so the account is not drained early. See "Rotation policy". |
| `pace_lookahead` | how long before an account's weekly reset the paced allowance opens to the full cap: an integer and one unit `m`, `h` or `d`, for example `"12h"` (default `"12h"`). Applies only to accounts with `weekly_pacing: true`. |
| `accounts[].pace_lookahead` | overrides `pace_lookahead` for this account (default: the top-level value). Applies only with `weekly_pacing: true`. |
| `bind` / `port` | address and port the proxy listens on (defaults `"127.0.0.1"` / 8484). The environment variable `CLAUDE_ROTATE_BIND` overrides `bind`, for a container that must listen on `0.0.0.0` whatever the mounted config says. Both need a restart. |
| `reload_poll_s` | seconds between checks of `config.json` and the token files for changes, which are then reloaded (default 5; 0 = off, leaving `SIGHUP`). Needs a restart. See "Reloading the configuration". |
| `switch_threshold` | 5h-window utilization that triggers rotation (default 0.8) |
| `switch_threshold_7d` | weekly-window utilization that makes an account unusable (default 0.98) |
| `strategy` | `consume-first` (default), `least-used`, `most-weekly-left` or `weekly-pace`; see below |
| `switch_cooldown_s` | min seconds between voluntary switches (default 300); hard limits ignore it |
| `switch_margin` | hysteresis: a threshold switch needs a candidate this much better (default 0.05) |
| `switch_margin_7d` | `most-weekly-left` proactive switch only if the candidate's weekly utilization is this much lower (default 0.03) |
| `pace_ratio` | `weekly-pace` proactive switch only if the candidate's pace is this many times the active account's (default 1.25) |
| `reserve_sessions` | `weekly-pace`: a proactive switch onto a fresh 5h window needs at least this many other accounts (the one being left counts) with room to rotate into (default 1) |
| `reserve_headroom` | room an account needs left in both its 5h and weekly windows, below their thresholds, to count as somewhere to rotate into (default 0.03) |
| `hard_limit` | utilization above which an account is not used even as reserve; Anthropic rejects at 1.0 (default 1.0) |
| `reserve_spend_5h_s` / `reserve_spend_7d_s` | an account above its switch threshold is held in reserve, but spent on purpose when the window holding it there resets within this many seconds (defaults 3600 / 43200) |
| `bad_recover_s` / `bad_recover_max_s` | an account answering 403/404 on `/v1/messages` (closed or free plan) is skipped, then retried after `bad_recover_s` (default 600), doubling each time it refuses again up to `bad_recover_max_s` (default 86400); 0 = never retry |
| `consume_first_margin_s` | proactive switch only if the candidate's weekly reset is this much sooner (default 3600) |
| `hold_max_s` | when *every* account is spent, hold the request open up to this long waiting for a window reset instead of returning 429 (default 0 = off) |
| `refresh_idle_s` | send an account with no traffic for this long a 1-token request (see `refresh_idle_model`) to read its current usage, so quota spent outside the proxy is seen before a switch (default 1800; 0 = off). Usage is otherwise only learned from traffic. Cost, computed rather than measured: at most 86400 / `refresh_idle_s` probes a day per idle account (48 at the default), none while the account carries traffic. Each probe is a one-character prompt with `max_tokens` 1, about 9 tokens, so at most about 432 tokens a day per idle account at the default. |
| `refresh_idle_model` | model the idle refresh probes with (default `"claude-haiku-4-5"`). Set it when that model retires: a model the plan can't use answers 429 without usage headers, so the refresh learns nothing. |
| `panel_tailnet` | `true` lets tailnet users open the panel without a device key, trusting the `Tailscale-User-Login` header from Tailscale Serve; requires a loopback `bind` (default false). See [SECURITY.md](SECURITY.md). |
| `prices_per_mtok` | substring-matched `[input, output]` $ per MTok for the cost columns |
| `alerts` | `device_tokens_per_hour`, `expensive_model_patterns`, `util_warn`, `webhook_url`. An unset key keeps its default, so `{"webhook_url": "..."}` alone is valid. |
| `alerts.webhook_url` | optional URL (default unset = off). Once a minute the proxy POSTs each current panel alert as JSON `{"text", "level", "ts"}` to it, each alert at most once an hour, with a 5 s timeout and no retry; a failure prints a warning. The URL is a secret: see [SECURITY.md](SECURITY.md). |

### Reloading the configuration

The proxy reloads `config.json` and the token files without a restart, so
adding an account or device, benching an account, replacing a token or revoking
a device takes effect on the next reload. Two things start one:

- **A file change.** Every `reload_poll_s` seconds (default 5) the proxy checks
  `config.json` and the token file of every account, and reloads when one has
  been replaced or edited. A token file can sit outside the config directory
  (an absolute `token_file`), so a Docker or Kubernetes secret mount works the
  same way.
- **`SIGHUP`**, for an immediate reload, and the only trigger when
  `reload_poll_s` is 0: `systemctl reload claude-rotate`, `kill -HUP <pid>`,
  `launchctl kill SIGHUP gui/$(id -u)/<label>` or
  `docker compose kill -s HUP claude-rotate`.

A reload applies all of the new files or none. If they do not validate, the
proxy keeps the configuration it is running, logs `config reload (...) failed;
keeping the running config: <reason>` to stderr and reports the reason as
`error` in the `config` object of `/rotate/status`, next to `loaded_at`,
`last_reload` and `restart_needed`. Fixing the file is enough: the next change
is loaded on its own.

Requests already open carry on. A stream or a held request on an account or
device that the reload removed finishes, and the next request sees the new
configuration. Quota state and each device's current account are kept.

Some settings are read once at startup. A reload checks them but leaves them
alone, and lists any that changed in `restart_needed`: `bind`, `port`,
`reload_poll_s`, `max_request_bytes`, `max_response_bytes`,
`max_concurrent_requests`, `audit_cache_records`, `audit_cache_bytes`,
`audit_max_bytes` and `audit_backups`. A value fixed by its `CLAUDE_ROTATE_*`
environment variable is never listed.

### Rotation policy

An account is *usable* while its 5h window is under `switch_threshold` and its
weekly window under `switch_threshold_7d`; a window past its reset counts as
0%. State survives restarts in `state.json`.

- **`consume-first`** (default): burn the usable account whose **weekly window
  resets soonest**: weekly quota is use-it-or-lose-it, so the perishable
  account is spent first and no paid quota expires unused. The proxy also
  switches *proactively* (below the threshold) when another usable account's
  weekly reset is at least `consume_first_margin_s` sooner.
- **`least-used`**: classic; lowest 5h utilization wins.
- **`most-weekly-left`**: among accounts with 5h room, the one with the
  **lowest weekly utilization** wins (ties go to lower 5h use). The proxy
  switches *proactively* when another usable account's weekly utilization is
  at least `switch_margin_7d` lower, so weekly usage stays balanced and a
  5h-spent account is left for the one with the most week remaining.
- **`weekly-pace`**: combines how much is left with how long it has left.
  Each account scores `weight × (1 − weekly utilization) / hours to weekly
  reset`: the rate it must be used at from now on so none of it expires.
  The highest score among accounts with 5h room wins, so quota close to
  expiring gets spent first. A proactive switch needs a score at least
  `pace_ratio` times the active account's, and it only starts a fresh 5h
  window if at least `reserve_sessions` other accounts (the one being left
  included) still have `reserve_headroom` in both windows, so there is always
  somewhere to rotate into. Forced switches (5h spent,
  quota rejected) can use the reserve. `/rotate/status` shows each account's
  `weekly_pace_pct_per_h` and `fresh_session`.
- **Weekly cap** (`accounts[].weekly_cap`, `accounts[].weekly_pacing`): an
  account with a cap may use at most `weekly_cap` of its weekly window, a flat
  maximum. With `weekly_pacing: true` (default `false`) the allowance ramps
  instead: `weekly_cap × (time into the week + pace_lookahead) / one week`,
  never more than `weekly_cap`, or up to 1.0 when no `weekly_cap` is set. Early
  in the week the allowance is small, which leaves quota for chat use outside
  the proxy; it reaches the cap `pace_lookahead` before the weekly reset, so the
  last evening burns the account down to the cap. An account with neither key
  is unchanged. Upgrade note: the lookahead key was `cap_lookahead`. The old
  key now refuses startup, so rename `cap_lookahead` to `pace_lookahead` in
  config.json before upgrading. An account at or over its allowance is neither
  usable nor reserve, and it ranks after every other account in the all-blocked
  fallback. If it is the active account it is left at once, with no cooldown or
  margin, and the switch is logged as `weekly_cap`. A manual
  `claude-rotate-admin rotate` onto such an account is left on its next reply.
  With `weekly-pace`, a capped account's quota left is
  `weekly_cap − weekly utilization`.
- If nothing is usable, the proxy waits on the account that can serve again
  soonest. A week-exhausted account counts from its weekly reset (a 5h reset
  can't help it), and an account whose last response was a quota rejection
  isn't usable until a window has reset since.
- A cooldown plus a hysteresis margin stop accounts ping-ponging at the
  threshold; a hard limit (quota actually rejected) always switches
  immediately.
- **Burst vs quota 429s**: a per-minute rate-limit 429 (utilization not
  exhausted) does *not* rotate: rotating would move the burst to the next
  account and throw away its warm prompt cache. The account is paced for
  `retry-after` seconds and the request retried.
- **Hold-until-reset** (`hold_max_s` > 0): when every account is spent, the
  proxy keeps the request open and retries once the soonest account this
  device may use can serve again (a window reset, or a weekly cap lifting)
  instead of failing: an unattended CI/agent run finishes on its own instead of dying
  at 2am. Make sure your client's request timeout tolerates the wait (Claude
  Code's default is generous; other clients may need tuning).

### Preferring one account over another

Four levels of control, from lazy to pro, all in the same `config.json`,
deliberately **no separate rules file** (see design note below):

**Level 0: do nothing.** The defaults (consume-first + thresholds + cooldown)
already make a sane global decision. Most single-owner setups need nothing else.

**Level 1: priority tiers.** Give accounts a `priority` (lower = preferred):

```json
"accounts": [
  { "name": "max-20x",  "token_file": "tokens/max-20x.token",  "priority": 1 },
  { "name": "max-5x",   "token_file": "tokens/max-5x.token",   "priority": 1 },
  { "name": "old-pro",  "token_file": "tokens/old-pro.token",  "priority": 2 }
]
```

Rotation happens *within* the lowest-numbered tier that still has a usable
account; `old-pro` above is touched only when both Max accounts are spent.
When a preferred account's window resets, traffic is pulled back automatically
(`priority_recovery` in the events feed) after the cooldown; the backup is a
spillway, not a new home. Accounts without a `priority` share one default tier,
which is why Level 0 works unchanged.

"Spent" means past a switch threshold, not at 100%. An account is usable while
its 5h window is under `switch_threshold` and its weekly window is under
`switch_threshold_7d`, so the backup tier takes over before 100% (defaults 0.8
and 0.98). The band from a threshold up to `hard_limit` is the account's
reserve. A preferred account's reserve is used only when no account in any tier
is usable, or in the last `reserve_spend_5h_s` or `reserve_spend_7d_s` before
the window holding it there resets. A `weekly_cap` is a hard maximum: an
account at its cap is neither usable nor reserve, so it never reaches 100%.

To keep a preferred tier filling further before the backup takes over, raise
`switch_threshold` or `switch_threshold_7d`, or raise or remove `weekly_cap`.
The cost is a later in-tier switch and a higher chance that a request near 100%
is rejected and retried.

**Level 2: bench an account.** `"disabled": true` takes an account out of
rotation entirely (a work account you don't want touched, one you're resting)
while keeping it visible in the panel. If the *active* account is disabled in
config, the proxy selects an enabled account before sending the next request.
If every account is disabled, it returns 503 without contacting upstream. Re-enable
by deleting the flag; both changes apply on the next
[reload](#reloading-the-configuration).

Combine with `strategy` for the remaining temperament choice: `consume-first`
(spend perishable weekly quota first), `least-used` (spread evenly) or
`most-weekly-left` (always ride the account with the most weekly quota left) or
`weekly-pace` (spend the quota likeliest to expire, keeping a fresh session in reserve).

**Level 3: pin an account to devices.** `"private_to": "<device name>"` (or a
list, `["laptop", "desktop"]`) marks an account as belonging to those devices: it
is served *only* to them, and they share it. Every other device is never routed
to it. It takes part in the priority tiers (Level 1) like any account, and within
a tier its owners use it before the shared accounts; `strategy` orders accounts
within that. `--private-to` writes `"priority": 1` for a new private account, so
its owners use it first; set `"priority": 2` to draw it after the p1 accounts and
before the other p2 ones. Nothing defaults it in the code: a hand-written
`private_to` with no `priority` is in the default tier (100), owners first. An owner moves on when the private account is spent,
and is pulled back (`priority_recovery`) after the cooldown once it has room
again. The names must be registered devices in
`devices{}`: startup fails otherwise. Each device keeps its own current account
(`device_accounts` in `state.json`), so one device's switch never moves another's.
Set it in `config.json`, or with the CLI:

```sh
./setup.sh add-account max-20x --private-to kamal-mbp
sudo claude-rotate-admin add-token max-20x --private-to kamal-mbp,kamal-mini   # Debian service
```

Both take one device or a comma-separated list. Renewing a token without
`--private-to` keeps the account's `private_to`; to make an account shared
again, delete `private_to` from `config.json` (on the Debian service,
`claude-rotate-admin set-account NAME --shared` will do it once it lands).
`claude-rotate-admin remove-user`
takes a device out of every `private_to` list that has other owners, and refuses
a device that is the only owner of an account.

This is what you reach for when someone joins with their own subscription and
you don't want the rest of the pool quietly burning their weekly quota.

**Design note: why no rules YAML:** a rules engine (per-model routes,
time-of-day windows, and the like) would add a parser, a second config file,
and an ordering semantics to a one-file tool, and every use case we've
actually hit decomposes into the knobs above (strategy, priority, disabled,
private_to, thresholds). If a real need appears that doesn't decompose, add it
as another plain field on the existing config objects, not as a DSL.

## Run as a service

For routine Debian service management, see the [operations quick guide](OPERATIONS.md).

- **macOS**: `deploy/com.example.claude-rotate.plist` (read its comments;
  launchd needs the full python3 path and a local-disk install).
- **Debian 12 / Linux**: `sudo ./install.sh`, then `sudo claude-rotate-admin
  add-token` / `add-user`; see [the deployment guide](deploy/README.md). The
  supplied unit uses a dedicated account, root-owned releases, separate private
  state/log directories, watchdog recovery, and conservative limits for a small
  VPS. `sudo ./uninstall.sh` removes it and keeps the configuration. Migration
  and rollback instructions are included.
- **Docker**: see [Docker](#docker) below.

### Docker

The repository's `Dockerfile` builds a small image (Python 3.12, non-root user,
dependencies from `requirements.lock`). It is published to
`ghcr.io/mrglrlgrlgrl/claude-rotate` for amd64 and arm64: each push to `main`
publishes `edge`, and each `vX.Y.Z` tag publishes `X.Y.Z`, `X.Y` and `latest`.
[`deploy/compose.yaml`](deploy/compose.yaml) is a ready example:

```sh
mkdir -p config/tokens data
# config.json in ./config, tokens in ./config/tokens (mode 600, owned by the
# "user:" set in the compose file), then:
docker compose up -d
```

- `/config` holds `config.json` and the tokens, mounted read-only. Mount the
  directory, not the file: an editor or `setup.sh` replaces `config.json`, and a
  single-file mount would keep serving the old one.
- `/data` holds `state.json`, the usage files and `audit.jsonl`. Use a bind
  mount, so the `user:` you set can write to it.
- The image sets `CLAUDE_ROTATE_BIND=0.0.0.0`, because a container has to listen
  on all its addresses; `bind` in `config.json` is ignored. That rules out
  `panel_tailnet`, which needs a loopback bind. Publish the port on loopback or
  a tailnet address only (`127.0.0.1:8484:8484` in the example).
- Edits to the config or token files load by themselves within `reload_poll_s`
  seconds. For an immediate reload: `docker compose kill -s HUP claude-rotate`.
  A token supplied as a Compose `secrets:` entry (an absolute `token_file` such
  as `/run/secrets/acct-1`) is a single-file mount, so replacing it needs
  `docker compose up -d --force-recreate`. A Kubernetes Secret volume swaps its
  files in place and is picked up like any other edit.
- The same image runs as a sidecar next to a router: mount the config as a
  ConfigMap and the tokens as a Secret.
- `/rotate/status` needs a device key, so the image has no `HEALTHCHECK`.
  Monitor it as described under [Operations](#operations).

#### Adding accounts and devices

`setup.sh` works on the `config.json` and `tokens/` next to itself, so run it
from the host against the directory you mount as `/config`. It writes
`token_file` as `tokens/<name>.token`, the relative form the container needs:

```sh
cp setup.sh config.example.json ./config/
./config/setup.sh add-account acct-1      # paste the output of: claude setup-token
./config/setup.sh add-device alice-laptop
./config/setup.sh onboard alice laptop
```

- `add-account` also takes `--private-to <device>[,<device>...]` to reserve the
  account for those devices. `onboard <user> <dev>` mints the key
  `<user>-<dev>` and prints a welcome block.
- Run the commands as the `user:` set in the compose file. That user must own
  the token files, with mode 600, or the container cannot read them.
- The proxy loads the change within `reload_poll_s` seconds, or at once with
  `docker compose kill -s HUP claude-rotate`. Replacing a Compose `secrets:`
  token still needs `docker compose up -d --force-recreate`, as above.
- Every other setting is an edit to `./config/config.json`, loaded the same
  way: `disabled`, `priority`, `weight`, `weekly_cap`, `weekly_pacing`,
  `pace_lookahead`, `private_to`, `public_url`, and removing a device or an
  account.

#### Managing a container-only deployment

The image also carries `claude-rotate-admin`, set to its container mode. The
compose file has a one-off `admin` service for it, under the `admin` profile, so
`docker compose up -d` never starts it:

```sh
docker compose run --rm admin add-token acct-1
docker compose run --rm admin status
```

- The `admin` service mounts `./config` read-write. The proxy keeps
  `./config:/config:ro`, so a compromised proxy cannot rewrite its devices or
  tokens, and only the one-off admin container can write them.
- Both services run as the same `user:`. The admin writes token files at mode
  600 and the config directory at 750, so a different user leaves the proxy
  unable to read a new token.
- The proxy loads each change within `reload_poll_s` seconds. `rotate` edits
  `state.json`, which a running proxy rewrites from memory, so run
  `docker compose stop claude-rotate` first.
- `status` asks the proxy over HTTP at `http://claude-rotate:8484`, the compose
  service name. Set `CLAUDE_ROTATE_ADMIN_URL` on the `admin` service to change
  it.

The host route above (`setup.sh` against the mounted directory) still works as
an alternative. Docker has no `rotate` or `status` shortcut beyond the admin:
use `/rotate/status` and the [analytics panel](#analytics-panel).

## Troubleshooting

**Client gets 401 with `claude-rotate: unknown device key`.** The proxy is
rejecting the *device* (the error body names claude-rotate, so it's not
Anthropic). The device's `CLAUDE_CODE_OAUTH_TOKEN` doesn't match any entry in
`config.json` `devices{}`: re-check the key, or re-run
`./setup.sh add-device` (standalone) / `sudo claude-rotate-admin add-user`
(Debian service); the proxy loads the new key without a restart.

**Client gets a 401 whose error body comes from Anthropic.** The *account
token* the proxy swapped in was rejected upstream, on a previously working
account this almost always means the setup token died (revoked, or hit its
~1-year expiry). Re-mint with `claude setup-token`, then replace
`tokens/<name>.token` (the proxy loads it within `reload_poll_s` seconds), or on the Debian service run
`sudo claude-rotate-admin add-token <name>` for the existing account.

**A request seems to hang.** If every account is spent and `hold_max_s` is
set, that's hold-until-reset doing its job: the request is parked until the
soonest 5h window reset. Check `recent_events` in `/rotate/status` for a
`hold` entry. Set `hold_max_s: 0` if you'd rather fail fast.

**"Why did it switch accounts?"** Every switch is an event in
`/rotate/status` → `recent_events` (and the panel's *Recent switches*), with a
reason: `quota_exhausted`, `utilization>=<threshold>`, `consume_first`,
`priority_recovery`, `account_disabled`, or `manual` (an operator ran
`claude-rotate-admin rotate`). If you saw a 429 upstream but *no*
switch, it was a per-minute burst and the proxy paced the same account on
purpose (see [Rotation policy](#rotation-policy)).

**Config edits don't take effect.** The proxy loads a change within
`reload_poll_s` seconds (default 5), or at once on `SIGHUP`. Look at `config` in
`/rotate/status`: an `error` means the new files were rejected and the old
configuration is still running, and `restart_needed` lists the settings that
only a restart applies (see [Reloading the
configuration](#reloading-the-configuration)). To restart:
`launchctl kickstart -k gui/$(id -u)/<label>` (macOS) or
`sudo systemctl restart claude-rotate` (Debian service). `sudo
claude-rotate-admin activate` validates the config and reloads without one.

**Does the VS Code extension work?** Yes, it honours the same
`ANTHROPIC_BASE_URL` / `CLAUDE_CODE_OAUTH_TOKEN` environment variables. Launch
VS Code from a shell that has them exported (or set them user-wide) so the
extension inherits them.

**Panel is empty or unauthorized.** The panel needs
`?key=<any device key>` in the URL once per browser tab. Without a valid key
it shows one banner, "No device key: open the panel link with ?key=<device
key>", and nothing else. Consumption tables build from
`logs/audit.jsonl` and only show the last 24h, so a fresh install shows
zeros until traffic flows.

**Client throws Zlib/Brotli decoding errors.** Shouldn't happen: the proxy
forces `accept-encoding: identity` upstream and strips `content-encoding`
from responses. If it appears after modifying `rotator.py`, you broke
the encoding fact in `SPEC.md` section 4.

## Limitations

These limits follow the same spirit as the [comparison](#comparison-with-similar-projects):

- **One active account, globally.** Every device rides the same account at any
  moment, so a token-hungry CI box drains the window your laptop is using.
  Per-device *visibility* exists (panel, alerts); per-device *quotas* don't.
- **No per-model weekly caps.** Anthropic tracks some model families
  separately; the proxy only tracks the account-wide 5h/7d windows, so an
  account out of (say) Opus quota gets benched entirely even if Sonnet still
  has room. teamclaude does this better today.
- **Switches drop warm prompt caches.** After a rotation, every device's first
  request re-writes its prompt cache on the new account; that's the cost that
  burst-pacing (and the hysteresis margin) exist to avoid paying needlessly.
- **Some settings need a restart.** The listen address, port and resource
  limits are read once; see [Reloading the
  configuration](#reloading-the-configuration).
- **Plain HTTP by design.** Transport privacy is delegated to the network
  layer; see [Connecting distributed devices](#connecting-distributed-devices).
- **Built on undocumented headers.** Quota telemetry comes from
  `anthropic-ratelimit-unified-*`; if Anthropic changes them, rotation
  degrades to reacting to 429s until the code is updated.
- **No automatic token renewal.** Setup tokens last ~1 year and die silently;
  the symptom is upstream 401s (see Troubleshooting).

## Operations

- **Bounded audit history.** The current audit file rotates at 16 MiB with four
  backups by default. Analytics incrementally read only the current file, retain
  at most 100,000 records / 32 MiB of encoded records, and report partial totals
  after rotation, truncation, or a retention limit. Python memory exceeds the encoded
  byte count. The Debian unit lowers these limits to 8 MiB per file and 10,000
  records / 8 MiB of analytics history. Archives remain available for offline
  accounting; the dashboard is not a complete long-term ledger. Audit records
  include non-message upstream responses too. Storage-write failures are reported
  in the journal and `/rotate/status` `storage_errors`; requests continue, so
  audit/state persistence can be lost until storage is repaired.
- **What to back up.** `tokens/` and `config.json` (`/etc/claude-rotate/` on
  the Debian service) are the whole identity of the install. `state.json`
  is disposable (quota state is re-learned from the first responses; you only
  lose the events history), and `logs/` and `usage_history.json` are
  disposable analytics. `device_usage.json` is rebuilt from `logs/` if lost,
  but all-time totals older than the oldest kept log go with it.
- **Upgrades.** For the Debian service, run `sudo ./install.sh` from the newer
  reviewed checkout; it builds a new immutable release and switches to it. Roll
  back with the [manual procedure](deploy/README.md#immutable-upgrades-and-rollback).
  Other installations should also test with dummy credentials before restarting.
  Check the active account, gauges, and an actual streamed request afterwards.
- **Manual account switch.** `sudo claude-rotate-admin rotate [ACCOUNT]` makes
  another usable account active, with a brief restart. The configured policy
  resumes after the switch cooldown; see [OPERATIONS.md](OPERATIONS.md#switching-the-upstream-account).
- **Health check.** Monitor `/rotate/status` using an `Authorization: Bearer`
  header, then separately monitor real provider success. Local readiness/watchdog
  checks cannot detect an expired provider token or an upstream outage. Keep
  credentials out of shared command histories and URL logs.
- **Compatibility.** OpenAI `max_tokens` / `max_completion_tokens` must be positive
  JSON integers; numeric strings and floats are no longer coerced. The Debian
  profile rejects bodies above 4 MiB (413), buffered upstream responses above
  8 MiB (502), and excess active proxy requests (503 with `Retry-After`). Streams
  remain streaming and consume a request slot until they finish or disconnect.

## Comparison with similar projects

The multi-account rotation niche is well populated. Stars as of Sep 2026.

| Project | ⭐ | Type | Auto-rotate | Exact quota telemetry | Multi-device | Dashboard | ToS risk |
|---|---|---|---|---|---|---|---|
| **claude-rotate** (this) | n/a | server proxy | ✓ consume-first, burst pacing, hold, [herd-safe under load](#performance-herd-safe) | ✓ unified headers | ✓ device keys, per-device analytics | ✓ web panel + $-equivalent | ⚠️ gray |
| [teamclaude](https://github.com/KarpelesLab/teamclaude) | 283 | local proxy | ✓ quota-based, per-model caps, burst pacing | ✓ unified headers | ✗ (one user's machine) | ✓ TUI | ⚠️ gray |
| [claude-swap](https://github.com/realiti4/claude-swap) | 2.2k | credential switcher | ✓ threshold + consume-first | ✓ polls usage | ✗ per-machine | ✓ CLI/TUI + menu bar | ⚠️ gray |
| [claude-relay-service](https://github.com/Wei-Shaw/claude-relay-service) | 12.6k | relay platform | ✓ account pool | partial | ✓ per-key clients | ✓ full admin UI | ❌ built for account sharing |
| [CC-Router](https://github.com/VictorMinemu/CC-Router) | 28 | local proxy | round-robin only | ✗ | ✗ | ✗ | ⚠️ gray |
| [claude-account-switcher](https://github.com/Symbioose/claude-account-switcher) | 53 | menu-bar switcher | ✓ at limits | ✓ live usage | ✗ macOS local | menu bar | ⚠️ gray / ✓ manual |
| [ccrotate](https://github.com/somersby10ml/ccrotate), [claude-rotator](https://github.com/jtxmp/claude-rotator), [cc-relay-proxy](https://github.com/in-jun/cc-relay-proxy) | ≤7 | small CLIs/proxies | basic | ✗ | ✗ | ✗ | ⚠️ gray |

**Reviews:**

- **teamclaude**: the most sophisticated rotation engine in the field:
  per-model weekly caps, burst-vs-quota 429 discrimination, post-failover
  pacing, OAuth refresh, a MITM catch for hardcoded endpoints, zero npm
  dependencies. claude-rotate adopted its burst-pacing and hold-until-reset
  ideas. If you're one person on one machine, it's arguably the better tool;
  it has no multi-device story.
- **claude-swap**: the most popular and most polished UX (uv/pipx install,
  hysteresis, quarantine of dead tokens, parallel sessions per terminal).
  It swaps local credentials rather than proxying, so there's no
  fleet/CI story and each machine manages its own accounts. Its
  `consume-first` strategy is where we took the name and idea from.
- **claude-relay-service**: by far the biggest (Redis, admin UI, multi-provider:
  Claude/OpenAI/Gemini). It is explicitly built for **拼车 / carpooling**,
  pooling subscription accounts across multiple people to split costs. That is
  not a gray area: sharing consumer subscription access with third parties
  violates Anthropic's consumer terms, and account bans around such services
  are regularly reported. Impressive engineering; don't use it with consumer
  Max/Pro accounts.
- **CC-Router**: clean and simple, but blind round-robin: no telemetry, so it
  can rotate onto a spent account and drops warm prompt caches for no reason.
- **claude-account-switcher** (and the Alfred/menu-bar family) solves login
  juggling, not routing. Manual switching between your own accounts is the
  most ToS-defensible workflow of all; the auto-switch-at-limit mode shares
  the same gray area as everything else here.
- **claude-rotate**: what we actually add over the field is the **multi-device
  server model** (devices hold revocable device keys, year-long account tokens
  never leave the server), **per-device consumption/cost attribution**, and a
  **published concurrency story**. The [stress suite](#performance-herd-safe)
  proves one-switch-per-herd and no-rotation burst storms under 100+ in-flight
  requests, numbers you can re-run rather than take on faith. (teamclaude
  engineered for herd failover too, its post-switch pacing, but doesn't
  publish load measurements.) What others do better: teamclaude's per-model
  caps and OAuth refresh, claude-swap's install/UX polish. One file, no
  database, by design.

**On ToS:** every tool above that *automatically* rotates consumer
subscriptions to continue past a rate limit, including claude-rotate, sits
in the same gray area described in the note at the top of this README: you're
automating around limits Anthropic set per account, using accounts you
personally own and pay for. Anthropic's consumer terms prohibit sharing
account credentials and reselling access; they are less explicit about one
person owning several subscriptions. The bright line: **rotating your own
accounts = gray, at your own risk; pooling/sharing/reselling access
(claude-relay-service's core use case) = violation.** If your workload is
commercial or shared, use the API with metered billing instead.

## Security notes

- `tokens/*.token` are year-long bearer credentials for your Anthropic
  accounts. They stay on the server, mode 600, and are gitignored along with
  `config.json`, `state.json`, and `logs/`.
- Device keys authenticate devices to the proxy only; revoke one by deleting
  its entry in `config.json`. The proxy drops it at its next reload.
- Don't expose port 8484 to the public internet; bind to a tailnet/LAN
  interface or keep `bind: 0.0.0.0` behind a firewall.

## Fork changes

This section lists this fork's changes since doxaras `19529c6`, including
those taken from other forks.

**Merged from other forks**

- Built on [clement-z/claude-rotate](https://github.com/clement-z/claude-rotate) by Clément Zrounba up to `56ef19e`, 16 commits, authorship preserved ([compare](https://github.com/clement-z/claude-rotate/compare/19529c6...56ef19e)). Its changes are listed in the groups below, marked `clement-z:`.
- Merged [doxaras/claude-rotate#4](https://github.com/doxaras/claude-rotate/pull/4) (by Dave Marsden, 9 commits, authorship preserved): `most-weekly-left` and `weekly-pace` rotation strategies, a reserve band above the switch thresholds, and rotation off closed/free accounts (403/404 on `/v1/messages`) with backoff recovery.
- Took private accounts from kamal/claude-rotate with authorship preserved, leaving out its admin dashboard and join page: `private_to` with per-device rotation pointers, and `--private-to` on `setup.sh add-account` ([kamal 98c5520](https://github.com/kamal/claude-rotate/commit/98c5520), [kamal ca5d02b](https://github.com/kamal/claude-rotate/commit/ca5d02b)).
- Merged [mrglrlgrlgrl/claude-rotate#1](https://github.com/mrglrlgrlgrl/claude-rotate/pull/1) (by EccentricBlock, 2 commits, authorship preserved): repeated `anthropic-beta` request lines are forwarded comma-joined instead of keeping only the last, and the admin and install preflight tests pass on a host that has the `claude-rotate` account.

**Rotation policy**

- Fixed [doxaras/claude-rotate#3](https://github.com/doxaras/claude-rotate/issues/3): the all-blocked fallback and the hold-until-reset wait rank accounts by when they can serve again, and a rejected account is never treated as usable on a stale reading.
- Added a per-account weekly cap, `accounts[].weekly_cap`: the fraction of its weekly window an account may use, a flat maximum, so an account needed for other purposes, such as chat through the week, keeps quota back ([5ad787a](https://github.com/mrglrlgrlgrl/claude-rotate/commit/5ad787a), [18a4c3a](https://github.com/mrglrlgrlgrl/claude-rotate/commit/18a4c3a)).
- Added per-account weekly pacing, `accounts[].weekly_pacing: true` (default `false`): the account's weekly allowance ramps up through the week to `weekly_cap`, or to the full week when no cap is set, and reaches it `pace_lookahead` before the weekly reset, so the account is not drained early ([18a4c3a](https://github.com/mrglrlgrlgrl/claude-rotate/commit/18a4c3a), [00d659c](https://github.com/mrglrlgrlgrl/claude-rotate/commit/00d659c)).
- `accounts[].private_to` names one or more devices that alone may use an account; within its priority tier they use it before the shared accounts. `--private-to` on `setup.sh add-account` and `claude-rotate-admin add-token` takes a comma list and writes `priority: 1` for a new account ([74d23ae](https://github.com/mrglrlgrlgrl/claude-rotate/commit/74d23ae), [3da39c2](https://github.com/mrglrlgrlgrl/claude-rotate/commit/3da39c2), [fec60f9](https://github.com/mrglrlgrlgrl/claude-rotate/commit/fec60f9), [af3fc70](https://github.com/mrglrlgrlgrl/claude-rotate/commit/af3fc70)).

**Service and deployment**

- Added hot config reload: `config.json` and the token files are reloaded when they change (polled every `reload_poll_s`) or on `SIGHUP`, an invalid file keeps the running config and is reported in `/rotate/status`, and `setup.sh` and `claude-rotate-admin` reload the service instead of restarting it ([4cf2fce](https://github.com/mrglrlgrlgrl/claude-rotate/commit/4cf2fce), [ca5344c](https://github.com/mrglrlgrlgrl/claude-rotate/commit/ca5344c), [19f8a27](https://github.com/mrglrlgrlgrl/claude-rotate/commit/19f8a27)).
- Added `claude-rotate-admin set-account NAME`, which changes an account's `disabled`, `priority`, `weight`, `weekly_cap`, `weekly_pacing`, `pace_lookahead` and `private_to` without editing `config.json`, and `status` shows them ([280d62b](https://github.com/mrglrlgrlgrl/claude-rotate/commit/280d62b)).
- Added `claude-rotate-admin menu`, a prompt-driven menu for selecting, adding, changing, re-tokening, activating and removing accounts, built on the same functions as the flag commands ([cd86d99](https://github.com/mrglrlgrlgrl/claude-rotate/commit/cd86d99)).
- Added `CLAUDE_ROTATE_BIND`, which overrides `bind` ([35f4624](https://github.com/mrglrlgrlgrl/claude-rotate/commit/35f4624)).
- Added a Docker image and compose example, published to GHCR for amd64 and arm64 ([fdcff28](https://github.com/mrglrlgrlgrl/claude-rotate/commit/fdcff28), [35da23e](https://github.com/mrglrlgrlgrl/claude-rotate/commit/35da23e)).
- Shipped `claude-rotate-admin` in the Docker image and added a one-off `admin` compose service with a read-write `./config`, so a container-only deployment can be managed while the proxy keeps its read-only mount ([eb9e541](https://github.com/mrglrlgrlgrl/claude-rotate/commit/eb9e541)).
- Changed publishing: a push to `main` publishes only `edge` and `sha-<short>`, and a `vX.Y.Z` tag publishes `X.Y.Z`, `X.Y` and `latest`, skipping the test matrix when `main` already built that commit green ([ffd6acc](https://github.com/mrglrlgrlgrl/claude-rotate/commit/ffd6acc)).
- clement-z: Harden credential boundaries, dashboard rendering and request handling; `SECURITY.md` added ([e19d5b3](https://github.com/clement-z/claude-rotate/commit/e19d5b3)).
- clement-z: Harden the Debian systemd service and bound resource use on small VPSes ([89ec74a](https://github.com/clement-z/claude-rotate/commit/89ec74a)).
- clement-z: Let tailnet users open the panel without a device key via `panel_tailnet` (loopback bind only) ([de813a1](https://github.com/clement-z/claude-rotate/commit/de813a1)).
- clement-z: Managed installer/uninstaller (`install.sh`, `uninstall.sh`) and `claude-rotate-admin` for local account/device administration ([fbf026f](https://github.com/clement-z/claude-rotate/commit/fbf026f)).
- clement-z: `OPERATIONS.md` documenting routine service operations ([d5c4d6b](https://github.com/clement-z/claude-rotate/commit/d5c4d6b)).

**Usage tracking**

- Made the idle refresh's probe model configurable with `refresh_idle_model`, and documented the refresh's daily cost per idle account ([05f1c6e](https://github.com/mrglrlgrlgrl/claude-rotate/commit/05f1c6e)).
- clement-z: Refresh idle accounts' usage every `refresh_idle_s` with a 1-token request ([969ed52](https://github.com/clement-z/claude-rotate/commit/969ed52)).
- clement-z: Keep 7-day, 30-day and all-time totals per device and model in `device_usage.json` ([4b89abb](https://github.com/clement-z/claude-rotate/commit/4b89abb)).

**Panel**

- Warned in the panel when upstream refused an account's token with a 401 in the last hour ([bc51277](https://github.com/mrglrlgrlgrl/claude-rotate/commit/bc51277)).
- Posted the panel's alerts to an optional `alerts.webhook_url`, each at most once an hour ([5f664ff](https://github.com/mrglrlgrlgrl/claude-rotate/commit/5f664ff)).
- Showed the exact reset time of each usage window on hover in the panel, in the browser's time zone and in UTC ([7ef60c0](https://github.com/mrglrlgrlgrl/claude-rotate/commit/7ef60c0)).
- Showed each account's weekly cap or paced allowance as a badge in the panel, and added `weekly_cap`, `weekly_pacing` and `weekly_allowance` to `/rotate/status` ([32b5221](https://github.com/mrglrlgrlgrl/claude-rotate/commit/32b5221)).
- Drew each account's weekly cap as a line with the unavailable part hatched, and its paced allowance as a marker with a visible percentage tag that moves along the weekly meter, in place of the badge ([191d6df](https://github.com/mrglrlgrlgrl/claude-rotate/commit/191d6df)).
- Kept the panel key across a page reload: the panel stores the `?key=` value in the tab's `sessionStorage` before it strips it from the address bar, `/rotate/panel` serves its static page without a key, and a 401 from `/rotate/status` shows a banner and forgets the key ([2bd5321](https://github.com/mrglrlgrlgrl/claude-rotate/commit/2bd5321)).
- Added cost-share pie charts under the panel's By model and By account tables, and moved Recent switches into its own card ([9572508](https://github.com/mrglrlgrlgrl/claude-rotate/commit/9572508)).
- Showed the accounts a full week needs under the weekly chart, from the last 7 days of demand while an account was available, and added `weekly.demand` to `/rotate/status` ([e9b742d](https://github.com/mrglrlgrlgrl/claude-rotate/commit/e9b742d)).
- Gave the panel's By account card its own 24 h / 7 d / 30 d / all time selector, backed by an `account` kind in `device_usage.json` that starts at the upgrade, and centred the pies and an odd last account card ([02f7109](https://github.com/mrglrlgrlgrl/claude-rotate/commit/02f7109)).
- Showed the running version and commit in the panel footer, from build arguments the image workflow passes (`edge` on `main`, `X.Y.Z` on a release tag, `dev` outside Docker), and added `version` to `/rotate/status` ([0bf41de](https://github.com/mrglrlgrlgrl/claude-rotate/commit/0bf41de)).
- clement-z: Chart weekly quota across accounts and estimate when it runs out; `accounts[].weight` for mixed plan sizes ([206e956](https://github.com/clement-z/claude-rotate/commit/206e956), [9cc2167](https://github.com/clement-z/claude-rotate/commit/9cc2167)).
- clement-z: Project 7 days ahead through shortfalls, shade periods at the limit, and measure the rate only over time with quota left ([c625633](https://github.com/clement-z/claude-rotate/commit/c625633), [5047e8d](https://github.com/clement-z/claude-rotate/commit/5047e8d), [0d8493e](https://github.com/clement-z/claude-rotate/commit/0d8493e)).
- clement-z: Plot summed weekly usage against capacity, dropping at each reset; scrollable history around now ([a02cebb](https://github.com/clement-z/claude-rotate/commit/a02cebb), [56ef19e](https://github.com/clement-z/claude-rotate/commit/56ef19e)).
- clement-z: Redesign: run-out headline, tiles, meters, per-device 24 h / 7 d / 30 d / all-time usage ([948691e](https://github.com/clement-z/claude-rotate/commit/948691e)).
- clement-z: Phone layout and tooltip position fix ([277cdb8](https://github.com/clement-z/claude-rotate/commit/277cdb8)).

## License

[MIT](LICENSE) © 2026 John Doxaras. The Terms-of-Service note at the top of
this README is part of the deal: this software is for rotating accounts *you*
own and pay for, at your own risk.

This repository is a fork of [doxaras/claude-rotate](https://github.com/doxaras/claude-rotate)
and includes changes from [clement-z/claude-rotate](https://github.com/clement-z/claude-rotate).
The original copyright notice is retained in [LICENSE](LICENSE).
