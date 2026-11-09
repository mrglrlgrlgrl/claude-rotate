"""End-to-end tests for the claude-rotate HTTP layer against a mocked
Anthropic upstream (httpx.MockTransport) via the real ASGI app:

auth, header rewriting, SSE relay, quota-429 rotate+retry, burst-429 pacing,
429 passthrough when hold is off, hold-until-reset, and the admin endpoints.
"""
import asyncio
import contextlib
import gzip
import io
import json
import sys
import tempfile
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import rotator as R

PASS = 0


def check(name, cond):
    global PASS
    print(("ok  " if cond else "FAIL") + " " + name)
    if not cond:
        sys.exit(1)
    PASS += 1


# --- harness -----------------------------------------------------------------
tmp = Path(tempfile.mkdtemp())
R.AUDIT_PATH = tmp / "audit.jsonl"
R.save_state = lambda: None
R.ACCOUNTS = {"a": {"name": "a", "token": "tok-a"}, "b": {"name": "b", "token": "tok-b"}}
R.DEVICE_BY_KEY = {"devkey-1": "laptop"}
R.CFG["devices"] = {"laptop": "devkey-1"}
R.THRESHOLD, R.THRESHOLD_7D = 0.8, 0.98
R.STRATEGY, R.COOLDOWN_S, R.SWITCH_MARGIN, R.CF_MARGIN_S = "consume-first", 300, 0.05, 3600
R.HOLD_MAX_S = 0


def fresh_state(active="a", accounts=None):
    R.STATE = {"active_account": active, "accounts": accounts or {}, "events": [],
               "last_switch_ts": 0}
    R.AUDIT_PATH.write_text("")


def unified(u5="0.5", u7="0.2", status="allowed", **extra):
    now = time.time()
    h = {"anthropic-ratelimit-unified-5h-utilization": u5,
         "anthropic-ratelimit-unified-7d-utilization": u7,
         "anthropic-ratelimit-unified-5h-reset": str(now + 3600),
         "anthropic-ratelimit-unified-7d-reset": str(now + 86400),
         "anthropic-ratelimit-unified-status": status}
    h.update(extra)
    return h


def use_upstream(handler):
    R.client = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                 base_url=R.UPSTREAM, timeout=30.0)


def call(path="/v1/messages", key="devkey-1", body=None, headers=None, method="POST", raw=None,
         header_lines=None):
    """header_lines: (name, value) pairs sent as separate field lines, repeats included."""
    async def _go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=R.app),
                                     base_url="http://proxy", timeout=30.0) as ac:
            hdrs = {"authorization": f"Bearer {key}"} if key else {}
            hdrs.update(headers or {})
            content = raw if raw is not None else json.dumps(
                body or {"model": "claude-opus-5", "messages": []})
            return await ac.request(method, path, content=content,
                                    headers=list(hdrs.items()) + list(header_lines or []))
    return asyncio.run(_go())


def audit_recs():
    return [json.loads(l) for l in R.AUDIT_PATH.read_text().splitlines() if l.strip()]


# --- 1. auth -----------------------------------------------------------------
fresh_state()
use_upstream(lambda req: httpx.Response(200))
r = call(key="wrong-key")
check("unknown device key → 401", r.status_code == 401
      and r.json()["error"]["type"] == "authentication_error")
check("401 without any credentials", call(key=None).status_code == 401)

# --- 2. transparent forwarding + header rewriting ----------------------------
fresh_state()
seen = {}


def ok_handler(req):
    seen["auth"] = req.headers.get("authorization")
    seen["accept-encoding"] = req.headers.get("accept-encoding")
    seen["x-api-key"] = req.headers.get("x-api-key")
    seen["beta"] = req.headers.get("anthropic-beta")
    seen["version"] = req.headers.get("anthropic-version")
    # Genuinely gzipped body so the proxy-side httpx auto-decompresses and the
    # stale content-encoding header must be dropped (load-bearing fact #2).
    body = gzip.compress(json.dumps({"usage": {"input_tokens": 7, "output_tokens": 3}}).encode())
    return httpx.Response(200, content=body,
                          headers={**unified(), "content-encoding": "gzip",
                                   "content-type": "application/json"})


use_upstream(ok_handler)
r = call(headers={"x-api-key": "should-be-dropped", "anthropic-beta": "oauth-2025-04-20"})
check("success passes through", r.status_code == 200)
check("device key swapped for account token", seen["auth"] == "Bearer tok-a")
check("accept-encoding forced to identity", seen["accept-encoding"] == "identity")
check("x-api-key stripped", seen["x-api-key"] is None)
check("client anthropic-beta preserved", seen["beta"] == "oauth-2025-04-20")
# Regression: a dict of request headers kept only the last repeated line.
call(header_lines=[("anthropic-beta", "oauth-2025-04-20"),
                   ("anthropic-beta", "extended-cache-ttl-2025-04-11")])
check("repeated anthropic-beta lines are all forwarded, in order",
      seen["beta"] == "oauth-2025-04-20, extended-cache-ttl-2025-04-11")
call(header_lines=[("authorization", "Bearer second-client-token")])
check("repeated authorization lines still end as the account token", seen["auth"] == "Bearer tok-a")
call(header_lines=[("anthropic-version", "2023-06-01"), ("anthropic-version", "2023-06-02")])
check("other repeated lines stay last-wins, not comma-joined", seen["version"] == "2023-06-02")
check("content-encoding dropped from response", "content-encoding" not in r.headers)
check("state updated from unified headers",
      R.STATE["accounts"]["a"]["util_5h"] == 0.5 and R.STATE["accounts"]["a"]["util_7d"] == 0.2)
recs = audit_recs()
check("audit written with usage + device + account",
      recs and recs[-1]["device"] == "laptop" and recs[-1]["account"] == "a"
      and recs[-1]["usage"]["input_tokens"] == 7 and recs[-1]["model"] == "claude-opus-5")

# --- 3. SSE relay ------------------------------------------------------------
fresh_state()
SSE = ('event: message_start\n'
       'data: {"message": {"usage": {"input_tokens": 11, "output_tokens": 1}}}\n\n'
       'event: message_delta\n'
       'data: {"usage": {"output_tokens": 33}}\n\n')
use_upstream(lambda req: httpx.Response(
    200, content=SSE.encode(),
    headers={**unified(), "content-type": "text/event-stream"}))
r = call()
check("SSE body relayed untouched", r.text == SSE)
recs = audit_recs()
check("SSE usage captured to audit",
      recs and recs[-1]["usage"] == {"input_tokens": 11, "output_tokens": 33,
                                     "cache_read_input_tokens": 0,
                                     "cache_creation_input_tokens": 0})



class ChunkedBody(httpx.AsyncByteStream):
    def __init__(self, *chunks):
        self.chunks = chunks

    async def __aiter__(self):
        for c in self.chunks:
            yield c


def sse_upstream(*chunks):
    use_upstream(lambda req: httpx.Response(
        200, stream=ChunkedBody(*chunks),
        headers={**unified(), "content-type": "text/event-stream"}))


# A usage line cut inside its JSON, and again inside a multi-byte character.
fresh_state()
cut = SSE.index('"output_tokens": 33') + len('"output_tokens": 3')
sse_upstream(SSE[:cut].encode(), SSE[cut:].encode())
r = call()
check("split SSE body relayed byte-identical", r.content == SSE.encode())
recs = audit_recs()
check("usage line split across chunks still counted",
      recs[-1]["usage"]["input_tokens"] == 11 and recs[-1]["usage"]["output_tokens"] == 33)

fresh_state()
sse_upstream(SSE.rstrip("\n").encode())
r = call()
recs = audit_recs()
check("final usage line without a newline still counted",
      r.text == SSE.rstrip("\n") and recs[-1]["usage"]["output_tokens"] == 33)

fresh_state()
raw = 'data: {"usage": {"output_tokens": 5}, "t": "é"}\n'.encode()
mid = raw.index("é".encode()) + 1  # inside the two-byte character
sse_upstream(raw[:mid], raw[mid:])
call()
check("usage line cut inside a UTF-8 character still counted",
      audit_recs()[-1]["usage"]["output_tokens"] == 5)

# --- 4. quota 429 → rotate → transparent retry -------------------------------
fresh_state("a", {"b": {"util_5h": 0.1, "util_7d": 0.1}})
calls = []


def quota_handler(req):
    calls.append(req.headers["authorization"])
    if req.headers["authorization"] == "Bearer tok-a":
        return httpx.Response(429, json={"error": "limit"},
                              headers=unified(u5="1.0", status="rejected"))
    return httpx.Response(200, json={"usage": {"input_tokens": 1, "output_tokens": 1}},
                          headers=unified(u5="0.1"))


use_upstream(quota_handler)
r = call()
check("quota 429 hidden from client, retry succeeds", r.status_code == 200)
check("retry rode the next account", calls == ["Bearer tok-a", "Bearer tok-b"])
check("active account rotated", R.STATE["active_account"] == "b")
recs = audit_recs()
check("audit records final account + retry count",
      recs[-1]["account"] == "b" and recs[-1]["retries"] == 1 and recs[-1]["status"] == 200)

# --- 5. burst 429 → pace same account, no rotation ---------------------------
fresh_state("a", {"a": {"util_5h": 0.3, "util_7d": 0.1}, "b": {"util_5h": 0.0}})
calls = []


def burst_handler(req):
    calls.append(req.headers["authorization"])
    if len(calls) == 1:
        return httpx.Response(429, json={"error": "burst"},
                              headers={**unified(u5="0.3"), "retry-after": "1"})
    return httpx.Response(200, json={"usage": {"input_tokens": 1, "output_tokens": 1}},
                          headers=unified(u5="0.3"))


use_upstream(burst_handler)
t0 = time.time()
r = call()
elapsed = time.time() - t0
check("burst 429 hidden from client", r.status_code == 200)
check("burst retried on the SAME account", calls == ["Bearer tok-a", "Bearer tok-a"])
check("no rotation on burst", R.STATE["active_account"] == "a")
check("pace honored (~retry-after)", 0.9 <= elapsed < 5)

# --- 6. every account spent, hold off → 429 passthrough ----------------------
now = time.time()
spent = {"a": {"util_5h": 1.0, "reset_5h": now + 100},
         "b": {"util_5h": 1.0, "reset_5h": now + 2000}}
fresh_state("a", dict(spent))
R.HOLD_MAX_S = 0
# Pin the active account's reset as the soonest so pick_account's fallback
# stays on it → "exhausted" (not a bounce to the other spent account).
use_upstream(lambda req: httpx.Response(429, json={"error": "limit"},
                                        headers=unified(
                                            u5="1.0", status="rejected",
                                            **{"anthropic-ratelimit-unified-5h-reset": str(now + 100)})))
r = call()
check("all spent + hold off → 429 passthrough", r.status_code == 429)
check("audit records the 429", audit_recs()[-1]["status"] == 429)

# --- 7. every account spent, hold on → wait for reset, then succeed ----------
now = time.time()
fresh_state("a", {"a": {"util_5h": 1.0, "reset_5h": now + 100},
                  "b": {"util_5h": 1.0, "reset_5h": now + 2000}})
R.HOLD_MAX_S = 2  # deadline caps the wait so the test stays fast
calls = []


def hold_handler(req):
    calls.append(1)
    if len(calls) == 1:
        # Keep the active account's reset the soonest (see scenario 6) so the
        # verdict is "exhausted" and the hold path actually engages.
        return httpx.Response(429, json={"error": "limit"},
                              headers=unified(
                                  u5="1.0", status="rejected",
                                  **{"anthropic-ratelimit-unified-5h-reset": str(now + 100)}))
    return httpx.Response(200, json={"usage": {"input_tokens": 1, "output_tokens": 1}},
                          headers=unified(u5="0.0"))


use_upstream(hold_handler)
t0 = time.time()
r = call()
elapsed = time.time() - t0
check("held request eventually succeeds", r.status_code == 200 and len(calls) == 2)
check("hold waited for the deadline window", 1.5 <= elapsed < 10)
check("hold event recorded", any(e["type"] == "hold" for e in R.STATE["events"]))
R.HOLD_MAX_S = 0

# --- 8. admin endpoints ------------------------------------------------------
fresh_state()
check("status needs auth", call(path="/rotate/status", key="nope", method="GET").status_code == 401)
r = call(path="/rotate/status", method="GET")
check("status shape", r.status_code == 200 and r.json()["strategy"] == "consume-first"
      and "accounts" in r.json() and "series" in r.json()["weekly"])
fresh_state(accounts={"a": {"reset_5h": 1793883600, "reset_7d": 1793883600}})
acct =call(path="/rotate/status", method="GET").json()["accounts"]["a"]
check("status returns raw reset epochs", acct["reset_5h"] == 1793883600 and acct["reset_7d"] == 1793883600)
check("status shows no owner for a shared account", acct["private_to"] is None)
R.ACCOUNTS["a"]["private_to"] = "dev-1"
try:
    acct = call(path="/rotate/status", method="GET").json()["accounts"]["a"]
finally:
    del R.ACCOUNTS["a"]["private_to"]
check("status lists the owners of a private account", acct["private_to"] == ["dev-1"])
R.ACCOUNTS["a"]["private_to"] = ["dev-1", "dev-2"]
try:
    acct = call(path="/rotate/status", method="GET").json()["accounts"]["a"]
finally:
    del R.ACCOUNTS["a"]["private_to"]
check("status lists every owner", acct["private_to"] == ["dev-1", "dev-2"])
def status_for(**keys):
    R.ACCOUNTS["a"].update(keys)
    try:
        return call(path="/rotate/status", method="GET").json()["accounts"]["a"]
    finally:
        for k in keys:
            del R.ACCOUNTS["a"][k]


acct = status_for()
check("status: no cap or pacing gives null cap, false pacing, null allowance",
      acct["weekly_cap"] is None and acct["weekly_pacing"] is False and acct["weekly_allowance"] is None)
acct = status_for(weekly_cap=0.5)
check("status: flat cap gives the cap as the allowance",
      acct["weekly_cap"] == 0.5 and acct["weekly_pacing"] is False and acct["weekly_allowance"] == 0.5)
fresh_state(accounts={"a": {"reset_7d": time.time() + 6 * 86400}})
acct = status_for(weekly_cap=0.5, weekly_pacing=True)
check("status: paced allowance is the cap ramped through the week",
      acct["weekly_cap"] == 0.5 and acct["weekly_pacing"] is True and acct["weekly_allowance"] == 0.1071)
acct = status_for(weekly_pacing=True)
check("status: pacing without a cap gives a null cap and a ramped allowance",
      acct["weekly_cap"] is None and acct["weekly_pacing"] is True and acct["weekly_allowance"] == 0.2143)
_bad_s, _bad_max = R.BAD_RECOVER_S, R.BAD_RECOVER_MAX_S
R.BAD_RECOVER_S, R.BAD_RECOVER_MAX_S = 1200, 86400
try:
    fresh_state(accounts={"a": {"bad_status": 403, "last_bad_seen": time.time() - 600, "bad_count": 1}})
    acct = status_for()
    check("status: a bad account shows the minutes left of its recovery wait",
          acct["account_bad"] is True and acct["bad_recover_in_min"] == 10)
    fresh_state(accounts={"a": {"bad_status": 403, "last_bad_seen": time.time() - 1180, "bad_count": 1}})
    acct = status_for()
    check("status: the minutes left bottom out at 0 as the wait ends",
          acct["account_bad"] is True and acct["bad_recover_in_min"] == 0)
    fresh_state(accounts={"a": {"bad_status": 403, "last_bad_seen": time.time() - 1300, "bad_count": 1}})
    acct = status_for()
    check("status: once the wait has passed the account is no longer bad, with no countdown",
          acct["account_bad"] is False and "bad_recover_in_min" not in acct)
    R.BAD_RECOVER_S = 0
    fresh_state(accounts={"a": {"bad_status": 403, "last_bad_seen": time.time() - 99999, "bad_count": 1}})
    acct = status_for()
    check("status: with bad_recover_s 0 a bad account shows no countdown",
          acct["account_bad"] is True and "bad_recover_in_min" not in acct)
finally:
    R.BAD_RECOVER_S, R.BAD_RECOVER_MAX_S = _bad_s, _bad_max
fresh_state()
fresh_state()
check("status auth via ?key=",call(path="/rotate/status?key=devkey-1", key=None,
                                    method="GET").status_code == 200)
r = call(path="/rotate/stats", method="GET")
check("stats returns rollups", r.status_code == 200 and "by_device" in r.json()
      and set(r.json()["periods"]) == {"since", "7d", "30d", "all", "trend"})
r = call(path="/rotate/panel?key=devkey-1", key=None, method="GET")
check("panel serves html", r.status_code == 200 and "text/html" in r.headers["content-type"])
r = call(path="/rotate/panel", key=None, method="GET")
check("keyless panel page (a reload) serves private html",
      r.status_code == 200 and "text/html" in r.headers["content-type"]
      and r.headers["cache-control"] == "no-store" and r.headers["referrer-policy"] == "no-referrer")
for p in ("/rotate/status", "/rotate/stats"):
    check(f"keyless {p} is still refused", call(path=p, key=None, method="GET").status_code == 401)

# --- 9. idle-account usage refresh -------------------------------------------
fresh_state(active="a", accounts={"a": {"last_seen": time.time()},
                                  "b": {"last_seen": time.time() - 7200, "util_7d": 0.01}})
R.REFRESH_IDLE_S = 1800
R._REFRESH_TRIED.clear()
probes = []


probe_models = []


def probe_handler(req):
    probes.append((req.url.path, req.headers["authorization"], json.loads(req.content)["max_tokens"]))
    probe_models.append(json.loads(req.content)["model"])
    return httpx.Response(200, headers=unified(u5="0.14", u7="0.69"))


use_upstream(probe_handler)
asyncio.run(R.refresh_idle_accounts())
check("refresh sends one 1-token request, to the idle account only",
      probes == [("/v1/messages", "Bearer tok-b", 1)])
check("the probe body carries the default refresh_idle_model", probe_models == ["claude-haiku-4-5"])
check("refresh records the idle account's real usage", R.STATE["accounts"]["b"]["util_7d"] == 0.69)
check("refresh never changes the active account", R.STATE["active_account"] == "a")
asyncio.run(R.refresh_idle_accounts())
check("a just-refreshed account is not probed again", len(probes) == 1)

fresh_state(active="a", accounts={"a": {"last_seen": time.time()}, "b": {}})
R._REFRESH_TRIED.clear()
probes.clear()
use_upstream(lambda req: probes.append(1) or httpx.Response(429, json={"type": "error"}))
asyncio.run(R.refresh_idle_accounts())
asyncio.run(R.refresh_idle_accounts())
check("headerless 429 (retired model) neither paces nor gets retried before the interval",
      "pace_until" not in R.STATE["accounts"].get("b", {}) and len(probes) == 1)

fresh_state(active="a", accounts={"a": {"last_seen": time.time()}, "b": {}})
R._REFRESH_TRIED.clear()
probes.clear()
R.ACCOUNTS["b"]["disabled"] = True
try:
    asyncio.run(R.refresh_idle_accounts())
finally:
    del R.ACCOUNTS["b"]["disabled"]
check("a disabled idle account gets no probe", probes == [])

# a transport failure on one account's probe is reported and the next account still refreshes
fresh_state(active="a", accounts={"a": {"last_seen": time.time() - 7200, "util_7d": 0.01},
                                  "b": {"last_seen": time.time() - 7200, "util_7d": 0.01}})
R._REFRESH_TRIED.clear()
probes.clear()


def failing_first(req):
    probes.append(req.headers["authorization"])
    if req.headers["authorization"] == "Bearer tok-a":
        raise httpx.ConnectError("boom", request=req)
    return httpx.Response(200, headers=unified(u5="0.14", u7="0.69"))


use_upstream(failing_first)
err = io.StringIO()
with contextlib.redirect_stderr(err):
    asyncio.run(R.refresh_idle_accounts())
check("a failed probe warns with the exception type and the account",
      "usage refresh of a failed (ConnectError)" in err.getvalue())
check("the next account is still probed after the failure",
      probes == ["Bearer tok-a", "Bearer tok-b"])
check("the next account's usage is refreshed; the failed one is untouched",
      R.STATE["accounts"]["b"]["util_7d"] == 0.69 and R.STATE["accounts"]["a"]["util_7d"] == 0.01)

# refresh_idle_s 0 = off. The guard lives in refresh_loop(), so drive one
# iteration; the stubbed sleep ends the loop after it.
R.HISTORY_PATH, R.USAGE_PATH = tmp / "usage_history.json", tmp / "device_usage.json"
fresh_state(active="a", accounts={"a": {"last_seen": time.time(), "reset_7d": time.time() + 86400},
                                  "b": {}})
R.HISTORY.clear()
R._REFRESH_TRIED.clear()
probes.clear()
use_upstream(lambda req: probes.append(1) or httpx.Response(200, headers=unified()))
real_sleep, real_idle_s = R.asyncio.sleep, R.REFRESH_IDLE_S


async def stop_loop(_seconds):
    raise asyncio.CancelledError


R.asyncio.sleep, R.REFRESH_IDLE_S = stop_loop, 0
try:
    asyncio.run(R.refresh_loop())
    raise AssertionError("refresh_loop must end on the stubbed sleep")
except asyncio.CancelledError:
    pass
finally:
    R.asyncio.sleep, R.REFRESH_IDLE_S = real_sleep, real_idle_s
check("refresh_idle_s 0: one loop iteration sends no probe", probes == [])
check("refresh_idle_s 0: the loop still records history", R.HISTORY_PATH.exists())

# --- 10. panel_tailnet: Tailscale Serve identity opens the panel, not the proxy
ts = {"tailscale-user-login": "alice@example.com"}
R.PANEL_TAILNET = False
check("Tailscale header alone is refused while panel_tailnet is off",
      call(path="/rotate/status", key=None, method="GET", headers=ts).status_code == 401)
R.PANEL_TAILNET = True
for p in ("/rotate/status", "/rotate/stats", "/rotate/panel"):
    check(f"panel_tailnet: keyless {p} with the Tailscale header → 200",
          call(path=p, key=None, method="GET", headers=ts).status_code == 200)
check("panel_tailnet: keyless without the header → 401",
      call(path="/rotate/stats", key=None, method="GET").status_code == 401)
use_upstream(lambda req: httpx.Response(200))
check("panel_tailnet: the header never opens the proxy itself",
      call(key=None, headers=ts).status_code == 401)
R.PANEL_TAILNET = False

# --- 11. /v1/chat/completions end to end -------------------------------------
OAI_BODY = {"model": "claude-sonnet-5", "messages": [{"role": "user", "content": "hi"}]}
real_note = R.note_response
noted_calls = []


async def spy_note(account, headers, status, **kw):
    noted_calls.append((account, status))
    return await real_note(account, headers, status, **kw)


R.note_response = spy_note


def sse_events(*texts):
    out = ('data: {"type":"message_start","message":{"id":"msg_7","model":"claude-sonnet-5",'
           '"usage":{"input_tokens":12,"cache_read_input_tokens":3}}}\n\n')
    for t in texts:
        out += ('data: ' + json.dumps({"type": "content_block_delta",
                                       "delta": {"type": "text_delta", "text": t}}) + '\n\n')
    return out + ('data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
                  '"usage":{"output_tokens":7}}\n\ndata: {"type":"message_stop"}\n\n')


# (1) no valid device key
fresh_state()
use_upstream(lambda req: httpx.Response(200))
r = call("/v1/chat/completions", key="wrong-key", body=OAI_BODY)
check("chat: unknown device key → 401 authentication_error",
      r.status_code == 401 and r.json()["error"]["type"] == "authentication_error"
      and "unknown device key" in r.json()["error"]["message"])

# (2) every account spent → 503 + retry-after from earliest_relief
now = time.time()
fresh_state("a", {"a": {"util_5h": 1.0, "reset_5h": now + 100},
                  "b": {"util_5h": 1.0, "reset_5h": now + 2000}})
R.HOLD_MAX_S = 0
use_upstream(lambda req: httpx.Response(
    429, json={"error": "limit"},
    headers=unified(u5="1.0", status="rejected",
                    **{"anthropic-ratelimit-unified-5h-reset": str(now + 100)})))
r = call("/v1/chat/completions", body=OAI_BODY)
check("chat: spent accounts give 503 overloaded_error",
      r.status_code == 503 and r.json()["error"]["type"] == "overloaded_error")
check("chat: retry-after comes from earliest_relief",
      95 <= int(r.headers["retry-after"]) <= 101
      and f"retry in {r.headers['retry-after']}s" in r.json()["error"]["message"])
check("chat: 503 carries the x-rotate and rate-limit headers",
      r.headers["x-rotate-account"] == "a"
      and r.headers["anthropic-ratelimit-unified-status"] == "rejected"
      and r.headers["anthropic-ratelimit-unified-5h-utilization"] == "1.0")
check("chat: the audit record has status 503",
      audit_recs()[-1]["status"] == 503 and audit_recs()[-1]["path"] == "/v1/chat/completions")

# (3) stream: true, answered as an SSE stream
fresh_state()
noted_calls.clear()
closed = []


class TrackedBody(ChunkedBody):
    async def aclose(self):
        closed.append(1)


SSE_TEXT = sse_events("He", "llo ", "world")
use_upstream(lambda req: httpx.Response(
    200, stream=TrackedBody(SSE_TEXT[:60].encode(), SSE_TEXT[60:].encode()),
    headers={**unified(u5="0.42"), "content-type": "text/event-stream"}))
r = call("/v1/chat/completions", body={**OAI_BODY, "stream": True})
data_lines = [l for l in r.text.splitlines() if l.startswith("data:")]
chunks = [json.loads(l[5:]) for l in data_lines if l[5:].strip() != "[DONE]"]
check("chat stream: 200 text/event-stream", r.status_code == 200
      and "text/event-stream" in r.headers["content-type"])
check("chat stream: delta text equals the upstream text",
      "".join(c["choices"][0]["delta"].get("content", "")
              for c in chunks if c["choices"]) == "Hello world")
check("chat stream: the last chunk is data: [DONE]", data_lines[-1] == "data: [DONE]")
check("chat stream: the upstream is closed", closed == [1])
rec = audit_recs()[-1]
check("chat stream: usage and util_5h are audited",
      rec["usage"] == {"input_tokens": 12, "output_tokens": 7,
                       "cache_read_input_tokens": 3, "cache_creation_input_tokens": 0}
      and rec["util_5h"] == "0.42" and rec["status"] == 200)
check("chat stream: note_response is called once, for the account",
      noted_calls == [("a", 200)] and R.STATE["accounts"]["a"]["util_5h"] == 0.42)

# (4) 200 JSON body converted to chat.completion
fresh_state()
noted_calls.clear()
MSG = {"id": "msg_j", "model": "claude-sonnet-5", "stop_reason": "end_turn",
       "content": [{"type": "text", "text": "hello json"}],
       "usage": {"input_tokens": 10, "output_tokens": 5}}
use_upstream(lambda req: httpx.Response(200, json=MSG, headers=unified(u5="0.31", u7="0.22")))
r = call("/v1/chat/completions", body=OAI_BODY)
check("chat json: converted by anthropic_to_oai",
      r.status_code == 200 and r.json()["object"] == "chat.completion"
      and r.json()["id"] == "msg_j"
      and r.json()["choices"][0]["message"]["content"] == "hello json"
      and r.json()["usage"] == {"prompt_tokens": 10, "completion_tokens": 5,
                                "total_tokens": 15})
rec = audit_recs()[-1]
check("chat json: usage is summarised into the audit record",
      rec["usage"] == {"input_tokens": 10, "output_tokens": 5,
                       "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
      and rec["util_5h"] == "0.31" and rec["model"] == "claude-sonnet-5")
check("chat json: note_response is called once, since the send did not note it",
      noted_calls == [("a", 200)] and R.STATE["accounts"]["a"]["util_7d"] == 0.22)
check("chat json: the forwarded rate-limit headers are echoed",
      r.headers["x-rotate-account"] == "a"
      and r.headers["anthropic-ratelimit-unified-5h-utilization"] == "0.31"
      and r.headers["anthropic-ratelimit-unified-7d-utilization"] == "0.22"
      and r.headers["anthropic-ratelimit-unified-status"] == "allowed"
      and "anthropic-ratelimit-unified-5h-reset" in r.headers)

# (5) non-200 reply becomes an api_error with the upstream status
fresh_state()
use_upstream(lambda req: httpx.Response(
    500, json={"type": "error", "error": {"type": "api_error", "message": "boom inside"}},
    headers=unified()))
r = call("/v1/chat/completions", body=OAI_BODY)
check("chat error: JSON error message is relayed with the upstream status",
      r.status_code == 500 and r.json() == {"error": {"message": "upstream: boom inside",
                                                      "type": "api_error"}})
check("chat error: the audit record keeps the upstream status", audit_recs()[-1]["status"] == 500)
fresh_state()
page = "<html>" + "x" * 300
use_upstream(lambda req: httpx.Response(502, content=page.encode(), headers=unified()))
r = call("/v1/chat/completions", body=OAI_BODY)
check("chat error: a non-JSON body gives its first 200 bytes",
      r.status_code == 502 and r.json()["error"]["type"] == "api_error"
      and r.json()["error"]["message"] == "upstream: " + page[:200])
R.note_response = real_note

# --- 12. transparent proxy: odd queries and bodies, odd upstream replies ------
def raw_asgi(path, query, body=b"{}"):
    """Drive the app with a raw query string; httpx would percent-encode it."""
    sent = []

    async def _go():
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                 "method": "POST", "scheme": "http", "path": path,
                 "raw_path": path.encode(), "query_string": query,
                 "headers": [(b"authorization", b"Bearer devkey-1")],
                 "server": ("proxy", 80), "client": ("127.0.0.1", 1)}
        pending = [{"type": "http.request", "body": body, "more_body": False}]

        async def receive():
            if pending:
                return pending.pop(0)
            await asyncio.Event().wait()  # the client stays connected

        async def send(msg):
            sent.append(msg)

        await R.app(scope, receive, send)
    asyncio.run(_go())
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    return status, b"".join(m.get("body", b"") for m in sent)


# (1) a query string that is not percent-encoded ASCII
fresh_state()
reached = []
use_upstream(lambda req: reached.append(req.url) or httpx.Response(200))
status, content = raw_asgi("/v1/messages", "x=é".encode())
check("raw non-ASCII query string → 400 with the percent-encoding detail",
      status == 400 and json.loads(content) == {"detail": "query string must use percent-encoding"})
check("...and the upstream was never called", reached == [])
status, _ = raw_asgi("/v1/messages", b"x=%C3%A9")
check("...while a percent-encoded query is forwarded", status == 200 and len(reached) == 1)

# (2) a body that is not JSON, or nested too deeply, leaves model None
for label, raw_body in (("not valid JSON", b"{not json"),
                        ("nested too deeply", b"[" * 100000 + b"]" * 100000)):
    fresh_state()
    got = []
    use_upstream(lambda req: got.append(req.content) or httpx.Response(
        200, json={"usage": {"input_tokens": 1, "output_tokens": 1}}, headers=unified()))
    r = call(raw=raw_body)
    rec = audit_recs()[-1]
    check(f"body {label}: model is None in the audit record", rec["model"] is None)
    check(f"body {label}: still forwarded unchanged and answered 200",
          r.status_code == 200 and rec["status"] == 200 and got == [raw_body])

# (3) a non-JSON upstream body
for label, upstream_body in (("text", b"plain text, not json"),
                             ("bytes that are not UTF-8", b"\xff\xfe\x00 raw")):
    fresh_state()
    use_upstream(lambda req: httpx.Response(
        200, content=upstream_body, headers={**unified(), "content-type": "text/plain"}))
    r = call()
    rec = audit_recs()[-1]
    check(f"upstream {label}: relayed unchanged with its status",
          r.status_code == 200 and r.content == upstream_body)
    check(f"upstream {label}: no usage in the audit record", "usage" not in rec
          and rec["status"] == 200)

# (4) burst 429 pacing, read from the account's pace_until
for label, hdrs, want in (("retry-after: soon", {"retry-after": "soon"}, 15),
                          ("no retry-after", {}, 15),
                          ("retry-after: 600", {"retry-after": "600"}, 60)):
    fresh_state("a", {"a": {"util_5h": 0.3, "util_7d": 0.1}})
    t0 = time.time()
    verdict = asyncio.run(R.note_response(
        "a", httpx.Headers({**unified(u5="0.3"), **hdrs}), 429))
    pace_until = R.STATE["accounts"]["a"]["pace_until"]
    check(f"burst 429 with {label}: verdict burst, paced {want} s",
          verdict == "burst" and abs(pace_until - (t0 + want)) < 1)

print(f"\nall {PASS} checks passed")
