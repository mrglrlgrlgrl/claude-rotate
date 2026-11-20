"""load_config safety guards: a placeholder device key must never boot
(GitHub issue #2 regression), and missing token files are skipped.

  python3 tests/test_config_guard.py
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import rotator as R


def _load_with(cfg: dict):
    tmp = Path(tempfile.mkdtemp())
    (tmp / "tokens").mkdir()
    (tmp / "tokens" / "a.token").write_text("sk-ant-oat01-test")
    (tmp / "config.json").write_text(json.dumps(cfg))
    old_cfg, old_root = R.CONFIG_PATH, R.ROOT
    R.CONFIG_PATH, R.ROOT = tmp / "config.json", tmp
    try:
        return R.load_config()[0]
    finally:
        R.CONFIG_PATH, R.ROOT = old_cfg, old_root


ACCT = [{"name": "a", "token_file": "tokens/a.token"}]


def test_placeholder_device_key_refuses_startup():
    try:
        _load_with({"accounts": ACCT,
                    "devices": {"my-laptop": "dev-my-laptop-REPLACE-WITH-RANDOM-HEX"}})
        raise AssertionError("placeholder key must refuse startup")
    except R.ConfigError as e:
        assert "placeholder" in str(e), str(e)


def test_real_keys_boot():
    cfg = _load_with({"accounts": ACCT, "devices": {"laptop": "dev-laptop-a1b2c3"}})
    assert cfg["accounts"][0]["token"] == "sk-ant-oat01-test"


def test_missing_token_file_is_skipped_not_fatal():
    cfg = _load_with({"accounts": ACCT + [{"name": "b", "token_file": "tokens/nope.token"}],
                      "devices": {"laptop": "dev-laptop-a1b2c3"}})
    assert [a["name"] for a in cfg["accounts"]] == ["a"]


def test_no_token_file_at_all_refuses_startup():
    try:
        _load_with({"accounts": [{"name": "b", "token_file": "tokens/nope.token"},
                                 {"name": "c", "token_file": "tokens/nada.token"}],
                    "devices": {"laptop": "dev-laptop-a1b2c3"}})
        raise AssertionError("no loadable account must refuse startup")
    except R.ConfigError as error:
        assert str(error).startswith("no accounts with tokens configured"), str(error)


def test_resource_limit_override_must_be_an_integer():
    var = "CLAUDE_ROTATE_MAX_INFLIGHT"
    old = os.environ.get(var)
    try:
        os.environ[var] = "ten"
        try:
            R.resource_limit({}, "max_inflight", 5)
            raise AssertionError("a non-integer override must be refused")
        except R.ConfigError as error:
            assert str(error) == "max_inflight must be an integer", str(error)
        os.environ[var] = "7"
        assert R.resource_limit({}, "max_inflight", 5) == 7
    finally:
        if old is None:
            os.environ.pop(var, None)
        else:
            os.environ[var] = old


def test_empty_and_duplicate_device_keys_refuse_startup():
    for devices in ({"device": ""}, {"device": "   "}, {"device": None},
                    {"one": "same", "two": "same"}, None):
        try:
            _load_with({"accounts": ACCT, "devices": devices})
            raise AssertionError("invalid device keys must refuse startup")
        except R.ConfigError as error:
            assert "device" in str(error)


def test_account_weight_must_be_a_positive_number():
    devices = {"laptop": "dev-laptop-a1b2c3"}
    for w in (0, -1, "1", True):
        try:
            _load_with({"accounts": [dict(ACCT[0], weight=w)], "devices": devices})
            raise AssertionError(f"weight {w!r} must refuse startup")
        except R.ConfigError as error:
            assert "weight" in str(error)
    _load_with({"accounts": [dict(ACCT[0], weight=0.25)], "devices": devices})


def test_account_weekly_cap_must_be_a_number_from_0_to_1():
    devices = {"laptop": "dev-laptop-a1b2c3"}
    for cap in (-0.1, 1.5, "0.5", True, None):
        try:
            _load_with({"accounts": [dict(ACCT[0], weekly_cap=cap)], "devices": devices})
            raise AssertionError(f"weekly_cap {cap!r} must refuse startup")
        except R.ConfigError as error:
            assert "account 'a'" in str(error) and "weekly_cap" in str(error), str(error)
    for cap in (0, 0.5, 1):
        _load_with({"accounts": [dict(ACCT[0], weekly_cap=cap)], "devices": devices})


def test_account_weekly_pacing_must_be_a_boolean():
    devices = {"laptop": "dev-laptop-a1b2c3"}
    for value in (1, "yes", None):
        try:
            _load_with({"accounts": [dict(ACCT[0], weekly_pacing=value)], "devices": devices})
            raise AssertionError(f"weekly_pacing {value!r} must refuse startup")
        except R.ConfigError as error:
            assert str(error) == "account 'a': weekly_pacing must be true or false", str(error)
    for acct in (dict(ACCT[0], weekly_pacing=True), dict(ACCT[0], weekly_pacing=False), ACCT[0]):
        _load_with({"accounts": [acct], "devices": devices})


def test_account_pace_lookahead_must_parse():
    devices = {"laptop": "dev-laptop-a1b2c3"}
    for value in ("12", "1.5h", "12x", "", 12, None):
        try:
            _load_with({"accounts": [dict(ACCT[0], pace_lookahead=value)], "devices": devices})
            raise AssertionError(f"pace_lookahead {value!r} must refuse startup")
        except R.ConfigError as error:
            assert "account 'a'" in str(error) and "pace_lookahead" in str(error), str(error)
    for value in ("30m", "12h", "2d"):
        _load_with({"accounts": [dict(ACCT[0], pace_lookahead=value)], "devices": devices})


def test_account_final_window_must_parse_and_needs_weekly_pacing():
    devices = {"laptop": "dev-laptop-a1b2c3"}
    paced = dict(ACCT[0], weekly_pacing=True)
    for value in ("-12h", "12", 12, "1.5h", "", None):
        try:
            _load_with({"accounts": [dict(paced, final_window=value)], "devices": devices})
            raise AssertionError(f"final_window {value!r} must refuse startup")
        except R.ConfigError as error:
            assert "account 'a'" in str(error) and "final_window" in str(error), str(error)
    for acct in (dict(ACCT[0]), dict(ACCT[0], weekly_pacing=False)):
        try:
            _load_with({"accounts": [dict(acct, final_window="12h")], "devices": devices})
            raise AssertionError("final_window without weekly_pacing must refuse startup")
        except R.ConfigError as error:
            assert str(error) == "account 'a': final_window needs weekly_pacing: true", str(error)
    for value in ("12h", "30m", "2d"):
        _load_with({"accounts": [dict(paced, final_window=value)], "devices": devices})
    _load_with({"accounts": [dict(paced, final_window="12h", pace_lookahead="2d")], "devices": devices})
    for zero in ("0m", "0h", "0d"):
        _load_with({"accounts": [dict(ACCT[0], final_window=zero)], "devices": devices})


def test_account_cap_lookahead_is_refused_as_renamed():
    devices = {"laptop": "dev-laptop-a1b2c3"}
    want = "account 'a': cap_lookahead is renamed pace_lookahead: rename the key in config.json"
    for extra in ({"cap_lookahead": "12h"}, {"cap_lookahead": "12h", "pace_lookahead": "12h"}):
        try:
            _load_with({"accounts": [dict(ACCT[0], **extra)], "devices": devices})
            raise AssertionError(f"{extra!r} must refuse startup")
        except R.ConfigError as error:
            assert str(error) == want, str(error)


def test_account_private_to_must_name_registered_devices():
    devices = {"laptop": "dev-laptop-a1b2c3", "desk": "dev-desk-d4e5f6"}
    for owner in ("ghost", "", None, 5, [], ["laptop", "ghost"], ["laptop", "laptop"],
                  ["laptop", 5], {"laptop": 1}):
        try:
            _load_with({"accounts": [dict(ACCT[0], private_to=owner)], "devices": devices})
            raise AssertionError(f"private_to {owner!r} must refuse startup")
        except R.ConfigError as error:
            assert "account 'a'" in str(error) and "private_to" in str(error), str(error)
    for owner in ("laptop", ["laptop"], ["laptop", "desk"]):
        cfg = _load_with({"accounts": [dict(ACCT[0], private_to=owner)], "devices": devices})
        assert cfg["accounts"][0]["private_to"] == owner


def _import_with(cfg: dict, code: str = "import rotator", env: dict | None = None):
    tmp = Path(tempfile.mkdtemp())
    (tmp / "tokens").mkdir()
    (tmp / "tokens" / "a.token").write_text("sk-ant-oat01-test")
    (tmp / "config.json").write_text(json.dumps(
        {"accounts": ACCT, "devices": {"laptop": "dev-laptop-a1b2c3"}, **cfg}))
    return subprocess.run([sys.executable, "-c", code], cwd=Path(R.__file__).parent,
                          text=True, capture_output=True,
                          env={**os.environ, "CLAUDE_ROTATE_CONFIG": str(tmp / "config.json"),
                               **(env or {})})


def test_panel_tailnet_needs_loopback_bind_and_a_bool():
    # Off loopback, any client could send Tailscale-User-Login itself.
    for cfg in ({"panel_tailnet": True, "bind": "0.0.0.0"},
                {"panel_tailnet": True, "bind": "100.101.102.103"},
                {"panel_tailnet": "false"}):
        child = _import_with(cfg)
        assert child.returncode != 0 and "panel_tailnet" in child.stderr, (cfg, child.stderr)
    child = _import_with({"panel_tailnet": True, "bind": "127.0.0.1"})
    assert child.returncode == 0, child.stderr


def test_bind_env_overrides_config_and_counts_for_panel_tailnet():
    show = "import rotator; print(rotator.BIND, rotator.PORT)"
    for cfg, env, want in (({}, {}, "127.0.0.1 8484"),
                           ({"bind": "10.0.0.5", "port": 9000}, {}, "10.0.0.5 9000"),
                           ({"bind": "10.0.0.5"}, {"CLAUDE_ROTATE_BIND": "0.0.0.0"}, "0.0.0.0 8484"),
                           ({}, {"CLAUDE_ROTATE_BIND": ""}, "127.0.0.1 8484")):
        child = _import_with(cfg, show, env)
        assert child.returncode == 0 and child.stdout.strip() == want, (cfg, env, child.stderr)
    # The override is the address actually listened on, so it is the one checked.
    child = _import_with({"panel_tailnet": True, "bind": "127.0.0.1"}, env={"CLAUDE_ROTATE_BIND": "0.0.0.0"})
    assert child.returncode != 0 and "panel_tailnet" in child.stderr, child.stderr


def test_refresh_idle_model_defaults_configures_and_must_be_a_nonempty_string():
    show = "import json, rotator; print(json.loads(rotator.REFRESH_BODY)['model'])"
    for cfg, model in (({}, "claude-haiku-4-5"),
                       ({"refresh_idle_model": "claude-haiku-5-5"}, "claude-haiku-5-5")):
        child = _import_with(cfg, show)
        assert child.returncode == 0 and child.stdout.strip() == model, (cfg, child.stderr)
    for bad in ("", "  ", 12, None, ["x"]):
        child = _import_with({"refresh_idle_model": bad})
        assert child.returncode != 0 and "refresh_idle_model" in child.stderr, (bad, child.stderr)


def test_top_level_pace_lookahead_defaults_to_12h_and_must_parse():
    show = "import rotator; print(rotator.PACE_LOOKAHEAD_S)"
    for cfg, seconds in (({}, 43200), ({"pace_lookahead": "2d"}, 172800),
                         ({"pace_lookahead": "30m"}, 1800)):
        child = _import_with(cfg, show)
        assert child.returncode == 0 and child.stdout.strip() == str(seconds), (cfg, child.stderr)
    for bad in ("bogus", "12", 12, None):
        child = _import_with({"pace_lookahead": bad})
        assert child.returncode != 0 and "pace_lookahead" in child.stderr, (bad, child.stderr)


def test_top_level_cap_lookahead_is_refused_as_renamed():
    for cfg in ({"cap_lookahead": "12h"}, {"cap_lookahead": "12h", "pace_lookahead": "12h"}):
        child = _import_with(cfg)
        assert child.returncode != 0 and "cap_lookahead is renamed pace_lookahead" in child.stderr, (
            cfg, child.stderr)


if __name__ == "__main__":
    fns = [(n, f) for n, f in sorted(globals().items())
           if n.startswith("test_") and callable(f)]
    for n, f in fns:
        f()
        print(f"ok   {n}")
    print(f"{len(fns)} passed")
