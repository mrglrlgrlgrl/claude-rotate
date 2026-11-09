"""claude-rotate Phase 1: account-rotating auth proxy for Claude Code (Max subscriptions).

Devices point at this proxy and authenticate with an internal device key:

    export ANTHROPIC_BASE_URL=http://my-server:8484   # over tailscale
    export CLAUDE_CODE_OAUTH_TOKEN=<device key from config.json>

Per request, the proxy:
  1. maps the device key -> device name (401 if unknown),
  2. swaps in the active account's real setup-token,
  3. forwards to api.anthropic.com (transparent, incl. SSE streaming),
  4. records usage + account utilization (from anthropic-ratelimit-unified-*
     headers) to logs/audit.jsonl and state.json,
  5. rotates accounts: consume-first by default (burn the account whose weekly
     window resets soonest, use-it-or-lose-it), with cooldown + hysteresis so
     accounts never ping-pong at the threshold,
  6. tells spent quota apart from per-minute burst 429s: a burst paces the
     same account (keeps the warm prompt cache) instead of rotating,
  7. optionally holds a request open until the soonest window reset when every
     account is spent (hold_max_s), so unattended runs finish on their own,
  8. refreshes the usage of idle accounts with a 1-token request every
     refresh_idle_s, so quota spent outside the proxy is seen before a switch,
  9. keeps 14 days of weekly usage for the panel's chart and run-out estimate,
     and daily totals per device and model for its 7-day, 30-day and all-time views.

GET /rotate/status (device-key auth) returns live state for the admin page.
"""

import asyncio
import json
import os
import re
import signal
import sys
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import unquote_plus

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.datastructures import MutableHeaders

from oai_compat import SseToOai, anthropic_to_oai, oai_to_anthropic

ROOT = Path(__file__).parent
CONFIG_PATH = Path(os.environ.get("CLAUDE_ROTATE_CONFIG", ROOT / "config.json"))
STATE_PATH = Path(os.environ.get("CLAUDE_ROTATE_STATE", ROOT / "state.json"))
AUDIT_PATH = Path(os.environ.get("CLAUDE_ROTATE_AUDIT", ROOT / "logs" / "audit.jsonl"))

UPSTREAM = "https://api.anthropic.com"
HOP = {
    "host", "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-length", "accept-encoding",
}
# These belong to the local browser or reverse-proxy trust boundary.
PRIVATE_HEADERS = {"cookie", "referer", "origin", "forwarded", "x-real-ip"}

@asynccontextmanager
async def lifespan(app):
    # uvicorn handles only SIGINT and SIGTERM, so SIGHUP is free for a reload.
    asyncio.get_running_loop().add_signal_handler(signal.SIGHUP, reload_config, "SIGHUP")
    tasks = [asyncio.create_task(refresh_loop())]
    if RELOAD_POLL_S:
        tasks.append(asyncio.create_task(watch_config()))
    yield
    for task in tasks:
        task.cancel()


app = FastAPI(lifespan=lifespan)
client =httpx.AsyncClient(base_url=UPSTREAM, timeout=httpx.Timeout(600.0, connect=15.0))
state_lock = asyncio.Lock()


class PrivateResponseHeaders:
    """Wrap only send, preserving receive/disconnect signals for held requests."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        async def send_private(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["Cache-Control"] = "no-store"
                headers["Referrer-Policy"] = "no-referrer"
                headers["X-Content-Type-Options"] = "nosniff"
            await send(message)

        await self.app(scope, receive, send_private if scope["type"] == "http" else send)


def duration_s(value) -> int | None:
    """Seconds in a duration string, an integer and one unit m, h or d ("12h"),
    or None when it does not parse."""
    m = re.fullmatch(r"(\d+)([mhd])", value) if isinstance(value, str) else None
    return int(m[1]) * {"m": 60, "h": 3600, "d": 86400}[m[2]] if m else None


class ConfigError(Exception):
    """A configuration the proxy refuses to run with."""


def token_path(acct: dict) -> Path:
    return CONFIG_PATH.parent / acct["token_file"]


def file_stamp(path: Path):
    """Identity of a file's current content, or None when it cannot be read.
    stat() follows symlinks, so a swapped Kubernetes Secret link changes it."""
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_ino, st.st_mtime_ns, st.st_size)


def load_config() -> tuple[dict, tuple]:
    """Read and validate config.json and its token files. Returns the config and
    the (path, stamp) of every file it depends on, each stamped before it is read
    so an edit during the read is seen again rather than missed."""
    files = [(CONFIG_PATH, file_stamp(CONFIG_PATH))]
    cfg = json.loads(CONFIG_PATH.read_text())
    loaded = []
    for acct in cfg["accounts"]:
        w = acct.get("weight", 1)
        if type(w) not in (int, float) or not w > 0:
            raise ConfigError(f"account '{acct['name']}': weight must be a number > 0")
        cap = acct.get("weekly_cap")
        if "weekly_cap" in acct and (type(cap) not in (int, float) or not 0 <= cap <= 1):
            raise ConfigError(f"account '{acct['name']}': weekly_cap must be a number from 0 to 1")
        if "weekly_pacing" in acct and type(acct["weekly_pacing"]) is not bool:
            raise ConfigError(f"account '{acct['name']}': weekly_pacing must be true or false")
        if "cap_lookahead" in acct:
            raise ConfigError(f"account '{acct['name']}': cap_lookahead is renamed pace_lookahead: rename the key in config.json")
        if "pace_lookahead" in acct and duration_s(acct["pace_lookahead"]) is None:
            raise ConfigError(f"account '{acct['name']}': pace_lookahead must be an integer and "
                             "one unit m, h or d, for example \"12h\"")
        path = token_path(acct)
        files.append((path, file_stamp(path)))
        if path.exists():
            acct["token"] = path.read_text().strip()
            loaded.append(acct)
        else:
            print(f"warning: skipping account '{acct['name']}' — {acct['token_file']} not found",
                  file=sys.stderr)
    if not loaded:
        raise ConfigError("no accounts with tokens configured — run: ./setup.sh add-account <name>")
    cfg["accounts"] = loaded
    # The example config ships a placeholder device key that is PUBLIC (it's in
    # the repo). It must never be accepted as a live credential: refuse to start
    # while one remains, so add-account-without-add-device can't open the proxy
    # to anyone who read the example (GitHub issue #2).
    devices = cfg.get("devices")
    if not isinstance(devices, dict) or any(
            not isinstance(k, str) or not k.strip() for k in devices.values()):
        raise ConfigError("devices must map names to nonempty device keys")
    if len(set(devices.values())) != len(devices):
        raise ConfigError("device keys must be unique")
    for acct in cfg["accounts"]:
        if "private_to" in acct:
            names = acct["private_to"]
            names = [names] if isinstance(names, str) else names
            if (not isinstance(names, list) or not names or len(set(map(str, names))) != len(names)
                    or any(not isinstance(n, str) or n not in devices for n in names)):
                raise ConfigError(f"account '{acct['name']}': private_to must be a device name "
                                 "or a list of distinct device names, all in devices{}")
    bad = [d for d, k in devices.items() if "REPLACE-WITH" in k]
    if bad:
        raise ConfigError(
            f"placeholder device key(s) in config.json: {', '.join(bad)} — the example "
            "value is public and must never be a valid credential. "
            "Run: ./setup.sh add-device <name> (it replaces the placeholder).")
    return cfg, tuple(files)


def resource_limit(cfg: dict, name: str, default: int, *, allow_zero=False, minimum=1) -> int:
    """Deployment overrides are non-secret; invalid limits fail at startup."""
    value = cfg.get(name, default)
    override = os.environ.get("CLAUDE_ROTATE_" + name.upper())
    if override is not None:
        try:
            value = int(override)
        except ValueError:
            raise ConfigError(f"{name} must be an integer")
    lower = 0 if allow_zero else minimum
    if type(value) is not int or value < lower:
        raise ConfigError(f"{name} must be an integer >= {lower}")
    return value


# API list prices per MTok (input, output), matched by substring of the model id.
# Cost columns are *API-equivalent* dollars: what this usage would cost pay-per-token.
# Override in config.json under "prices_per_mtok".
DEFAULT_PRICES = {
    "fable": [10.0, 50.0], "mythos": [10.0, 50.0],
    "opus": [5.0, 25.0],
    "sonnet": [3.0, 15.0],
    "haiku": [1.0, 5.0],
}
DEFAULT_ALERTS = {
    "device_tokens_per_hour": 5_000_000,
    "expensive_model_patterns": ["opus", "fable", "mythos"],
    "util_warn": 0.8,
}
LOOPBACK = ("127.0.0.1", "::1", "localhost")


def settings(cfg: dict) -> dict:
    """Validate a loaded config and return the module globals derived from it.
    Pure: it raises ConfigError or returns everything, so a caller can apply the
    whole set in one step."""
    # CLAUDE_ROTATE_BIND wins over config.json: a container must listen on
    # 0.0.0.0 whatever the mounted config says.
    bind = os.environ.get("CLAUDE_ROTATE_BIND") or cfg.get("bind", "127.0.0.1")
    panel_tailnet = cfg.get("panel_tailnet", False)
    if type(panel_tailnet) is not bool:
        raise ConfigError("panel_tailnet must be true or false")
    if panel_tailnet and bind not in LOOPBACK:
        raise ConfigError("panel_tailnet needs bind 127.0.0.1 behind Tailscale Serve: on any other "
                          "address a client could send Tailscale-User-Login itself and skip the key")
    if "cap_lookahead" in cfg:
        raise ConfigError("cap_lookahead is renamed pace_lookahead: rename the key in config.json")
    pace_lookahead = duration_s(cfg.get("pace_lookahead", "12h"))
    if pace_lookahead is None:
        raise ConfigError("pace_lookahead must be an integer and one unit m, h or d, for example \"12h\"")
    model = cfg.get("refresh_idle_model", "claude-haiku-4-5")
    if not isinstance(model, str) or not model.strip():
        raise ConfigError("refresh_idle_model must be a nonempty model name string")
    return {
        "CFG": cfg,
        "BIND": bind,
        "PORT": cfg.get("port", 8484),
        "RELOAD_POLL_S": resource_limit(cfg, "reload_poll_s", 5, allow_zero=True),  # 0 = off
        "DEVICE_BY_KEY": {v: k for k, v in cfg["devices"].items()},
        "ACCOUNTS": {a["name"]: a for a in cfg["accounts"]},
        "MAX_REQUEST_BYTES": resource_limit(cfg, "max_request_bytes", 32 * 1024 * 1024),
        "MAX_RESPONSE_BYTES": resource_limit(cfg, "max_response_bytes", 32 * 1024 * 1024),
        "MAX_CONCURRENT_REQUESTS": resource_limit(cfg, "max_concurrent_requests", 0, allow_zero=True),
        "AUDIT_CACHE_RECORDS": resource_limit(cfg, "audit_cache_records", 100_000),
        "AUDIT_CACHE_BYTES": resource_limit(cfg, "audit_cache_bytes", 32 * 1024 * 1024),
        "AUDIT_MAX_BYTES": resource_limit(cfg, "audit_max_bytes", 16 * 1024 * 1024, minimum=4096),
        "AUDIT_BACKUPS": resource_limit(cfg, "audit_backups", 4),
        "THRESHOLD": cfg.get("switch_threshold", 0.8),
        "THRESHOLD_7D": cfg.get("switch_threshold_7d", 0.98),
        # "consume-first" / "least-used" / "most-weekly-left" / "weekly-pace"
        "STRATEGY": cfg.get("strategy", "consume-first"),
        "COOLDOWN_S": cfg.get("switch_cooldown_s", 300),
        "SWITCH_MARGIN": cfg.get("switch_margin", 0.05),
        "SWITCH_MARGIN_7D": cfg.get("switch_margin_7d", 0.03),  # most-weekly-left hysteresis
        "PACE_RATIO": cfg.get("pace_ratio", 1.25),  # weekly-pace hysteresis (multiplicative)
        "RESERVE_SESSIONS": cfg.get("reserve_sessions", 1),  # weekly-pace: fallbacks to keep
        "RESERVE_HEADROOM": cfg.get("reserve_headroom", 0.03),  # room a fallback needs in both windows
        "CF_MARGIN_S": cfg.get("consume_first_margin_s", 3600),
        # Above the switch thresholds an account is held in reserve, not dropped: it
        # stays usable up to HARD_LIMIT (Anthropic rejects at 1.0) for emergencies
        # (nothing else under its thresholds) and is spent on purpose once the window
        # holding it there resets within the spend horizon (use it or lose it).
        "HARD_LIMIT": cfg.get("hard_limit", 1.0),
        "RESERVE_SPEND_5H_S": cfg.get("reserve_spend_5h_s", 3600),
        "RESERVE_SPEND_7D_S": cfg.get("reserve_spend_7d_s", 43200),
        "HOLD_MAX_S": cfg.get("hold_max_s", 0),  # 0 = return 429 when every account is spent
        "REFRESH_IDLE_S": resource_limit(cfg, "refresh_idle_s", 1800, allow_zero=True),  # 0 = off
        "PANEL_TAILNET": panel_tailnet,
        "PACE_LOOKAHEAD_S": pace_lookahead,
        "REFRESH_IDLE_MODEL": model,
        # Cheapest call that returns the unified headers on a setup-token: /api/oauth/usage
        # needs the user:profile scope and count_tokens omits them. A model the plan
        # can't use answers 429 with no headers, so set refresh_idle_model when the
        # default retires.
        "REFRESH_BODY": json.dumps({"model": model, "max_tokens": 1,
                                    "messages": [{"role": "user", "content": "."}]}).encode(),
        # A closed/free account (403/404) is marked bad and skipped. The mark must not be
        # permanent (a plan can be renewed, a block lifted), so an account is reconsidered
        # BAD_RECOVER_S after it was last marked bad, doubling each time it refuses again
        # (bad_count), capped at BAD_RECOVER_MAX_S, and only a 200 fully clears it.
        # 0 = never reconsider.
        "BAD_RECOVER_S": cfg.get("bad_recover_s", 600),
        "BAD_RECOVER_MAX_S": cfg.get("bad_recover_max_s", 86400),
        "PRICES": cfg.get("prices_per_mtok", DEFAULT_PRICES),
        "ALERT_CFG": {**DEFAULT_ALERTS, **cfg.get("alerts", {})},
    }


try:
    _cfg, CONFIG_FILES = load_config()
    globals().update(settings(_cfg))
except ConfigError as exc:
    raise SystemExit(str(exc))

# Settings fixed when the process starts: a reload validates them but leaves
# them alone and reports the config key in CONFIG_STATUS["restart_needed"].
# MAX_CONCURRENT_REQUESTS is captured by the middleware, the listen address and
# port by the server, RELOAD_POLL_S by the watcher task; the rest are deployment
# limits that the Debian unit pins through environment variables.
RESTART_ONLY = {
    "BIND": "bind", "PORT": "port", "RELOAD_POLL_S": "reload_poll_s",
    "MAX_REQUEST_BYTES": "max_request_bytes", "MAX_RESPONSE_BYTES": "max_response_bytes",
    "MAX_CONCURRENT_REQUESTS": "max_concurrent_requests",
    "AUDIT_CACHE_RECORDS": "audit_cache_records", "AUDIT_CACHE_BYTES": "audit_cache_bytes",
    "AUDIT_MAX_BYTES": "audit_max_bytes", "AUDIT_BACKUPS": "audit_backups",
}
CONFIG_STATUS = {"loaded_at": time.time(), "last_reload": None, "error": None,
                 "restart_needed": []}


def watched_files() -> tuple:
    """The files the running config came from, stamped as they are now."""
    return tuple((path, file_stamp(path)) for path, _ in CONFIG_FILES)


def reload_config(reason: str) -> None:
    """Re-read config.json and the token files and apply them. Any problem keeps
    the config already running and is reported in CONFIG_STATUS and on stderr.
    Runs as a plain callback on the event loop, so a request sees either all of
    the old settings or all of the new ones; a request already sent keeps the
    account it started on."""
    global CONFIG_FILES
    now = time.time()
    CONFIG_STATUS["last_reload"] = now
    before = watched_files()  # stamped first: an edit during the load is seen again
    try:
        cfg, files = load_config()
        new = settings(cfg)
        if new["PANEL_TAILNET"] and BIND not in LOOPBACK:
            # BIND is the address actually listened on; the file's may differ.
            raise ConfigError(f"panel_tailnet needs the proxy to listen on loopback, "
                              f"and it is listening on {BIND}; restart first")
    except (ConfigError, OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        CONFIG_FILES = before  # do not retry or log again until a file changes
        error = str(exc) if isinstance(exc, ConfigError) else f"{type(exc).__name__}: {exc}"
        CONFIG_STATUS["error"] = error
        print(f"config reload ({reason}) failed; keeping the running config: {error}",
              file=sys.stderr)
        return
    restart = [key for name, key in RESTART_ONLY.items() if new.pop(name) != globals()[name]]
    globals().update(new)
    CONFIG_FILES = files
    CONFIG_STATUS.update(loaded_at=now, error=None, restart_needed=restart)
    print(f"config reloaded ({reason}): {len(ACCOUNTS)} accounts, {len(DEVICE_BY_KEY)} devices"
          + (f"; restart to apply: {', '.join(restart)}" if restart else ""), file=sys.stderr)


async def watch_config() -> None:
    while True:
        await asyncio.sleep(RELOAD_POLL_S)
        if watched_files() != CONFIG_FILES:
            reload_config("file change")


class RequestAdmission:
    """Reject overload before body buffering; retain slots through SSE/holds."""

    def __init__(self, app, limit: int):
        self.app, self.limit, self.active = app, limit, 0

    async def __call__(self, scope, receive, send):
        admin = scope.get("method") == "GET" and scope.get("path") in {
            "/rotate/status", "/rotate/stats", "/rotate/panel"}
        if scope["type"] != "http" or not self.limit or admin:
            return await self.app(scope, receive, send)
        # One event loop, with no await between the check and increment.
        if self.active >= self.limit:
            response = JSONResponse({"error": "proxy at capacity; retry later"},
                                    status_code=503, headers={"Retry-After": "5"})
            return await response(scope, receive, send)
        self.active += 1
        try:
            await self.app(scope, receive, send)
        finally:
            self.active -= 1


app.add_middleware(RequestAdmission, limit=MAX_CONCURRENT_REQUESTS)
app.add_middleware(PrivateResponseHeaders)
STORAGE_ERRORS: dict = {}


def storage_error(operation: str, error: Exception) -> None:
    """Keep serving after a full/unavailable data disk, without silent loss."""
    now = time.time()
    prior = STORAGE_ERRORS.get(operation, {})
    if now - prior.get("last_warning", 0) >= 60:
        print(f"warning: {operation} failed ({type(error).__name__}, errno {getattr(error, 'errno', None)}); "
              "continuing with in-memory state; check data storage", file=sys.stderr)
        prior["last_warning"] = now
    prior.update(count=prior.get("count", 0) + 1, last_failure=now)
    STORAGE_ERRORS[operation] = prior


BAD_KEYS = ("bad_status", "last_bad_seen", "bad_count")


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {
        "active_account": CFG["accounts"][0]["name"],
        "accounts": {a["name"]: {} for a in CFG["accounts"]},
        "events": [],
    }


STATE = load_state()


def save_state() -> None:
    # Write-then-rename so a crash mid-write can never leave a truncated
    # state.json (which would refuse to parse on the next start).
    tmp = STATE_PATH.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(STATE, indent=2))
        tmp.replace(STATE_PATH)
    except OSError as exc:
        storage_error("state_write", exc)


def audit(rec: dict) -> None:
    try:
        _write_audit(rec)
    except (OSError, ValueError) as exc:
        storage_error("audit_write", exc)
    roll_up(rec, time.strftime("%Y-%m-%d"))


def _write_audit(rec: dict) -> None:
    rec["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    # A single process writes synchronously, so rollover cannot interleave with
    # another request. Each append reopens the path; external rename also works.
    line = (json.dumps(rec) + "\n").encode("utf-8")
    if len(line) > AUDIT_MAX_BYTES:
        # A client-controlled model/path must not defeat the disk bound.
        rec = dict(rec, model=str(rec.get("model", ""))[:256],
                   path=str(rec.get("path", ""))[:1024], metadata_truncated=True)
        line = (json.dumps(rec) + "\n").encode("utf-8")
    if len(line) > AUDIT_MAX_BYTES:
        raise ValueError("audit_max_bytes is too small for one audit record")
    if AUDIT_PATH.exists() and AUDIT_PATH.stat().st_size + len(line) > AUDIT_MAX_BYTES:
        for n in range(AUDIT_BACKUPS, 0, -1):
            source = AUDIT_PATH if n == 1 else Path(f"{AUDIT_PATH}.{n - 1}")
            if source.exists():
                source.replace(Path(f"{AUDIT_PATH}.{n}"))
    with AUDIT_PATH.open("ab") as f:
        f.write(line)


def utilization(name: str) -> float:
    info = STATE["accounts"].get(name, {})
    now = time.time()
    # A window that has reset no longer counts against the account.
    if info.get("reset_5h") and now >= info["reset_5h"]:
        return 0.0
    return info.get("util_5h", 0.0)


def util_7d(name: str) -> float:
    info = STATE["accounts"].get(name, {})
    if info.get("reset_7d") and time.time() >= info["reset_7d"]:
        return 0.0
    return info.get("util_7d", 0.0)


def priority(name: str) -> int:
    """Lower = preferred. Accounts without a priority share the default tier."""
    return ACCOUNTS[name].get("priority", 100)


def weight(name: str) -> float:
    """Weekly capacity relative to the other accounts (a Max 20x is 4 of a Max 5x)."""
    return ACCOUNTS[name].get("weight", 1)


def pace_lookahead_s(name: str) -> float:
    """Pacing lookahead in seconds: the account's pace_lookahead, where "0m"
    means none, else the top-level one when the account sets none."""
    own = duration_s(ACCOUNTS[name].get("pace_lookahead"))
    return PACE_LOOKAHEAD_S if own is None else own


def cap_allowance(name: str) -> float:
    """Weekly use an account may reach now. Without weekly_pacing this is
    weekly_cap, flat. With it, weekly_cap (1.0 when unset) scaled by how much
    of the week has passed, counted from `lookahead` ahead. It reaches the cap
    at lookahead before the weekly reset and never exceeds it. An unknown or
    passed reset gives the cap."""
    cap = ACCOUNTS[name].get("weekly_cap", 1.0)
    reset, now = STATE["accounts"].get(name, {}).get("reset_7d"), time.time()
    if not ACCOUNTS[name].get("weekly_pacing") or not reset or now >= reset:
        return cap
    lookahead = pace_lookahead_s(name)
    return cap * min(max((now + lookahead - (reset - 604800)) / 604800, 0.0), 1.0)


def cap_ok(name: str) -> bool:
    """Under the weekly allowance, or the account has no cap and no pacing."""
    acct = ACCOUNTS[name]
    return not ("weekly_cap" in acct or acct.get("weekly_pacing")) \
        or util_7d(name) < cap_allowance(name)


def disabled(name: str) -> bool:
    return bool(ACCOUNTS[name].get("disabled"))


def account_bad(name: str) -> bool:
    """A dead account: Anthropic refused a /v1/messages request outright instead
    of returning traffic (403 = closed/permission blocked, 404 = model not
    available on this acct, e.g. a free tier asked to serve a premium model).
    Unlike quota rejection this is not a utilisation header, so it is tracked
    here in state. It stays dead until it answers 200 again or bad_recover_wait()
    elapses since it was last marked bad; after that it is only on probation
    (see on_probation) and gets re-probed when traffic is forced onto it."""
    info = STATE["accounts"].get(name, {})
    if info.get("bad_status") not in (403, 404):
        return False
    wait = bad_recover_wait(name)
    return not wait or time.time() - info.get("last_bad_seen", 0) < wait


def bad_recover_wait(name: str) -> float:
    """How long a bad mark holds: BAD_RECOVER_S, doubled for every repeat refusal
    since the last 200, capped at BAD_RECOVER_MAX_S (0 = forever)."""
    if not BAD_RECOVER_S:
        return 0
    n = max(1, STATE["accounts"].get(name, {}).get("bad_count", 1))
    return min(BAD_RECOVER_S * 2 ** min(n - 1, 30), max(BAD_RECOVER_MAX_S, BAD_RECOVER_S))


def on_probation(name: str) -> bool:
    """Refused a request (403/404) and has not answered 200 since, whether or
    not the bad mark has expired. Such an account is only used when traffic is
    forced off the active one; no voluntary switch moves onto it, so a stale low
    utilisation can't make a strategy keep re-picking a closed account."""
    return STATE["accounts"].get(name, {}).get("bad_status") in (403, 404)


def under_threshold(name: str) -> bool:
    return (not disabled(name) and not account_bad(name) and not rejected(name)
            and cap_ok(name)
            and utilization(name) < THRESHOLD and util_7d(name) < THRESHOLD_7D)


def rejected(name: str) -> bool:
    """Last response said quota rejected, and no window has reset since."""
    info = STATE["accounts"].get(name, {})
    if info.get("status") != "rejected":
        return False
    now, seen = time.time(), info.get("last_seen", 0)
    return not any(info.get(k) and seen < info[k] <= now for k in ("reset_5h", "reset_7d"))


def hard_ok(name: str) -> bool:
    """Can still serve a request at all: not disabled, not a dead/closed account,
    under the hard limit in both windows, not currently rejected, under its
    weekly pacing cap."""
    return (not disabled(name) and not account_bad(name) and not rejected(name)
            and cap_ok(name)
            and utilization(name) < HARD_LIMIT and util_7d(name) < HARD_LIMIT)


def binding_reset(name: str) -> float:
    """Reset time of the window with the least room left, when the account's
    remaining capacity stops being the bottleneck (or is lost, if unused)."""
    info = STATE["accounts"].get(name, {})
    room_5h = HARD_LIMIT - utilization(name)
    room_7d = HARD_LIMIT - util_7d(name)
    key = "reset_5h" if room_5h <= room_7d else "reset_7d"
    return info.get(key) or float("inf")


def relief_ts(name: str) -> float:
    """When a blocked account can serve again: the latest reset among the windows
    at the hard limit (a 5h reset doesn't help a week-exhausted account). A
    rejected account whose stored utilisation reads lower falls back to its
    soonest future reset. inf = unknown."""
    info, now = STATE["accounts"].get(name, {}), time.time()
    over = [k for k, u in (("reset_5h", utilization(name)), ("reset_7d", util_7d(name)))
            if u >= HARD_LIMIT]
    if over:
        return max(info.get(k) or float("inf") for k in over)
    future = [info[k] for k in ("reset_5h", "reset_7d") if info.get(k) and info[k] > now]
    return min(future) if future else float("inf")


def reserve_spendable(name: str) -> bool:
    """In the reserve band (over a threshold, under the hard limit) and every
    window holding it there resets within its spend horizon: that reserve is
    about to be lost unused, so spend it now."""
    if not hard_ok(name) or under_threshold(name):
        return False
    info, now = STATE["accounts"].get(name, {}), time.time()
    for util, thr, key, horizon in (
            (utilization(name), THRESHOLD, "reset_5h", RESERVE_SPEND_5H_S),
            (util_7d(name), THRESHOLD_7D, "reset_7d", RESERVE_SPEND_7D_S)):
        if util >= thr and not (info.get(key) and info[key] - now <= horizon):
            return False
    return True


def usable(name: str) -> bool:
    """Normal traffic may go here: under both thresholds, or reserve that is
    about to expire."""
    return under_threshold(name) or reserve_spendable(name)


def rank(name: str, device: str | None) -> tuple[int, int]:
    """Sort key for preference: `priority` tiers come first, and within a tier the
    accounts reserved for this device come before the rest. With no device, only
    priority counts."""
    return (priority(name), 0 if device and device in owners(name) else 1)


def owners(name: str) -> list[str]:
    """Device names this account is reserved for; empty if every device may use it."""
    value = ACCOUNTS[name].get("private_to") or []
    return [value] if isinstance(value, str) else list(value)


def available_to(name: str, device: str | None) -> bool:
    """A shared account (no private_to) serves any device; a private one serves
    only its owners. device=None means no scoping: everything is available (the
    device-less rotation logic and the offline tests)."""
    own = owners(name)
    return not own or device is None or device in own


def device_current(device: str | None) -> str | None:
    """The account a device is riding. device=None is the global active_account.
    A named device has its own pointer, so a private account can pin one device
    while every other device keeps rotating through the shared pool. A device
    with no pointer yet inherits the global active account if it may use it,
    otherwise it takes its own best pick, so it never starts on someone else's
    private account. A device that owns an account always takes its own pick,
    so it starts on that account rather than the global one."""
    if not device:
        return STATE.get("active_account")
    pointed = STATE.get("device_accounts", {}).get(device)
    if pointed:
        return pointed
    active = STATE.get("active_account")
    if (active in ACCOUNTS and available_to(active, device)
            and not any(device in owners(n) for n in ACCOUNTS)):
        return active
    return pick_account(device)


def set_device_current(device: str | None, account: str) -> None:
    STATE["active_account"] = account  # doubles as the display value
    if device:
        STATE.setdefault("device_accounts", {})[device] = account


def weekly_reset(name: str) -> float:
    return STATE["accounts"].get(name, {}).get("reset_7d") or float("inf")


def weekly_pace(name: str) -> float:
    """Share of the weekly quota that must be used per hour from now on to not
    lose any of it: weight * (1 - weekly util) / hours until the weekly reset.
    A capped account counts (weekly_cap - weekly util) as its quota left.
    Higher = more perishable. An unknown or passed reset counts as a full week;
    the hours are floored at 1 so the score stays finite near the reset."""
    left = max(0.0, ACCOUNTS[name].get("weekly_cap", 1.0) - util_7d(name))
    reset = STATE["accounts"].get(name, {}).get("reset_7d")
    now = time.time()
    hours = (reset - now) / 3600 if reset and reset > now else 168.0
    return weight(name) * left / max(hours, 1.0)


def fresh_session(name: str) -> bool:
    """5h window not started (or already reset), a session to rotate into."""
    return utilization(name) < 0.01


def can_fall_back_to(name: str) -> bool:
    """Somewhere to rotate into: usable, with real room left in both windows."""
    return (usable(name)
            and THRESHOLD - utilization(name) >= RESERVE_HEADROOM
            and THRESHOLD_7D - util_7d(name) >= RESERVE_HEADROOM)


def cap_relief_ts(name: str) -> float:
    """When the weekly cap of an account that fails cap_ok lifts. Flat: at the
    weekly reset. Paced: when the rising allowance passes util_7d, no earlier
    than now and no later than the reset; at or over the cap, the reset. An
    unknown or passed reset, or weekly_cap 0, gives inf (no known relief)."""
    cap = ACCOUNTS[name].get("weekly_cap", 1.0)
    reset, now = STATE["accounts"].get(name, {}).get("reset_7d"), time.time()
    if not reset or reset <= now or cap <= 0:
        return float("inf")
    used = util_7d(name)
    if not ACCOUNTS[name].get("weekly_pacing") or used >= cap:
        return reset
    lifts = reset - 604800 - pace_lookahead_s(name) + used * 604800 / cap
    return min(max(lifts, now), reset)


def earliest_relief(device: str | None = None) -> float:
    """Soonest moment an account this device may use can serve again (for
    hold-until-reset and Retry-After): a window reset, or a weekly cap lifting.
    Disabled and dead accounts and other devices' private accounts do not
    count. device=None means no scoping."""
    now = time.time()
    resets = []
    for n in ACCOUNTS:
        if disabled(n) or account_bad(n) or not available_to(n, device):
            continue
        relief = relief_ts(n)
        if not cap_ok(n):
            spent = rejected(n) or utilization(n) >= HARD_LIMIT or util_7d(n) >= HARD_LIMIT
            relief = max(relief, cap_relief_ts(n)) if spent else cap_relief_ts(n)
        resets.append(relief)
    resets = [r for r in resets if now < r < float("inf")]
    return min(resets) if resets else now + 300


def pick_account(device: str | None = None) -> str | None:
    """Usable = not disabled, both windows under their thresholds (a passed
    reset counts as 0), and available to this device: a private account is only
    a candidate for the device it is reserved for, in every tier below, so a
    device with nothing else left gets None rather than another's private
    account. Tiers come first: a lower `priority` beats a higher one, and within a
    tier an account reserved for this device beats the shared ones (see rank());
    the strategy orders accounts within that.

    consume-first (default): burn the usable account whose weekly window resets
    soonest; weekly quota is use-it-or-lose-it, so spend the perishable one.
    least-used: lowest 5h utilization.
    most-weekly-left: lowest weekly utilization (most weekly quota left), among
    accounts that still have 5h session room; ties broken by lower 5h use.
    weekly-pace: highest weekly_pace(), the account whose remaining weekly
    quota is likeliest to expire unused; ties broken by lower 5h use.
    Within a tier, reserve about to expire (reserve_spendable) goes first,
    soonest reset first, it is lost otherwise.
    Emergency (nothing usable): the reserve band, any hard_ok account, the one
    whose binding window resets soonest, so traffic runs to the real limit
    instead of stopping at the thresholds.
    If nothing is hard_ok: the non-disabled account that can serve again soonest
    (relief_ts): a week-exhausted account counts from its weekly reset, since a
    5h reset doesn't help it. An account over its weekly_cap allowance ranks
    after every account that is not.
    """
    mine = [n for n in ACCOUNTS if available_to(n, device)]
    under = [n for n in mine if usable(n)]
    if under:
        if STRATEGY == "consume-first":
            key = lambda n: (weekly_reset(n), utilization(n))
        elif STRATEGY == "most-weekly-left":
            key = lambda n: (util_7d(n), utilization(n))
        elif STRATEGY == "weekly-pace":
            key = lambda n: (-weekly_pace(n), utilization(n))
        else:
            key = lambda n: (utilization(n),)
        return min(under, key=lambda n: (
            on_probation(n),  # a known-refusing account only as a last resort
            rank(n, device),
            (0, binding_reset(n)) if reserve_spendable(n) else (1, 0),
            key(n)))
    reserve = [n for n in mine if hard_ok(n)]
    if reserve:
        return min(reserve, key=lambda n: (on_probation(n), rank(n, device), binding_reset(n)))
    pool = ([n for n in mine if not disabled(n) and not account_bad(n)]
            or [n for n in mine if not disabled(n)])
    if not pool:
        return None
    return min(pool, key=lambda n: (not cap_ok(n), relief_ts(n), rank(n, device)))


def record_switch(frm: str, to: str, reason: str, now: float,
                  device: str | None = None) -> None:
    """Make `to` the active account (for `device`, when given) and log the
    switch event. Caller holds state_lock and saves state."""
    set_device_current(device, to)
    STATE["last_switch_ts"] = now
    STATE["events"].append({
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "type": "switch",
        "from": frm, "to": to,
        "reason": reason,
        "util": utilization(frm),
    })
    STATE["events"] = STATE["events"][-200:]


async def note_response(account: str, headers: httpx.Headers, status: int,
                        bad: bool = False, device: str | None = None) -> str | None:
    """Update account state from unified rate-limit headers; maybe switch.
    device: whose pointer a switch moves, and whose pool it may pick from.

    bad: True when this account is a closed/free account (403/404 on messages):
    it is marked unusable and a hard switch away is forced, so the very next
    request (and a mid-stream retry) lands on a working account.

    Returns what happened: "burst" (per-minute 429, paced, no rotation),
    "switched" (active account changed, retry can ride it), "exhausted"
    (quota gone and nowhere better to go), or None.
    """
    def _f(h):
        v = headers.get(h)
        return float(v) if v is not None else None

    async with state_lock:
        if account not in ACCOUNTS:
            return None  # the config dropped it while this request was in flight
        info = STATE["accounts"].setdefault(account, {})
        for key, header in (
            ("util_5h", "anthropic-ratelimit-unified-5h-utilization"),
            ("util_7d", "anthropic-ratelimit-unified-7d-utilization"),
            ("reset_5h", "anthropic-ratelimit-unified-5h-reset"),
            ("reset_7d", "anthropic-ratelimit-unified-7d-reset"),
        ):
            val = _f(header)
            if val is not None:
                info[key] = val
        if headers.get("anthropic-ratelimit-unified-status"):
            info["status"] = headers["anthropic-ratelimit-unified-status"]
        now = time.time()
        info["last_seen"] = now

        if status == 200 and info.get("bad_status"):
            # A successful reply means the account can serve again; clear the
            # closed/free marker so it is reconsidered for rotation.
            for k in BAD_KEYS:
                info.pop(k, None)

        quota_hit = info.get("status") == "rejected" or (
            status == 429 and (utilization(account) >= 1.0 or util_7d(account) >= 1.0))
        if bad:
            # Closed/free account refused the request outright (403/404). Mark it
            # dead so pick_account skips it, and force a hard switch below.
            # A repeat refusal (re-probe of an expired mark) doubles the wait.
            info["bad_count"] = info.get("bad_count", 0) + 1 if info.get("bad_status") else 1
            info["bad_status"] = status
            info["last_bad_seen"] = now
            quota_hit = True  # hard stop: immediately rotate to a working account
        if status == 429 and not quota_hit:
            # Per-minute burst limit, not spent quota. Rotating would just move
            # the burst to the next account and drop its warm prompt cache,
            # pace this account briefly instead.
            try:
                pace = min(float(headers.get("retry-after", "")), 60.0)
            except ValueError:
                pace = 15.0
            info["pace_until"] = now + pace
            save_state()
            return "burst"

        verdict = None
        if device_current(device) != account:
            # Another request already rotated; a retry rides the new active.
            verdict = "switched" if quota_hit else None
        else:
            cooled = now - STATE.get("last_switch_ts", 0) >= COOLDOWN_S
            over = not usable(account)  # expiring reserve is being spent on purpose
            nxt = pick_account(device)
            reason = None
            if nxt is not None and nxt != account:
                if bad:
                    # Only rotate to an account that can actually serve now.
                    # If every other account is also dead (all bad), stay put and
                    # surface the refusal instead of bouncing between dead ones.
                    reason = "account_unusable" if hard_ok(nxt) else None
                elif quota_hit:
                    reason = "quota_exhausted"  # hard stop: no cooldown, no margin
                elif disabled(account):
                    reason = "account_disabled"  # hard rule: leave immediately
                elif not cap_ok(account):
                    reason = "weekly_cap"  # over its pacing allowance: no cooldown, no margin
                elif over and cooled and (usable(nxt) or not hard_ok(account)):
                    # Hysteresis: only move to a meaningfully better account, so
                    # two accounts hovering at the line never ping-pong. A move
                    # to a preferred tier, off a week-exhausted account, or onto
                    # expiring reserve skips the margin. If nxt is itself only
                    # reserve (emergency), stay put: the active account keeps
                    # its warm cache until it is actually rejected.
                    if (util_7d(account) >= THRESHOLD_7D
                            or rank(nxt, device) < rank(account, device)
                            or reserve_spendable(nxt)
                            or utilization(nxt) <= utilization(account) - SWITCH_MARGIN):
                        reason = f"utilization>={THRESHOLD}"
                elif cooled and usable(nxt) and not over and not on_probation(nxt):
                    # Voluntary moves never land on an account that last refused
                    # outright; it gets re-probed only when traffic is forced.
                    if rank(nxt, device) < rank(account, device):
                        # A preferred account has room again: pull traffic back.
                        reason = "priority_recovery"
                    elif reserve_spendable(nxt) and not reserve_spendable(account):
                        # Another account's last few % reset soon: spend them
                        # before they are lost; come back when it is rejected.
                        reason = "reserve_expiring"
                    elif (STRATEGY == "consume-first"
                          and weekly_reset(nxt) < weekly_reset(account) - CF_MARGIN_S):
                        # Use-it-or-lose-it: proactively jump to an account whose
                        # weekly window expires clearly sooner than the active one's.
                        reason = "consume_first"
                    elif (STRATEGY == "most-weekly-left"
                          and util_7d(nxt) <= util_7d(account) - SWITCH_MARGIN_7D):
                        # Proactively move to the account with clearly more weekly
                        # quota left; the margin stops near-equal accounts churning
                        # (each switch costs the warm prompt cache).
                        reason = "most_weekly_left"
                    elif (STRATEGY == "weekly-pace"
                          and weekly_pace(nxt) >= weekly_pace(account) * PACE_RATIO
                          and (not fresh_session(nxt)
                               or sum(can_fall_back_to(n) for n in ACCOUNTS if n != nxt)
                               >= RESERVE_SESSIONS)):
                        # Proactively move to clearly more perishable quota, but a
                        # voluntary move must not start a fresh 5h window unless
                        # somewhere else (the account being left counts) still has
                        # room to rotate into when the new session runs out. Forced
                        # switches above may always use the reserve.
                        reason = "weekly_pace"
            if reason:
                record_switch(account, nxt, reason, now, device)
                verdict = "switched"
            elif quota_hit:
                verdict = "exhausted"
        save_state()
        return verdict


_REFRESH_TRIED: dict = {}


async def refresh_idle_accounts() -> None:
    """Usage headers only arrive with traffic, so an idle account's numbers go
    stale and quota spent outside the proxy (claude.ai, a direct Claude Code
    login) stays invisible until a switch lands on it. Send each account not
    seen or tried for REFRESH_IDLE_S a 1-token request and record its headers."""
    now = time.time()
    for name in ACCOUNTS:
        if name not in ACCOUNTS:
            continue  # a reload replaced ACCOUNTS during an earlier probe's await
        last = max(STATE["accounts"].get(name, {}).get("last_seen", 0), _REFRESH_TRIED.get(name, 0))
        if disabled(name) or now - last < REFRESH_IDLE_S:
            continue
        _REFRESH_TRIED[name] = now
        try:
            r = await client.post("/v1/messages", content=REFRESH_BODY, headers={
                "authorization": f"Bearer {ACCOUNTS[name]['token']}",
                "anthropic-version": "2023-06-01", "anthropic-beta": "oauth-2025-04-20",
                "content-type": "application/json"})
        except httpx.HTTPError as exc:
            print(f"warning: usage refresh of {name} failed ({type(exc).__name__})", file=sys.stderr)
            continue
        if "anthropic-ratelimit-unified-status" in r.headers:
            await note_response(name, r.headers, r.status_code)
        else:
            # note_response would read a headerless 429 as a burst and pace the account.
            print(f"warning: usage refresh of {name}: HTTP {r.status_code} without usage headers",
                  file=sys.stderr)


ALERT_SENT: dict = {}  # alert key -> when it was last posted to the webhook
webhook_client = httpx.AsyncClient(timeout=httpx.Timeout(5.0))


async def send_alert_webhook() -> None:
    """POST each current panel alert to alerts.webhook_url, at most once an hour
    per alert. An alert is its rule and subject (its key), so a count that
    changes in the text does not post it again. A failure is reported and not
    retried. The URL is a secret, so it is never printed."""
    url = ALERT_CFG.get("webhook_url")
    if not isinstance(url, str) or not url:
        return
    now = time.time()
    for key in [k for k, sent in ALERT_SENT.items() if now - sent >= 3600]:
        del ALERT_SENT[key]
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    for alert in aggregate_audit()["alerts"]:
        if alert["key"] in ALERT_SENT:
            continue
        ALERT_SENT[alert["key"]] = now
        try:
            r = await webhook_client.post(url, json={
                "text": alert["text"], "level": alert["level"], "ts": stamp})
            r.raise_for_status()
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            print(f"warning: alert webhook failed ({type(exc).__name__})", file=sys.stderr)


async def refresh_loop() -> None:
    while True:
        if REFRESH_IDLE_S:
            await refresh_idle_accounts()
        await send_alert_webhook()
        record_history(time.time())
        save_rollup()
        await asyncio.sleep(60)


HISTORY_PATH = STATE_PATH.with_name("usage_history.json")
HISTORY_DAYS, SAMPLE_S, PROJECT_DAYS = 14, 1800, 7.5


def load_history() -> list:
    try:
        history = json.loads(HISTORY_PATH.read_text())
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        # The chart is disposable; a damaged file must not stop the proxy.
        print(f"warning: ignoring {HISTORY_PATH} ({type(exc).__name__})", file=sys.stderr)
        return []
    return history if isinstance(history, list) else []


HISTORY = load_history()


def weekly_snapshot() -> dict:
    return {n: [round(util_7d(n), 4), STATE["accounts"][n]["reset_7d"]] for n in ACCOUNTS
            if not disabled(n) and STATE["accounts"].get(n, {}).get("reset_7d")}


def record_history(now: float) -> None:
    """One weekly-usage snapshot of every enabled account per SAMPLE_S, kept HISTORY_DAYS."""
    if HISTORY and now - HISTORY[-1]["ts"] < SAMPLE_S:
        return
    snapshot = weekly_snapshot()
    if not snapshot:
        return
    HISTORY.append({"ts": round(now), "accounts": snapshot})
    while HISTORY[0]["ts"] < now - HISTORY_DAYS * 86400:
        HISTORY.pop(0)
    tmp = HISTORY_PATH.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(HISTORY))
        tmp.replace(HISTORY_PATH)
    except OSError as exc:
        storage_error("history_write", exc)


def weekly_series(now: float) -> list:
    """[ts, used, capacity, spent] in % of one weekly quota. `used` sums each enabled
    account's current weekly usage (weighted), so it drops at that account's reset;
    `capacity` sums the weights. `spent` adds up the weekly rises seen since the first
    sample, so the run-out rate can be measured without resets cancelling usage."""
    points, spent, seen = [], 0.0, {}
    for sample in HISTORY + [{"ts": now, "accounts": weekly_snapshot()}]:
        used = cap = 0.0
        for name, (u, reset) in sample["accounts"].items():
            if name not in ACCOUNTS or disabled(name):
                continue
            last_reset, top = seen.get(name, (reset, u))  # first sight is a baseline
            if reset > last_reset + 3600:  # a new weekly window: all of it is new usage
                spent, top = spent + weight(name) * u, u
            elif u > top:
                spent, top = spent + weight(name) * (u - top), u
            seen[name] = (max(reset, last_reset), top)
            used += weight(name) * u
            cap += weight(name)
        points.append([round(sample["ts"]), round(used * 100, 1), round(cap * 100, 1), round(spent * 100, 1)])
    return points


def weekly_outlook(now: float) -> dict:
    """The chart's series plus a PROJECT_DAYS projection: spend the last 24 h rate
    consume-first (soonest weekly reset first); while every enabled account is at
    100% usage pauses until the next reset. `eta` is the first such moment."""
    points = weekly_series(now)
    out = {"series": [p[:3] for p in points], "projection": [], "rate_per_day": None,
           "eta": None, "back": None, "back_account": None,
           "capacity": round(sum(weight(n) for n in weekly_snapshot()) * 100)}
    day = [p for p in points if p[0] >= now - 86400]
    # Usage cannot rise while every account is spent, so time at the limit would
    # understate demand: count only the stretches with quota left (as the panel shades).
    span = sum(b[0] - a[0] for a, b in zip(day, day[1:]) if a[2] - a[1] > 0.1)
    if span < 3600:
        out["note"] = "not enough history with quota left yet to measure a rate"
        return out
    rate = (day[-1][3] - day[0][3]) / span  # % of one weekly quota per second
    out["rate_per_day"] = round(rate * 86400, 1)
    accts = {n: [u * 100, reset] for n, (u, reset) in weekly_snapshot().items()}
    if rate <= 0 or not accts:
        out["note"] = "weekly usage did not rise over the last 24 h"
        return out
    for a in accts.values():
        while a[1] <= now:  # reset passed with no reading since: assume the next week follows
            a[1] += 7 * 86400

    def left():
        return sum(weight(n) * (100 - u) for n, (u, _) in accts.items())

    def point(t):
        return [round(t), round(sum(weight(n) * u for n, (u, _) in accts.items()), 1), out["capacity"]]

    t, horizon = now, now + PROJECT_DAYS * 86400
    proj = [point(t)]
    while t < horizon:
        nxt = min(accts, key=lambda n: accts[n][1])
        reset = accts[nxt][1]
        end = min(reset, horizon)
        empty = t + left() / rate
        if empty < end:  # everything spent before the next reset: flat until it
            for a in accts.values():
                a[0] = 100.0
            proj.append(point(empty))
            if out["eta"] is None:
                out.update(eta=round(empty), back=round(reset), back_account=nxt)
        else:
            spend = rate * (end - t)
            for n in sorted(accts, key=lambda n: accts[n][1]):
                take = min(spend, weight(n) * (100 - accts[n][0]))
                accts[n][0] += take / weight(n)
                spend -= take
        proj.append(point(end))
        if end == reset:
            accts[nxt] = [0.0, reset + 7 * 86400]
            proj.append(point(end))
        t = end
    out["projection"] = proj
    return out


def usage_from_sse(text: str, sink: dict) -> None:
    for line in text.splitlines():
        if line.startswith("data:") and '"usage"' in line:
            try:
                obj = json.loads(line[5:].strip())
            except json.JSONDecodeError:
                continue
            usage = obj.get("usage") or obj.get("message", {}).get("usage")
            if usage:
                for k in ("input_tokens", "output_tokens",
                          "cache_read_input_tokens", "cache_creation_input_tokens"):
                    if usage.get(k):
                        sink[k] = max(sink.get(k, 0), usage[k])


def summarize_usage(usage: dict | None) -> dict | None:
    if not usage:
        return None
    return {k: usage.get(k, 0) for k in (
        "input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")}


@app.get("/rotate/status")
async def rotate_status(request: Request):
    if not panel_allowed(request):
        return JSONResponse({"error": "unknown device key"}, status_code=401)
    now = time.time()
    accounts = {}
    for name in ACCOUNTS:
        info = dict(STATE["accounts"].get(name, {}))
        info["effective_util_5h"] = utilization(name)
        info["effective_util_7d"] = util_7d(name)
        info["priority"] = priority(name)
        info["disabled"] = disabled(name)
        info["private_to"] = owners(name) or None
        info["weekly_cap"] = ACCOUNTS[name].get("weekly_cap")
        info["weekly_pacing"] = bool(ACCOUNTS[name].get("weekly_pacing"))
        info["weekly_allowance"] = round(cap_allowance(name), 4) \
            if info["weekly_cap"] is not None or info["weekly_pacing"] else None
        info["account_bad"] = account_bad(name)
        info["on_probation"] = on_probation(name)
        if account_bad(name) and bad_recover_wait(name):
            info["bad_recover_in_min"] = max(0, round(
                (info.get("last_bad_seen", 0) + bad_recover_wait(name) - now) / 60))
        if info.get("reset_5h"):
            info["reset_5h_in_min"] = max(0, round((info["reset_5h"] - now) / 60))
        if info.get("reset_7d"):
            info["reset_7d_in_h"] = max(0, round((info["reset_7d"] - now) / 3600, 1))
        info["weekly_pace_pct_per_h"] = round(weekly_pace(name) * 100, 3)
        info["fresh_session"] = fresh_session(name)
        info["hard_ok"] = hard_ok(name)
        info["reserve_spendable"] = reserve_spendable(name)
        accounts[name] = info
    return {
        "active_account": STATE["active_account"],
        "switch_threshold": THRESHOLD,
        "switch_threshold_7d": THRESHOLD_7D,
        "hard_limit": HARD_LIMIT,
        "strategy": STRATEGY,
        "hold_max_s": HOLD_MAX_S,
        "accounts": accounts,
        "recent_events": STATE["events"][-10:],
        "storage_errors": STORAGE_ERRORS,
        "config": CONFIG_STATUS,
        "weekly": weekly_outlook(now),
    }


def panel_allowed(request: Request) -> bool:
    # Tailscale Serve sets Tailscale-User-Login for tailnet users and strips client
    # copies; startup enforces a loopback bind, so only Serve (or a local process) can add it.
    return ((PANEL_TAILNET and bool(request.headers.get("tailscale-user-login")))
            or device_from_request(request) is not None)


def device_from_request(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    key = auth[7:] if auth.lower().startswith("bearer ") else request.headers.get("x-api-key", "")
    if not key:
        key = request.query_params.get("key", "")
    if not key:
        return None
    return DEVICE_BY_KEY.get(key)


async def bounded_body(request: Request) -> bytes:
    """Bound actual bytes, including bodies without a Content-Length header."""
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_REQUEST_BYTES:
            raise HTTPException(status_code=413, detail="request body too large")
        body.extend(chunk)
    return bytes(body)


async def bounded_response(upstream: httpx.Response) -> bytes:
    """Limit buffered upstream responses; streaming responses stay streaming."""
    body = bytearray()
    try:
        async for chunk in upstream.aiter_bytes(chunk_size=65536):
            if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                raise HTTPException(status_code=502, detail="upstream response too large")
            body.extend(chunk)
    finally:
        await upstream.aclose()
    return bytes(body)


DEFAULT_PRICE = [5.0, 25.0]


def price_for(model: str | None) -> list[float]:
    model = model if isinstance(model, str) else None
    for pat, p in PRICES.items():
        if model and pat in model:
            return p
    return DEFAULT_PRICE


def api_cost(model: str | None, usage: dict) -> float:
    """API-equivalent $ for one request: in + out + cache read (0.1x) + cache write (1.25x)."""
    p_in, p_out = price_for(model)
    return (
        usage.get("input_tokens", 0) * p_in
        + usage.get("output_tokens", 0) * p_out
        + usage.get("cache_read_input_tokens", 0) * p_in * 0.1
        + usage.get("cache_creation_input_tokens", 0) * p_in * 1.25
    ) / 1_000_000


# Incremental, bounded tail of the current audit file. Counts become partial
# after rotation or either retention limit; the API/panel explicitly says so.
_AUDIT_CACHE: dict = {"offset": 0, "records": deque(), "sig": b""}


def _tail_audit() -> None:
    import datetime as dt

    cache = _AUDIT_CACHE
    cache.setdefault("bytes", 0)
    cache.setdefault("limited_until", 0)
    now = time.time()
    if not AUDIT_PATH.exists():
        if cache["offset"]:
            cache["limited_until"] = now + 86400
        cache["offset"] = 0
        cache["sig"] = b""
        cache["bytes"] = 0
        cache.pop("identity", None)  # a first file is not a rotation
        cache["records"].clear()
        return
    day_ago = now - 86400
    with AUDIT_PATH.open("rb") as f:
        stat = os.fstat(f.fileno())
        size = stat.st_size
        identity = (stat.st_dev, stat.st_ino)
        head = f.read(64)
        # A shrunken file, or changed leading bytes, means audit.jsonl was
        # rotated or rewritten out from under us; the cached window no
        # longer describes this file, so rebuild from the top. (Size alone
        # can't catch truncate-and-rewrite-bigger; the head fingerprint can.)
        if (cache.get("identity", identity) != identity
                or size < cache["offset"] or not head.startswith(cache["sig"])):
            cache["limited_until"] = now + 86400
            cache["offset"] = 0
            cache["bytes"] = 0
            cache["records"].clear()
        cache["identity"] = identity
        cache["sig"] = head
        # After restart, recent rotated files mean the current log is not the
        # whole 24h history. Retained archives remain available for offline use.
        previous = Path(f"{AUDIT_PATH}.1")
        if previous.exists():
            cache["limited_until"] = max(cache["limited_until"],
                                         previous.stat().st_mtime + 86400)
        if size == cache["offset"]:
            return
        if size - cache["offset"] > AUDIT_CACHE_BYTES:
            cache["offset"] = size - AUDIT_CACHE_BYTES
            cache["limited_until"] = now + 86400
            cache["bytes"] = 0
            cache["records"].clear()
            f.seek(cache["offset"] - 1)
            # Skip a first fragment without reading an unbounded long line.
            begins_mid_line = f.read(1) != b"\n"
        else:
            begins_mid_line = False
        f.seek(cache["offset"])
        chunk = f.read(min(AUDIT_CACHE_BYTES, size - cache["offset"]))
    if begins_mid_line:
        start = chunk.find(b"\n") + 1
        if not start:
            # The tail is one oversized fragment; consume it without caching.
            cache["offset"] += len(chunk)
            return
        cache["offset"] += start
        chunk = chunk[start:]
    # Consume complete lines only; a trailing partial line (an append in
    # flight from a writer outside this process) is left for the next call.
    end = chunk.rfind(b"\n") + 1
    cache["offset"] += end
    for line in chunk[:end].splitlines():
        try:
            rec = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            cache["limited_until"] = now + 86400
            continue
        if not isinstance(rec, dict):
            cache["limited_until"] = now + 86400
            continue
        try:
            rec["_ts"] = dt.datetime.strptime(
                rec.get("ts", ""), "%Y-%m-%dT%H:%M:%S%z").timestamp()
        except (ValueError, TypeError):
            cache["limited_until"] = now + 86400
            continue
        if rec["_ts"] >= day_ago:
            rec["_bytes"] = len(line) + 1
            cache["records"].append(rec)
            cache["bytes"] += rec["_bytes"]
            while (len(cache["records"]) > AUDIT_CACHE_RECORDS
                   or cache["bytes"] > AUDIT_CACHE_BYTES):
                removed = cache["records"].popleft()
                cache["bytes"] -= removed["_bytes"]
                cache["limited_until"] = max(cache["limited_until"],
                                             removed["_ts"] + 86400)


def bump(bucket, key, usage, cost):
    b = bucket.setdefault(key, {"requests": 0, "input_tokens": 0, "output_tokens": 0,
                                "cache_read": 0, "cache_write": 0, "cost_usd": 0.0})
    b["requests"] += 1
    b["input_tokens"] += usage.get("input_tokens", 0)
    b["output_tokens"] += usage.get("output_tokens", 0)
    b["cache_read"] += usage.get("cache_read_input_tokens", 0)
    b["cache_write"] += usage.get("cache_creation_input_tokens", 0)
    b["cost_usd"] += cost


def aggregate_audit() -> dict:
    """Roll up the last 24h of audit.jsonl by device/model/account, with alerts."""
    now = time.time()
    hour_ago = now - 3600
    day_ago = now - 86400
    by_device: dict = {}
    by_model: dict = {}
    by_account: dict = {}
    device_hour_tokens: dict = {}
    device_hour_reqs: dict = {}
    device_meta: dict = {}
    expensive_hour: dict = {}
    refused_hour: dict = {}

    _tail_audit()
    records = _AUDIT_CACHE["records"]
    while records and records[0]["_ts"] < day_ago:
        _AUDIT_CACHE["bytes"] -= records.popleft().get("_bytes", 0)
    for rec in records:
        # Old logs may contain arbitrary JSON values supplied as `model`.
        model = rec.get("model")
        model = model if isinstance(model, str) else None
        usage = rec.get("usage") or {}
        ts = rec["_ts"]
        cost = api_cost(model, usage)
        dev = rec.get("device", "?")
        meta = device_meta.setdefault(dev, {})
        if ts >= meta.get("last_ts", 0):
            meta.update({"last_ts": ts, "last_model": model,
                         "last_account": rec.get("account")})
        bump(by_device, rec.get("device", "?"), usage, cost)
        bump(by_model, model or "?", usage, cost)
        bump(by_account, rec.get("account", "?"), usage, cost)
        if ts >= hour_ago:
            total = sum(usage.get(k, 0) for k in (
                "input_tokens", "output_tokens", "cache_creation_input_tokens"))
            device_hour_tokens[rec.get("device", "?")] = (
                device_hour_tokens.get(rec.get("device", "?"), 0) + total)
            device_hour_reqs[dev] = device_hour_reqs.get(dev, 0) + 1
            if rec.get("status") == 401:
                acct = rec.get("account", "?")
                refused_hour[acct] = refused_hour.get(acct, 0) + 1
            if any(p in (model or "") for p in ALERT_CFG["expensive_model_patterns"]):
                key = f'{rec.get("device", "?")} / {model}'
                expensive_hour[key] = expensive_hour.get(key, 0) + 1

    limited = now < _AUDIT_CACHE.get("limited_until", 0)
    # Each alert has a stable key (rule and subject) that the webhook dedupes
    # on; its text embeds counts that move.
    alerts = ([{"level": "warn", "key": "partial",
                "text": "Statistics are partial: older history was "
                "rotated, skipped or dropped to stay within resource limits."}] if limited else [])
    for dev, toks in device_hour_tokens.items():
        if toks > ALERT_CFG["device_tokens_per_hour"]:
            alerts.append({"level": "warn", "key": f"device_tokens:{dev}",
                           "text": f"{dev} consumed {toks:,} tokens in the last hour "
                                   f"(threshold {ALERT_CFG['device_tokens_per_hour']:,})"})
    for key, n in expensive_hour.items():
        alerts.append({"level": "info", "key": f"expensive_model:{key}",
                       "text": f"expensive model in use: {key} — {n} requests in the last hour"})
    for acct, n in refused_hour.items():
        alerts.append({"level": "warn", "key": f"token_refused:{acct}",
                       "text": f"{acct}: upstream refused its token (401) {n} times "
                               "in the last hour; renew it with add-account/add-token"})
    for name in ACCOUNTS:
        u = utilization(name)
        if u >= ALERT_CFG["util_warn"]:
            alerts.append({"level": "warn", "key": f"util_5h:{name}",
                           "text": f"{name} 5h window at {u:.0%} (switch threshold {THRESHOLD:.0%})"})

    # Roster covers every registered device, traffic or not.
    devices = {}
    for name, key in CFG["devices"].items():
        meta = device_meta.get(name, {})
        last_ts = meta.get("last_ts")
        devices[name] = {
            "key_suffix": "…" + key[-4:],
            "online": bool(last_ts and now - last_ts < 600),
            "last_seen_min": round((now - last_ts) / 60) if last_ts else None,
            "last_model": meta.get("last_model"),
            "last_account": meta.get("last_account"),
            "req_1h": device_hour_reqs.get(name, 0),
            "tokens_1h": device_hour_tokens.get(name, 0),
        }

    return {"window": "last 24h (partial)" if limited else "last 24h",
            "history_limited": limited, "retained_records": len(records),
            "devices": devices, "by_device": by_device,
            "by_model": by_model, "by_account": by_account, "alerts": alerts}


USAGE_PATH = STATE_PATH.with_name("device_usage.json")
ROLLUP_DAYS = 31
ROLLUP: dict = {"days": {}, "total": {"device": {}, "model": {}}, "since": None}
ROLLUP_DIRTY = False


def roll_up(rec: dict, day: str) -> None:
    """Add one audit record to the per-day and all-time totals by device and model.
    The audit log rotates and the stats cache holds 24 h; this keeps the longer view."""
    global ROLLUP_DIRTY
    model = rec.get("model")
    model = model if isinstance(model, str) else None
    usage = rec.get("usage") or {}
    cost = api_cost(model, usage)
    buckets = ROLLUP["days"].setdefault(day, {"device": {}, "model": {}})
    for kind, key in (("device", str(rec.get("device", "?"))), ("model", model or "?")):
        bump(buckets[kind], key, usage, cost)
        bump(ROLLUP["total"][kind], key, usage, cost)
    ROLLUP["since"] = min(ROLLUP["since"] or day, day)
    ROLLUP_DIRTY = True


def load_rollup() -> None:
    """Load the totals; without a usable file, rebuild them once from the audit
    log and its rotated copies, oldest first."""
    global ROLLUP
    try:
        loaded = json.loads(USAGE_PATH.read_text())
        if isinstance(loaded, dict) and {"days", "total", "since"} <= loaded.keys():
            ROLLUP = loaded
            return
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as exc:
        print(f"warning: rebuilding {USAGE_PATH} from the audit log ({type(exc).__name__})",
              file=sys.stderr)
    for path in [Path(f"{AUDIT_PATH}.{n}") for n in range(AUDIT_BACKUPS, 0, -1)] + [AUDIT_PATH]:
        try:
            with path.open("rb") as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(rec, dict) and isinstance(rec.get("ts"), str):
                        roll_up(rec, rec["ts"][:10])
        except OSError:
            continue


load_rollup()


def save_rollup() -> None:
    global ROLLUP_DIRTY
    if not ROLLUP_DIRTY:
        return
    import datetime as dt
    cutoff = (dt.date.today() - dt.timedelta(days=ROLLUP_DAYS)).isoformat()
    for day in [d for d in ROLLUP["days"] if d < cutoff]:
        del ROLLUP["days"][day]
    tmp = USAGE_PATH.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(ROLLUP))
        tmp.replace(USAGE_PATH)
        ROLLUP_DIRTY = False
    except OSError as exc:
        storage_error("usage_write", exc)


def usage_periods() -> dict:
    """7-day, 30-day and all-time totals by device and model, in calendar days with
    today included, plus each device's daily cost-equivalent over the last 30 days."""
    import datetime as dt
    today = dt.date.today()
    days = [(today - dt.timedelta(days=i)).isoformat() for i in range(29, -1, -1)]

    def total(span):
        out = {"device": {}, "model": {}}
        for day in span:
            for kind, buckets in ROLLUP["days"].get(day, {}).items():
                for key, b in buckets.items():
                    acc = out[kind].setdefault(key, dict.fromkeys(b, 0))
                    for field, v in b.items():
                        acc[field] += v
        return out

    trend = {dev: [round(ROLLUP["days"].get(d, {}).get("device", {}).get(dev, {}).get("cost_usd", 0), 2)
                   for d in days] for dev in ROLLUP["total"]["device"]}
    return {"since": ROLLUP["since"], "7d": total(days[-7:]), "30d": total(days),
            "all": ROLLUP["total"], "trend": trend}


@app.get("/rotate/stats")
async def rotate_stats(request: Request):
    if not panel_allowed(request):
        return JSONResponse({"error": "unknown device key"}, status_code=401)
    return aggregate_audit() | {"periods": usage_periods()}


PANEL_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>claude-rotate</title>
<style>
 :root{color-scheme:dark;--page:#0d0d0d;--surface:#1a1a19;--ink:#f0efec;--ink2:#c3c2b7;--muted:#898781;--grid:#2c2c2a;--axis:#383835;
  --border:rgba(255,255,255,.10);--s1:#3987e5;--s2:#d95926;--track:#0d366b;--good:#0ca30c;--warn:#fab219;--crit:#d03b3b}
 *{box-sizing:border-box} body{margin:0;background:var(--page);color:var(--ink);font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif;-webkit-text-size-adjust:100%;text-size-adjust:100%}
 main{max-width:1180px;margin:0 auto;padding:20px 24px 40px;display:grid;gap:16px}
 header{display:flex;align-items:center;gap:12px;flex-wrap:wrap} h1{font-size:18px;margin:0;font-weight:600}
 .pill{border:1px solid var(--border);border-radius:999px;padding:2px 10px;color:var(--ink2);font-size:13px}
 .dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;vertical-align:1px}
 .spacer{flex:1} .muted{color:var(--muted)} .small{font-size:12px}
 .card{position:relative;background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:16px 18px;overflow-x:auto}
 .card h2{font-size:13px;font-weight:600;color:var(--ink2);margin:0 0 12px;display:flex;align-items:center;gap:10px;flex-wrap:wrap}
 .banner{display:flex;gap:10px;padding:10px 14px;border-radius:10px;border:1px solid var(--border);color:var(--ink2);margin-bottom:8px}
 .banner.warn{border-color:rgba(250,178,25,.35);background:rgba(250,178,25,.06)} .banner.warn b{color:var(--warn)}
 .kpis{display:grid;grid-template-columns:1.6fr repeat(4,1fr);gap:16px}
 .hero .v{font-size:48px;font-weight:600;line-height:1.1;margin:4px 0} .label{color:var(--ink2);font-size:13px}
 .tile .v{font-size:26px;font-weight:600;margin:6px 0 2px}
 .accounts{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(380px,100%),1fr));gap:16px}
 .acct-head{display:flex;align-items:baseline;gap:8px;margin-bottom:12px;flex-wrap:wrap} .acct-head b{font-size:16px}
 .badge{font-size:12px;border-radius:999px;padding:1px 8px;border:1px solid var(--border);color:var(--ink2)}
 .meter-row{display:grid;grid-template-columns:28px 1fr 250px;gap:10px;align-items:center;margin:8px 0}
 .meter{position:relative;height:8px;border-radius:4px;background:var(--track)} .meter i{position:absolute;inset:0 auto 0 0;border-radius:4px}
 .meter .tick{position:absolute;top:-3px;bottom:-3px;width:2px;background:var(--ink2);opacity:.6}
 .legend{display:flex;gap:16px;flex-wrap:wrap;font-size:12px;color:var(--ink2);font-weight:400;margin-left:auto}
 .key{display:inline-block;width:16px;border-top:2px solid;vertical-align:4px;margin-right:6px}
 .key.dash{border-top-style:dashed} .key.band{height:10px;border:0;vertical-align:-1px;background:rgba(208,59,59,.3)}
 .wk{display:flex} .wk svg{display:block} .wk-scroll{flex:1;min-width:0;overflow-x:auto;scrollbar-width:thin;scrollbar-color:var(--axis) transparent} svg text{fill:var(--muted);font-size:11px;font-family:inherit}
 .tip{position:absolute;pointer-events:none;background:#262624;border:1px solid var(--border);border-radius:8px;padding:8px 10px;font-size:12px;min-width:170px;display:none}
 .tip .row{display:flex;justify-content:space-between;gap:12px} .tip .v{color:var(--ink);font-weight:600}
 .sentence{margin-top:8px;color:var(--ink2)}
 table{border-collapse:collapse;width:100%} th,td{padding:8px 10px;border-bottom:1px solid var(--grid);text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
 th{color:var(--muted);font-weight:500;font-size:12px} td:first-child,th:first-child,.l{text-align:left} tr:last-child td{border-bottom:0}
 td .sub{display:block;color:var(--muted);font-size:11px} tr.removed td{color:var(--muted)}
 .seg{display:inline-flex;border:1px solid var(--border);border-radius:8px;overflow:hidden;margin-left:auto}
 .seg button{background:none;border:0;color:var(--ink2);padding:4px 12px;font:inherit;font-size:12px;cursor:pointer} .seg button.on{background:var(--grid);color:var(--ink);font-weight:600}
 .split{display:grid;grid-template-columns:1.3fr 1fr;gap:16px}
 .timeline div{display:flex;gap:12px;padding:6px 0;border-bottom:1px solid var(--grid);color:var(--ink2)} .timeline div:last-child{border-bottom:0}
 .linkbtn{background:none;border:1px solid var(--border);border-radius:6px;color:var(--ink2);font:inherit;font-size:12px;padding:2px 8px;cursor:pointer}
 @media (max-width:900px){.kpis{grid-template-columns:1fr 1fr}.hero{grid-column:1/-1}.split{grid-template-columns:1fr}
  .meter-row{grid-template-columns:28px 1fr;row-gap:4px}.meter-row>span:last-child{grid-column:2}
  #devices,#devices tbody{display:block} #devices tr:first-child{display:none}
  #devices tr{display:grid;grid-template-columns:repeat(4,1fr);gap:8px 12px;padding:12px 0;border-bottom:1px solid var(--grid)} #devices tr:last-child{border-bottom:0}
  #devices td{padding:0;border:0;text-align:left} #devices td:nth-child(n+4):not(:last-child){order:1}
  #devices td[data-l]::before{content:attr(data-l);display:block;color:var(--muted);font-size:11px}}
 @media (max-width:600px){main{padding:12px 12px 32px;gap:12px}.card{padding:14px}#devices tr{grid-template-columns:1fr 1fr}
  th,td{padding:8px 6px} .split :is(th,td):nth-child(4){display:none}}
</style></head><body><main>
<header><h1>claude-rotate</h1><span class="muted" id="host"></span><span id="pills"></span><span class="spacer"></span><span class="muted small" id="upd"></span></header>
<div id="alerts"></div>
<section class="kpis">
 <div class="card hero"><div class="label">Every account spent in</div><div class="v" id="hero">–</div><div class="muted" id="hero-sub"></div></div>
 <div class="card tile"><div class="label">Weekly quota left</div><div class="v" id="k-left">–</div><div class="muted small" id="k-left-sub"></div></div>
 <div class="card tile"><div class="label">Burn rate</div><div class="v" id="k-rate">–</div><div class="muted small">of a weekly quota per day</div></div>
 <div class="card tile"><div class="label">Requests, 24 h</div><div class="v" id="k-req">–</div><div class="muted small" id="k-req-sub"></div></div>
 <div class="card tile"><div class="label">Cost-equivalent, 7 d</div><div class="v" id="k-cost">–</div><div class="muted small" id="k-cost-sub"></div></div>
</section>
<section class="accounts" id="accounts"></section>
<section class="card"><h2>Weekly quota, all accounts <span class="muted" style="font-weight:400">summed weekly usage &middot; 14 days + 7.5-day projection</span>
 <span class="legend"><span><span class="key" style="border-color:var(--s1)"></span>Usage</span><span><span class="key" style="border-color:var(--s2)"></span>Limit</span>
 <span><span class="key dash" style="border-color:var(--muted)"></span>Projected</span><span><span class="key band"></span>&#9940; Every account at its limit</span></span>
 <button class="linkbtn" id="tbl">Table</button></h2>
 <div id="weekly" class="muted">collecting data</div><div class="tip" id="tip"></div></section>
<section class="card"><h2>Devices</h2><table id="devices"></table></section>
<section class="split">
 <div class="card"><h2>By model <span class="seg" id="period"></span></h2><table id="models"></table></div>
 <div class="card"><h2>By account, 24 h</h2><table id="by-account"></table><h2 style="margin-top:20px">Recent switches</h2><div class="timeline" id="events"></div></div>
</section>
<p class="muted small">Cost-equivalent = API list prices (cache read 0.1&times;, write 1.25&times;); actual spend is covered by the subscriptions.</p>
</main>
<script>
const SKEY = 'claude-rotate-key';
let KEY = new URLSearchParams(location.search).get('key');
if (KEY) { try { sessionStorage.setItem(SKEY, KEY) } catch (e) {} }
else { try { KEY = sessionStorage.getItem(SKEY) } catch (e) {} }
const cleanURL = new URL(location.href);
cleanURL.searchParams.delete('key');
history.replaceState(null, '', cleanURL.pathname + cleanURL.search + cleanURL.hash);
const $=id=>document.getElementById(id), H=3600, D=86400, PERIODS={'24 h':'24h','7 d':'7d','30 d':'30d','All time':'all'};
let period='7 d', showTable=false, WK=null;
function esc(value){return String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function fmt(n){n=n||0;return n>=1e6?(n/1e6).toFixed(n>=1e7?0:1)+'M':n>=1e3?(n/1e3).toFixed(n>=1e4?0:1)+'k':''+Math.round(n)}
function money(v){v=v||0;return '$'+(v>=100?Math.round(v).toLocaleString():v.toFixed(2))}
function tok(b){return (b.input_tokens||0)+(b.output_tokens||0)+(b.cache_write||0)}
function sum(o,f){return Object.values(o||{}).reduce((a,b)=>a+(f(b)||0),0)}
function when(ts){return new Date(ts*1000).toLocaleString([],{weekday:'short',hour:'2-digit',minute:'2-digit'})}
function resetTitle(ts){const d=new Date(ts*1000);return d.toLocaleString([],{weekday:'short',day:'numeric',month:'short',hour:'2-digit',minute:'2-digit'})+' local / '+d.toISOString().slice(0,16).replace('T',' ')+' UTC'}
function day(ts){return new Date(ts*1000).toLocaleDateString([],{month:'short',day:'numeric'})}
function dur(s){return s<5400?Math.round(s/60)+' min':s<2*D?(s/H).toFixed(s<10*H?1:0)+' h':(s/D).toFixed(1)+' d'}
function ago(min){return min==null?'never':min<1?'just now':min<60?min+' min ago':(min/60).toFixed(1)+' h ago'}
function cell(b,l){return b?`<td data-l="${l}">${money(b.cost_usd)}<span class="sub">${fmt(tok(b))} tok &middot; ${fmt(b.requests)} req</span></td>`:`<td data-l="${l}" class="muted">&ndash;</td>`}
function meter(label,u,thr,reset,ts){
  const sev=u>=1?['var(--crit)',' &#10005; limit reached']:u>=thr?['var(--warn)',' &#9888; past switch point']:['var(--s1)',''];
  return `<div class="meter-row"><span class="muted">${label}</span><div class="meter"><i style="width:${Math.min(100,u*100)}%;background:${sev[0]}"></i><span class="tick" style="left:${thr*100}%"></span></div>`
    +`<span><b>${Math.round(u*100)}%</b> <span class="muted small"${ts?` title="${esc(resetTitle(ts))}"`:''}>${reset}</span><span class="small">${sev[1]}</span></span></div>`}
function spark(v){if(!v||!v.length)return '';const w=96,h=24,m=Math.max(...v)||1,y=x=>(h-2-x/m*(h-4)).toFixed(1);
  return `<svg width="${w}" height="${h}"><polyline points="${v.map((x,i)=>(i/(v.length-1)*w).toFixed(1)+','+y(x)).join(' ')}" fill="none" stroke="var(--muted)" stroke-width="1.5"/><circle cx="${w}" cy="${y(v[v.length-1])}" r="3" fill="var(--s1)"/></svg>`}
function weekly(p){
  if(!p||!p.series.length) return 'collecting data';
  const cw=Math.max(300,$('weekly').clientWidth||1100),Hh=cw<600?220:270,L=48,R=12,T=14,B=26, now=p.series[p.series.length-1][0];
  const all=p.series.concat(p.projection.slice(1)), last=p.series[p.series.length-1];
  // The plot scrolls from the first sample to the projection's end and opens centred on now.
  const half=(cw<600?3.5:7.5)*D, x0=Math.min(p.series[0][0],now-half), x1=Math.max(now+half,all[all.length-1][0]);
  const pps=(cw-L-R)/(2*half), st=pps*D<60?2:1;
  const ymax=Math.ceil(Math.max(100,...all.map(q=>q[2]))*1.1/50)*50, gs=ymax>300?100:50;
  const X=t=>(t-x0)*pps, Y=v=>T+(1-v/ymax)*(Hh-T-B), PW=Math.ceil(X(x1)+R);
  const path=(a,i)=>a.map((q,k)=>(k?'L':'M')+X(q[0]).toFixed(1)+','+Y(q[i]).toFixed(1)).join('');
  WK={all,now,x0,pps,start:X(now-half)};
  if(showTable){let h='<table><tr><th>Time</th><th>Usage</th><th>Limit</th><th>Headroom</th></tr>';
    for(const q of all.filter((q,i)=>i%48===0||i===all.length-1)) h+=`<tr><td>${esc(when(q[0]))}${q[0]>now?' (projected)':''}</td><td>${Math.round(q[1])}%</td><td>${Math.round(q[2])}%</td><td>${Math.round(q[2]-q[1])}%</td></tr>`;
    return h+'</table>'+sentence(p,now)}
  let y=`<svg width="${L}" height="${Hh}">`, s=`<svg width="${PW}" height="${Hh}"><rect x="${X(now)}" y="${T}" width="${X(x1)-X(now)}" height="${Hh-T-B}" fill="#fff" opacity=".025"/>`, from=null;
  const band=(a,b)=>`<rect class="hit" x="${X(a)}" y="${T}" width="${(X(b)-X(a)).toFixed(1)}" height="${Hh-T-B}" fill="var(--crit)" opacity=".2"/>`;
  for(const q of all){const hit=q[2]-q[1]<=0.1;  // every account at its weekly limit: usage has met the limit line
    if(hit&&from==null) from=q[0]; else if(!hit&&from!=null){s+=band(from,q[0]);from=null}}
  if(from!=null) s+=band(from,all[all.length-1][0]);
  for(let v=0;v<=ymax;v+=gs){y+=`<text x="${L-6}" y="${Y(v)+4}" text-anchor="end">${v}%</text>`; s+=`<line x1="0" x2="${PW}" y1="${Y(v)}" y2="${Y(v)}" stroke="${v?'var(--grid)':'var(--axis)'}"/>`}
  for(let t=Math.ceil(x0/D/st)*st*D;t<x1;t+=st*D) if(X(t)>18) s+=`<text x="${X(t)}" y="${Hh-8}" text-anchor="middle">${esc(day(t))}</text>`;
  s+=`<line x1="${X(now)}" x2="${X(now)}" y1="${T}" y2="${Hh-B}" stroke="var(--ink2)" opacity=".5"/><text x="${X(now)+4}" y="${T+10}">now</text>${cw<600?'':`<text x="${X(x1)-4}" y="${T+10}" text-anchor="end">projected</text>`}`
    +`<path d="${path(p.series,1)}L${X(now)},${Y(0)}L${X(p.series[0][0])},${Y(0)}Z" fill="var(--s1)" opacity=".1"/>`
    +`<path d="${path(p.series,2)}" fill="none" stroke="var(--s2)" stroke-width="2" stroke-linejoin="round"/><path d="${path(p.series,1)}" fill="none" stroke="var(--s1)" stroke-width="2" stroke-linejoin="round"/>`
    +`<path d="${path(p.projection,2)}" fill="none" stroke="var(--s2)" stroke-width="2" stroke-dasharray="6 5" opacity=".8"/><path d="${path(p.projection,1)}" fill="none" stroke="var(--s1)" stroke-width="2" stroke-dasharray="6 5" opacity=".8"/>`;
  for(let i=1;i<all.length;i++){const dn=all[i-1][1]-all[i][1]; if(dn>=1) s+=`<text x="${X(all[i-1][0])}" y="${Y(all[i-1][1])-8}" text-anchor="middle">reset &minus;${Math.round(dn)}</text>`}  // a weekly reset gave quota back
  for(const [i,c,dy] of [[1,'var(--s1)',18],[2,'var(--s2)',-9]])  // usage label below its dot, limit above
    s+=`<circle cx="${X(now)}" cy="${Y(last[i])}" r="4.5" fill="${c}" stroke="var(--surface)" stroke-width="2"/><text x="${X(now)-8}" y="${Y(last[i])+dy}" text-anchor="end" style="fill:var(--ink)">${Math.round(last[i])}%</text>`;
  return `<div class="wk">${y}</svg><div class="wk-scroll" id="wk-scroll">${s}<line id="xh" x1="0" x2="0" y1="${T}" y2="${Hh-B}" stroke="var(--ink2)" opacity="0"/></svg></div></div>`+sentence(p,now)}
function sentence(p,now){
  const rate=p.rate_per_day==null?'':`At the current rate (${p.rate_per_day}% of a weekly quota per day), `;
  if(p.eta) return `<div class="sentence">${rate}every account ${p.eta<=now+60?'is at 100% now':'is spent at <b>'+esc(when(p.eta))+'</b>'}; quota returns at ${esc(when(p.back))} when ${esc(p.back_account)} resets.</div>`;
  return `<div class="sentence">${p.note?esc(p.note):rate+'no shortfall in the next 7.5 days.'}</div>`}
$('weekly').onpointerdown=$('weekly').onpointermove=e=>{  // pointerdown: a tap on a touch screen
  const svg=e.currentTarget.querySelector('#wk-scroll svg'); if(!svg||!WK) return;
  const r=svg.getBoundingClientRect(), t=WK.x0+(e.clientX-r.left)/WK.pps;
  let q=WK.all[0]; for(const p of WK.all) if(Math.abs(p[0]-t)<Math.abs(q[0]-t)) q=p;
  const xh=svg.querySelector('#xh'), x=(q[0]-WK.x0)*WK.pps;
  xh.setAttribute('x1',x); xh.setAttribute('x2',x); xh.setAttribute('opacity','.6');
  const tip=$('tip'); tip.replaceChildren();
  const head=document.createElement('div'); head.className='muted'; head.textContent=when(q[0])+(q[0]>WK.now?' · projected':''); tip.append(head);
  for(const [k,v,c] of [['Usage',q[1],'var(--s1)'],['Limit',q[2],'var(--s2)'],[q[2]-q[1]<=0.1?'Blocked':'Headroom',q[2]-q[1],'']]){
    const row=document.createElement('div'), a=document.createElement('span'), b=document.createElement('span');
    row.className='row'; b.className='v'; b.textContent=Math.round(v)+'%';
    if(c){const key=document.createElement('span'); key.className='key'; key.style.borderColor=c; a.append(key)} a.append(k); row.append(a,b); tip.append(row)}
  const c=e.currentTarget.parentNode.getBoundingClientRect(), px=e.clientX-c.left;
  tip.style.display='block'; tip.style.top=(r.top-c.top+8)+'px';
  tip.style.left=Math.max(8,px+16+tip.offsetWidth>c.width?px-16-tip.offsetWidth:px+16)+'px'};
$('weekly').onpointerleave=e=>{if(e.pointerType==='touch') return;  // touch fires leave right after the tap
  $('tip').style.display='none'; const xh=document.querySelector('#xh'); if(xh) xh.setAttribute('opacity','0')};
$('tbl').onclick=()=>{showTable=!showTable; $('tbl').textContent=showTable?'Chart':'Table'; refresh()};
$('period').onclick=e=>{const p=e.target.dataset&&e.target.dataset.p; if(p){period=p; refresh()}};
const api=path=>fetch(path,{headers:KEY?{Authorization:'Bearer '+KEY}:{},cache:'no-store'});
async function refresh(){
  const [sr, tr] = await Promise.all([api('/rotate/status'), api('/rotate/stats')]);
  if(sr.status===401){
    KEY=null; try{sessionStorage.removeItem(SKEY)}catch(e){}
    $('alerts').innerHTML='<div class="banner warn">&#9888; <span>No device key: open the panel link with ?key=&lt;device key&gt;</span></div>';
    return}
  const [st, stats] = await Promise.all([sr.json(), tr.json()]);
  const per=stats.periods||{}, wk=st.weekly||{}, accts=Object.entries(st.accounts||{});
  $('host').textContent=location.host||'';
  $('pills').innerHTML=`<span class="pill"><span class="dot" style="background:var(--good)"></span>active: ${esc(st.active_account)}</span> <span class="pill">${esc(st.strategy)} &middot; ${accts.length} accounts &middot; ${Object.keys(stats.devices||{}).length} devices</span>`;
  $('alerts').innerHTML=(stats.alerts||[]).map(a=>`<div class="banner ${a.level==='warn'?'warn':''}">&#9888; <span><b>${a.level==='warn'?'Warning':'Note'}</b> ${esc(a.text)}</span></div>`).join('');
  // headline figures
  const last=(wk.series||[]).slice(-1)[0], now=last?last[0]:Date.now()/1000;
  $('hero').textContent=wk.eta?(wk.eta<=now+60?'now':dur(wk.eta-now)):wk.rate_per_day!=null?'> 7.5 days':'–';
  $('hero-sub').textContent=wk.eta?`${when(wk.eta)} · quota returns ${when(wk.back)} when ${wk.back_account} resets`:(wk.note||'no shortfall projected');
  $('k-left').textContent=last?Math.round(last[2]-last[1])+'%':'–'; $('k-left-sub').textContent=`of ${wk.capacity||0}% · ${accts.length} accounts`;
  $('k-rate').textContent=wk.rate_per_day!=null?wk.rate_per_day+'%':'–';
  $('k-req').textContent=fmt(sum(stats.by_device,b=>b.requests)); $('k-req-sub').textContent=fmt(sum(stats.by_device,tok))+' tokens';
  $('k-cost').textContent=money(sum((per['7d']||{}).model,b=>b.cost_usd)); $('k-cost-sub').textContent=money(sum(stats.by_model,b=>b.cost_usd))+' in the last 24 h';
  // accounts
  $('accounts').innerHTML=accts.map(([name,a])=>{const u5=a.effective_util_5h||0, u7=a.effective_util_7d||0;
    const seen=a.last_seen?ago(Math.round((Date.now()/1000-a.last_seen)/60)):'never';
    return `<div class="card"><div class="acct-head"><b>${esc(name)}</b>${name===st.active_account?'<span class="badge"><span class="dot" style="background:var(--good)"></span>active</span>':''}`
      +(a.disabled?'<span class="badge">disabled</span>':'')+(a.private_to?`<span class="badge">private to ${esc([].concat(a.private_to).join(', '))}</span>`:'')+(a.priority!==100?`<span class="badge">prio ${esc(a.priority)}</span>`:'')
      +(a.weekly_allowance!=null?`<span class="badge">${esc((a.weekly_pacing?'paced '+Math.round(a.weekly_allowance*100)+'% now of '+Math.round((a.weekly_cap==null?1:a.weekly_cap)*100)+'%':'weekly cap '+Math.round(a.weekly_allowance*100)+'%')+(u7>=a.weekly_allowance?' · reached':''))}</span>`:'')
      +`<span class="muted small" style="margin-left:auto">upstream: ${esc(a.status||'n/a')} &middot; seen ${seen}</span></div>`
      +meter('5h',u5,st.switch_threshold,a.reset_5h_in_min>0?'resets in '+dur(a.reset_5h_in_min*60):'window idle',a.reset_5h_in_min>0&&a.reset_5h)
      +meter('7d',u7,st.switch_threshold_7d,a.reset_7d_in_h>0?'resets in '+dur(a.reset_7d_in_h*H):'window idle',a.reset_7d_in_h>0&&a.reset_7d)+'</div>'}).join('');
  const sc=$('wk-scroll'), keep=sc&&sc.scrollWidth?sc.scrollWidth-sc.clientWidth-sc.scrollLeft:null;  // keep the view across redraws
  $('weekly').innerHTML=weekly(wk);
  const nsc=$('wk-scroll'); if(nsc&&WK) nsc.scrollLeft=keep==null?WK.start:nsc.scrollWidth-nsc.clientWidth-keep;
  // devices: configured ones, then removed ones that still have history
  const roster=stats.devices||{}, gone=Object.keys((per.all||{}).device||{}).filter(n=>!(n in roster)&&n!=='?');
  let dh=`<tr><th>Device</th><th class="l">Status</th><th class="l">Last model</th><th>24 h</th><th>7 d</th><th>30 d</th><th>All time${per.since?' <span class="muted">since '+esc(per.since)+'</span>':''}</th><th>30-day trend</th></tr>`;
  for(const name of Object.keys(roster).concat(gone)){const d=roster[name], removed=!d;
    const status=removed?'removed':d.online?'<span class="dot" style="background:var(--good)"></span>online':'<span class="dot" style="background:var(--muted)"></span>idle';
    dh+=`<tr class="${removed?'removed':''}"><td><b>${esc(name)}</b>${d?'<span class="sub">key '+esc(d.key_suffix)+'</span>':''}</td><td class="l">${status}${d?'<span class="sub">'+ago(d.last_seen_min)+'</span>':''}</td><td class="l" data-l="Last model">${esc(d&&d.last_model||'–')}</td>`
      +cell((stats.by_device||{})[name],'24 h')+cell(((per['7d']||{}).device||{})[name],'7 d')+cell(((per['30d']||{}).device||{})[name],'30 d')
      +cell(((per.all||{}).device||{})[name],'All time'+(per.since?' since '+esc(per.since):''))+`<td data-l="30-day trend">${spark((per.trend||{})[name])}</td></tr>`}
  $('devices').innerHTML=dh;
  // models over the chosen period; accounts over 24 h
  $('period').innerHTML=Object.keys(PERIODS).map(p=>`<button data-p="${p}" class="${p===period?'on':''}">${p}</button>`).join('');
  const rows=o=>Object.entries(o||{}).sort((a,b)=>(b[1].cost_usd||0)-(a[1].cost_usd||0)).map(([n,b])=>{const prompt=(b.input_tokens||0)+(b.cache_read||0)+(b.cache_write||0);
    return `<tr><td>${esc(n)}</td><td>${fmt(b.requests)}</td><td>${fmt(tok(b))}</td><td>${prompt?Math.round((b.cache_read||0)/prompt*100):0}%</td><td>${money(b.cost_usd)}</td></tr>`}).join('');
  const head='<tr><th>{}</th><th>Requests</th><th>Tokens</th><th>From cache</th><th>$ equiv</th></tr>';
  $('models').innerHTML=head.replace('{}','Model')+rows(PERIODS[period]==='24h'?stats.by_model:(per[PERIODS[period]]||{}).model);
  $('by-account').innerHTML=head.replace('{}','Account')+rows(stats.by_account);
  $('events').innerHTML=(st.recent_events||[]).slice().reverse().map(e=>`<div><span class="muted">${esc(String(e.ts||'').slice(5,16).replace('T',' '))}</span><span>${esc(e.from)} &rarr; ${esc(e.to)}</span><span class="badge" style="margin-left:auto">${esc(e.reason)}</span></div>`).join('')||'<span class="muted">none</span>';
  $('upd').textContent='updated '+new Date().toLocaleTimeString();
}
refresh(); setInterval(refresh, 10000);
</script></body></html>"""


@app.get("/rotate/panel")
async def rotate_panel(request: Request):
    # The page is static and holds no account data; its JSON endpoints check the key.
    return Response(content=PANEL_HTML, media_type="text/html")


async def retry_wait(seconds: float, request: Request | None) -> None:
    """Release held request bodies promptly when their caller has left."""
    deadline = time.monotonic() + seconds
    while True:
        if request is not None and await request.is_disconnected():
            raise HTTPException(status_code=499, detail="client disconnected")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        await asyncio.sleep(min(remaining, 1.0) if request is not None else remaining)


def messages_unusable(upstream, url: str) -> bool:
    """A /v1/messages response that signals this account can't do the request at
    all: 403 (closed / permission-blocked) or 404 (model not available on this
    account, e.g. a free tier asked to serve a premium model). Quota exhaustion
    shows up as 429 instead, handled separately."""
    return (method_is_messages(url)
            and getattr(upstream, "status_code", None) in (403, 404))


def method_is_messages(url: str) -> bool:
    """True when the upstream path is the Anthropic /v1/messages endpoint itself,
    not /v1/messages/count_tokens or /v1/messages/batches/..., whose 404s (e.g. an
    unknown batch id) say nothing about the account."""
    return url.split("?", 1)[0].rstrip("/") == "/v1/messages"


def pick_working(device: str | None = None) -> str | None:
    """The best account that can actually serve right now, or None if none.
    Distinguishes 'a dead account and at least one other works' (return the good
    one) from 'every account is dead' (None), so the next request isn't fired
    at a closed/free account when a working one is idle."""
    ok = [n for n in ACCOUNTS if hard_ok(n) and available_to(n, device)]
    if not ok:
        return None
    return min(ok, key=lambda n: (on_probation(n), rank(n, device), binding_reset(n)))


async def revert_refusals(refused: dict, start: str, status: int,
                          device: str | None = None) -> None:
    """A request ended refused, and every previously healthy account it was
    tried on refused it with the same 403/404: that is the request's fault
    (unknown model name, a header the API rejects), not the accounts'. Undo the
    bad marks it made on those accounts and go back to the account it started
    on, so one bad request can't take the whole herd out of rotation. Accounts
    already on probation keep their re-mark."""
    async with state_lock:
        for name, prior in refused.items():
            info = STATE["accounts"].setdefault(name, {})
            for k in BAD_KEYS:
                if prior.get(k) is None:
                    info.pop(k, None)
                else:
                    info[k] = prior[k]
        now = time.time()
        if device_current(device) != start:
            record_switch(device_current(device), start, "request_refused_revert", now,
                          device)
        STATE["events"].append({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "type": "refusal",
            "from": start, "to": start,
            "reason": f"{status} from every account tried ({', '.join(refused)}); "
                      "treated as a bad request, bad marks reverted"})
        STATE["events"] = STATE["events"][-200:]
        save_state()


async def send_upstream(method: str, url: str, fwd_headers: dict, body: bytes,
                        request: Request | None = None, device: str | None = None):
    """The rotation loop shared by the transparent proxy and the OpenAI endpoint:
    pick the device's active account, pace bursts, retry through switches, optionally hold
    until the soonest window reset. Returns (upstream, account, retries, noted,
    verdict): noted means note_response already ran for the returned response;
    verdict is note_response's word for the LAST 429 seen ("burst" / "switched" /
    "exhausted" / None)."""
    hold_deadline = time.time() + HOLD_MAX_S if HOLD_MAX_S else None
    retries = 0
    noted = False
    verdict = None
    start = device_current(device)
    # Accounts that were clean (not on probation) before this request and then
    # refused it: name -> (their bad-mark fields before, the refusal status).
    refused: dict = {}
    while True:
        await retry_wait(0, request)
        account = device_current(device)
        if account not in ACCOUNTS or disabled(account) or not available_to(account, device):
            account = pick_account(device)
            if account is None:
                raise HTTPException(status_code=503, detail="no enabled accounts")
        if device and device not in STATE.get("device_accounts", {}) or account != device_current(device):
            # Record a first-time device's pick so every later check agrees with it.
            set_device_current(device, account)
            save_state()
        # Never fire a request at an account we already know is a closed/free
        # dead-end when a working account is available: leave before sending.
        if account_bad(account):
            async with state_lock:
                good = pick_working(device)
                if good is not None and good != device_current(device):
                    record_switch(device_current(device), good, "account_unusable",
                                  time.time(), device)
                    save_state()
                    account = good
        token = ACCOUNTS.get(account, {}).get("token")
        if token is None:
            continue  # the config dropped it while this request waited; pick again
        fwd_headers["authorization"] = f"Bearer {token}"
        pace =STATE["accounts"].get(account, {}).get("pace_until", 0) - time.time()
        if pace > 0:
            await retry_wait(min(pace, 60), request)
        upstream = await client.send(
            client.build_request(method, url, headers=fwd_headers, content=body),
            stream=True)
        if upstream.status_code != 429 and not messages_unusable(upstream, url):
            return upstream, account, retries, noted, verdict
        # A 429 (quota/burst) or a /v1/messages refusal on a closed/free account
        # (403 permission-blocked, or 404 model-not-found) both mean: rotate.
        note_status = upstream.status_code
        bad = note_status in (403, 404)
        if bad and account not in refused and not on_probation(account):
            info = STATE["accounts"].get(account, {})
            refused[account] = ({k: info.get(k) for k in BAD_KEYS}, note_status)
        verdict = await note_response(account, upstream.headers, note_status, bad=bad,
                                      device=device)
        wait = 0.0
        retry = verdict in ("switched", "burst") and retries < 5
        if (not retry and bad and refused
                and len({st for _, st in refused.values()}) == 1):
            # The request ends refused and every account that was healthy before
            # it refused it the same way: the request is at fault, so don't
            # leave those accounts marked dead.
            await revert_refusals({n: p for n, (p, _) in refused.items()}, start,
                                  next(iter(refused.values()))[1], device)
        # Holding only makes sense for spent quota (429), not for refusals.
        if not retry and verdict == "exhausted" and hold_deadline and not bad:
            # Every account is spent: hold the request open until the soonest
            # window reset instead of failing, so unattended runs finish.
            wait = min(earliest_relief(device) + 5, hold_deadline) - time.time()
            retry = wait > 0
        if not retry:
            noted = True
            return upstream, account, retries, noted, verdict
        await bounded_response(upstream)
        retries += 1
        if wait > 0:
            async with state_lock:
                STATE["events"].append({
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "type": "hold",
                    "from": account, "to": account,
                    "reason": f"all accounts spent; holding {round(wait)}s"})
                STATE["events"] = STATE["events"][-200:]
                save_state()
            await retry_wait(wait, request)


@app.post("/v1/chat/completions")
async def openai_chat(request: Request):
    """OpenAI-compatible front door (Phase 3): any OpenAI-format client or router
    rides the rotated Max capacity through here. Translation to /v1/messages with
    the oauth beta header; NO client headers are forwarded, so caller identity
    stops at this proxy: upstream sees one consumer per device key. When every
    account's window is spent this returns 503 + Retry-After (a breaker-friendly
    signal for routers), unlike the transparent proxy below, which stays
    transparent because Claude Code handles Anthropic's own 429s natively."""
    device = device_from_request(request)
    if device is None:
        return JSONResponse({"error": {"message": "claude-rotate: unknown device key",
                                       "type": "authentication_error"}}, status_code=401)
    try:
        oai = json.loads(await bounded_body(request))
    except (ValueError, RecursionError):
        return JSONResponse({"error": {"message": "body is not valid JSON",
                                       "type": "invalid_request_error"}}, status_code=400)
    abody, err = oai_to_anthropic(oai)
    if err:
        return JSONResponse({"error": {"message": err, "type": "invalid_request_error"}},
                            status_code=400)
    fwd = {"content-type": "application/json", "accept-encoding": "identity",
           "anthropic-version": "2023-06-01", "anthropic-beta": "oauth-2025-04-20"}
    upstream, account, retries, noted, verdict = await send_upstream(
        "POST", "/v1/messages", fwd, json.dumps(abody).encode(), request, device)

    rec = {"device": device, "account": account, "path": "/v1/chat/completions", "method": "POST",
           "model": abody.get("model"), "status": upstream.status_code}
    if retries:
        rec["retries"] = retries
    resp_headers = {"x-rotate-account": account}
    for h in ("anthropic-ratelimit-unified-5h-utilization",
              "anthropic-ratelimit-unified-7d-utilization",
              "anthropic-ratelimit-unified-5h-reset",
              "anthropic-ratelimit-unified-status"):
        if upstream.headers.get(h):
            resp_headers[h] = upstream.headers[h]

    if upstream.status_code == 429 and verdict == "exhausted":
        await bounded_response(upstream)
        retry_after = max(1, round(earliest_relief(device) - time.time()))
        rec["status"] = 503
        audit(rec)
        return JSONResponse(
            {"error": {"message": "claude-rotate: every account's window is spent; "
                                  f"retry in {retry_after}s",
                       "type": "overloaded_error"}},
            status_code=503, headers=resp_headers | {"retry-after": str(retry_after)})

    if abody.get("stream") and upstream.status_code == 200 \
            and "text/event-stream" in upstream.headers.get("content-type", ""):
        tr = SseToOai()

        async def relay():
            try:
                async for chunk in upstream.aiter_bytes():
                    out = tr.feed_bytes(chunk)
                    if out:
                        yield out.encode()
                yield tr.tail().encode()
            finally:
                await upstream.aclose()
                rec["usage"] = summarize_usage(tr.usage)
                rec["util_5h"] = upstream.headers.get(
                    "anthropic-ratelimit-unified-5h-utilization")
                audit(rec)
                await note_response(account, upstream.headers, upstream.status_code,
                                    device=device)

        return StreamingResponse(relay(), status_code=200, headers=resp_headers,
                                 media_type="text/event-stream")

    content = await bounded_response(upstream)
    if not noted:
        await note_response(account, upstream.headers, upstream.status_code,
                                    device=device)
    rec["util_5h"] = upstream.headers.get("anthropic-ratelimit-unified-5h-utilization")
    if upstream.status_code != 200:
        try:
            msg = json.loads(content).get("error", {}).get("message") \
                or content[:200].decode(errors="replace")
        except (json.JSONDecodeError, AttributeError):
            msg = content[:200].decode(errors="replace")
        audit(rec)
        return JSONResponse({"error": {"message": f"upstream: {msg}",
                                       "type": "api_error"}},
                            status_code=upstream.status_code, headers=resp_headers)
    data = json.loads(content)
    rec["usage"] = summarize_usage(data.get("usage"))
    audit(rec)
    return JSONResponse(anthropic_to_oai(data), headers=resp_headers)


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
async def proxy(request: Request, path: str):
    device = device_from_request(request)
    if device is None:
        return JSONResponse(
            {"type": "error", "error": {"type": "authentication_error",
                                        "message": "claude-rotate: unknown device key"}},
            status_code=401)

    body = await bounded_body(request)
    # anthropic-beta is a comma list that a client or hop may split across field
    # lines (RFC 9110 5.3). A plain dict keeps only the last line and would drop
    # betas such as extended cache TTL, so join it. Other repeats stay last-wins.
    fwd_headers: dict = {}
    for k, v in request.headers.items():
        k = k.lower()
        if k in HOP | PRIVATE_HEADERS or k.startswith(("tailscale-", "x-forwarded-", "sec-fetch-")):
            continue
        fwd_headers[k] = f"{fwd_headers[k]}, {v}" if k == "anthropic-beta" and k in fwd_headers else v
    fwd_headers["accept-encoding"] = "identity"
    fwd_headers.pop("x-api-key", None)

    model = None
    if body and request.method == "POST":
        try:
            model = json.loads(body).get("model")
            if not isinstance(model, str):
                model = None
        except (ValueError, RecursionError, AttributeError):
            pass

    # Preserve raw upstream query spelling/order while removing local credentials.
    try:
        query = request.scope["query_string"].decode("ascii")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="query string must use percent-encoding")
    query = "&".join(part for part in query.split("&")
                     if unquote_plus(part.partition("=")[0]) != "key")
    url = "/" + path + ("?" + query if query else "")
    upstream, account, retries, noted, _verdict = await send_upstream(
        request.method, url, fwd_headers, body, request, device)

    rec = {"device": device, "account": account, "path": "/" + path, "method": request.method,
           "model": model, "status": upstream.status_code}
    if retries:
        rec["retries"] = retries
    resp_headers = {k: v for k, v in upstream.headers.items()
                    if k.lower() not in HOP and k.lower() != "content-encoding"}
    resp_headers["x-rotate-account"] = account

    if "text/event-stream" in upstream.headers.get("content-type", ""):
        usage_acc: dict = {}

        async def relay():
            pending = b""  # tail of the stream after its last newline
            try:
                async for chunk in upstream.aiter_bytes():
                    pending += chunk
                    cut = pending.rfind(b"\n") + 1
                    if cut:
                        usage_from_sse(pending[:cut].decode("utf-8", errors="replace"),
                                       usage_acc)
                        pending = pending[cut:]
                    yield chunk
            finally:
                await upstream.aclose()
                if pending:
                    usage_from_sse(pending.decode("utf-8", errors="replace"), usage_acc)
                rec["usage"] = summarize_usage(usage_acc)
                rec["util_5h"] = upstream.headers.get("anthropic-ratelimit-unified-5h-utilization")
                audit(rec)
                await note_response(account, upstream.headers, upstream.status_code,
                                    device=device)

        return StreamingResponse(relay(), status_code=upstream.status_code,
                                 headers=resp_headers,
                                 media_type=upstream.headers.get("content-type"))

    content = await bounded_response(upstream)
    try:
        rec["usage"] = summarize_usage(json.loads(content).get("usage"))
    except (json.JSONDecodeError, UnicodeDecodeError, AttributeError):
        pass
    rec["util_5h"] = upstream.headers.get("anthropic-ratelimit-unified-5h-utilization")
    audit(rec)
    if not noted:
        await note_response(account, upstream.headers, upstream.status_code,
                                    device=device)
    return Response(content=content, status_code=upstream.status_code, headers=resp_headers)


if __name__ == "__main__":
    uvicorn.run(app, host=BIND, port=PORT, log_level="warning")
