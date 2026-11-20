"""Offline tests for claude-rotate switch logic (pick_account / note_response)."""
import asyncio
import sys
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import rotator as R

R.save_state = lambda: None  # don't touch the real state.json


class H(dict):
    """httpx.Headers stand-in (case-insensitive get)."""
    def get(self, k, default=None):
        return super().get(k.lower(), default)


def headers(u5=None, u7=None, r5=None, r7=None, status=None, retry_after=None):
    h = H()
    if u5 is not None: h["anthropic-ratelimit-unified-5h-utilization"] = str(u5)
    if u7 is not None: h["anthropic-ratelimit-unified-7d-utilization"] = str(u7)
    if r5 is not None: h["anthropic-ratelimit-unified-5h-reset"] = str(r5)
    if r7 is not None: h["anthropic-ratelimit-unified-7d-reset"] = str(r7)
    if status is not None: h["anthropic-ratelimit-unified-status"] = status
    if retry_after is not None: h["retry-after"] = str(retry_after)
    return h


def fresh_state(active="a"):
    R.STATE = {"active_account": active, "accounts": {}, "events": [], "last_switch_ts": 0}


R.ACCOUNTS = {"a": {"name": "a"}, "b": {"name": "b"}, "c": {"name": "c"}}
R.THRESHOLD, R.THRESHOLD_7D = 0.8, 0.98
R.STRATEGY, R.COOLDOWN_S, R.SWITCH_MARGIN, R.CF_MARGIN_S = "consume-first", 300, 0.05, 3600
R.SWITCH_MARGIN_7D = 0.03
R.PACE_RATIO, R.RESERVE_SESSIONS = 1.25, 1
R.HARD_LIMIT, R.RESERVE_SPEND_5H_S, R.RESERVE_SPEND_7D_S = 1.0, 3600, 43200
run = asyncio.run
now = time.time()
PASS = 0

def check(name, cond):
    global PASS
    print(("ok  " if cond else "FAIL") + " " + name)
    if not cond:
        sys.exit(1)
    PASS += 1


# 1. consume-first: pick usable account with soonest weekly reset
fresh_state()
R.STATE["accounts"] = {
    "a": {"util_5h": 0.1, "util_7d": 0.3, "reset_7d": now + 500000},
    "b": {"util_5h": 0.5, "util_7d": 0.6, "reset_7d": now + 100000},  # soonest weekly reset
    "c": {"util_5h": 0.05, "util_7d": 0.1, "reset_7d": now + 300000},
}
check("consume-first picks soonest weekly reset", R.pick_account() == "b")

# 1b. ...but skips unusable (5h over threshold) accounts
R.STATE["accounts"]["b"]["util_5h"] = 0.9
check("consume-first skips 5h-exhausted account", R.pick_account() == "c")

# 1c. least-used strategy picks lowest 5h util
R.STRATEGY = "least-used"
check("least-used picks lowest 5h util", R.pick_account() == "c")
R.STRATEGY = "consume-first"

# 1d. most-weekly-left picks lowest weekly util among 5h-usable accounts
R.STRATEGY = "most-weekly-left"
R.STATE["accounts"] = {
    "a": {"util_5h": 0.1, "util_7d": 0.3},
    "b": {"util_5h": 0.9, "util_7d": 0.05},  # most weekly left but 5h session spent
    "c": {"util_5h": 0.5, "util_7d": 0.2},
}
check("most-weekly-left picks most weekly left with session room", R.pick_account() == "c")
R.STATE["accounts"]["b"]["reset_5h"] = now - 10  # b's session window has reset
check("most-weekly-left uses account once its 5h window resets", R.pick_account() == "b")
# 1e. proactive switch below threshold, gated by weekly margin
fresh_state("a")
R.STATE["accounts"] = {"a": {"util_5h": 0.2, "util_7d": 0.5}, "b": {"util_5h": 0.6, "util_7d": 0.3}, "c": {"util_5h": 0.9, "util_7d": 0.9}}
v = run(R.note_response("a", headers(u5=0.2, u7=0.5), 200))
check("most-weekly-left proactive switch", v == "switched" and R.STATE["active_account"] == "b")
check("event reason most_weekly_left", R.STATE["events"][-1]["reason"] == "most_weekly_left")
fresh_state("a")
R.STATE["accounts"] = {"a": {"util_5h": 0.2, "util_7d": 0.32}, "b": {"util_5h": 0.2, "util_7d": 0.30}, "c": {"util_5h": 0.9, "util_7d": 0.9}}
v = run(R.note_response("a", headers(u5=0.2, u7=0.32), 200))
check("most-weekly-left margin prevents churn", v is None and R.STATE["active_account"] == "a")
fresh_state("a")
R.STATE["accounts"] = {"a": {"util_5h": 0.85, "util_7d": 0.1}, "b": {"util_5h": 0.1, "util_7d": 0.6}, "c": {"util_5h": 0.9, "util_7d": 0.9}}
v = run(R.note_response("a", headers(u5=0.85, u7=0.1), 200))
check("most-weekly-left leaves account when its session is spent", v == "switched" and R.STATE["active_account"] == "b")
R.STRATEGY = "consume-first"

# 1f. weekly-pace: most perishable remaining quota wins, not just most left
H_ = 3600
R.STRATEGY = "weekly-pace"
fresh_state("a")
R.STATE["accounts"] = {
    "a": {"util_5h": 0.2, "util_7d": 0.40, "reset_7d": now + 120 * H_},  # 0.60/120h = 0.5%/h
    "b": {"util_5h": 0.2, "util_7d": 0.85, "reset_7d": now + 6 * H_},    # 0.15/6h  = 2.5%/h
    "c": {"util_5h": 0.2, "util_7d": 0.10, "reset_7d": now + 160 * H_},  # 0.90/160h = 0.56%/h
}
check("weekly-pace picks quota about to expire", R.pick_account() == "b")
R.STATE["accounts"]["b"]["util_5h"] = 0.9
check("weekly-pace skips 5h-spent account", R.pick_account() == "c")
check("weekly_pace math", abs(R.weekly_pace("a") - 0.6 / 120) < 1e-4)
R.ACCOUNTS["a"]["weight"] = 4  # e.g. a 20x plan next to 5x plans
check("weekly-pace honours account weight", R.pick_account() == "a")
del R.ACCOUNTS["a"]["weight"]
# 1g. proactive switch needs the ratio margin
fresh_state("a")
R.STATE["accounts"] = {
    "a": {"util_5h": 0.2, "util_7d": 0.5, "reset_7d": now + 100 * H_},  # 0.5%/h
    "b": {"util_5h": 0.3, "util_7d": 0.4, "reset_7d": now + 100 * H_},  # 0.6%/h (1.2x)
    "c": {"util_5h": 0.9, "util_7d": 0.0, "reset_7d": now + 100 * H_},
}
v = run(R.note_response("a", headers(u5=0.2, u7=0.5), 200))
check("weekly-pace ratio margin prevents churn", v is None and R.STATE["active_account"] == "a")
R.STATE["accounts"]["b"]["util_7d"] = 0.3  # 0.7%/h (1.4x), b already mid-session
v = run(R.note_response("a", headers(u5=0.2, u7=0.5), 200))
check("weekly-pace proactive switch", v == "switched" and R.STATE["active_account"] == "b")
check("event reason weekly_pace", R.STATE["events"][-1]["reason"] == "weekly_pace")
R.RESERVE_HEADROOM = 0.03
# 1h. reserve: a voluntary switch may not start a fresh 5h window unless
# somewhere else (the account being left counts) still has room
fresh_state("a")
R.STATE["accounts"] = {
    "a": {"util_5h": 0.78, "util_7d": 0.5, "reset_7d": now + 100 * H_},  # 5h nearly spent
    "b": {"util_5h": 0.0, "util_7d": 0.0, "reset_7d": now + 100 * H_},   # fresh, top pace
    "c": {"util_5h": 0.5, "util_7d": 0.97, "reset_7d": now + 100 * H_},  # week spent
}
v = run(R.note_response("a", headers(u5=0.78, u7=0.5), 200))
check("weekly-pace keeps fresh session when nothing else has room", v is None and R.STATE["active_account"] == "a")
R.STATE["accounts"]["c"]["util_7d"] = 0.9  # c has weekly room again -> a fallback exists
v = run(R.note_response("a", headers(u5=0.78, u7=0.5), 200))
check("weekly-pace starts fresh session when another account has room",
      v == "switched" and R.STATE["active_account"] == "b")
# 1h2. the account being left counts as a fallback (live case 2026-10-23:
# active at 93% week, other account week-spent, freshly reset account idle)
fresh_state("b")
R.STATE["accounts"] = {
    "a": {"util_5h": 0.0, "util_7d": 0.98, "reset_7d": now + 80 * H_},
    "b": {"util_5h": 0.06, "util_7d": 0.93, "reset_7d": now + 85 * H_},
    "c": {"util_5h": 0.0, "util_7d": 1.03, "reset_7d": now - 3600},  # weekly window reset
}
check("week-reset account scores highest", R.pick_account() == "c")
v = run(R.note_response("b", headers(u5=0.06, u7=0.93), 200))
check("weekly-pace switches to freshly reset account, leaving active as fallback",
      v == "switched" and R.STATE["active_account"] == "c")
# 1i. forced switch (5h spent) may use the reserve
fresh_state("a")
R.STATE["accounts"] = {
    "a": {"util_5h": 0.85, "util_7d": 0.5, "reset_7d": now + 100 * H_},
    "b": {"util_5h": 0.0, "util_7d": 0.2, "reset_7d": now + 100 * H_},
    "c": {"util_5h": 0.9, "util_7d": 0.0, "reset_7d": now + 100 * H_},
}
v = run(R.note_response("a", headers(u5=0.85, u7=0.5), 200))
check("weekly-pace forced switch uses reserve session", v == "switched" and R.STATE["active_account"] == "b")
R.STRATEGY = "consume-first"

# 1j. all-unusable fallback skips week-exhausted accounts (live case
# 2026-10-22 21:16: rotated onto a 103%-week account and was rejected)
R.STRATEGY = "weekly-pace"
fresh_state("b")
R.STATE["accounts"] = {
    "a": {"util_5h": 0.07, "util_7d": 0.98, "reset_5h": now + 9000, "reset_7d": now + 300000},
    "b": {"util_5h": 0.98, "util_7d": 0.60, "reset_5h": now + 7000, "reset_7d": now + 400000},
    "c": {"util_5h": 0.0, "util_7d": 1.03, "reset_5h": now - 10, "reset_7d": now + 90000},
}
check("fallback skips week-exhausted accounts", R.pick_account() == "b")
R.STATE["accounts"]["b"]["util_7d"] = 0.99
# b's binding window is now the week (1% left, resets in 400000s); a's week
# (2% left) resets sooner — still reserve, so no early stop
check("emergency reserve picks soonest binding reset", R.pick_account() == "a")
R.STATE["accounts"]["a"]["util_7d"] = 1.0
R.STATE["accounts"]["b"]["util_7d"] = 1.01
check("fallback with every week spent picks soonest weekly reset", R.pick_account() == "c")
R.STRATEGY = "consume-first"

# 1k. doxaras/claude-rotate#3: a week-exhausted or rejected account with a
# stale (passed) 5h reset must not win the fallback over the live account.
def issue3(a_extra, b_extra, active="b"):
    fresh_state(active)
    R.STATE["accounts"] = {
        "a": dict({"util_5h": 0.1, "util_7d": 1.0, "reset_5h": now - 3600,
                   "reset_7d": now + 3 * 86400, "status": "rejected",
                   "last_seen": now - 60}, **a_extra),
        "b": dict({"util_5h": 0.85, "util_7d": 0.26, "reset_5h": now + 7200,
                   "reset_7d": now + 5 * 86400, "status": "allowed",
                   "last_seen": now - 10}, **b_extra),
    }
saved_accts = R.ACCOUNTS
R.ACCOUNTS = {"a": {"name": "a"}, "b": {"name": "b"}}
issue3({}, {})
check("#3: week-exhausted a with stale 5h reset loses to b", R.pick_account() == "b")
issue3({}, {"util_5h": 1.0, "status": "rejected"})
check("#3: same with b rejected at 5h", R.pick_account() == "b")
issue3({"util_7d": 0.99, "status": "allowed"}, {})
check("#3: a in the weekly reserve band still loses to b", R.pick_account() == "b")
issue3({"util_7d": 0.95}, {})
check("#3: rejected a with stale 7d reading is not usable", R.pick_account() == "b")
issue3({"util_7d": 0.95}, {"util_5h": 1.0, "status": "rejected"})
check("#3: rejected a with stale 7d loses to b's sooner relief", R.pick_account() == "b")
issue3({"util_7d": 0.99, "status": "allowed"}, {"util_5h": 1.0, "status": "rejected"})
check("#3: a in the weekly reserve band is used when b is rejected",
      R.pick_account() == "a")
issue3({}, {"util_5h": 1.0, "util_7d": 1.0, "reset_7d": now + 6 * 86400,
            "status": "rejected"})
check("#3: both week-exhausted: sooner weekly reset wins", R.pick_account() == "a")
issue3({}, {"util_5h": 1.0, "status": "rejected"})
check("#3: earliest_relief skips the week-exhausted account's stale 5h reset",
      abs(R.earliest_relief() - R.STATE["accounts"]["b"]["reset_5h"]) < 1)
R.ACCOUNTS = saved_accts

# 1l. accounts[].weekly_cap: allowance ramps up through the week, reaching
# weekly_cap at pace_lookahead before the weekly reset and never exceeding it.
D_ = 86400
check("top-level pace_lookahead defaults to 12h", R.PACE_LOOKAHEAD_S == 12 * H_)
check("duration_s parses m, h and d",
      (R.duration_s("30m"), R.duration_s("12h"), R.duration_s("2d")) == (1800, 43200, 172800))
check("duration_s rejects anything else",
      all(R.duration_s(v) is None for v in ("12", "h", "1.5h", "12x", "", " 2d", 12, None)))
saved_accts, saved_lookahead = R.ACCOUNTS, R.PACE_LOOKAHEAD_S
R.ACCOUNTS = {"a": {"name": "a", "weekly_cap": 0.5, "weekly_pacing": True},
              "b": {"name": "b"}}
R.PACE_LOOKAHEAD_S = 12 * H_
def cap_state(a_extra=None, b_extra=None, active="b"):
    fresh_state(active)
    R.STATE["last_switch_ts"] = time.time()  # inside the cooldown
    R.STATE["accounts"] = {
        "a": dict({"util_5h": 0.1, "util_7d": 0.2, "reset_7d": now + 6 * D_}, **(a_extra or {})),
        "b": dict({"util_5h": 0.1, "util_7d": 0.3, "reset_7d": now + 6.5 * D_}, **(b_extra or {})),
    }
cap_state()
check("cap_allowance is small early in the week (one day gone, 12h lookahead)",
      abs(R.cap_allowance("a") - 0.5 * 36 / 168) < 1e-4)
R.STATE["accounts"]["a"]["reset_7d"] = now + 12 * H_
check("cap_allowance equals weekly_cap at lookahead before the reset",
      R.cap_allowance("a") == 0.5)
R.STATE["accounts"]["a"]["reset_7d"] = now + 6 * H_
check("cap_allowance stays at weekly_cap inside the lookahead window",
      R.cap_allowance("a") == 0.5)
del R.STATE["accounts"]["a"]["reset_7d"]
check("cap_allowance with an unknown reset is weekly_cap", R.cap_allowance("a") == 0.5)
R.STATE["accounts"]["a"]["reset_7d"] = now - 10
check("cap_allowance with a passed reset is weekly_cap", R.cap_allowance("a") == 0.5)
cap_state()
R.PACE_LOOKAHEAD_S = D_
check("a configured top-level pace_lookahead is honoured",
      abs(R.cap_allowance("a") - 0.5 * 48 / 168) < 1e-4)
R.ACCOUNTS["a"]["pace_lookahead"] = "2d"
check("accounts[].pace_lookahead overrides the top-level one",
      abs(R.cap_allowance("a") - 0.5 * 72 / 168) < 1e-4)
del R.ACCOUNTS["a"]["pace_lookahead"]
R.PACE_LOOKAHEAD_S = 12 * H_
for zero in ("0m", "0h", "0d"):
    R.ACCOUNTS["a"]["pace_lookahead"] = zero
    check(f"accounts[].pace_lookahead {zero} means no lookahead, not the top-level 12h",
          R.pace_lookahead_s("a") == 0
          and abs(R.cap_allowance("a") - 0.5 * 24 / 168) < 1e-4)
del R.ACCOUNTS["a"]["pace_lookahead"]
check("an absent accounts[].pace_lookahead falls back to the top-level one",
      R.pace_lookahead_s("a") == 12 * H_)

# a: 20% of the week used against an allowance of about 10.7%, so cap-blocked
check("over its allowance: not under_threshold, hard_ok, usable or reserve_spendable",
      not (R.cap_ok("a") or R.under_threshold("a") or R.hard_ok("a")
           or R.usable("a") or R.reserve_spendable("a")))
R.STATE["accounts"]["a"]["util_7d"] = 0.05
check("under its allowance: usable", R.cap_ok("a") and R.under_threshold("a") and R.usable("a"))

# usable tier: a has the soonest weekly reset and would win without the cap
cap_state()
check("usable tier skips a cap-blocked account", R.pick_account() == "b")
del R.ACCOUNTS["a"]["weekly_cap"], R.ACCOUNTS["a"]["weekly_pacing"]
check("no weekly_cap: unchanged, a wins on the soonest weekly reset",
      R.cap_ok("a") and R.pick_account() == "a")
R.ACCOUNTS["a"]["weekly_cap"], R.ACCOUNTS["a"]["weekly_pacing"] = 0.5, True

# reserve tier: both over the 5h threshold, a's binding reset is sooner
cap_state({"util_5h": 0.9, "reset_5h": now + 7200}, {"util_5h": 0.9, "reset_5h": now + 9000})
check("reserve tier skips a cap-blocked account", R.pick_account() == "b")
del R.ACCOUNTS["a"]["weekly_cap"], R.ACCOUNTS["a"]["weekly_pacing"]
check("no weekly_cap: a wins the reserve tier", R.pick_account() == "a")
R.ACCOUNTS["a"]["weekly_cap"], R.ACCOUNTS["a"]["weekly_pacing"] = 0.5, True

# pool fallback: b is week-exhausted and serves again later than a
cap_state({"reset_5h": now + 7200}, {"util_7d": 1.0, "reset_7d": now + 7 * D_})
check("pool fallback: a would be served first on relief alone",
      R.relief_ts("a") < R.relief_ts("b"))
check("pool fallback ranks a cap-blocked account after a cap_ok one", R.pick_account() == "b")

# note_response: a cap-blocked active account leaves at once (cooldown running)
cap_state(active="a")
v = run(R.note_response("a", headers(u5=0.1, u7=0.2, r7=now + 6 * D_), 200))
check("cap-blocked active account is left with reason weekly_cap",
      v == "switched" and R.STATE["active_account"] == "b"
      and R.STATE["events"][-1]["reason"] == "weekly_cap")
cap_state({}, {"util_5h": 1.0, "reset_5h": now + 7200, "status": "rejected",
               "last_seen": now - 10}, active="a")
v = run(R.note_response("a", headers(u5=0.1, u7=0.2, r7=now + 6 * D_), 200))
check("...even when the account it leaves for is rejected",
      v == "switched" and R.STATE["active_account"] == "b"
      and R.STATE["events"][-1]["reason"] == "weekly_cap")
cap_state({"util_7d": 0.05}, active="a")
v = run(R.note_response("a", headers(u5=0.1, u7=0.05, r7=now + 6 * D_), 200))
check("under its allowance: stays put", v is None and R.STATE["active_account"] == "a")

# weekly_pace: a capped account has weekly_cap - weekly util left, times weight
R.ACCOUNTS["a"]["weight"] = 2
cap_state({"reset_7d": now + 100 * H_})
check("weekly_pace of a capped account is weight * (cap - util_7d) / hours",
      abs(R.weekly_pace("a") - 2 * (0.5 - 0.2) / 100) < 1e-4)
check("weekly_pace of an uncapped account is unchanged",
      abs(R.weekly_pace("b") - (1 - 0.3) / (6.5 * 24)) < 1e-4)

# weekly_pacing off (the default): weekly_cap is a flat maximum
for flat in ({}, {"weekly_pacing": False}):
    R.ACCOUNTS["a"] = dict({"name": "a", "weekly_cap": 0.5}, **flat)
    cap_state({"util_7d": 0.5})
    check(f"flat cap {flat}: allowance is weekly_cap one day into the week",
          R.cap_allowance("a") == 0.5)
    check(f"flat cap {flat}: at the cap is blocked, not usable",
          not R.cap_ok("a") and not R.usable("a"))
    R.STATE["accounts"]["a"]["util_7d"] = 0.4
    check(f"flat cap {flat}: under the cap is cap_ok and usable",
          R.cap_ok("a") and R.usable("a"))
# weekly_pacing on with no weekly_cap ramps up to 1.0
R.ACCOUNTS["a"] = {"name": "a", "weekly_pacing": True}
cap_state({"util_7d": 0.3})
check("pacing with no cap: allowance is 36/168 one day into the week",
      abs(R.cap_allowance("a") - 36 / 168) < 1e-4)
check("pacing with no cap: above the allowance is not cap_ok", not R.cap_ok("a"))
R.STATE["accounts"]["a"]["util_7d"] = 0.1
check("pacing with no cap: under the allowance is cap_ok", R.cap_ok("a"))
R.ACCOUNTS["a"] = {"name": "a"}
cap_state({"util_7d": 0.9})
check("neither weekly_cap nor weekly_pacing: cap_ok at util_7d 0.9", R.cap_ok("a"))

# edge branches of the allowance and cap_ok
for pacing in (False, True):
    R.ACCOUNTS["a"] = {"name": "a", "weekly_cap": 0, "weekly_pacing": pacing}
    cap_state({"util_7d": 0.0})
    check(f"weekly_cap 0 (pacing {pacing}): allowance 0, never cap_ok",
          R.cap_allowance("a") == 0 and not R.cap_ok("a"))
R.ACCOUNTS["a"] = {"name": "a", "weekly_cap": 0.5, "weekly_pacing": True}
cap_state({"util_7d": 0.0, "reset_7d": now + 8 * D_})
check("paced, reset more than a week plus the lookahead away: allowance clamps to 0",
      R.cap_allowance("a") == 0.0 and not R.cap_ok("a"))
cap_state({"util_7d": 0.5, "reset_7d": now + 6 * H_})
check("paced: util_7d equal to the allowance is blocked", R.cap_allowance("a") == 0.5
      and not R.cap_ok("a"))
R.STATE["accounts"]["a"]["util_7d"] = 0.49
check("paced: util_7d just under the allowance is cap_ok", R.cap_ok("a"))
R.ACCOUNTS["a"] = {"name": "a", "weekly_cap": 0.5, "pace_lookahead": "2d"}
R.PACE_LOOKAHEAD_S = D_
cap_state()
check("a flat cap ignores both pace_lookahead keys",
      R.cap_allowance("a") == 0.5)
R.PACE_LOOKAHEAD_S = 12 * H_

# earliest_relief: dead accounts and weekly caps
saved_bad = R.BAD_RECOVER_S
R.BAD_RECOVER_S = 600
R.ACCOUNTS = {"a": {"name": "a"}, "b": {"name": "b"}}
fresh_state("b")
R.STATE["accounts"] = {
    "a": {"util_5h": 1.0, "util_7d": 0.3, "reset_5h": now + 1800, "reset_7d": now + 3 * D_,
          "bad_status": 403, "last_bad_seen": time.time()},
    "b": {"util_5h": 1.0, "util_7d": 0.3, "reset_5h": now + 7200, "reset_7d": now + 3 * D_,
          "status": "rejected", "last_seen": now - 10},
}
check("earliest_relief skips a bad account's sooner reset",
      abs(R.earliest_relief() - (now + 7200)) < 60)
R.BAD_RECOVER_S = saved_bad

R.ACCOUNTS = {"a": {"name": "a", "weekly_cap": 0.5}}
fresh_state("a")
R.STATE["accounts"] = {"a": {"util_5h": 0.1, "util_7d": 0.6, "reset_5h": now + 3600,
                             "reset_7d": now + 2 * D_}}
check("flat cap: earliest_relief is the weekly reset, not the 5h one",
      abs(R.earliest_relief() - (now + 2 * D_)) < 60)
R.ACCOUNTS["a"]["weekly_cap"] = 0
check("weekly_cap 0: no relief is known, so the fallback is now + 300",
      R.cap_relief_ts("a") == float("inf") and abs(R.earliest_relief() - (now + 300)) < 60)

R.ACCOUNTS = {"a": {"name": "a", "weekly_cap": 0.5, "weekly_pacing": True}}
R.PACE_LOOKAHEAD_S = 12 * H_
fresh_state("a")
R.STATE["accounts"] = {"a": {"util_5h": 0.1, "util_7d": 0.2, "reset_5h": now + 3600,
                             "reset_7d": now + 6 * D_}}
check("paced cap: earliest_relief is when the allowance passes util_7d (now + 1.3 days)",
      abs(R.earliest_relief() - (now + 112320)) < 60)
R.STATE["accounts"]["a"]["util_7d"] = 0.6
check("paced cap, util_7d over the cap: earliest_relief is the weekly reset",
      abs(R.earliest_relief() - (now + 6 * D_)) < 60)
R.STATE["accounts"]["a"]["util_7d"] = 0.3  # cap lifts at reset - 7d - 12h + 4.2d = now + 1.7d - 12h
cap_lifts = now + 6 * D_ - 7 * D_ - 12 * H_ + 4.2 * D_
check("paced cap: cap_relief_ts follows the rising allowance",
      abs(R.cap_relief_ts("a") - cap_lifts) < 60)
R.STATE["accounts"]["a"].update(util_5h=1.0, status="rejected", last_seen=now - 10,
                                reset_5h=now + 3 * D_)
check("capped and spent, 5h reset later than the cap lifts: takes the 5h reset",
      abs(R.earliest_relief() - (now + 3 * D_)) < 60)
R.STATE["accounts"]["a"]["reset_5h"] = now + 3600
check("capped and spent, 5h reset sooner than the cap lifts: takes the cap relief",
      abs(R.earliest_relief() - cap_lifts) < 60)
R.ACCOUNTS, R.PACE_LOOKAHEAD_S = saved_accts, saved_lookahead

# 2. burst 429 (util well under 1.0, no rejected status) → pace, no rotation
fresh_state("a")
R.STATE["accounts"] = {"a": {"util_5h": 0.4, "util_7d": 0.2}}
v = run(R.note_response("a", headers(u5=0.4, u7=0.2, retry_after=7), 429))
check("burst 429 verdict is 'burst'", v == "burst")
check("burst 429 does not rotate", R.STATE["active_account"] == "a")
check("burst 429 sets pace_until ~retry-after",
      6 < R.STATE["accounts"]["a"]["pace_until"] - time.time() <= 7.5)

# 3. quota 429 (unified-status rejected) → rotate despite cooldown
fresh_state("a")
R.STATE["last_switch_ts"] = time.time()  # cooldown active — must not block a hard stop
R.STATE["accounts"] = {
    "a": {"util_5h": 1.0, "util_7d": 0.5, "reset_7d": now + 500000},
    "b": {"util_5h": 0.1, "util_7d": 0.2, "reset_7d": now + 100000},
    "c": {"util_5h": 0.1, "util_7d": 0.2, "reset_7d": now + 300000},
}
v = run(R.note_response("a", headers(u5=1.0, status="rejected"), 429))
check("quota 429 switches", v == "switched")
check("quota 429 lands on soonest weekly reset", R.STATE["active_account"] == "b")
check("switch event reason", R.STATE["events"][-1]["reason"] == "quota_exhausted")

# 4. all spent → exhausted
fresh_state("a")
R.STATE["accounts"] = {
    "a": {"util_5h": 1.0, "reset_5h": now + 1000},
    "b": {"util_5h": 0.95, "reset_5h": now + 2000},
    "c": {"util_5h": 0.9, "reset_5h": now + 500},
}
v = run(R.note_response("a", headers(u5=1.0, status="rejected"), 429))
# fallback pick = earliest 5h reset = c; c != a so it switches (bounce toward relief)
check("all-spent quota hit still moves toward earliest reset", v == "switched" and R.STATE["active_account"] == "c")
v = run(R.note_response("c", headers(u5=0.9, status="rejected"), 429))
# b at 95% is reserve, not spent: rejected c must hand over, not 429 the client
check("rejected account hands over to remaining reserve", v == "switched" and R.STATE["active_account"] == "b")
v = run(R.note_response("b", headers(u5=0.95, status="rejected"), 429))
check("all rejected: bounce to soonest relief", v == "switched" and R.STATE["active_account"] == "c")
v = run(R.note_response("c", headers(u5=0.9, status="rejected"), 429))
check("exhausted when fallback is itself", v == "exhausted")
check("earliest_relief is soonest reset", abs(R.earliest_relief() - (now + 500)) < 5)

# 5. threshold switch respects cooldown
fresh_state("a")
R.STATE["last_switch_ts"] = time.time()
R.STATE["accounts"] = {"a": {"util_5h": 0.85}, "b": {"util_5h": 0.1}, "c": {"util_5h": 0.2}}
v = run(R.note_response("a", headers(u5=0.85), 200))
check("threshold switch blocked by cooldown", v is None and R.STATE["active_account"] == "a")
R.STATE["last_switch_ts"] = 0
v = run(R.note_response("a", headers(u5=0.85), 200))
check("threshold switch after cooldown", v == "switched" and R.STATE["active_account"] == "b")

# 6. hysteresis: candidate must beat active by margin
fresh_state("a")
R.STATE["accounts"] = {"a": {"util_5h": 0.81}, "b": {"util_5h": 0.79}, "c": {"util_5h": 0.80}}
v = run(R.note_response("a", headers(u5=0.81), 200))
check("hysteresis blocks ping-pong at the line", v is None and R.STATE["active_account"] == "a")

# 7. proactive consume-first: jump to clearly-sooner weekly reset even below threshold
fresh_state("a")
R.STATE["accounts"] = {
    "a": {"util_5h": 0.3, "util_7d": 0.4, "reset_7d": now + 500000},
    "b": {"util_5h": 0.3, "util_7d": 0.4, "reset_7d": now + 100000},
    "c": {"util_5h": 0.3, "util_7d": 0.4, "reset_7d": now + 300000},
}
v = run(R.note_response("a", headers(u5=0.3, u7=0.4), 200))
check("proactive consume-first switch", v == "switched" and R.STATE["active_account"] == "b")
check("event reason consume_first", R.STATE["events"][-1]["reason"] == "consume_first")
# 7b. margin: near-equal weekly resets don't churn
fresh_state("a")
R.STATE["accounts"] = {
    "a": {"util_5h": 0.3, "util_7d": 0.4, "reset_7d": now + 101000},
    "b": {"util_5h": 0.3, "util_7d": 0.4, "reset_7d": now + 100000},
}
v = run(R.note_response("a", headers(u5=0.3, u7=0.4), 200))
check("consume-first margin prevents churn", v is None and R.STATE["active_account"] == "a")

# 8. window that passed its reset counts as 0
fresh_state("a")
R.STATE["accounts"] = {"a": {"util_5h": 1.0, "reset_5h": now - 10, "util_7d": 0.9, "reset_7d": now - 10}}
check("passed 5h reset → util 0", R.utilization("a") == 0.0)
check("passed 7d reset → util 0", R.util_7d("a") == 0.0)

# 9. priority tiers: preferred tier always beats a sooner weekly reset
R.ACCOUNTS = {"a": {"name": "a", "priority": 2},
              "b": {"name": "b", "priority": 1}, "c": {"name": "c", "priority": 1}}
fresh_state()
R.STATE["accounts"] = {
    "a": {"util_5h": 0.1, "util_7d": 0.1, "reset_7d": now + 1000},    # soonest, but backup tier
    "b": {"util_5h": 0.1, "util_7d": 0.1, "reset_7d": now + 500000},
    "c": {"util_5h": 0.1, "util_7d": 0.1, "reset_7d": now + 100000},
}
check("priority tier beats sooner weekly reset", R.pick_account() == "c")

# 9b. backup tier only used once the preferred tier is spent
R.STATE["accounts"]["b"]["util_5h"] = 0.9
R.STATE["accounts"]["c"]["util_5h"] = 0.9
check("backup tier used when preferred spent", R.pick_account() == "a")

# 10. priority recovery: traffic pulled back once a preferred account has room
fresh_state("a")  # active on the backup
R.STATE["accounts"] = {
    "a": {"util_5h": 0.2, "util_7d": 0.1},
    "b": {"util_5h": 0.1, "util_7d": 0.1},
    "c": {"util_5h": 0.9, "util_7d": 0.1},
}
v = run(R.note_response("a", headers(u5=0.2, u7=0.1), 200))
check("priority recovery switches back", v == "switched" and R.STATE["active_account"] == "b")
check("recovery event reason", R.STATE["events"][-1]["reason"] == "priority_recovery")

# 10b. recovery waits out the cooldown
fresh_state("a")
R.STATE["last_switch_ts"] = time.time()
R.STATE["accounts"] = {"a": {"util_5h": 0.2}, "b": {"util_5h": 0.1}, "c": {"util_5h": 0.9}}
v = run(R.note_response("a", headers(u5=0.2), 200))
check("priority recovery respects cooldown", v is None and R.STATE["active_account"] == "a")

# 11. disabled accounts: never picked, active one abandoned immediately
R.ACCOUNTS = {"a": {"name": "a", "disabled": True}, "b": {"name": "b"}, "c": {"name": "c"}}
fresh_state("a")
R.STATE["accounts"] = {"a": {"util_5h": 0.0}, "b": {"util_5h": 0.2}, "c": {"util_5h": 0.1}}
check("disabled excluded from pick", R.pick_account() == "c")
R.STATE["last_switch_ts"] = time.time()  # even inside the cooldown
v = run(R.note_response("a", headers(u5=0.0), 200))
check("disabled active abandoned", v == "switched" and R.STATE["active_account"] == "c")
check("disabled event reason", R.STATE["events"][-1]["reason"] == "account_disabled")

# 11b. all-spent fallback skips disabled accounts too
fresh_state("b")
R.STATE["accounts"] = {"a": {"util_5h": 1.0, "reset_5h": now + 10},
                       "b": {"util_5h": 1.0, "reset_5h": now + 100},
                       "c": {"util_5h": 1.0, "reset_5h": now + 50}}
check("fallback skips disabled", R.pick_account() == "c")

R.ACCOUNTS = {"a": {"name": "a"}, "b": {"name": "b"}, "c": {"name": "c"}}

# 12. reserve band (live: everything stopped at 98%; 2026-10-18 17:50 bounced
# onto rejected accounts while another still had room)
R.THRESHOLD, R.THRESHOLD_7D, R.STRATEGY = 0.95, 0.95, "weekly-pace"
fresh_state("a")
R.STATE["accounts"] = {
    "a": {"util_5h": 0.97, "util_7d": 0.3, "reset_5h": now + 9000, "reset_7d": now + 300000},
    "b": {"util_5h": 0.96, "util_7d": 0.2, "reset_5h": now + 5000, "reset_7d": now + 300000},
    "c": {"util_5h": 0.5, "util_7d": 0.99, "reset_5h": now + 5000, "reset_7d": now + 200000},
}
check("reserve not spendable far from reset", not R.reserve_spendable("a") and not R.usable("a"))
check("emergency uses reserve, soonest binding reset", R.pick_account() == "b")
v = run(R.note_response("a", headers(u5=0.97), 200))
check("emergency: active keeps warm cache instead of hopping reserve", v is None and R.STATE["active_account"] == "a")
v = run(R.note_response("a", headers(u5=1.0, status="rejected"), 429))
check("rejected active moves into reserve", v == "switched" and R.STATE["active_account"] == "b")
check("rejected account not hard_ok", not R.hard_ok("a"))
R.STATE["accounts"]["a"]["last_seen"] = time.time() - 60
R.STATE["accounts"]["a"]["reset_5h"] = time.time() - 1
check("rejection forgotten once its window resets", R.hard_ok("a") and R.usable("a"))

# 12b. reserve whose window resets soon is spent first, even proactively
fresh_state("a")
R.STATE["accounts"] = {
    "a": {"util_5h": 0.2, "util_7d": 0.3, "reset_5h": now + 15000, "reset_7d": now + 300000},
    "b": {"util_5h": 0.97, "util_7d": 0.2, "reset_5h": now + 1800, "reset_7d": now + 300000},
    "c": {"util_5h": 0.1, "util_7d": 0.1, "reset_5h": now + 15000, "reset_7d": now + 300000},
}
check("5h reserve near reset is spendable", R.reserve_spendable("b"))
check("expiring reserve picked first", R.pick_account() == "b")
v = run(R.note_response("a", headers(u5=0.2), 200))
check("proactive switch to expiring reserve", v == "switched" and R.STATE["active_account"] == "b"
      and R.STATE["events"][-1]["reason"] == "reserve_expiring")
v = run(R.note_response("b", headers(u5=0.99), 200))
check("expiring reserve used past the threshold", v is None and R.STATE["active_account"] == "b")
R.STATE["last_switch_ts"] = 0
v = run(R.note_response("b", headers(u5=1.0, status="rejected"), 429))
check("spent reserve returns to normal pick", v == "switched" and R.STATE["active_account"] != "b")

# 12c. weekly reserve: spendable within the 7d horizon only if the 5h window
# is not also holding it
fresh_state("a")
R.STATE["accounts"] = {"a": {"util_5h": 0.3, "util_7d": 0.97, "reset_5h": now + 9000,
                             "reset_7d": now + 20000}}
check("7d reserve near weekly reset is spendable", R.reserve_spendable("a"))
R.STATE["accounts"]["a"]["util_5h"] = 0.96
check("...not while 5h reserve is far from its reset", not R.reserve_spendable("a"))
R.THRESHOLD, R.THRESHOLD_7D, R.STRATEGY = 0.8, 0.98, "consume-first"

# 13. closed / free accounts (403 permission-blocked, 404 model-not-found):
# marked dead and skipped, and a hard switch away is forced.
fresh_state("a")
R.STATE["accounts"] = {"a": {"util_5h": 0.1, "util_7d": 0.1},
                       "b": {"util_5h": 0.1, "util_7d": 0.1},
                       "c": {"util_5h": 0.1, "util_7d": 0.1}}
v = run(R.note_response("a", headers(u5=0.1, u7=0.1), 403, bad=True))
check("closed account (403) is marked bad", R.account_bad("a"))
check("closed account (403) forces a switch", v == "switched" and R.STATE["active_account"] != "a")
check("event reason account_unusable",
      R.STATE["events"][-1]["reason"] == "account_unusable")
check("bad account excluded from pick", R.pick_account() not in ("a",))
check("bad account not hard_ok", not R.hard_ok("a"))
# a successful reply clears the marker so it can rotate in again later
v = run(R.note_response("a", headers(u5=0.1, u7=0.1), 200))
check("success clears closed marker", not R.account_bad("a"))

fresh_state("a")
R.STATE["accounts"] = {"a": {"util_5h": 0.1, "util_7d": 0.1},
                       "b": {"util_5h": 0.1, "util_7d": 0.1},
                       "c": {"util_5h": 0.1, "util_7d": 0.1}}
v = run(R.note_response("a", headers(u5=0.1, u7=0.1), 404, bad=True))
check("free-tier model-not-found (404) marked bad", R.account_bad("a"))
check("404 forces a switch", v == "switched" and R.STATE["active_account"] != "a")

# all accounts bad: no switch possible — nothing better to serve
fresh_state("a")
R.STATE["accounts"] = {"a": {"bad_status": 403, "last_bad_seen": now},
                       "b": {"bad_status": 404, "last_bad_seen": now},
                       "c": {"bad_status": 403, "last_bad_seen": now}}
# note_response on a bad active account whose peers are also bad: exhausted
v = run(R.note_response("a", headers(u5=0.1, u7=0.1), 403, bad=True))
check("all accounts bad: no rotation, exhausted", v == "exhausted"
      and R.STATE["active_account"] == "a")

fresh_state("a")
R.STATE["accounts"] = {"a": {"bad_status": 403, "last_bad_seen": now},
                       "b": {"bad_status": 404, "last_bad_seen": now}}
check("bad accounts excluded from usable", not R.usable("a") and not R.usable("b"))

# 13b. bounce prevention: a bad active must not flip to another DEAD account.
fresh_state("a")
# a is active and bad; its two peers are also bad -> no switch, exhausted
R.STATE["accounts"] = {"a": {"bad_status": 403, "last_bad_seen": now},
                       "b": {"bad_status": 404, "last_bad_seen": now},
                       "c": {"bad_status": 403, "last_bad_seen": now}}
# manually set active=a which is bad; a new 403 must NOT rotate to the dead b/c
v = run(R.note_response("a", headers(u5=0.1, u7=0.1), 403, bad=True))
check("dead active with dead peers: no bounce", v == "exhausted"
      and R.STATE["active_account"] == "a")

# 13c. a dead active SHOULD leave for a genuinely working peer.
fresh_state("a")
R.STATE["accounts"] = {
    "a": {"bad_status": 403, "last_bad_seen": now},
    "b": {"bad_status": 404, "last_bad_seen": now},
    "c": {"util_5h": 0.1, "util_7d": 0.1},   # the one working account
}
v = run(R.note_response("a", headers(u5=0.1, u7=0.1), 403, bad=True))
check("dead active leaves for a working peer", v == "switched"
      and R.STATE["active_account"] == "c")
check("dead active does not jump to another dead peer", R.STATE["active_account"] != "b")

# 13d. pick_working(): prefers a working account, None when all accounts dead.
fresh_state("a")
R.STATE["accounts"] = {"a": {"bad_status": 403, "last_bad_seen": now},
                       "b": {"util_5h": 0.1, "util_7d": 0.1},
                       "c": {"util_5h": 0.1, "util_7d": 0.1}}
check("pick_working chooses the working account", R.pick_working() == "b")
for n in ("a", "b", "c"):
    R.STATE["accounts"][n]["bad_status"] = 403
    R.STATE["accounts"][n]["last_bad_seen"] = now
check("pick_working None when every account dead", R.pick_working() is None)

# 13e. recovery: the bad mark is not permanent — once the recovery window
# elapses the account is reconsidered so the next request re-probes it.
R.BAD_RECOVER_S = 18000
fresh_state("a")
t0 = time.time()
R.STATE["accounts"] = {"a": {"bad_status": 403, "last_bad_seen": t0},
                       "b": {"bad_status": 404, "last_bad_seen": t0},
                       "c": {"bad_status": 403, "last_bad_seen": t0}}
check("freshly marked accounts are bad", all(R.account_bad(n) for n in R.ACCOUNTS)
      and R.pick_working() is None)
# advance time past the recovery window (monkeypatch time.time)
_real_t = time.time
R.STATE["_now"] = [t0 + 18001]
time.time = lambda: R.STATE["_now"][0]
try:
    check("bad mark recovered after bad_recover_s", not R.account_bad("a"))
    ok_accounts = [n for n in R.ACCOUNTS if R.hard_ok(n)]
    check("recovered account is hard_ok again", "a" in ok_accounts and len(ok_accounts) == 3)
    check("pick_working returns a recovered account", R.pick_working() is not None)
finally:
    time.time = _real_t
# a still-bad reply re-marks it immediately (re-probe loop)
fresh_state("a")
R.BAD_RECOVER_S = 18000
t2 = time.time()
R.STATE["accounts"] = {"a": {"bad_status": 403, "last_bad_seen": t2},
                       "b": {"util_5h": 0.1, "util_7d": 0.1},
                       "c": {"util_5h": 0.1, "util_7d": 0.1}}
v = run(R.note_response("a", headers(u5=0.1, u7=0.1), 403, bad=True))
check("still-dead account re-marked after re-probe", R.account_bad("a")
      and R.STATE["active_account"] == "b")


# 14. probation: an expired bad mark must not attract voluntary switches.
# (Live 2026-11-01: weekly-pace moved acct-3 -> dead acct-1 every 10 min.)
R.BAD_RECOVER_S, R.BAD_RECOVER_MAX_S = 600, 86400
R.STRATEGY, R.COOLDOWN_S = "weekly-pace", 0
fresh_state("c")
t = time.time()
R.STATE["accounts"] = {
    "a": {"util_5h": 0.0, "util_7d": 0.0, "reset_7d": t + 3600,
          "bad_status": 403, "last_bad_seen": t - 700, "bad_count": 1},
    "b": {"util_5h": 0.0, "util_7d": 0.0, "reset_7d": t + 3600,
          "bad_status": 404, "last_bad_seen": t - 700, "bad_count": 1},
    "c": {"util_5h": 0.2, "util_7d": 0.2, "reset_7d": t + 500000},
}
check("expired mark: no longer account_bad", not R.account_bad("a"))
check("expired mark: still on probation", R.on_probation("a"))
check("pick_account prefers the clean account over probation ones", R.pick_account() == "c")
v = run(R.note_response("c", headers(u5=0.2, u7=0.2), 200))
check("weekly-pace makes no voluntary switch onto a probation account",
      R.STATE["active_account"] == "c" and v is None)
# ...but a forced move (quota gone on c) may re-probe one
v = run(R.note_response("c", headers(u5=1.0, u7=0.3, status="rejected"), 429))
check("forced switch may land on a probation account",
      v == "switched" and R.STATE["active_account"] in ("a", "b"))
R.STRATEGY, R.COOLDOWN_S = "consume-first", 300

# 14b. backoff: each repeat refusal doubles the wait; a 200 resets it.
fresh_state("a")
R.STATE["accounts"] = {"a": {}, "b": {}, "c": {}}
run(R.note_response("a", headers(), 403, bad=True))
check("first refusal: count 1, wait = bad_recover_s",
      R.STATE["accounts"]["a"]["bad_count"] == 1 and R.bad_recover_wait("a") == 600)
run(R.note_response("a", headers(), 403, bad=True))
run(R.note_response("a", headers(), 403, bad=True))
check("third refusal: wait 4x", R.bad_recover_wait("a") == 2400)
R.STATE["accounts"]["a"]["bad_count"] = 40
check("wait capped at bad_recover_max_s", R.bad_recover_wait("a") == 86400)
run(R.note_response("a", headers(), 200))
check("200 clears the mark and the backoff",
      not any(k in R.STATE["accounts"]["a"] for k in R.BAD_KEYS) and not R.on_probation("a"))

# 14c. reverting a refused request restores a probation account's earlier mark
# (not a pop), and still clears the mark of an account that was healthy before.
fresh_state("a")
t = time.time()
R.STATE["accounts"] = {
    "a": {"bad_status": 404, "last_bad_seen": t, "bad_count": 3},  # re-marked by the request
    "b": {"bad_status": 404, "last_bad_seen": t, "bad_count": 1},
    "c": {}}
prior_a = {"bad_status": 403, "last_bad_seen": t - 5000, "bad_count": 2}
run(R.revert_refusals({"a": prior_a, "b": {k: None for k in R.BAD_KEYS}}, "a", 404))
check("revert restores the prior bad_status of a probation account",
      R.STATE["accounts"]["a"]["bad_status"] == 403)
check("revert restores the prior last_bad_seen and bad_count",
      R.STATE["accounts"]["a"]["last_bad_seen"] == t - 5000
      and R.STATE["accounts"]["a"]["bad_count"] == 2)
check("revert pops the marks of an account that was healthy before",
      not any(k in R.STATE["accounts"]["b"] for k in R.BAD_KEYS))
check("revert logs a refusal event naming both accounts",
      R.STATE["events"][-1]["type"] == "refusal" and "a, b" in R.STATE["events"][-1]["reason"])

# 14d. bad_recover_s 0 means a bad mark holds forever.
R.BAD_RECOVER_S = 0
fresh_state("c")
t0 = time.time()
R.STATE["accounts"] = {"a": {"bad_status": 403, "last_bad_seen": t0, "bad_count": 1},
                       "b": {}, "c": {}}
check("bad_recover_s 0: the recovery wait is 0", R.bad_recover_wait("a") == 0)
_real_t = time.time
time.time = lambda: t0 + 3600
try:
    check("bad_recover_s 0: still bad an hour after the mark", R.account_bad("a"))
finally:
    time.time = _real_t
R.BAD_RECOVER_S = 600

# 14e. load_state returns what save_state wrote, so rotation state survives a restart.
import json
import tempfile
_real_state_path = R.STATE_PATH
R.STATE_PATH = Path(tempfile.mkdtemp()) / "state.json"
try:
    saved = {"active_account": "b", "accounts": {"a": {"bad_status": 403, "bad_count": 2}, "b": {}},
             "events": [{"type": "switch", "from": "a", "to": "b"}], "last_switch_ts": 1234.5}
    R.STATE_PATH.write_text(json.dumps(saved))
    check("load_state round-trips the stored state dict", R.load_state() == saved)
    R.STATE_PATH.unlink()
    check("load_state with no file starts on the first configured account",
          R.load_state()["active_account"] == R.CFG["accounts"][0]["name"])
finally:
    R.STATE_PATH = _real_state_path

# 15. pick_account with every account disabled must not crash, and returns None
# (the proxy answers 503 "no enabled accounts" instead of using a benched account).
fresh_state("a")
R.STATE["accounts"] = {}
saved = R.ACCOUNTS
R.ACCOUNTS = {n: {"name": n, "disabled": True} for n in ("a", "b", "c")}
try:
    check("all accounts disabled: pick_account returns None",
          R.pick_account() is None)
finally:
    R.ACCOUNTS = saved


# 16. send_upstream end-to-end against a fake upstream.
class FakeResp:
    def __init__(self, status):
        self.status_code, self.headers = status, H()
    async def aread(self): return b"{}"
    async def aiter_bytes(self, chunk_size=None): yield b"{}"
    async def aclose(self): pass


class FakeClient:
    """Answers per account (keyed by bearer token) from a status table."""
    def __init__(self, table):
        self.table, self.calls = table, []
    def build_request(self, method, url, headers=None, content=None):
        return dict(headers)["authorization"].split()[-1]
    async def send(self, token, stream=True):
        self.calls.append(token)
        return FakeResp(self.table[token])


R.ACCOUNTS = {n: {"name": n, "token": n} for n in ("a", "b", "c")}
R.HOLD_MAX_S = 0


def go(table, url="/v1/messages"):
    R.client = FakeClient(table)
    up, acct, retries, noted, verdict = run(R.send_upstream("POST", url, {}, b"{}"))
    return up.status_code, acct, R.client.calls


# 16a. a request-level 404 (e.g. a typo'd model) on every account: marks reverted
fresh_state("a")
R.STATE["accounts"] = {"a": {}, "b": {}, "c": {}}
st, acct, calls = go({"a": 404, "b": 404, "c": 404})
check("bad model: client gets the 404", st == 404 and len(calls) == 3)
check("bad model: no account left marked bad",
      not any(R.on_probation(n) for n in R.ACCOUNTS))
check("bad model: back on the starting account", R.STATE["active_account"] == "a")
check("bad model: refusal event logged",
      R.STATE["events"][-1]["type"] == "refusal")

# 16b. live layout: a=403 (closed), b=404 (free), c works -> marks stick
fresh_state("a")
R.STATE["accounts"] = {"a": {}, "b": {}, "c": {}}
st, acct, calls = go({"a": 403, "b": 404, "c": 200})
check("real dead accounts: request served by c", st == 200 and acct == "c")
check("real dead accounts: a and b stay marked",
      R.account_bad("a") and R.account_bad("b") and not R.on_probation("c"))

# 16c. only-working account + bad request while others are on probation:
# the healthy account must not be poisoned.
fresh_state("c")
t = time.time()
R.STATE["accounts"] = {
    "a": {"bad_status": 403, "last_bad_seen": t - 700, "bad_count": 1},
    "b": {"bad_status": 404, "last_bad_seen": t - 700, "bad_count": 1},
    "c": {}}
st, acct, calls = go({"a": 403, "b": 404, "c": 404})
check("bad request with dead peers: c not left marked", not R.on_probation("c"))
check("bad request with dead peers: back on c", R.STATE["active_account"] == "c")
check("probation peers that refused again keep their (bumped) mark",
      R.STATE["accounts"]["a"].get("bad_count") == 2)

# 16d. pre-send switch off a dead active is logged as an event
fresh_state("a")
t = time.time()
R.STATE["accounts"] = {"a": {"bad_status": 403, "last_bad_seen": t, "bad_count": 1},
                       "b": {}, "c": {}}
st, acct, calls = go({"a": 403, "b": 200, "c": 200})
check("pre-send: dead active skipped, no request fired at it", "a" not in calls)
check("pre-send: switch recorded",
      any(e.get("reason") == "account_unusable" and e["from"] == "a"
          for e in R.STATE["events"]) and R.STATE["last_switch_ts"] > 0)

# 16e. only /v1/messages itself counts; a batches 404 is passed straight through
fresh_state("a")
R.STATE["accounts"] = {"a": {}, "b": {}, "c": {}}
st, acct, calls = go({"a": 404, "b": 200, "c": 200}, url="/v1/messages/batches/xyz")
check("batches 404: no rotation, nothing marked",
      st == 404 and calls == ["a"] and not R.on_probation("a"))
check("query string still counts as /v1/messages",
      R.method_is_messages("/v1/messages?beta=true"))

# 17. accounts[].final_window: paced up to max(lookahead, window), ranked first
# in the window, weekly threshold given way to the hard limit
D_ = 86400
saved_th7, saved_look = R.THRESHOLD_7D, R.PACE_LOOKAHEAD_S
R.THRESHOLD_7D, R.PACE_LOOKAHEAD_S = 0.98, 12 * H_
T = time.time()

R.ACCOUNTS = {"a": {"name": "a", "weekly_cap": 0.5, "weekly_pacing": True,
                    "final_window": "0m"}}
check("final_window 0m: pace_lookahead_s is the top-level value",
      R.pace_lookahead_s("a") == 12 * H_)
R.ACCOUNTS["a"]["pace_lookahead"] = "2d"
check("final_window 0m: pace_lookahead_s is the account's own value",
      R.pace_lookahead_s("a") == 2 * D_)
fresh_state("a")
R.STATE["accounts"] = {"a": {"reset_7d": time.time() + H_}}
check("final_window 0m: in_final_window is false at reset - 1h",
      not R.in_final_window("a") and not R.in_final_window("a", T + H_, T))

R.ACCOUNTS = {"a": {"name": "a", "weekly_cap": 0.5, "weekly_pacing": True,
                    "final_window": "12h", "pace_lookahead": "0m"}}
check("N > L: the effective lookahead is the window",
      R.pace_lookahead_s("a") == 12 * H_)
check("N > L: a newly reset account is paced as if 12h old",
      abs(R.cap_allowance("a", T + 168 * H_, T) - 0.5 * 12 / 168) < 1e-9)
check("N > L: 13h before the reset the allowance is 167/168 of the cap",
      abs(R.cap_allowance("a", T + 13 * H_, T) - 0.5 * 167 / 168) < 1e-9)
check("N > L: the full cap is reached as the window opens",
      R.cap_allowance("a", T + 12 * H_, T) == 0.5)
R.ACCOUNTS = {"a": {"name": "a", "weekly_cap": 0.5, "weekly_pacing": True,
                    "final_window": "6h"}}
check("N <= L: the top-level lookahead stays",
      R.pace_lookahead_s("a") == 12 * H_
      and abs(R.cap_allowance("a", T + 6 * D_, T) - 0.5 * 36 / 168) < 1e-9)
del R.ACCOUNTS["a"]["final_window"]
plain = R.cap_allowance("a", T + 6 * D_, T)
R.ACCOUNTS["a"]["final_window"] = "6h"
check("N <= L: the allowance equals the one without final_window",
      R.cap_allowance("a", T + 6 * D_, T) == plain)
check("N <= L: the full cap is reached at reset - 12h",
      R.cap_allowance("a", T + 12 * H_, T) == 0.5)
check("N <= L: not in the window at reset - 7h, in it at reset - 5h",
      not R.in_final_window("a", T + 7 * H_, T) and R.in_final_window("a", T + 5 * H_, T))
fresh_state("a")
R.STATE["accounts"] = {"a": {}}
check("in_final_window: false once the reset has passed or when it is unknown",
      not R.in_final_window("a", T - 10, T) and not R.in_final_window("a", None, T))
R.ACCOUNTS = {"a": {"name": "a", "weekly_cap": 0.5, "weekly_pacing": True,
                    "final_window": "2d", "pace_lookahead": "1d"}}
check("N > L with an own L: the effective lookahead is 2d", R.pace_lookahead_s("a") == 2 * D_)

# 17b. the 7d threshold gives way to hard_limit in the window; the 5h one stays
R.THRESHOLD_7D = 0.5
R.ACCOUNTS = {"a": {"name": "a", "weekly_pacing": True, "final_window": "12h"}}
fresh_state("a")
R.STATE["accounts"] = {"a": {"util_5h": 0.1, "util_7d": 0.6, "reset_7d": time.time() + 13 * H_}}
check("outside the window util_7d 0.6 is over the 7d threshold",
      R.cap_ok("a") and not R.under_threshold("a"))
R.STATE["accounts"]["a"]["reset_7d"] = time.time() + 11 * H_
check("in the window util_7d 0.6 is under the hard limit: under_threshold and usable",
      R.under_threshold("a") and R.usable("a"))
R.STATE["accounts"]["a"]["util_5h"] = 0.85
check("in the window the 5h threshold still applies", not R.under_threshold("a"))
R.STATE["accounts"]["a"].update(util_5h=0.1, util_7d=1.0)
check("in the window util_7d 1.0 is not hard_ok", not R.hard_ok("a"))
R.THRESHOLD_7D = 0.98

# 17c. the 7d leg of reserve_spendable follows the hard limit in the window
R.ACCOUNTS = {"a": {"name": "a", "weekly_pacing": True, "final_window": "1d"}}
fresh_state("a")
R.STATE["accounts"] = {"a": {"util_5h": 0.9, "reset_5h": time.time() + 1800,
                             "util_7d": 0.99, "reset_7d": time.time() + 20 * H_}}
check("in the window only the 5h leg decides reserve_spendable",
      R.reserve_spendable("a") and R.usable("a"))
R.STATE["accounts"]["a"]["util_7d"] = 1.0
check("in the window at util_7d 1.0 it is not reserve_spendable", not R.reserve_spendable("a"))
R.STATE["accounts"]["a"]["util_7d"] = 0.99
del R.ACCOUNTS["a"]["final_window"]
check("without final_window the 7d leg refuses 20h from the reset",
      not R.reserve_spendable("a"))

# 17d. ranking: priority 2 in its window outranks two priority 1 accounts
def fw_pool(a_reset, window="6h"):
    R.ACCOUNTS = {"a": {"name": "a", "priority": 2, "weekly_pacing": True},
                  "b": {"name": "b", "priority": 1}, "c": {"name": "c", "priority": 1}}
    if window:
        R.ACCOUNTS["a"]["final_window"] = window
    fresh_state("b")
    t = time.time()
    R.STATE["accounts"] = {
        "a": {"util_5h": 0.1, "util_7d": 0.1, "reset_7d": t + a_reset},
        "b": {"util_5h": 0.1, "util_7d": 0.1, "reset_7d": t + 3 * D_},
        "c": {"util_5h": 0.1, "util_7d": 0.1, "reset_7d": t + 3 * D_},
    }
fw_pool(8 * H_)
check("reset in 8h, outside the window: a priority 1 account is picked",
      R.pick_account() in ("b", "c") and R.cap_allowance("a") == 1.0)
fw_pool(5 * H_)
check("reset in 5h, in the window: the priority 2 account is picked",
      R.pick_account() == "a" and R.rank("a", None)[0] == 0 and R.priority("a") == 2)
fw_pool(5 * H_, window=None)
check("no final_window: the priority 1 tier still wins", R.pick_account() in ("b", "c"))

# 17e. in the window the elevated account is still skipped when it cannot serve
def blocked(mutate):
    fw_pool(5 * H_)
    mutate()
    return R.in_final_window("a") and R.pick_account() in ("b", "c")
check("in the window, over the 5h switch_threshold: not picked",
      blocked(lambda: R.STATE["accounts"]["a"].update(util_5h=0.9)))
check("in the window, disabled: not picked",
      blocked(lambda: R.ACCOUNTS["a"].update(disabled=True)))
check("in the window, bad: not picked",
      blocked(lambda: R.STATE["accounts"]["a"].update(
          bad_status=403, last_bad_seen=time.time(), bad_count=1)))
check("in the window, rejected: not picked",
      blocked(lambda: R.STATE["accounts"]["a"].update(
          status="rejected", last_seen=time.time() - 10, util_5h=1.0,
          reset_5h=time.time() + 3600)))
def at_cap():
    R.ACCOUNTS["a"]["weekly_cap"] = 0.5
    R.STATE["accounts"]["a"]["util_7d"] = 0.5
check("in the window, at its flat weekly_cap: not picked", blocked(at_cap))

# 17f. note_response switches to the account in its window and logs final_window
def fw_active(a_reset=5 * H_):
    fw_pool(a_reset)
    R.STATE["active_account"] = "b"
fw_active()
v = run(R.note_response("b", headers(u5=0.1, u7=0.1), 200))
check("after the cooldown the account in its window takes over, reason final_window",
      v == "switched" and R.STATE["active_account"] == "a"
      and R.STATE["events"][-1]["reason"] == "final_window")
fw_active()
R.STATE["last_switch_ts"] = time.time()
v = run(R.note_response("b", headers(u5=0.1, u7=0.1), 200))
check("inside the cooldown it does not switch", v is None and R.STATE["active_account"] == "b")
fw_active()
del R.STATE["accounts"]["a"]["reset_7d"]
check("an unknown reset_7d is not in the window", not R.in_final_window("a"))
v = run(R.note_response("b", headers(u5=0.1, u7=0.1), 200))
check("an unknown reset_7d causes no switch onto the priority 2 account",
      v is None and R.STATE["active_account"] == "b")
fw_pool(5 * H_)
R.STATE["active_account"] = "a"
R.STATE["accounts"]["a"]["reset_7d"] = time.time() - 10
check("after the reset the window is over", not R.in_final_window("a") and R.priority("a") == 2)
v = run(R.note_response("a", headers(u5=0.1, u7=0.1), 200))
check("after the reset traffic returns through priority_recovery",
      v == "switched" and R.STATE["active_account"] in ("b", "c")
      and R.STATE["events"][-1]["reason"] == "priority_recovery")

# 17g. cap_relief_ts follows the effective lookahead
def relief(extra):
    R.ACCOUNTS = {"a": dict({"name": "a", "weekly_cap": 0.5, "weekly_pacing": True}, **extra)}
    fresh_state("a")
    R.STATE["accounts"] = {"a": {"util_5h": 0.1, "util_7d": 0.4, "reset_7d": reset_7d}}
reset_7d = time.time() + 6 * D_
relief({"final_window": "12h", "pace_lookahead": "0m"})
check("cap-blocked here: util 0.4 is over the allowance and under the cap",
      not R.cap_ok("a") and 0.4 < 0.5)
check("N > L: cap_relief_ts uses the 12h effective lookahead",
      abs(R.cap_relief_ts("a") - (reset_7d - 604800 - 43200 + 0.4 * 604800 / 0.5)) < 1e-6)
relief({"final_window": "6h"})
with_fw = R.cap_relief_ts("a")
relief({})
check("N <= L: cap_relief_ts equals the value without final_window",
      with_fw == R.cap_relief_ts("a") and with_fw > time.time())

R.THRESHOLD_7D, R.PACE_LOOKAHEAD_S = saved_th7, saved_look

print(f"\nall {PASS} checks passed")
