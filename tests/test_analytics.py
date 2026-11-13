"""Offline tests for claude-rotate analytics: SSE usage capture, pricing,
cost math, and aggregate_audit rollups/alerts/roster."""
import asyncio
import contextlib
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


# --- usage_from_sse: max-accumulate across events, ignore junk ---
sink = {}
sse = (
    'event: message_start\n'
    'data: {"message": {"usage": {"input_tokens": 10, "output_tokens": 1, "cache_read_input_tokens": 200}}}\n\n'
    'event: message_delta\n'
    'data: {"usage": {"output_tokens": 42}}\n\n'
    'data: {broken json with "usage" inside}\n'
    'data: [DONE]\n'
)
R.usage_from_sse(sse, sink)
check("sse captures input_tokens", sink.get("input_tokens") == 10)
check("sse max-accumulates output_tokens", sink.get("output_tokens") == 42)
check("sse captures cache reads", sink.get("cache_read_input_tokens") == 200)

# --- summarize_usage ---
check("summarize None → None", R.summarize_usage(None) is None)
check("summarize fills zeros", R.summarize_usage({"input_tokens": 5}) == {
    "input_tokens": 5, "output_tokens": 0,
    "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0})

# --- price_for: substring match + default ---
check("opus priced", R.price_for("claude-opus-5") == [5.0, 25.0])
check("fable priced", R.price_for("claude-fable-5") == [10.0, 50.0])
check("unknown model → default", R.price_for("weird-model-9") == R.DEFAULT_PRICE)
check("None model → default", R.price_for(None) == R.DEFAULT_PRICE)

# --- api_cost: in + out + cache read 0.1x + cache write 1.25x ---
u = {"input_tokens": 1_000_000, "output_tokens": 1_000_000,
     "cache_read_input_tokens": 1_000_000, "cache_creation_input_tokens": 1_000_000}
# opus: 5 + 25 + 0.5 + 6.25
check("api_cost math (opus)", abs(R.api_cost("claude-opus-5", u) - 36.75) < 1e-9)
check("api_cost empty usage is 0", R.api_cost("claude-opus-5", {}) == 0.0)

# --- aggregate_audit: rollups, window cut-off, alerts, roster ---
tmp = Path(tempfile.mkdtemp())
R.AUDIT_PATH = tmp / "audit.jsonl"
R.CFG["devices"] = {"laptop": "key-laptop-abcd", "ci-box": "key-ci-efgh", "idle-dev": "key-idle-ijkl"}
R.ACCOUNTS = {"a": {"name": "a"}, "b": {"name": "b"}}
R.STATE = {"active_account": "a", "events": [],
           "accounts": {"a": {"util_5h": 0.85, "reset_5h": time.time() + 3600}, "b": {}}}

FMT = "%Y-%m-%dT%H:%M:%S%z"
now = time.time()


def rec(ts, device, model, account, inp=0, out=0, cr=0, cw=0):
    return {"ts": time.strftime(FMT, time.localtime(ts)), "device": device,
            "model": model, "account": account, "status": 200,
            "usage": {"input_tokens": inp, "output_tokens": out,
                      "cache_read_input_tokens": cr, "cache_creation_input_tokens": cw}}


records = [
    rec(now - 60, "laptop", "claude-opus-5", "a", inp=1000, out=500),        # last hour, expensive
    rec(now - 7200, "laptop", "claude-haiku-4-5", "a", inp=2000, out=100),   # today, outside hour
    rec(now - 90000, "laptop", "claude-opus-5", "a", inp=9999, out=9999),    # >24h → excluded
    rec(now - 120, "ci-box", "claude-sonnet-5", "b", inp=6_000_000, out=10), # token/hour alert
]
with R.AUDIT_PATH.open("w") as f:
    for r in records:
        f.write(json.dumps(r) + "\n")
    f.write("not json\n")  # must be skipped, not crash

agg = R.aggregate_audit()

check("old record excluded from device rollup",
      agg["by_device"]["laptop"]["requests"] == 2)
check("device rollup sums tokens",
      agg["by_device"]["laptop"]["input_tokens"] == 3000)
check("model rollup present", agg["by_model"]["claude-opus-5"]["requests"] == 1)
check("account rollup present", agg["by_account"]["b"]["requests"] == 1)
check("cost accumulated", agg["by_device"]["laptop"]["cost_usd"] > 0)

alert_texts = " | ".join(a["text"] for a in agg["alerts"])
check("token/hour alert fires", "ci-box" in alert_texts and "6,000,010" in alert_texts)
check("expensive-model alert fires", "laptop / claude-opus-5" in alert_texts)
check("util warn alert fires", "a 5h window at 85%" in alert_texts)

check("no token alert without a 401", "refused its token" not in alert_texts)


def with_status(ts, account, status):
    r = rec(ts, "laptop", "claude-haiku-4-5", account)
    r["status"] = status
    return r


with R.AUDIT_PATH.open("a") as f:
    for r in (with_status(now - 30, "a", 401), with_status(now - 20, "a", 401),
              with_status(now - 7200, "b", 401), with_status(now - 10, "b", 403)):
        f.write(json.dumps(r) + "\n")
token_alerts = [x["text"] for x in R.aggregate_audit()["alerts"]
                if "refused its token" in x["text"]]
check("recent 401s give one warn alert naming the account, with the count",
      token_alerts == ["a: upstream refused its token (401) 2 times in the last "
                       "hour; renew it with add-account/add-token"])
check("a 401 older than an hour and a 403 give none",
      not any(t.startswith("b:") for t in token_alerts))

# --- alerts.webhook_url: partial alerts object, one POST per alert per hour ---
HOOK = "http://hooks.test/secret-path"
R.ALERT_CFG = R.settings({"devices": {}, "accounts": [], "alerts": {"webhook_url": HOOK}})["ALERT_CFG"]
check("an alerts object holding only webhook_url keeps the default rules",
      R.ALERT_CFG == {**R.DEFAULT_ALERTS, "webhook_url": HOOK})
check("...and the default rules still alert (no KeyError)",
      any("ci-box consumed 6,000,010 tokens" in x["text"] for x in R.aggregate_audit()["alerts"]))
full = {"device_tokens_per_hour": 1, "expensive_model_patterns": ["x"], "util_warn": 0.5}
check("a full alerts object behaves as before",
      R.settings({"devices": {}, "accounts": [], "alerts": full})["ALERT_CFG"] == full)

posted = []


def receiver(request):
    posted.append(json.loads(request.content))
    return httpx.Response(200)


def send(handler):
    R.webhook_client = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                         timeout=httpx.Timeout(5.0))
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        asyncio.run(R.send_alert_webhook())
    return err.getvalue()


R.ALERT_SENT.clear()
now_alerts = [x["text"] for x in R.aggregate_audit()["alerts"]]
send(receiver)
check("each active alert is posted once, with text, level and ts",
      sorted(p["text"] for p in posted) == sorted(now_alerts) and len(posted) >= 2
      and all(set(p) == {"text", "level", "ts"} for p in posted)
      and {p["level"] for p in posted} == {"warn", "info"})
n = len(posted)
send(receiver)
check("a second evaluation within the hour sends nothing", len(posted) == n)
for t in R.ALERT_SENT:
    R.ALERT_SENT[t] -= 3601
send(receiver)
check("an alert is sent again after an hour", len(posted) == 2 * n)

# Alerts are deduped by a stable key per rule and subject, not by their text,
# which embeds counts that move.
keys = [x["key"] for x in R.aggregate_audit()["alerts"]]
check("every alert has a key, and the keys are unique within one evaluation",
      len(keys) >= 4 and len(set(keys)) == len(keys)
      and {"partial", "device_tokens:ci-box", "expensive_model:laptop / claude-opus-5",
           "token_refused:a", "util_5h:a"} <= set(keys))
with R.AUDIT_PATH.open("a") as f:
    f.write(json.dumps(with_status(now - 5, "a", 401)) + "\n")
    f.write(json.dumps(rec(now - 4, "ci-box", "claude-sonnet-5", "b", inp=1000)) + "\n")
changed = [x["text"] for x in R.aggregate_audit()["alerts"]]
check("the 401 count and the token total changed in the alert texts",
      any("(401) 3 times" in t for t in changed) and any("6,001,010" in t for t in changed))
m = len(posted)
send(receiver)
check("a changed count inside the hour posts nothing", len(posted) == m)
with R.AUDIT_PATH.open("a") as f:
    f.write(json.dumps(with_status(now - 3, "b", 401)) + "\n")
send(receiver)
check("a 401 for a second account posts exactly one new alert, for that account",
      len(posted) == m + 1 and posted[-1]["text"].startswith("b: upstream refused its token"))
m = len(posted)
for k in R.ALERT_SENT:
    R.ALERT_SENT[k] -= 3601
send(receiver)
check("after an hour the alerts post again, with the current count in the text",
      len(posted) == m + len(keys) + 1
      and any(p["text"].startswith("a: upstream refused its token (401) 3 times")
              for p in posted[m:]))
n = len(posted)  # the checks below count from here
R.ALERT_CFG ={k: v for k, v in R.ALERT_CFG.items() if k != "webhook_url"}
R.ALERT_SENT.clear()
send(receiver)
check("with no webhook_url nothing is sent", len(posted) == n)

# Only the 401 alerts (one per refused account) are left active, so each failed
# POST prints one warning. The unparsable audit line above set the
# partial-history alert; clear it.
R._AUDIT_CACHE["limited_until"] = 0
R.ALERT_CFG = {"device_tokens_per_hour": 10**12, "expensive_model_patterns": [],
               "util_warn": 2.0, "webhook_url": HOOK}
N_ACTIVE = len(R.aggregate_audit()["alerts"])


def timing_out(request):
    raise httpx.ReadTimeout("slow", request=request)


for name, handler in (("a 500", lambda request: httpx.Response(500)), ("a timeout", timing_out)):
    R.ALERT_SENT.clear()
    err = send(handler)
    check(f"{name} prints one warning without the URL and does not raise",
          err.count("warning: alert webhook failed") == N_ACTIVE and "secret-path" not in err)
    check(f"{name} is not retried within the hour", send(handler) == "")

devs = agg["devices"]
check("roster covers idle device too", "idle-dev" in devs)
check("recent device online", devs["laptop"]["online"] is True)
check("idle device offline, never seen",
      devs["idle-dev"]["online"] is False and devs["idle-dev"]["last_seen_min"] is None)
check("roster last-hour counters", devs["ci-box"]["req_1h"] == 1
      and devs["ci-box"]["tokens_1h"] == 6_000_010)
check("roster shows key suffix", devs["laptop"]["key_suffix"] == "…abcd")
check("roster last model/account", devs["laptop"]["last_model"] == "claude-opus-5"
      and devs["laptop"]["last_account"] == "a")

# --- weekly chart: summed usage dropping at resets, weights, run-out projection ---
H = 3600
old_a, new_a, rb = now - 20 * H, now - 20 * H + 7 * 86400, now + 50 * H


def weekly_state(ua, ub):
    R.STATE = {"active_account": "a", "events": [], "accounts": {
        "a": {"util_7d": ua, "reset_7d": new_a}, "b": {"util_7d": ub, "reset_7d": rb}}}


R.ACCOUNTS = {"a": {"name": "a"}, "b": {"name": "b"}}
R.HISTORY = [{"ts": now - 30 * H, "accounts": {"a": [0.90, old_a], "b": [0.20, rb]}},
             {"ts": now - 20 * H, "accounts": {"a": [0.00, old_a], "b": [0.20, rb]}},  # a's week reset
             {"ts": now - 10 * H, "accounts": {"a": [0.10, new_a], "b": [0.30, rb]}}]
weekly_state(0.30, 0.30)
s = R.weekly_series(now)
check("chart sums each account's current weekly usage, dropping at a reset",
      [p[1] for p in s] == [110, 20, 40, 60])
check("capacity sums the weights", [p[2] for p in s] == [200, 200, 200, 200])
check("spent counts rises after the first sample, a new window in full",
      [p[3] for p in s] == [0, 0, 20, 40])

out = R.weekly_outlook(now)
check("rate taken over the last 24 h; the series goes out without spent",
      out["rate_per_day"] == 48.0 and out["capacity"] == 200
      and [len(p) for p in out["series"]] == [3, 3, 3, 3])
proj = out["projection"]
# b resets at +50 h with its week fully used (-100); everything is spent at +120 h and
# usage pauses until a resets at +148 h (-100); then it resumes until the 7.5-day horizon.
check("7.5-day projection drops usage at each reset and pauses while all is spent",
      [p[1] for p in proj] == [60, 160, 60, 200, 200, 100, 164])
check("projected capacity stays flat", all(p[2] == 200 for p in proj))
check("ETA is the first shortfall; quota returns at a's reset",
      abs(out["eta"] - (now + 120 * H)) <= 5 and out["back"] == round(new_a)  # ts rounding
      and out["back_account"] == "a" and proj[-1][0] == round(now + 7.5 * 86400))

weekly_state(0.95, 0.95)
out = R.weekly_outlook(now)
check("run-out ETA when the remaining 10 points go at 8.5 points/h",
      abs(out["eta"] - (now + 10 / 8.5 * H)) <= 1 and out["back"] == round(rb)
      and out["back_account"] == "b")

R.ACCOUNTS["b"]["weight"] = 0.25  # b becomes a quarter-size plan
weekly_state(0.30, 0.30)
check("weights scale an account's share", R.weekly_series(now)[-1][1:3] == [37.5, 125])
R.ACCOUNTS["b"] = {"name": "b", "disabled": True}
check("disabled accounts leave the chart", R.weekly_series(now)[-1][1:3] == [30, 100])
R.ACCOUNTS["b"] = {"name": "b"}

R.HISTORY = [{"ts": now - 20 * H, "accounts": {"a": [0.50, now + 5 * H], "b": [0.50, rb]}},
             {"ts": now - 10 * H, "accounts": {"a": [1.00, now + 5 * H], "b": [1.00, rb]}}]
R.STATE = {"active_account": "a", "events": [], "accounts": {
    "a": {"util_7d": 1.0, "reset_7d": now + 5 * H}, "b": {"util_7d": 1.0, "reset_7d": rb}}}
out = R.weekly_outlook(now)
check("rate ignores time at the limit: 100 points in the 10 h with quota left",
      out["rate_per_day"] == 240.0 and abs(out["eta"] - now) <= 1 and out["back_account"] == "a")

# flat usage over the last 24 h: no rate, so a note and no projection
R.HISTORY = [{"ts": now - 20 * H, "accounts": {"a": [0.40, now + 5 * H], "b": [0.40, rb]}},
             {"ts": now - 10 * H, "accounts": {"a": [0.40, now + 5 * H], "b": [0.40, rb]}}]
R.STATE = {"active_account": "a", "events": [], "accounts": {
    "a": {"util_7d": 0.4, "reset_7d": now + 5 * H}, "b": {"util_7d": 0.4, "reset_7d": rb}}}
out = R.weekly_outlook(now)
check("flat weekly usage: the note says it did not rise, with no projection or ETA",
      out["note"] == "weekly usage did not rise over the last 24 h"
      and out["rate_per_day"] == 0.0 and out["projection"] == [] and out["eta"] is None)

# a weekly reset already past is rolled forward by 7 days before the forecast
past = now - 5 * H
R.HISTORY = [{"ts": now - 20 * H, "accounts": {"a": [0.50, past], "b": [0.50, rb]}},
             {"ts": now - 10 * H, "accounts": {"a": [1.00, past], "b": [1.00, rb]}}]
R.STATE = {"active_account": "a", "events": [], "accounts": {
    "a": {"util_7d": 1.0, "reset_7d": past}, "b": {"util_7d": 1.0, "reset_7d": rb}}}
out = R.weekly_outlook(now)
check("a past weekly reset is rolled forward 7 days: it is the one used as the quota return",
      out["back_account"] == "b" and abs(out["back"] - rb) <= 1)
R.STATE["accounts"]["b"]["reset_7d"] = now + 9 * 86400
R.HISTORY[0]["accounts"]["b"][1] = R.HISTORY[1]["accounts"]["b"][1] = now + 9 * 86400
out = R.weekly_outlook(now)
check("the rolled reset (past + 7 days) is the soonest return, within 1 s",
      out["back_account"] == "a" and abs(out["back"] - (past + 7 * 86400)) <= 1)

R.HISTORY = []
check("no rate without an hour of history", R.weekly_outlook(now)["eta"] is None
      and "not enough history" in R.weekly_outlook(now)["note"])

# --- weekly demand: the rate over stretches when an account could serve ---
R1, R2 = now + 20 * H, now + 20 * H + 7 * 86400
R.ACCOUNTS = {"a": {"name": "a", "weekly_cap": 0.3}, "b": {"name": "b", "weekly_cap": 0.3}}
# first 12 h: both accounts over their 0.3 allowance (rising 0.01/h each); then a new
# week at 0, rising 0.02/h each, under the allowance
R.HISTORY = [{"ts": now - (24 - i) * H, "accounts": (
    {n: [0.40 + 0.01 * i, R1] for n in "ab"} if i < 12 else {n: [0.02 * (i - 12), R2] for n in "ab"})}
    for i in range(24)]
R.STATE = {"active_account": "a", "events": [], "accounts": {
    n: {"util_7d": 0.24, "reset_7d": R2} for n in "ab"}}
d = R.weekly_demand(now)
check("an hour when every account is over its allowance is left out of the rate",
      d["rate_per_day"] == 96.0 and d["measured_h"] == 12.0 and d["window_h"] == 24.0)
check("week_pct is 7 days of the rate; capacity sums weight x weekly_cap",
      d["week_pct"] == 672.0 and d["capacity_pct"] == 60.0 and d["accounts_needed"] == 7
      and d["shortfall_pct"] == 612.0 and d["note"] is None)
R.ACCOUNTS = {n: {"name": n} for n in "ab"}
check("no weekly_cap means capacity 100% per account", R.weekly_demand(now)["capacity_pct"] == 200.0)
R.ACCOUNTS = {n: {"name": n, "weekly_cap": 0.5} for n in "ab"}
check("a 7-day cap of 0.5 halves capacity_pct", R.weekly_demand(now)["capacity_pct"] == 100.0)

# paced out by weekly_pacing: allowance at the sample's time, not now
R.ACCOUNTS = {n: {"name": n, "weekly_pacing": True, "pace_lookahead": "0m"} for n in "ab"}
R.HISTORY = [{"ts": now - (24 - i) * H, "accounts": {n: [0.60, now + 3.5 * 86400] for n in "ab"}}
             for i in range(12)] \
    + [{"ts": now - (12 - i) * H, "accounts": {n: [0.0, now + 6 * 86400] for n in "ab"}}
       for i in range(12)]
R.STATE = {"active_account": "a", "events": [], "accounts": {
    n: {"util_7d": 0.0, "reset_7d": now + 6 * 86400} for n in "ab"}}
check("pacing allowance is taken at the sample's time",
      R.weekly_demand(now)["measured_h"] == 12.0)
R.ACCOUNTS = {n: {"name": n} for n in "ab"}

# 2.5 days of history is used whole
R.HISTORY = [{"ts": now - (60 - i) * H, "accounts": {n: [0.01 * i, now + 100 * H] for n in "ab"}}
             for i in range(60)]
R.STATE = {"active_account": "a", "events": [], "accounts": {
    n: {"util_7d": 0.60, "reset_7d": now + 100 * H} for n in "ab"}}
d = R.weekly_demand(now)
check("a history shorter than 7 days is used whole",
      d["window_h"] == 60.0 and d["measured_h"] == 60.0 and d["rate_per_day"] == 48.0)
# only the last 7 days count
R.HISTORY = [{"ts": now - (240 - i) * H, "accounts": {"a": [0.0, now + 100 * H]}}
             for i in range(60)] + [
    {"ts": now - (24 - i) * H, "accounts": {"a": [0.01 * i, now + 100 * H]}} for i in range(24)]
R.ACCOUNTS = {"a": {"name": "a"}}
R.STATE = {"active_account": "a", "events": [], "accounts": {
    "a": {"util_7d": 0.24, "reset_7d": now + 100 * H}}}
d = R.weekly_demand(now)
check("older history than 7 days is outside the window",
      d["window_h"] == 24.0 and d["rate_per_day"] == 24.0)

# under 6 counted hours: nulls and the note
R.HISTORY = [{"ts": now - (5 - i) * H, "accounts": {"a": [0.01 * i, now + 100 * H]}} for i in range(5)]
d = R.weekly_demand(now)
check("under 6 counted hours gives the note and nulls",
      d["note"] == "not enough history with an account available yet"
      and all(d[k] is None for k in ("rate_per_day", "measured_h", "window_h", "week_pct",
                                      "capacity_pct", "accounts_needed", "shortfall_pct")))
R.HISTORY = []
check("no history gives the note", R.weekly_demand(now)["week_pct"] is None)


def demand_for(step):
    R.HISTORY = [{"ts": now - (24 - i) * H, "accounts": {"a": [step * i, now + 100 * H]}}
                 for i in range(24)]
    R.STATE = {"active_account": "a", "events": [], "accounts": {
        "a": {"util_7d": step * 24, "reset_7d": now + 100 * H}}}
    return R.weekly_demand(now)


d = demand_for(0.0239)
check("accounts_needed rounds up: week_pct 401 gives 5", 401 <= d["week_pct"] < 402
      and d["accounts_needed"] == 5)
d = demand_for(0.0237)
check("week_pct just under 400 gives 4 accounts", 398 <= d["week_pct"] < 400
      and d["accounts_needed"] == 4)
check("cap_allowance with no extra arguments is unchanged",
      R.cap_allowance("a") == 1.0)

R.HISTORY_PATH = tmp / "usage_history.json"
R.HISTORY = [{"ts": now - 15 * 86400, "accounts": {"a": [0.1, old_a]}}]
R.record_history(now)
check("history sample saved, 15-day-old one dropped",
      [h["ts"] for h in R.HISTORY] == [round(now)]
      and json.loads(R.HISTORY_PATH.read_text()) == R.HISTORY)
R.record_history(now + 600)
R.record_history(now + 1801)  # stored ts is rounded
check("one sample per 30 min", len(R.HISTORY) == 2)

# an unreadable or invalid history file is ignored with a warning naming it
for label, content in (("invalid JSON", "{not json"), ("a non-UTF-8 file", b"\xff\xfe\x00")):
    if isinstance(content, bytes):
        R.HISTORY_PATH.write_bytes(content)
    else:
        R.HISTORY_PATH.write_text(content)
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        got = R.load_history()
    check(f"load_history on {label} returns [] and the warning names the file",
          got == [] and str(R.HISTORY_PATH) in err.getvalue() and "warning" in err.getvalue())
R.HISTORY_PATH.write_text('{"ts": 1}')
check("load_history on JSON that is not a list returns []", R.load_history() == [])
R.HISTORY_PATH.unlink()
check("load_history with no file returns [] quietly", R.load_history() == [])

# --- device/model totals: 7 d, 30 d, all time, trend; backfill from the audit logs ---
import datetime as dt
today = dt.date.today()


def iso(n):
    return (today - dt.timedelta(days=n)).isoformat()


def empty_rollup():
    return {"days": {}, "total": {"device": {}, "model": {}, "account": {}}, "since": None,
            "account_since": None}


use = {"input_tokens": 1_000_000, "output_tokens": 10}
R.ROLLUP = empty_rollup()
R.roll_up({"device": "laptop", "model": "claude-opus-5", "account": "a", "usage": use}, iso(0))
R.roll_up({"device": "laptop", "model": "claude-opus-5", "account": "a", "usage": use}, iso(0))
R.roll_up({"device": "old-box", "model": {"bad": 1}, "usage": use}, iso(8))
per = R.usage_periods()
check("7 d covers today and the 6 days before",
      per["7d"]["device"]["laptop"]["requests"] == 2 and "old-box" not in per["7d"]["device"])
check("30 d and all time include older days",
      per["30d"]["device"]["old-box"]["requests"] == 1 and per["all"]["device"]["old-box"]["requests"] == 1)
check("non-string models fold into '?'", "?" in per["all"]["model"])
check("trend is 30 daily costs ending today", len(per["trend"]["laptop"]) == 30
      and per["trend"]["laptop"][-1] == 10.0 and per["trend"]["old-box"][-9] == 5.0)
check("since is the oldest day seen", per["since"] == iso(8))
check("account totals land in 7 d, 30 d and all time",
      per["7d"]["account"]["a"]["requests"] == 2 and per["30d"]["account"]["a"]["requests"] == 2
      and per["all"]["account"]["a"]["requests"] == 2)
check("a record with no account folds into '?', outside 7 d when old",
      per["all"]["account"]["?"]["requests"] == 1 and per["30d"]["account"]["?"]["requests"] == 1
      and "?" not in per["7d"]["account"])
check("account_since is the oldest day an account was rolled up", per["account_since"] == iso(8))

# A file written before the account kind: totals and days lack it.
old_b = {"requests": 4, "input_tokens": 0, "output_tokens": 0, "cache_read": 0,
         "cache_write": 0, "cost_usd": 1.0}
R.ROLLUP = {"days": {iso(2): {"device": {"laptop": dict(old_b)}, "model": {"m": dict(old_b)}}},
            "total": {"device": {"laptop": dict(old_b)}, "model": {"m": dict(old_b)}},
            "since": iso(2)}
old_per = R.usage_periods()
check("an old-shape file reads with empty account totals",
      old_per["all"]["account"] == {} and old_per["7d"]["account"] == {}
      and old_per["account_since"] is None)
R.audit({"device": "laptop", "model": "claude-opus-5", "account": "a", "usage": use})
new_per = R.usage_periods()
check("an old-shape file takes a new audit record and starts account history today",
      new_per["account_since"] == iso(0) and new_per["all"]["account"]["a"]["requests"] == 1
      and new_per["7d"]["account"]["a"]["requests"] == 1)
check("its device and model totals only gain the new record",
      new_per["all"]["device"]["laptop"]["requests"] == 5 and new_per["all"]["model"]["m"]["requests"] == 4
      and new_per["all"]["model"]["claude-opus-5"]["requests"] == 1 and new_per["since"] == iso(2))

R.AUDIT_PATH, R.USAGE_PATH = tmp / "backfill.jsonl", tmp / "device_usage.json"


def line(day, dev):
    return json.dumps({"ts": f"{day}T10:00:00+0000", "device": dev, "model": "claude-sonnet-5",
                       "usage": use}) + "\n"


Path(f"{R.AUDIT_PATH}.1").write_text(line(iso(40), "gone-box") + "not json\n")
R.AUDIT_PATH.write_text(line(iso(0), "laptop"))
R.ROLLUP = empty_rollup()
R.load_rollup()
check("first start rebuilds totals from the rotated copy and the current log",
      R.ROLLUP["total"]["device"]["gone-box"]["requests"] == 1
      and R.ROLLUP["total"]["device"]["laptop"]["requests"] == 1 and R.ROLLUP["since"] == iso(40))
R.save_rollup()
saved = json.loads(R.USAGE_PATH.read_text())
check("saved totals keep all time but only 31 days of daily buckets",
      iso(40) not in saved["days"] and saved["total"]["device"]["gone-box"]["requests"] == 1)
R.ROLLUP = empty_rollup()
R.load_rollup()
check("an existing file is loaded, not rebuilt", R.ROLLUP == saved)
R.audit({"device": "laptop", "model": "claude-sonnet-5", "usage": use})
check("every audited request is added", R.ROLLUP["total"]["device"]["laptop"]["requests"] == 2)

# --- audit tail edges: ageing out, bad lines, a mid-line tail; rollup rebuild ---
from collections import deque


def fresh_cache(path):
    R.AUDIT_PATH = path
    R._AUDIT_CACHE.clear()
    R._AUDIT_CACHE.update(offset=0, records=deque(), sig=b"", bytes=0, limited_until=0)


def jline(r):
    return json.dumps(r) + "\n"


fresh_cache(tmp / "ageing.jsonl")
t_old, t_new = now - 3600, now + 25 * 3600 - 600
old_lines = jline(rec(t_old, "laptop", "claude-haiku-4-5", "a", inp=5)) \
    + jline(rec(t_old + 60, "laptop", "claude-haiku-4-5", "a", inp=6))
R.AUDIT_PATH.write_text(old_lines)
check("both records are cached while they are fresh",
      R.aggregate_audit()["by_device"]["laptop"]["requests"] == 2
      and len(R._AUDIT_CACHE["records"]) == 2 and R._AUDIT_CACHE["bytes"] == len(old_lines))
new_line = jline(rec(t_new, "laptop", "claude-haiku-4-5", "a", inp=7))
with R.AUDIT_PATH.open("a") as f:
    f.write(new_line)
_real_time = time.time
time.time = lambda: now + 25 * 3600
try:
    agg = R.aggregate_audit()
finally:
    time.time = _real_time
check("25 h later the old records are dropped from the cache and the totals",
      [r["usage"]["input_tokens"] for r in R._AUDIT_CACHE["records"]] == [7]
      and agg["by_device"]["laptop"]["requests"] == 1 and agg["by_device"]["laptop"]["input_tokens"] == 7)
check("...and their bytes are subtracted from the cached total",
      R._AUDIT_CACHE["bytes"] == len(new_line) and agg["retained_records"] == 1)

for label, bad in (("a JSON line that is not an object", "[1, 2, 3]\n"),
                   ("a line whose ts does not parse", jline({"ts": "yesterday-ish", "device": "laptop"})),
                   ("a line whose ts is not a string", jline({"ts": 12345, "device": "laptop"}))):
    fresh_cache(tmp / "badline.jsonl")
    R.AUDIT_PATH.write_text(jline(rec(now - 60, "laptop", "claude-haiku-4-5", "a", inp=1)) + bad)
    agg = R.aggregate_audit()
    check(f"{label} is skipped and not counted", agg["by_device"]["laptop"]["requests"] == 1
          and len(R._AUDIT_CACHE["records"]) == 1)
    check(f"{label} marks the history as partial for a day",
          agg["history_limited"] is True and abs(R._AUDIT_CACHE["limited_until"] - (now + 86400)) < 5)

fresh_cache(tmp / "midline.jsonl")
R.AUDIT_PATH.write_text("x" * 500)  # one oversized fragment, no newline anywhere
real_bytes = R.AUDIT_CACHE_BYTES
R.AUDIT_CACHE_BYTES = 100
try:
    R._tail_audit()
finally:
    R.AUDIT_CACHE_BYTES = real_bytes
check("a tail that starts mid-line with no newline moves the offset over the fragment",
      R._AUDIT_CACHE["offset"] == R.AUDIT_PATH.stat().st_size == 500)
check("...and caches nothing, flagging the history as partial",
      len(R._AUDIT_CACHE["records"]) == 0 and R._AUDIT_CACHE["bytes"] == 0
      and R._AUDIT_CACHE["limited_until"] > now)

# an unreadable or invalid rollup file is rebuilt from the audit log with a warning
R.AUDIT_PATH, R.USAGE_PATH = tmp / "rebuild.jsonl", tmp / "rebuild_usage.json"
audited = [rec(now - 60, "laptop", "claude-opus-5", "a", inp=100, out=5),
           rec(now - 120, "ci-box", "claude-haiku-4-5", "b", inp=200, out=7)]
R.AUDIT_PATH.write_text("".join(jline(r) for r in audited))
R.ROLLUP = empty_rollup()
for r in audited:
    R.roll_up(r, r["ts"][:10])
expected = json.loads(json.dumps(R.ROLLUP))
for label, setup in (("invalid JSON", lambda: R.USAGE_PATH.write_text("{truncated")),
                     ("an unreadable path", lambda: R.USAGE_PATH.mkdir())):
    if R.USAGE_PATH.is_dir():
        R.USAGE_PATH.rmdir()
    elif R.USAGE_PATH.exists():
        R.USAGE_PATH.unlink()
    setup()
    R.ROLLUP = empty_rollup()
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        R.load_rollup()
    check(f"rollup file with {label}: a warning says it is rebuilding from the audit log",
          f"warning: rebuilding {R.USAGE_PATH} from the audit log" in err.getvalue())
    check(f"rollup file with {label}: the rollup equals one rebuilt from the audit records",
          R.ROLLUP == expected and R.ROLLUP["total"]["device"]["laptop"]["requests"] == 1)

print(f"\nall {PASS} checks passed")
