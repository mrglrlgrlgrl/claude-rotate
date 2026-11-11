"""claude-rotate-admin under a temporary prefix with a fake systemctl and dummy secrets.

  python3 tests/test_admin.py

Nothing here touches /etc, /opt, /var or a real service: CLAUDE_ROTATE_PREFIX points
every managed path into a temporary directory and a recording systemctl stub is
first on PATH.
"""
import contextlib
import http.server
import importlib.util
import io
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
ADMIN = ROOT / "deploy" / "claude_rotate_admin.py"
UNIT_TEXT = (ROOT / "deploy" / "claude-rotate.service").read_text()
TOKEN = "sk-ant-oat01-dummy-admin-test-token-0001"

FAKE_SYSTEMCTL = r"""#!/bin/sh
# Recording systemd stub for tests. State lives in $FAKE_SYSTEMD_DIR.
d="$FAKE_SYSTEMD_DIR"
echo "$*" >> "$d/calls.log"
[ -e "$d/fail-all" ] && exit 1
state() { cat "$d/active" 2>/dev/null || echo inactive; }
enabled() { cat "$d/enabled" 2>/dev/null || echo disabled; }
case "$1" in
  is-active) [ "$(state)" = active ] ;;
  is-enabled) enabled ;;
  enable|start|restart)
    if [ -e "$d/fail-start" ]; then echo inactive > "$d/active"; echo "Job for claude-rotate.service failed." >&2; exit 1; fi
    [ "$1" = enable ] && echo enabled > "$d/enabled"
    echo active > "$d/active" ;;
  reload)
    [ "$(state)" = active ] || { echo "Unit claude-rotate.service is not active." >&2; exit 1; }
    if [ -e "$d/fail-reload" ]; then echo "Job type reload is not applicable for unit claude-rotate.service." >&2; exit 1; fi ;;
  stop)
    [ -e "$d/fail-stop" ] && exit 1
    [ -x "$d/on-stop" ] && "$d/on-stop"
    echo inactive > "$d/active" ;;
  disable) echo disabled > "$d/enabled"; case "$*" in *--now*) echo inactive > "$d/active" ;; esac ;;
  show) printf 'ActiveState=%s\nSubState=running\nNRestarts=0\nUnitFileState=%s\nMainPID=0\n' "$(state)" "$(enabled)" ;;
  daemon-reload|reset-failed) ;;
  *) echo "fake systemctl: unexpected $*" >&2; exit 9 ;;
esac
"""


class Fixture:
    def __init__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="claude-rotate-admin-test-"))
        self.prefix = self.tmp / "prefix"
        self.prefix.mkdir()
        (self.prefix / "etc/systemd/system").mkdir(parents=True)
        (self.prefix / "etc/systemd/system/claude-rotate.service").write_text(UNIT_TEXT)
        self.fake = self.tmp / "fake-systemd"
        self.fake.mkdir()
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        (self.bin / "systemctl").write_text(FAKE_SYSTEMCTL)
        (self.bin / "systemctl").chmod(0o700)
        self.env = dict(os.environ, CLAUDE_ROTATE_PREFIX=str(self.prefix), FAKE_SYSTEMD_DIR=str(self.fake),
                        PATH=str(self.bin) + os.pathsep + os.environ["PATH"])
        self.etc = self.prefix / "etc/claude-rotate"
        self.config = self.etc / "config.json"
        self.tokens = self.etc / "tokens"
        self.state_dir = self.prefix / "var/lib/claude-rotate"
        self.state = self.state_dir / "state.json"

    def run(self, *args, input=None, env=None):
        return subprocess.run([sys.executable, "-I", str(ADMIN), *args], env=env or self.env, input=input,
                              text=True, capture_output=True, timeout=60)

    def calls(self):
        log = self.fake / "calls.log"
        return log.read_text().splitlines() if log.exists() else []

    def reset_calls(self):
        (self.fake / "calls.log").unlink(missing_ok=True)

    def set_active(self, active):
        (self.fake / "active").write_text("active" if active else "inactive")

    def cfg(self):
        return json.loads(self.config.read_text())

    def cleanup(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class FakeService:
    """Stands in for the running proxy's /rotate/status, with the config reload state a
    reloaded service reports. `error` is what a rejected reload would say."""

    def __init__(self, f, error=None, restart_needed=()):
        outer = self
        self.f, self.error, self.restart_needed, self.hits = f, error, list(restart_needed), 0

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                outer.hits += 1
                now = time.time()
                body = json.dumps({"active_account": "a", "accounts": {}, "config": {
                    "loaded_at": now, "last_reload": now, "error": outer.error,
                    "restart_needed": outer.restart_needed}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        cfg = f.cfg()
        self.old_port = cfg.get("port")
        cfg["port"] = self.server.server_address[1]
        f.config.write_text(json.dumps(cfg))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        cfg = self.f.cfg()
        if self.old_port is None:
            cfg.pop("port", None)
        else:
            cfg["port"] = self.old_port
        self.f.config.write_text(json.dumps(cfg))


def mode(path):
    return stat.S_IMODE(os.lstat(path).st_mode)


def test_prefix_keeps_the_callers_ids_even_if_the_host_has_the_service_account(f):
    """Regression: a host with a real claude-rotate user made a non-root test run chown to it."""
    if os.geteuid() == 0:
        return  # root uses the real identity by design
    import importlib.util
    import types
    old = os.environ.get("CLAUDE_ROTATE_PREFIX")
    os.environ["CLAUDE_ROTATE_PREFIX"] = str(f.prefix)
    try:
        spec = importlib.util.spec_from_file_location("admin_under_test", ADMIN)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        if old is None:
            os.environ.pop("CLAUDE_ROTATE_PREFIX")
        else:
            os.environ["CLAUDE_ROTATE_PREFIX"] = old
    mod.pwd = types.SimpleNamespace(getpwnam=lambda name: types.SimpleNamespace(pw_uid=os.getuid() + 1))
    mod.grp = types.SimpleNamespace(getgrnam=lambda name: types.SimpleNamespace(gr_gid=os.getgid() + 1))
    assert mod.service_ids() == (os.getuid(), os.getgid()), mod.service_ids()


@contextlib.contextmanager
def admin_in_process(f):
    """Import the admin under the fixture's prefix and fake systemctl, for code the subprocess
    runs cannot reach (a fixed timeout). Yields the module; the environment is put back after."""
    keys = ("CLAUDE_ROTATE_PREFIX", "FAKE_SYSTEMD_DIR", "PATH")
    old = {k: os.environ.get(k) for k in keys}
    os.environ.update({k: f.env[k] for k in keys})
    try:
        spec = importlib.util.spec_from_file_location("admin_in_process", ADMIN)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        yield mod
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_init_creates_empty_config_and_modes(f):
    r = f.run("init")
    assert r.returncode == 3, r
    assert "setup needed" in r.stdout and "add-token" in r.stdout and "add-user" in r.stdout
    cfg = f.cfg()
    assert cfg["accounts"] == [] and cfg["devices"] == {}
    assert "REPLACE-WITH" not in f.config.read_text()
    assert mode(f.config) == 0o640 and mode(f.etc) == 0o750 and mode(f.tokens) == 0o750
    assert f.run("check").returncode == 3
    # Adoption normalizes modes and keeps content.
    f.config.chmod(0o600)
    f.tokens.chmod(0o700)
    r = f.run("init")
    assert r.returncode == 3 and "adopted" in r.stdout
    assert mode(f.config) == 0o640 and mode(f.tokens) == 0o750
    # Fresh unconfigured install: activate must leave the unit disabled, never loop.
    f.reset_calls()
    r = f.run("activate")
    assert r.returncode == 3 and "setup needed" in r.stdout, r
    assert any(c.startswith("disable --now") for c in f.calls())
    assert not any(c.startswith(("start", "restart", "enable")) for c in f.calls())


def test_invalid_names_do_nothing(f):
    before = f.config.read_bytes()
    for bad in ("two words", "quote'name", "../outside", "a" * 65, "-leading", ""):
        for cmd in (("add-user", bad), ("add-token", bad), ("remove-user", bad), ("remove-token", bad), ("rotate", bad)):
            r = f.run(*cmd, input=TOKEN)
            assert r.returncode == 1, (cmd, r)
            assert not bad or bad not in r.stdout
    assert f.config.read_bytes() == before
    assert list(f.tokens.iterdir()) == []


def test_add_token_and_renewal(f):
    f.set_active(False)
    f.reset_calls()
    r = f.run("add-token", "acct-1", input=TOKEN + "\n")
    assert r.returncode == 3, r  # stored, but no device yet: setup needed, service disabled
    assert TOKEN not in r.stdout + r.stderr
    token_file = f.tokens / "acct-1.token"
    assert token_file.read_text() == TOKEN and mode(token_file) == 0o640
    assert f.cfg()["accounts"] == [{"name": "acct-1", "token_file": "tokens/acct-1.token"}]
    assert not any(c.startswith(("start", "restart", "enable")) for c in f.calls())
    # Bad tokens are refused without touching anything.
    before = token_file.read_bytes()
    for bad in ("", "sk-ant-oat01-", "not-a-token", "sk-ant-oat01-with space", "sk-ant-oat01-ok\nextra line"):
        r = f.run("add-token", "acct-1", input=bad)
        assert r.returncode == 1, (bad, r)
        assert token_file.read_bytes() == before
    # Renewal keeps account settings and replaces only the token.
    cfg = f.cfg()
    cfg["accounts"][0]["disabled"] = True
    cfg["accounts"][0]["priority"] = 5
    f.config.write_text(json.dumps(cfg))
    renewed = TOKEN.replace("0001", "0002")
    r = f.run("add-token", "acct-1", "--no-restart", input=renewed)
    assert r.returncode == 0 and "replaced" in r.stdout, r
    assert token_file.read_text() == renewed
    assert f.cfg()["accounts"][0] == {"name": "acct-1", "token_file": "tokens/acct-1.token", "disabled": True, "priority": 5}
    cfg["accounts"][0].pop("disabled")
    f.config.write_text(json.dumps(cfg))
    # --token-file and the add-account alias.
    source = f.tmp / "second.token"
    source.write_text(TOKEN.replace("0001", "0003") + "\n")
    r = f.run("add-account", "acct-2", "--token-file", str(source), "--no-restart")
    assert r.returncode == 0, r
    assert (f.tokens / "acct-2.token").read_text() == TOKEN.replace("0001", "0003")
    assert [a["name"] for a in f.cfg()["accounts"]] == ["acct-1", "acct-2"]


def test_add_user_activation_and_duplicates(f):
    f.set_active(False)
    f.reset_calls()
    r = f.run("add-user", "alice-laptop")
    assert r.returncode == 0, r
    cfg = f.cfg()
    key = cfg["devices"]["alice-laptop"]
    assert key.startswith("dev-alice-laptop-") and len(key) == len("dev-alice-laptop-") + 16
    assert f"export CLAUDE_CODE_OAUTH_TOKEN={key}" in r.stdout
    assert "export ANTHROPIC_BASE_URL=" in r.stdout
    assert "is live" in r.stdout
    assert any(c.startswith("enable --now") for c in f.calls())
    # Duplicate: refused, nothing changes, no service action.
    before = f.config.read_bytes()
    f.reset_calls()
    r = f.run("add-user", "alice-laptop")
    assert r.returncode == 1 and f.config.read_bytes() == before, r
    assert "already exists" in r.stderr and f.calls() == []
    # Active service: a second device is reloaded, not restarted, and live once the
    # service confirms it; alias accepted.
    f.set_active(True)
    f.reset_calls()
    with FakeService(f) as service:
        r = f.run("add-device", "bob-desktop")
    assert r.returncode == 0 and "reloaded" in r.stdout and "is live" in r.stdout, r
    assert "reload claude-rotate.service" in f.calls() and not any(c.startswith("restart") for c in f.calls())
    assert service.hits >= 1
    assert set(f.cfg()["devices"]) == {"alice-laptop", "bob-desktop"}
    assert f.cfg()["devices"]["alice-laptop"] == key


def test_private_to_flag_and_device_removal_guard(f):
    accounts_before = f.cfg()["accounts"]
    devices_before = f.cfg()["devices"]
    assert f.run("add-user", "carol-tmp", "--no-restart").returncode == 0
    # An unregistered, badly named or repeated device is refused without touching anything.
    config_before = f.config.read_bytes()
    for value, message in (("ghost", "not registered"), ("alice-laptop,ghost", "not registered"),
                           ("bad name", "device name"), ("alice-laptop,alice-laptop", "twice")):
        r = f.run("add-token", "acct-priv", "--private-to", value, "--no-restart", input=TOKEN + "\n")
        assert r.returncode == 1 and message in r.stderr, (value, r)
        assert f.config.read_bytes() == config_before and not (f.tokens / "acct-priv.token").exists()
    # One device is stored as a name, several as a list; a new account gets priority 1.
    r = f.run("add-token", "acct-priv", "--private-to", "alice-laptop", "--no-restart", input=TOKEN + "\n")
    assert r.returncode == 0, r
    assert f.cfg()["accounts"][-1] == {"name": "acct-priv", "token_file": "tokens/acct-priv.token",
                                       "private_to": "alice-laptop", "priority": 1}
    r = f.run("add-token", "acct-priv", "--private-to", "alice-laptop,carol-tmp", "--no-restart",
              input=TOKEN + "\n")
    assert r.returncode == 0 and f.cfg()["accounts"][-1]["private_to"] == ["alice-laptop", "carol-tmp"], r
    # Renewing the token without the flag leaves the reservation alone.
    r = f.run("add-token", "acct-priv", "--no-restart", input=TOKEN + "\n")
    assert r.returncode == 0 and f.cfg()["accounts"][-1]["private_to"] == ["alice-laptop", "carol-tmp"], r
    # One of several owners can be removed: it just leaves the list.
    r = f.run("remove-user", "carol-tmp", "--no-restart")
    assert r.returncode == 0 and "no longer private to 'carol-tmp'" in r.stdout, r
    assert f.cfg()["accounts"][-1]["private_to"] == "alice-laptop"
    assert f.cfg()["devices"] == devices_before
    # The sole owner cannot be removed: startup would fail, or the account would turn shared.
    config_before = f.config.read_bytes()
    r = f.run("remove-user", "alice-laptop", "--no-restart")
    assert r.returncode == 1 and "acct-priv" in r.stderr and "private only to" in r.stderr, r
    assert f.config.read_bytes() == config_before
    r = f.run("remove-token", "acct-priv", "--no-restart")
    assert r.returncode == 0 and f.cfg()["accounts"] == accounts_before, r


def test_readiness_needs_enabled_nonempty_token(f):
    cfg = f.cfg()
    saved = json.dumps(cfg)
    for acct in cfg["accounts"]:
        acct["disabled"] = True
    f.config.write_text(json.dumps(cfg))
    r = f.run("check")
    assert r.returncode == 3 and "no enabled account" in r.stdout, r
    f.config.write_text(saved)
    tokens = {a["name"]: (f.tokens / f"{a['name']}.token").read_bytes() for a in cfg["accounts"]}
    (f.tokens / "acct-2.token").write_bytes(b"")
    r = f.run("check")
    assert r.returncode == 0 and "empty" in r.stdout, r  # acct-1 still carries the service
    (f.tokens / "acct-1.token").write_bytes(b"")
    r = f.run("check")
    assert r.returncode == 3, r
    for name, data in tokens.items():
        (f.tokens / f"{name}.token").write_bytes(data)
    assert f.run("check").returncode == 0


def test_inherited_lock_descriptor(f):
    lock = f.prefix / "run/claude-rotate.lock"
    script = (
        "import fcntl, os, subprocess, sys\n"
        f"fd = os.open({str(lock)!r}, os.O_RDWR | os.O_CREAT, 0o600)\n"
        "fcntl.flock(fd, fcntl.LOCK_EX)\n"
        "env = dict(os.environ, CLAUDE_ROTATE_LOCK_FD=str(fd))\n"
        f"r = subprocess.run([sys.executable, '-I', {str(ADMIN)!r}, 'check'], env=env, pass_fds=[fd], timeout=20)\n"
        "sys.exit(r.returncode)\n")
    r = subprocess.run([sys.executable, "-c", script], env=f.env, text=True, capture_output=True, timeout=60)
    assert r.returncode == 0, r  # no deadlock: the child reused the parent's held lock
    # A descriptor for another file, or for a lock held by a third party, is refused.
    other = f.tmp / "other.lock"
    other.write_text("")
    script = (
        "import fcntl, os, subprocess, sys\n"
        f"fd = os.open({str(other)!r}, os.O_RDWR)\n"
        "env = dict(os.environ, CLAUDE_ROTATE_LOCK_FD=str(fd))\n"
        f"r = subprocess.run([sys.executable, '-I', {str(ADMIN)!r}, 'add-user', 'nope'], env=env, pass_fds=[fd], timeout=20, capture_output=True, text=True)\n"
        "print(r.stderr); sys.exit(r.returncode)\n")
    r = subprocess.run([sys.executable, "-c", script], env=f.env, text=True, capture_output=True, timeout=60)
    assert r.returncode == 1 and "is not" in r.stdout, r
    assert "nope" not in f.cfg()["devices"]


def test_restart_failure_is_reported(f):
    # An inactive service is started; a failed start is not live.
    (f.fake / "fail-start").write_text("")
    try:
        f.set_active(False)
        r = f.run("add-user", "carol-phone")
        assert r.returncode == 2, r
        assert "NOT live" in r.stdout and "FAILED" in r.stdout and "journalctl" in r.stdout
        assert "is live" not in r.stdout
        assert "carol-phone" in f.cfg()["devices"]  # stored, honestly reported as not live
    finally:
        (f.fake / "fail-start").unlink()
    # A running service is reloaded; a failed reload is not live either.
    f.set_active(True)
    (f.fake / "fail-reload").write_text("")
    try:
        r = f.run("remove-user", "carol-phone")
        assert r.returncode == 2, r
        assert "NOT live" in r.stdout and "reload FAILED" in r.stdout and "install.sh" in r.stdout
        assert "is live" not in r.stdout
        assert "carol-phone" not in f.cfg()["devices"]
    finally:
        (f.fake / "fail-reload").unlink()
    r = f.run("add-user", "erin-watch", "--no-restart")
    assert r.returncode == 0 and "reload_poll_s" in r.stdout and "systemctl reload" in r.stdout, r
    assert "restart" not in r.stdout
    assert f.run("remove-user", "erin-watch", "--no-restart").returncode == 0
    # A missing unit cannot be live either.
    unit = f.prefix / "etc/systemd/system/claude-rotate.service"
    unit.rename(unit.with_suffix(".bak"))
    try:
        r = f.run("add-user", "dave-tablet")
        assert r.returncode == 2 and "unit is not installed" in r.stdout, r
    finally:
        unit.with_suffix(".bak").rename(unit)
    f.run("remove-user", "dave-tablet", "--no-restart")


def test_concurrent_add_users_are_serialized(f):
    f.set_active(True)
    names = [f"worker-{i}" for i in range(8)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda n: f.run("add-user", n, "--no-restart"), names))
    assert all(r.returncode == 0 for r in results), [r.stderr for r in results]
    devices = f.cfg()["devices"]
    assert all(n in devices for n in names)
    assert len(set(devices.values())) == len(devices)
    for n in names:
        assert f.run("remove-user", n, "--no-restart").returncode == 0
    assert not any(n in f.cfg()["devices"] for n in names)


def test_status_has_no_secrets(f):
    f.set_active(False)
    r = f.run("status", "--json")
    assert r.returncode == 0, r
    report = json.loads(r.stdout)
    text = r.stdout
    assert TOKEN not in text and TOKEN.replace("0001", "0002") not in text
    for key in f.cfg()["devices"].values():
        assert key not in text
        assert key[-4:] in text
    assert report["ready"] is True
    acct = report["accounts"][0]
    assert acct["token_file_present"] is True and "valid" not in json.dumps(acct)
    assert report["active_account"] == "acct-1"
    human = f.run("status")
    assert human.returncode == 0 and "token file present" in human.stdout and TOKEN not in human.stdout
    assert "token validity is only visible through real upstream traffic" in human.stdout


def test_status_live_query_uses_header(f):
    seen = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen["path"] = self.path
            seen["auth"] = self.headers.get("Authorization")
            body = json.dumps({"active_account": "acct-2", "storage_errors": {"state_write": {"count": 3}},
                               "accounts": {"acct-1": {"effective_util_5h": 0.1}}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        cfg = f.cfg()
        cfg["port"] = server.server_address[1]
        f.config.write_text(json.dumps(cfg))
        f.set_active(True)
        r = f.run("status", "--json")
        assert r.returncode == 0, r
        report = json.loads(r.stdout)
        assert seen["path"] == "/rotate/status" and seen["auth"].startswith("Bearer dev-")
        assert report["live"]["active_account"] == "acct-2"
        assert report["live"]["storage_errors"] == {"state_write": {"count": 3}}
        human = f.run("status")
        assert "storage_errors" in human.stdout and seen["auth"][7:] not in human.stdout
    finally:
        server.shutdown()
        server.server_close()


def test_reload_is_confirmed_through_status(f):
    assert "ExecReload=/bin/kill -HUP $MAINPID" in UNIT_TEXT
    f.set_active(True)
    # The service rejected the new file and kept the old config: stored, not live.
    f.reset_calls()
    with FakeService(f, error="placeholder device key(s) in config.json"):
        r = f.run("add-user", "frank-pc")
        assert r.returncode == 2, r
        assert "NOT live" in r.stdout and "kept its previous configuration" in r.stdout
        assert "placeholder device key" in r.stdout and "is live" not in r.stdout
        assert "frank-pc" in f.cfg()["devices"]
        human = f.run("status")
        assert "LAST RELOAD FAILED" in human.stdout and "placeholder device key" in human.stdout
        live = json.loads(f.run("status", "--json").stdout)["live"]
        assert live["config"]["error"] == "placeholder device key(s) in config.json"
    assert "reload claude-rotate.service" in f.calls() and not any(c.startswith("restart") for c in f.calls())
    # Accepted, with a setting that only a restart applies.
    with FakeService(f, restart_needed=["port"]):
        r = f.run("remove-user", "frank-pc")
        assert r.returncode == 0 and "is live" in r.stdout, r
        assert "restart is needed to apply: port" in r.stdout
        assert "frank-pc" not in f.cfg()["devices"]
    # A service that never answers cannot confirm; say so rather than claim it is live.
    r = f.run("add-user", "gina-tv")
    assert r.returncode == 2 and "not confirmed" in r.stdout and "is live" not in r.stdout, r
    assert f.run("remove-user", "gina-tv", "--no-restart").returncode == 0


def write_state(f, state):
    f.state_dir.mkdir(parents=True, exist_ok=True)
    f.state.write_text(json.dumps(state))


def test_rotate_rejections_do_not_mutate(f):
    cfg = f.cfg()
    cfg["accounts"] = [{"name": "acct-1", "token_file": "tokens/acct-1.token"},
                       {"name": "acct-2", "token_file": "tokens/acct-2.token"},
                       {"name": "acct-3", "token_file": "tokens/acct-3.token", "disabled": True},
                       {"name": "acct-4", "token_file": "tokens/acct-4.token"}]
    f.config.write_text(json.dumps(cfg))
    (f.tokens / "acct-4.token").unlink(missing_ok=True)
    write_state(f, {"active_account": "acct-1", "accounts": {"acct-2": {"util_5h": 0.95, "reset_5h": time.time() + 3600}},
                    "events": [], "custom": "keep"})
    before_state, before_cfg = f.state.read_bytes(), f.config.read_bytes()
    f.set_active(True)
    f.reset_calls()
    for target, reason in (("nonexistent", "not in the configuration"), ("acct-3", "disabled"),
                           ("acct-4", "missing"), ("acct-2", "5h window")):
        r = f.run("rotate", target)
        assert r.returncode == 1 and reason in r.stderr, (target, r)
    r = f.run("rotate")  # every other account is ineligible
    assert r.returncode == 1 and "no other eligible" in r.stderr, r
    r = f.run("rotate", "acct-1")
    assert r.returncode == 0 and "already the active" in r.stdout
    assert f.state.read_bytes() == before_state and f.config.read_bytes() == before_cfg
    assert f.calls() == []  # never stopped the service for a refusal


def test_rotate_switches_after_stop_and_preserves_state(f):
    hook = f.fake / "on-stop"
    hook.write_text("#!/bin/sh\n" + f"{sys.executable} - <<'EOF'\n"
                    "import json\n"
                    f"p = {str(f.state)!r}\n"
                    "s = json.load(open(p))\n"
                    "s['accounts']['acct-1'] = {'util_5h': 0.42, 'last_seen': 1.0}\n"
                    "s['drained'] = True\n"
                    "json.dump(s, open(p, 'w'))\nEOF\n")
    hook.chmod(0o700)
    write_state(f, {"active_account": "acct-1", "accounts": {"acct-2": {"util_5h": 0.1}}, "events": [], "custom": "keep"})
    f.set_active(True)
    f.reset_calls()
    r = f.run("rotate")
    assert r.returncode == 0, r
    assert "active account is now 'acct-2'" in r.stdout and "cooldown" in r.stdout
    calls = f.calls()
    assert calls.index("stop claude-rotate.service") < calls.index("start claude-rotate.service")
    state = json.loads(f.state.read_text())
    assert state["active_account"] == "acct-2" and state["custom"] == "keep"
    assert state["drained"] is True and state["accounts"]["acct-1"]["util_5h"] == 0.42  # written during stop, kept
    assert state["accounts"]["acct-2"] == {"util_5h": 0.1}
    assert abs(state["last_switch_ts"] - time.time()) < 60
    assert state["events"][-1]["reason"] == "manual" and state["events"][-1]["to"] == "acct-2"
    assert mode(f.state) == 0o600
    hook.unlink()
    # Explicit target and wrap-around while stopped: no systemctl start/stop.
    f.set_active(False)
    f.reset_calls()
    r = f.run("rotate", "acct-1")
    assert r.returncode == 0 and "applies at the next start" in r.stdout, r
    assert json.loads(f.state.read_text())["active_account"] == "acct-1"
    assert not any(c.startswith(("stop", "start")) for c in f.calls())
    # Start failure after the switch is reported with the previous account.
    f.set_active(True)
    (f.fake / "fail-start").write_text("")
    try:
        r = f.run("rotate", "acct-2")
        assert r.returncode == 2 and "FAILED" in r.stdout and "'acct-1'" in r.stdout and "journalctl" in r.stdout, r
        assert json.loads(f.state.read_text())["active_account"] == "acct-2"
    finally:
        (f.fake / "fail-start").unlink()
    # Stop failure: nothing changes.
    (f.fake / "fail-stop").write_text("")
    try:
        f.set_active(True)
        before = f.state.read_bytes()
        r = f.run("rotate", "acct-1")
        assert r.returncode == 1 and "stopped state" in r.stderr and f.state.read_bytes() == before, r
    finally:
        (f.fake / "fail-stop").unlink()
    # A unit that is activating (auto-restart) is stopped before the file is written.
    (f.fake / "active").write_text("activating")
    f.reset_calls()
    r = f.run("rotate", "acct-1")
    assert r.returncode == 0, r
    calls = f.calls()
    assert calls.index("stop claude-rotate.service") < calls.index("start claude-rotate.service")
    assert json.loads(f.state.read_text())["active_account"] == "acct-1"
    # Eligibility is re-checked against the drained state; the service is started again.
    hook = f.fake / "on-stop"
    hook.write_text("#!/bin/sh\n" + f"{sys.executable} - <<'EOF'\n"
                    "import json, time\n"
                    f"p = {str(f.state)!r}\n"
                    "s = json.load(open(p))\n"
                    "s['accounts']['acct-2'] = {'util_5h': 0.99, 'reset_5h': time.time() + 3600}\n"
                    "json.dump(s, open(p, 'w'))\nEOF\n")
    hook.chmod(0o700)
    f.set_active(True)
    f.reset_calls()
    r = f.run("rotate", "acct-2")
    assert r.returncode == 1 and "no longer eligible" in r.stderr and "service started again" in r.stderr, r
    assert json.loads(f.state.read_text())["active_account"] == "acct-1"
    assert "start claude-rotate.service" in f.calls()
    hook.unlink()
    # No state file yet: the default shape is created.
    f.state.unlink()
    f.set_active(False)
    r = f.run("rotate", "acct-2")
    assert r.returncode == 0, r
    assert json.loads(f.state.read_text())["accounts"].keys() >= {"acct-1", "acct-2"}


def test_rotate_stop_timeout_restarts_the_service(f):
    write_state(f, {"active_account": "acct-1", "accounts": {}, "events": []})
    before = f.state.read_bytes()
    f.set_active(True)
    f.reset_calls()
    with admin_in_process(f) as mod:
        real = mod.systemctl

        def timing_out(*args, **kwargs):
            result = real(*args, **kwargs)  # the fake records and performs the stop
            if args[0] == "stop":
                raise subprocess.TimeoutExpired(["systemctl", *args], kwargs.get("timeout"))
            return result

        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(mod, "systemctl", timing_out), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            rc = mod.main(["rotate"])
    assert rc == 1, (rc, stdout.getvalue(), stderr.getvalue())
    assert "within 120 s" in stderr.getvalue() and "service started again" in stderr.getvalue(), stderr.getvalue()
    calls = f.calls()
    assert calls.index("stop claude-rotate.service") < calls.index("start claude-rotate.service"), calls
    assert f.state.read_bytes() == before


def test_rotate_failed_stop_refuses_without_a_restart(f):
    """A stop that fails, or leaves the unit active, refuses and leaves state alone."""
    before = f.state.read_bytes()
    marker = f.fake / "fail-stop"
    marker.write_text("")
    try:
        f.set_active(True)
        f.reset_calls()
        r = f.run("rotate")
        assert r.returncode == 1, r
        assert "could not bring claude-rotate.service to a stopped state (was active, now active" in r.stderr, r
        assert "rotation state unchanged" in r.stderr
        assert not any(c.startswith("start") for c in f.calls()), f.calls()
        assert f.state.read_bytes() == before
    finally:
        marker.unlink()


def test_rotate_unwritable_state_restarts_and_exits_not_live(f):
    """An OSError writing the state file restarts the service and exits 2."""
    if os.geteuid() == 0:
        return  # root writes through a read-only directory
    before = f.state.read_bytes()
    hook = f.fake / "on-stop"
    hook.write_text(f"#!/bin/sh\nchmod 500 {str(f.state_dir)!r}\n")
    hook.chmod(0o700)
    try:
        f.set_active(True)
        f.reset_calls()
        r = f.run("rotate")
    finally:
        f.state_dir.chmod(0o700)
        hook.unlink()
    assert r.returncode == 2, r
    assert "could not write" in r.stdout and "active account unchanged" in r.stdout, r
    assert "service started again" in r.stdout
    calls = f.calls()
    assert calls.index("stop claude-rotate.service") < calls.index("start claude-rotate.service"), calls
    assert f.state.read_bytes() == before


def test_remove_token_and_setup_needed(f):
    cfg = f.cfg()
    cfg["accounts"] = [a for a in cfg["accounts"] if a["name"] in ("acct-1", "acct-2")]
    f.config.write_text(json.dumps(cfg))
    # A symlinked token file is never deleted.
    outside = f.tmp / "outside.token"
    outside.write_text("keep")
    link = f.tokens / "acct-2.token"
    link.unlink()
    link.symlink_to(outside)
    r = f.run("remove-token", "acct-2", "--no-restart")
    assert r.returncode == 0 and "left in place" in r.stdout, r
    assert outside.exists() and link.is_symlink()
    link.unlink()
    assert [a["name"] for a in f.cfg()["accounts"]] == ["acct-1"]
    r = f.run("remove-token", "acct-2")
    assert r.returncode == 1 and "does not exist" in r.stderr
    # Removing the last account: service stopped and disabled, setup needed, exit 3.
    f.set_active(True)
    f.reset_calls()
    r = f.run("remove-account", "acct-1")
    assert r.returncode == 3 and "setup needed" in r.stdout and "stopped and disabled" in r.stdout, r
    assert not (f.tokens / "acct-1.token").exists() and f.cfg()["accounts"] == []
    assert any(c.startswith("disable --now") for c in f.calls())
    r = f.run("remove-user", "nobody")
    assert r.returncode == 1 and "does not exist" in r.stderr


def test_set_url_and_symlinked_config(f):
    for bad in ("ftp://x", "host.example", "https://", "https://h ost"):
        assert f.run("set-url", bad).returncode == 1, bad
    r = f.run("set-url", "https://proxy.tail1234.ts.net/")
    assert r.returncode == 0 and f.cfg()["public_url"] == "https://proxy.tail1234.ts.net"
    r = f.run("add-user", "eve-laptop", "--no-restart")
    assert "export ANTHROPIC_BASE_URL=https://proxy.tail1234.ts.net" in r.stdout
    assert "/rotate/panel?key=" in r.stdout
    real = f.config.read_bytes()
    f.config.unlink()
    (f.tmp / "elsewhere.json").write_bytes(real)
    f.config.symlink_to(f.tmp / "elsewhere.json")
    try:
        r = f.run("add-user", "mallory")
        assert r.returncode == 1 and "symbolic link" in r.stderr, r
        assert "mallory" not in (f.tmp / "elsewhere.json").read_text()
    finally:
        f.config.unlink()
        f.config.write_bytes(real)


def test_unexpected_errors_are_reported_without_a_traceback(f):
    """main() turns an unexpected exception into one `error:` line, never a traceback."""
    r = f.run("set-url", "http://[x")
    assert r.returncode == 1 and r.stderr.startswith("error:") and "Traceback" not in r.stderr, r
    assert len(r.stderr.strip().splitlines()) == 1, r
    before = sorted(p.name for p in f.tokens.iterdir())
    bad = f.tmp / "binary.token"
    bad.write_bytes(b"\xff\xfe")
    r = f.run("add-token", "binary-acct", "--token-file", str(bad), "--no-restart")
    assert r.returncode == 1 and r.stderr.startswith("error:") and "Traceback" not in r.stderr, r
    assert len(r.stderr.strip().splitlines()) == 1, r
    assert sorted(p.name for p in f.tokens.iterdir()) == before
    assert not (f.tokens / "binary-acct.token").exists()
    # A bare run prints the usage and exits 1.
    r = f.run()
    assert r.returncode == 1 and r.stdout.startswith("usage:"), r
    # A timeout is stored-but-not-confirmed (2); other OS and value errors are refusals (1).
    with admin_in_process(f) as mod:
        for exc, want_rc, want_text in (
                (subprocess.TimeoutExpired(["systemctl"], 5), 2, "not confirmed live"),
                (OSError(28, "No space left on device"), 1, "No space left on device"),
                (ValueError("bad value"), 1, "bad value")):
            stdout, stderr = io.StringIO(), io.StringIO()
            with mock.patch.object(mod, "cmd_check", mock.Mock(side_effect=exc)), \
                    contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                rc = mod.main(["check"])
            assert rc == want_rc, (exc, rc, stderr.getvalue())
            assert stderr.getvalue().startswith("error:") and want_text in stderr.getvalue(), stderr.getvalue()


def test_unreadable_config_directory_is_reported_as_permission_denied(f):
    if os.geteuid() == 0:
        print("skip test_unreadable_config_directory_is_reported_as_permission_denied: mode 000 has no effect as root")
        return
    f.etc.chmod(0)
    try:
        r = f.run("check")
    finally:
        f.etc.chmod(0o750)
    assert r.returncode == 1 and r.stderr.startswith("error: permission denied:") and "Traceback" not in r.stderr, r


def test_add_token_config_write_failure_removes_the_new_token_file(f):
    """A failed config write leaves no token file behind, so the same add-token can be retried."""
    if os.geteuid() == 0:
        print("skip test_add_token_config_write_failure_removes_the_new_token_file: "
              "a read-only directory has no effect as root")
        return
    before = f.config.read_bytes()
    etc_mode = mode(f.etc)
    f.etc.chmod(0o550)  # tokens/ stays writable; the config's temp file in etc cannot be created
    try:
        r = f.run("add-token", "new", "--no-restart", input=TOKEN + "\n")
    finally:
        f.etc.chmod(etc_mode)
    assert r.returncode == 1 and r.stderr.startswith("error:") and "ermission" in r.stderr, r
    assert "Traceback" not in r.stderr, r
    assert not (f.tokens / "new.token").exists()
    assert f.config.read_bytes() == before
    r = f.run("add-token", "new", "--no-restart", input=TOKEN + "\n")
    assert r.returncode == 0 and "already exists" not in r.stderr, r
    assert (f.tokens / "new.token").exists()
    assert "new" in [a["name"] for a in f.cfg()["accounts"]]


def tree_snapshot(f):
    """The config bytes plus every path under the tokens directory with its content (a symlink by
    target), so a refusal can be shown to have changed nothing."""
    files = {}
    for p in sorted(f.tokens.rglob("*")):
        rel = str(p.relative_to(f.tokens))
        if p.is_symlink():
            files[rel] = ("link", os.readlink(p))
        elif p.is_file():
            files[rel] = ("file", p.read_bytes(), mode(p))
        else:
            files[rel] = ("dir",)
    return f.config.read_bytes(), files


def test_add_token_refuses_a_new_name_whose_token_path_is_taken(f):
    taken = f.tokens / "taken.token"
    taken.write_text("left alone")
    outside = f.tmp / "elsewhere.token"
    outside.write_text("elsewhere")
    link = f.tokens / "linked.token"
    link.symlink_to(outside)
    dangling = f.tokens / "dangling.token"
    dangling.symlink_to(f.tmp / "no-such-file")
    before = tree_snapshot(f)
    for name in ("taken", "linked", "dangling"):
        r = f.run("add-token", name, "--no-restart", input=TOKEN + "\n")
        assert r.returncode == 1, (name, r)
        assert f"{f.tokens / (name + '.token')} already exists but no account uses it" in r.stderr, r
        assert TOKEN not in r.stdout + r.stderr
        assert tree_snapshot(f) == before, name
    assert outside.read_text() == "elsewhere"
    for p in (taken, link, dangling):
        p.unlink()


def test_add_token_refuses_an_existing_account_with_a_token_file_outside_the_tokens_directory(f):
    original = f.config.read_bytes()
    outside = f.tmp / "outside.token"
    outside.write_text("outside")
    (f.tokens / "sub").mkdir()
    (f.tokens / "sub" / "nested.token").write_text("nested")
    real = f.tokens / "real.token"
    real.write_text("real")
    (f.tokens / "via-link.token").symlink_to(real)
    try:
        for token_file in ("../outside.token", "tokens/sub/nested.token", "tokens/via-link.token", "",
                           str(outside)):
            cfg = json.loads(original)
            cfg["accounts"] = [{"name": "odd", "token_file": token_file}]
            f.config.write_text(json.dumps(cfg))
            before = tree_snapshot(f)
            r = f.run("add-token", "odd", "--no-restart", input=TOKEN + "\n")
            assert r.returncode == 1, (token_file, r)
            assert f"account 'odd' uses token_file {token_file!r}, which is not a regular file inside " \
                   f"{f.tokens}; fix the config first" in r.stderr, (token_file, r)
            assert TOKEN not in r.stdout + r.stderr
            assert tree_snapshot(f) == before, token_file
            assert outside.read_text() == "outside" and real.read_text() == "real"
    finally:
        f.config.write_bytes(original)
        (f.tokens / "via-link.token").unlink()
        real.unlink()
        shutil.rmtree(f.tokens / "sub")
    assert tree_snapshot(f)[0] == original


def test_add_token_refuses_a_missing_tokens_directory(f):
    original = f.config.read_bytes()
    moved = f.tmp / "tokens-moved"
    f.tokens.rename(moved)
    try:
        r = f.run("add-token", "gone", "--no-restart", input=TOKEN + "\n")
        assert r.returncode == 1, r
        assert f"{f.tokens} is missing; run install.sh" in r.stderr, r
        assert TOKEN not in r.stdout + r.stderr
        assert not f.tokens.exists(), "the refusal must not recreate the tokens directory"
        assert f.config.read_bytes() == original
    finally:
        moved.rename(f.tokens)
    assert not (f.tokens / "gone.token").exists()


def test_add_token_refuses_a_token_file_that_is_not_a_regular_file(f):
    directory = f.tmp / "a-directory"
    directory.mkdir()
    missing = f.tmp / "missing.token"
    before = tree_snapshot(f)
    for source in (directory, missing):
        r = f.run("add-token", "src", "--token-file", str(source), "--no-restart")
        assert r.returncode == 1, (source, r)
        assert f"token file {source} is not a regular file" in r.stderr, r
        assert tree_snapshot(f) == before, source
        assert not (f.tokens / "src.token").exists()


def test_add_token_from_a_terminal_prints_the_hint_and_hides_the_typed_token(f):
    import pty
    import select
    import signal
    name = "typed"
    pid, master = pty.fork()
    if pid == 0:  # child: stdin, stdout and stderr are the pty's slave side
        try:
            os.execve(sys.executable, [sys.executable, "-I", str(ADMIN), "add-token", name, "--no-restart"],
                      f.env)
        finally:
            os._exit(127)
    captured = b""
    deadline = time.monotonic() + 30
    sent = False
    status = None
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], 0.5)
            if not ready:
                continue
            try:
                chunk = os.read(master, 4096)
            except OSError:  # EIO: the child closed its side
                break
            if not chunk:
                break
            captured += chunk
            # getpass flushes pending input when it disables echo, so type only once it prompts.
            if not sent and b"input hidden" in captured:
                os.write(master, TOKEN.encode() + b"\n")
                sent = True
        else:
            raise AssertionError(f"admin did not finish within the timeout: {captured!r}")
        _, status = os.waitpid(pid, 0)
    finally:
        if status is None:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        os.close(master)
    text = captured.decode(errors="replace")
    assert sent and os.WIFEXITED(status) and os.WEXITSTATUS(status) in (0, 3), (os.WEXITSTATUS(status), text)
    assert "Run `claude setup-token` in a browser session logged into that account." in text, text
    assert "Paste the sk-ant-oat01-... token here (input hidden): " in text, text
    assert TOKEN not in text, text
    token_file = f.tokens / f"{name}.token"
    assert token_file.read_text() == TOKEN and mode(token_file) == 0o640
    assert name in [a["name"] for a in f.cfg()["accounts"]]
    assert f.run("remove-token", name, "--no-restart").returncode in (0, 3)
    assert not token_file.exists()


def test_check_refuses_a_bad_config_and_changes_nothing(f):
    good, good_mode = f.config.read_bytes(), mode(f.config)
    try:
        # (config text, expected refusal text)
        cases = [
            (None, f"{f.config} does not exist; run install.sh"),
            ("{not json", f"{f.config} is not valid JSON ("),
            ("[]", f"{f.config} must contain a JSON object"),
            ("3", f"{f.config} must contain a JSON object"),
            ('{"accounts": "x"}', "config 'accounts' must be a list of objects"),
            ('{"accounts": ["x"]}', "config 'accounts' must be a list of objects"),
            ('{"accounts": [{"name": 1, "token_file": "a"}]}', "config 'accounts' must be a list of objects"),
            ('{"accounts": [{"name": "a"}]}', "config 'accounts' must be a list of objects"),
            ('{"accounts": [{"name": "a", "token_file": 2}]}', "config 'accounts' must be a list of objects"),
            ('{"devices": []}', "config 'devices' must be an object"),
            ('{"devices": "x"}', "config 'devices' must be an object"),
            ('{"accounts": [{"name": "a", "token_file": "a.token"}, {"name": "a", "token_file": "b.token"}]}',
             "config 'accounts' has duplicate names"),
        ]
        for text, want in cases:
            if text is None:
                f.config.unlink()
            else:
                f.config.write_text(text)
            before = tree_snapshot(f) if text is not None else sorted(f.etc.rglob("*"))
            r = f.run("check")
            assert r.returncode == 1, (text, r)
            assert r.stderr.startswith("error: ") and want in r.stderr, (text, r)
            assert r.stdout == "", (text, r)
            assert (tree_snapshot(f) if text is not None else sorted(f.etc.rglob("*"))) == before, text
        # The parser error is passed on, not swallowed.
        f.config.write_text("{not json")
        assert "Expecting property name" in f.run("check").stderr
    finally:
        f.config.write_bytes(good)
        f.config.chmod(good_mode)


def test_refuses_without_root_or_a_prefix(f):
    if os.geteuid() == 0:
        print("skip test_refuses_without_root_or_a_prefix: running as root would act on the real /etc")
        return
    env = {k: v for k, v in f.env.items() if k != "CLAUDE_ROTATE_PREFIX"}
    before = tree_snapshot(f)
    f.reset_calls()
    r = f.run("check", env=env)
    assert r.returncode == 1, r
    assert r.stderr == "error: claude-rotate-admin must run as root: sudo claude-rotate-admin ...\n", r
    assert r.stdout == "", r
    assert tree_snapshot(f) == before
    assert f.calls() == []


def container_env(f, name):
    """A container-mode environment: the two paths the proxy reads, in a fresh tree, and no prefix."""
    base = f.tmp / name
    (base / "config").mkdir(parents=True)
    (base / "data").mkdir()
    env = {k: v for k, v in f.env.items() if k != "CLAUDE_ROTATE_PREFIX"}
    env.update(CLAUDE_ROTATE_ADMIN_MODE="container", CLAUDE_ROTATE_CONFIG=str(base / "config/config.json"),
               CLAUDE_ROTATE_STATE=str(base / "data/state.json"))
    return base, env


def test_container_mode_adds_a_token_and_a_device_as_the_caller(f):
    base, env = container_env(f, "container-add")
    config, tokens = base / "config/config.json", base / "config/tokens"
    r = f.run("init", env=env)
    assert r.returncode == 3, r
    assert mode(config) == 0o640 and mode(tokens) == 0o750, r
    r = f.run("add-token", "acct-1", input=TOKEN, env=env)
    assert r.returncode == 2 and "nothing is live" in r.stdout, r  # stored; there is no unit to restart
    assert TOKEN not in r.stdout + r.stderr
    token_file = tokens / "acct-1.token"
    assert token_file.read_text() == TOKEN
    assert mode(token_file) == 0o600 and os.lstat(token_file).st_uid == os.getuid(), r
    assert os.lstat(token_file).st_gid == os.getgid()
    cfg = json.loads(config.read_text())
    assert cfg["accounts"] == [{"name": "acct-1", "token_file": "tokens/acct-1.token"}]
    assert mode(config) == 0o640 and os.lstat(config).st_uid == os.getuid()
    r = f.run("add-user", "laptop", env=env)
    assert r.returncode == 2, r
    assert list(json.loads(config.read_text())["devices"]) == ["laptop"], r
    # The lock lives next to the state file, and nothing is made under the host paths.
    assert (base / "data/admin.lock").is_file() and mode(base / "data/admin.lock") == 0o600
    assert f.calls() == []
    # Renewal keeps the mode.
    r = f.run("add-token", "acct-1", "--no-restart", input=TOKEN.replace("0001", "0002"), env=env)
    assert r.returncode == 0 and mode(token_file) == 0o600, r
    # An init pass normalizes a loose token to the container mode, not the host one.
    token_file.chmod(0o644)
    f.run("init", env=env)
    assert mode(token_file) == 0o600


def test_container_mode_refuses_a_symlinked_config_and_rotate(f):
    base, env = container_env(f, "container-refuse")
    config = base / "config/config.json"
    r = f.run("init", env=env)
    assert r.returncode == 3, r
    elsewhere = base / "elsewhere.json"
    elsewhere.write_text(config.read_text())
    config.unlink()
    config.symlink_to(elsewhere)
    before = elsewhere.read_bytes()
    r = f.run("add-user", "laptop", env=env)
    assert r.returncode == 1 and "is a symbolic link" in r.stderr, r
    assert elsewhere.read_bytes() == before
    config.unlink()
    elsewhere.rename(config)
    for name, token in (("acct-1", TOKEN), ("acct-2", TOKEN.replace("0001", "0002"))):
        assert f.run("add-token", name, "--no-restart", input=token, env=env).returncode == 0
    state_before = (base / "data/state.json").exists()
    r = f.run("rotate", "acct-2", env=env)
    assert r.returncode == 1 and "stop the proxy first; rotate rewrites state.json" in r.stderr, r
    assert (base / "data/state.json").exists() == state_before
    assert f.calls() == []


def test_service_identity_missing_is_refused_in_process(f):
    with admin_in_process(f) as mod:
        found = mock.Mock(pw_uid=1, gr_gid=1)
        for gone, bad, fine in (("user", "pwd.getpwnam", "grp.getgrnam"),
                                ("group", "grp.getgrnam", "pwd.getpwnam")):
            with mock.patch.object(mod.os, "geteuid", return_value=0), \
                    mock.patch(bad, side_effect=KeyError("claude-rotate")), \
                    mock.patch(fine, return_value=found):
                try:
                    mod.service_ids()
                except mod.Refused as exc:
                    assert "service identity 'claude-rotate' is missing; run install.sh first" in str(exc), exc
                else:
                    raise AssertionError(f"service_ids did not refuse a missing {gone}")
        # No prefix and a non-root caller reaches the same lookup.
        with mock.patch.object(mod, "PREFIX", ""), mock.patch.object(mod.os, "geteuid", return_value=1000), \
                mock.patch("pwd.getpwnam", side_effect=KeyError("claude-rotate")):
            try:
                mod.service_ids()
            except mod.Refused as exc:
                assert "service identity" in str(exc), exc
            else:
                raise AssertionError("service_ids did not refuse without a prefix")


def test_symlinked_ancestor_of_a_managed_path_is_refused(f):
    real = f.prefix / "var-real"
    (f.prefix / "var").rename(real)
    (f.prefix / "var").symlink_to(real)
    try:
        before = tree_snapshot(f)
        state_before = sorted(p.name for p in real.rglob("*"))
        f.reset_calls()
        r = f.run("activate")
        assert r.returncode == 1, r
        assert r.stderr.startswith("error: ") and "is a symbolic link; refusing to operate through it" in r.stderr, r
        assert str(f.prefix / "var") in r.stderr, r
        assert tree_snapshot(f) == before
        assert sorted(p.name for p in real.rglob("*")) == state_before
        assert f.calls() == []
    finally:
        (f.prefix / "var").unlink()
        real.rename(f.prefix / "var")


def test_set_account(f):
    assert f.run("add-token", "sa", "--no-restart", input=TOKEN + "\n").returncode == 0
    assert f.run("add-user", "dev-two", "--no-restart").returncode == 0
    token_file = f.tokens / "sa.token"
    token_before = (token_file.read_bytes(), os.stat(token_file).st_mtime_ns)

    def acct():
        return next(a for a in f.cfg()["accounts"] if a["name"] == "sa")

    def set_ok(*flags):
        r = f.run("set-account", "sa", *flags, "--no-restart")
        assert r.returncode == 0, (flags, r)
        assert mode(f.config) == 0o640
        return acct()

    assert set_ok("--disable")["disabled"] is True
    assert "disabled" not in set_ok("--enable")
    assert set_ok("--priority", "3")["priority"] == 3
    assert set_ok("--weight", "0.25")["weight"] == 0.25
    assert set_ok("--weekly-cap", "0.5")["weekly_cap"] == 0.5
    assert set_ok("--weekly-pacing")["weekly_pacing"] is True
    assert set_ok("--pace-lookahead", "2d")["pace_lookahead"] == "2d"
    assert "weekly_cap" not in set_ok("--no-weekly-cap")
    assert "weekly_pacing" not in set_ok("--no-weekly-pacing")
    assert "pace_lookahead" not in set_ok("--no-pace-lookahead")
    # Several settings in one call, and the service is signalled unless --no-restart.
    a = set_ok("--disable", "--weight", "2", "--weekly-cap", "1")
    assert a["disabled"] is True and a["weight"] == 2 and a["weekly_cap"] == 1
    set_ok("--enable")
    f.reset_calls()
    f.set_active(True)
    r = f.run("set-account", "sa", "--priority", "5")
    assert r.returncode in (0, 2), r
    assert any(c.startswith("reload") for c in f.calls()), f.calls()
    assert acct()["priority"] == 5
    # Refusals exit 1 and leave config.json byte-identical.
    before = f.config.read_bytes()
    for flags, message in ((("sa", "--weight", "0"), "weight must be a number > 0"),
                           (("sa", "--weight", "nan"), "weight must be a number > 0"),
                           (("sa", "--weekly-cap", "1.5"), "weekly_cap must be a number from 0 to 1"),
                           (("sa", "--pace-lookahead", "12x"), "pace_lookahead must be an integer and one unit"),
                           (("sa", "--priority", "x"), "priority must be an integer"),
                           (("ghost", "--disable"), "does not exist"),
                           (("sa", "--private-to", "ghost"), "not registered"),
                           (("sa",), "no setting given"),
                           (("sa", "--disable", "--enable"), "not allowed with")):
        r = f.run("set-account", *flags, "--no-restart")
        assert r.returncode == 1 and message in r.stderr, (flags, r)
        assert f.config.read_bytes() == before, flags
    # private_to: a new list gets priority 1 only when there is none; --shared keeps priority.
    a = set_ok("--private-to", "alice-laptop,dev-two")
    assert a["private_to"] == ["alice-laptop", "dev-two"] and a["priority"] == 5
    cfg = f.cfg()
    del next(x for x in cfg["accounts"] if x["name"] == "sa")["priority"]
    f.config.write_text(json.dumps(cfg))
    assert set_ok("--private-to", "dev-two")["private_to"] == "dev-two"
    assert acct()["priority"] == 1
    a = set_ok("--shared")
    assert "private_to" not in a and a["priority"] == 1
    assert set_ok("--priority", "100")["priority"] == 100
    assert (token_file.read_bytes(), os.stat(token_file).st_mtime_ns) == token_before
    # status shows the settings, with defaults for absent keys.
    set_ok("--weight", "2", "--weekly-cap", "0.5", "--weekly-pacing", "--pace-lookahead", "6h",
           "--private-to", "alice-laptop")
    report = json.loads(f.run("status", "--json").stdout)
    row = next(x for x in report["accounts"] if x["name"] == "sa")
    assert (row["weight"], row["weekly_cap"], row["weekly_pacing"], row["pace_lookahead"],
            row["private_to"]) == (2, 0.5, True, "6h", "alice-laptop")
    set_ok("--shared", "--no-weekly-cap", "--no-weekly-pacing", "--no-pace-lookahead")
    set_ok("--weight", "1")
    cfg = f.cfg()
    del next(x for x in cfg["accounts"] if x["name"] == "sa")["weight"]
    f.config.write_text(json.dumps(cfg))
    report = json.loads(f.run("status", "--json").stdout)
    row = next(x for x in report["accounts"] if x["name"] == "sa")
    assert (row["weight"], row["weekly_cap"], row["weekly_pacing"], row["pace_lookahead"],
            row["private_to"]) == (1, None, False, None, None)
    human = f.run("status").stdout
    assert "weight 1, weekly_cap none, weekly_pacing off, pace_lookahead default, private_to none" in human


def test_menu(f):
    assert f.run("add-token", "mn", "--no-restart", input=TOKEN + "\n").returncode == 0
    f.set_active(False)

    def acct(name):
        return next((a for a in f.cfg()["accounts"] if a["name"] == name), None)

    # Select mn, change a setting, disable; the reprinted table shows it; quit exits 0.
    r = f.run("menu", input="1\nmn\n1\n1\n3\n")
    assert r.returncode == 0, r
    assert acct("mn")["disabled"] is True
    assert "mn: active no, disabled yes" in r.stdout and "mn: active no, disabled no" in r.stdout, r.stdout
    assert mode(f.config) == 0o640
    # An invalid value prints the refusal set-account prints, changes nothing, and returns to the menu.
    before = f.config.read_bytes()
    flag = f.run("set-account", "mn", "--weekly-cap", "1.5", "--no-restart")
    r = f.run("menu", input="1\nmn\n1\n5\n1.5\n3\n")
    assert r.returncode == 0 and f.config.read_bytes() == before, r
    assert flag.returncode == 1 and flag.stderr.strip() in r.stderr, (flag, r)
    # A valid value goes through the shared code.
    r = f.run("menu", input="1\nmn\n1\n5\n0.5\n3\n")
    assert r.returncode == 0 and acct("mn")["weekly_cap"] == 0.5, r
    # Remove with a wrong name changes nothing; with the right name the account and token are gone.
    before = f.config.read_bytes()
    r = f.run("menu", input="1\nmn\n4\nnope\n3\n")
    assert r.returncode == 0 and f.config.read_bytes() == before and (f.tokens / "mn.token").exists(), r
    assert "not confirmed" in r.stdout
    r = f.run("menu", input="1\nmn\n4\nmn\n3\n")
    assert r.returncode == 0 and acct("mn") is None and not (f.tokens / "mn.token").exists(), r
    # Add an account with a piped token, then select it and disable it: later answers are still read.
    r = f.run("menu", input="2\nc\nsk-ant-oat01-ci-dummy-token\n1\nc\n1\n1\n3\n")
    assert r.returncode == 0, r
    assert (f.tokens / "c.token").read_text().strip() == "sk-ant-oat01-ci-dummy-token"
    assert acct("c")["disabled"] is True
    # A token without the prefix prints read_token's refusal and writes nothing.
    before = f.config.read_bytes()
    r = f.run("menu", input="2\nd\nnot-a-token\n3\n")
    assert r.returncode == 0 and "token should start with sk-ant-oat01-; nothing stored" in r.stderr, r
    assert f.config.read_bytes() == before and not (f.tokens / "d.token").exists()
    # Replace a token through the menu.
    r = f.run("menu", input="1\nc\n2\nsk-ant-oat01-replaced\n3\n")
    assert r.returncode == 0 and (f.tokens / "c.token").read_text().strip() == "sk-ant-oat01-replaced", r
    # End of input at the main menu quits like quit; inside an action it abandons it and exits 1.
    assert f.run("menu", input="").returncode == 0
    before = f.config.read_bytes()
    r = f.run("menu", input="2\ne\n")
    assert r.returncode == 1 and "end of input; nothing changed" in r.stderr, r
    assert f.config.read_bytes() == before and not (f.tokens / "e.token").exists()
    # Clean up for the tests after this one, and a bare run still prints usage.
    assert f.run("remove-token", "c", "--no-restart").returncode == 0
    r = f.run()
    assert r.returncode == 1 and "usage" in r.stdout.lower(), r


HOLD_AND_RUN = (
    "import fcntl, json, os, subprocess, sys\n"
    "lock, admin, caller_holds = sys.argv[1], sys.argv[2], sys.argv[3] == 'held'\n"
    "args = sys.argv[4:]\n"
    "held = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)\n"
    "fcntl.flock(held, fcntl.LOCK_EX)\n"
    # The held descriptor is handed over, or a second descriptor on the same file that holds nothing.
    "fd = held if caller_holds else os.open(lock, os.O_RDWR)\n"
    "env = dict(os.environ, CLAUDE_ROTATE_LOCK_FD=str(fd))\n"
    "r = subprocess.run([sys.executable, '-I', admin, *args], env=env, pass_fds=[fd], timeout=20,\n"
    "                   capture_output=True, text=True)\n"
    "probe = ('import fcntl, os, sys\\n'\n"
    "         'fd = os.open(sys.argv[1], os.O_RDWR)\\n'\n"
    "         'try:\\n'\n"
    "         '    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\\n'\n"
    "         'except BlockingIOError:\\n'\n"
    "         '    sys.exit(0)\\n'\n"
    "         'sys.exit(1)\\n')\n"
    "third = subprocess.run([sys.executable, '-c', probe, lock], timeout=20)\n"
    "print(json.dumps({'rc': r.returncode, 'stdout': r.stdout, 'stderr': r.stderr,\n"
    "                  'still_locked': third.returncode == 0}))\n")


def run_with_lock_descriptor(f, holds, *args):
    """Run an admin command as install.sh does: a parent holds the lock file's flock and passes a
    descriptor in CLAUDE_ROTATE_LOCK_FD. With holds False the descriptor is a second open of the same
    file that the parent's lock excludes. Returns the admin's result plus whether a third process
    still cannot take the lock once the admin has finished."""
    lock = f.prefix / "run/claude-rotate.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run([sys.executable, "-c", HOLD_AND_RUN, str(lock), str(ADMIN), "held" if holds else "other",
                        *args], env=f.env, text=True, capture_output=True, timeout=60)
    assert r.returncode == 0, r
    return json.loads(r.stdout)


def test_inherited_lock_descriptor_is_reused_and_kept_by_a_locking_command(f):
    before = f.cfg()
    got = run_with_lock_descriptor(f, True, "add-user", "lock-held", "--no-restart")
    try:
        assert got["rc"] == 0, got
        assert "lock-held" in got["stdout"], got
        assert f.cfg()["devices"].get("lock-held", "").startswith("dev-lock-held-"), got
        # AdminLock never unlocks an inherited descriptor: the parent still holds the lock.
        assert got["still_locked"] is True, got
    finally:
        f.config.write_text(json.dumps(before))


def test_inherited_lock_descriptor_that_the_caller_does_not_hold_is_refused(f):
    before = f.config.read_bytes()
    got = run_with_lock_descriptor(f, False, "add-user", "lock-contended", "--no-restart")
    assert got["rc"] == 1, got
    assert got["stderr"].startswith("error: the inherited lock descriptor is not held by the caller"), got
    assert got["stdout"] == "", got
    assert f.config.read_bytes() == before
    assert "lock-contended" not in f.cfg()["devices"]
    assert got["still_locked"] is True, got


@contextlib.contextmanager
def config_and_tokens_restored(f):
    """Leave the config (bytes and mode) and the tokens directory as they were."""
    config, config_mode = f.config.read_bytes(), mode(f.config)
    names = {p.name for p in f.tokens.iterdir()}
    try:
        yield
    finally:
        for p in f.tokens.iterdir():
            if p.name not in names:
                p.unlink()
        f.config.write_bytes(config)
        f.config.chmod(config_mode)


def ready_config(f, devices=None, accounts=None):
    """Write a config with one account that has a token file, plus the given devices and accounts."""
    token = f.tokens / "ready.token"
    token.write_text(TOKEN)
    token.chmod(0o640)
    cfg = f.cfg()
    cfg["accounts"] = [{"name": "ready", "token_file": "tokens/ready.token"}] + (accounts or [])
    cfg["devices"] = {"good": "dev-good-0123456789abcdef"} if devices is None else devices
    f.config.write_text(json.dumps(cfg))


def test_readiness_reports_each_problem_through_check(f):
    with config_and_tokens_restored(f):
        # (1) An account without a token file warns that startup skips it; it does not fail readiness.
        ready_config(f, accounts=[{"name": "ghost", "token_file": "tokens/ghost.token"}])
        r = f.run("check")
        warning = "warning: account 'ghost' has no token file (tokens/ghost.token); it is skipped at startup"
        assert r.returncode == 0 and warning in r.stdout.splitlines(), r
        assert "configuration complete" in r.stdout, r
        # With nothing else to carry the service the same warning comes with a problem.
        cfg = f.cfg()
        cfg["accounts"] = [{"name": "ghost", "token_file": "tokens/ghost.token"}]
        f.config.write_text(json.dumps(cfg))
        r = f.run("check")
        assert r.returncode == 3 and warning in r.stdout.splitlines(), r
        assert "  - no enabled account with a non-empty token file: sudo claude-rotate-admin add-token NAME" \
            in r.stdout.splitlines(), r
        # (2) A device key that is empty, blank or not a string.
        ready_config(f, devices={"good": "dev-good-0123456789abcdef", "empty": "", "blank": "  ", "number": 5})
        r = f.run("check")
        assert r.returncode == 3 and "setup needed:" in r.stdout, r
        assert "  - device(s) with an empty key: empty, blank, number" in r.stdout.splitlines(), r
        assert "unique" not in r.stdout and "placeholder" not in r.stdout, r
        # (3) A key holding the example config's placeholder.
        ready_config(f, devices={"good": "dev-good-0123456789abcdef", "example": "dev-example-REPLACE-WITH-X"})
        r = f.run("check")
        assert r.returncode == 3, r
        assert ("  - placeholder device key(s) from the example config: example; "
                "remove them (remove-user) and use add-user") in r.stdout.splitlines(), r
        assert "empty key" not in r.stdout and "unique" not in r.stdout, r
        # (4) Two devices sharing a key.
        ready_config(f, devices={"one": "dev-shared-0123456789abcdef", "two": "dev-shared-0123456789abcdef"})
        r = f.run("check")
        assert r.returncode == 3, r
        assert "  - device keys are not unique" in r.stdout.splitlines(), r
        assert "empty key" not in r.stdout and "placeholder" not in r.stdout, r
        # A config with none of these is complete and prints no warning.
        ready_config(f)
        r = f.run("check")
        assert r.returncode == 0 and r.stdout == "configuration complete\n", r


def test_add_user_removes_placeholder_devices_and_names_each(f):
    with config_and_tokens_restored(f):
        ready_config(f, devices={"example-a": "dev-a-REPLACE-WITH-X", "keep": "dev-keep-0123456789abcdef",
                                 "example-b": "REPLACE-WITH-Y"})
        r = f.run("add-user", "fresh", "--no-restart")
        assert r.returncode == 0, r
        for name in ("example-a", "example-b"):
            assert f"removed placeholder device '{name}' from the example config" in r.stdout.splitlines(), r
        assert "keep" not in "".join(l for l in r.stdout.splitlines() if l.startswith("removed")), r
        devices = f.cfg()["devices"]
        assert sorted(devices) == ["fresh", "keep"], devices
        assert devices["keep"] == "dev-keep-0123456789abcdef"
        assert not any("REPLACE-WITH" in v for v in devices.values())
        assert "REPLACE-WITH" not in f.config.read_text()


def test_init_on_a_complete_configuration_reports_it_complete(f):
    with config_and_tokens_restored(f):
        ready_config(f)
        r = f.run("init")
        assert r.returncode == 0, r
        assert r.stdout.splitlines() == [f"adopted existing {f.config}", "configuration complete"], r
        assert r.stderr == ""
        assert f.cfg()["devices"] == {"good": "dev-good-0123456789abcdef"}


def test_init_without_a_config_or_an_example_refuses(f):
    # A copy of the admin whose sibling config.example.json does not exist.
    bare = f.tmp / "no-example" / "deploy"
    bare.mkdir(parents=True)
    copy = bare / "claude_rotate_admin.py"
    shutil.copy(ADMIN, copy)
    with config_and_tokens_restored(f):
        f.config.unlink()
        r = subprocess.run([sys.executable, "-I", str(copy), "init"], env=f.env, text=True,
                           capture_output=True, timeout=60)
        missing = copy.parent.parent / "config.example.json"
        assert not missing.exists()
        assert r.returncode == 1, r
        assert r.stderr == f"error: {missing} is missing; cannot create a default configuration\n", r
        assert r.stdout == "", r
        assert not f.config.exists()


def test_init_sets_token_modes_and_leaves_symbolic_links_alone(f):
    with config_and_tokens_restored(f):
        ready_config(f)
        plain = f.tokens / "plain.token"
        plain.write_text(TOKEN)
        plain.chmod(0o600)
        target = f.tmp / "link-target"
        target.write_text("target")
        target.chmod(0o600)
        link = f.tokens / "linked.token"
        link.symlink_to(target)
        r = f.run("init")
        assert r.returncode == 0 and "configuration complete" in r.stdout, r
        # (3) A regular file is 0640 and owned by the service identity (the caller under a prefix).
        assert mode(plain) == 0o640 and plain.read_text() == TOKEN
        st = os.lstat(plain)
        assert (st.st_uid, st.st_gid) == (os.getuid(), os.getgid())
        # (4) A symbolic link is reported and left alone, and so is what it points at.
        assert f"warning: {link} is a symbolic link; left unchanged" in r.stdout.splitlines(), r
        assert link.is_symlink() and os.readlink(link) == str(target)
        assert mode(target) == 0o600 and target.read_text() == "target"
        assert f"{plain} is a symbolic link" not in r.stdout


@contextlib.contextmanager
def state_restored(f):
    """Leave the state file (bytes) and, if it was absent, the state directory as they were."""
    dir_existed = f.state_dir.exists()
    data = f.state.read_bytes() if f.state.exists() else None
    try:
        yield
    finally:
        if not dir_existed:
            shutil.rmtree(f.state_dir, ignore_errors=True)
        elif data is None:
            f.state.unlink(missing_ok=True)
        else:
            f.state.write_bytes(data)


def rotate_config(f, *names, empty=()):
    """Two or more enabled accounts, `ready` first (the default active one), with default thresholds.
    Each name in `names` gets a token file; the ones in `empty` get a zero-byte one."""
    ready_config(f, accounts=[{"name": n, "token_file": f"tokens/{n}.token"} for n in names])
    for n in names:
        (f.tokens / f"{n}.token").write_bytes(b"" if n in empty else TOKEN.encode())
    cfg = f.cfg()
    cfg["switch_threshold"], cfg["switch_threshold_7d"] = 0.8, 0.98
    f.config.write_text(json.dumps(cfg))


def test_rotate_refuses_each_ineligible_target_with_its_reason_and_keeps_state(f):
    with config_and_tokens_restored(f), state_restored(f):
        rotate_config(f, "empty", "weekly", "other", empty=("empty",))
        far = time.time() + 3600
        write_state(f, {"active_account": "ready", "custom": "keep", "events": [],
                        "accounts": {"weekly": {"util_5h": 0.1, "util_7d": 0.98, "reset_7d": far}}})
        f.set_active(True)
        f.reset_calls()
        # (target, the whole refusal, which holds the one reason)
        cases = (("ghost", "error: account 'ghost' is not in the configuration; nothing changed\n"),
                 ("empty", "error: account 'empty' cannot be made active: token file is empty; nothing changed\n"),
                 ("weekly", "error: account 'weekly' cannot be made active: "
                            "7d window at 98% (threshold 98%); nothing changed\n"))
        for present in (True, False):
            if not present:
                f.state.unlink()
            for target, message in cases if present else cases[:2]:  # the 7d figure lives in the file
                r = f.run("rotate", target)
                assert r.returncode == 1 and r.stdout == "" and r.stderr == message, (target, present, r)
                assert f.state.exists() is present, (target, present)
            if present:
                assert json.loads(f.state.read_text())["custom"] == "keep"
        # A state file that is not valid JSON is refused, with the way out; one that is not an object too.
        for text, want in (("{not json", "is not valid JSON ("), ("[1]", "must contain a JSON object")):
            f.state.write_text(text)
            r = f.run("rotate", "other")
            assert r.returncode == 1 and r.stdout == "" and r.stderr.startswith(f"error: {f.state} "), (text, r)
            assert want in r.stderr, (text, r)
            if text == "{not json":
                assert "Stop the service and move the file aside to reset rotation state" in r.stderr, r
            assert f.state.read_text() == text
        assert f.calls() == []  # never stopped the service for a refusal


def test_rotate_counts_a_passed_reset_as_zero_utilisation(f):
    with config_and_tokens_restored(f), state_restored(f):
        rotate_config(f, "other")
        f.set_active(False)
        past, future = time.time() - 60, time.time() + 3600

        def state_with(reset_5h, reset_7d):
            write_state(f, {"active_account": "ready", "events": [], "accounts": {
                "other": {"util_5h": 0.95, "util_7d": 0.99, "reset_5h": reset_5h, "reset_7d": reset_7d}}})

        # Over both thresholds with a window still open: refused for that window only.
        for reset_5h, reset_7d, reason, absent in (
                (future, past, "5h window at 95% (threshold 80%)", "7d window"),
                (past, future, "7d window at 99% (threshold 98%)", "5h window")):
            state_with(reset_5h, reset_7d)
            before = f.state.read_bytes()
            r = f.run("rotate", "other")
            assert r.returncode == 1 and reason in r.stderr and absent not in r.stderr, r
            assert f.state.read_bytes() == before
        # Both resets passed: the same figures count as zero and the account is eligible.
        state_with(past, past)
        r = f.run("rotate", "other")
        assert r.returncode == 0 and "active account set to 'other' (was 'ready')" in r.stdout, r
        assert json.loads(f.state.read_text())["active_account"] == "other"
        assert json.loads(f.state.read_text())["events"][-1]["to"] == "other"


def test_rotate_creates_a_missing_state_directory_private_to_the_service_identity(f):
    if os.geteuid() == 0:
        print("skip test_rotate_creates_a_missing_state_directory_private_to_the_service_identity: "
              "root would need the real service account")
        return
    with config_and_tokens_restored(f), state_restored(f):
        rotate_config(f, "other")
        f.set_active(False)
        shutil.rmtree(f.state_dir)
        r = f.run("rotate", "other")
        assert r.returncode == 0, r
        assert f.state_dir.is_dir() and not f.state_dir.is_symlink()
        st = os.lstat(f.state_dir)
        assert stat.S_IMODE(st.st_mode) == 0o700 and (st.st_uid, st.st_gid) == (os.getuid(), os.getgid())
        assert json.loads(f.state.read_text())["active_account"] == "other"
        assert mode(f.state) == 0o600


def status_line(r, prefix):
    """The one line of a status run that starts with `prefix`."""
    lines = [l for l in r.stdout.splitlines() if l.startswith(prefix)]
    assert len(lines) == 1, (prefix, r)
    return lines[0]


def test_status_prints_a_failed_reload_and_the_settings_that_need_a_restart(f):
    with config_and_tokens_restored(f):
        ready_config(f)
        f.set_active(True)
        error = "placeholder device key(s) in config.json"
        with FakeService(f, error=error, restart_needed=["port", "bind"]):
            r = f.run("status")
        assert r.returncode == 0, r
        line = status_line(r, "live config: loaded ")
        assert line.endswith(f"; LAST RELOAD FAILED, previous config kept: {error}; restart needed for: port, bind"), line
        # A clean reload prints neither.
        with FakeService(f):
            r = f.run("status")
        assert r.returncode == 0 and "live /rotate/status: reachable" in r.stdout, r
        line = status_line(r, "live config: loaded ")
        assert "LAST RELOAD FAILED" not in line and "restart needed" not in line, line


def test_status_reports_a_live_query_that_nothing_answers(f):
    with config_and_tokens_restored(f):
        ready_config(f)
        f.set_active(True)
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()  # nothing listens here now
        cfg = f.cfg()
        cfg["port"] = port
        f.config.write_text(json.dumps(cfg))
        r = f.run("status")
        assert r.returncode == 0, r
        line = status_line(r, "live /rotate/status:")
        assert line.startswith("live /rotate/status: unavailable (URLError: ") and line.endswith(")"), line
        assert "live config:" not in r.stdout
        report = json.loads(f.run("status", "--json").stdout)
        assert report["live"] is None and report["live_error"].startswith("URLError: "), report


def test_status_lists_the_last_five_events_with_their_fields(f):
    with config_and_tokens_restored(f), state_restored(f):
        ready_config(f)
        f.set_active(False)
        events = [{"ts": f"2026-01-0{i}T10:00:00+0000", "type": "switch" if i % 2 else "exhausted",
                   "from": f"from-{i}", "to": f"to-{i}", "reason": "manual" if i % 2 else "quota"}
                  for i in range(1, 8)]
        write_state(f, {"active_account": "ready", "accounts": {}, "events": events})
        r = f.run("status")
        assert r.returncode == 0, r
        lines = r.stdout.splitlines()
        shown = [f"  {e['ts']} {e['type']} {e['from']} -> {e['to']} ({e['reason']})" for e in events[-5:]]
        assert lines[lines.index("recent events:") + 1:] == shown, lines
        assert "from-1" not in r.stdout and "from-2" not in r.stdout
        # No events, no heading.
        write_state(f, {"active_account": "ready", "accounts": {}, "events": []})
        r = f.run("status")
        assert r.returncode == 0 and "recent events:" not in r.stdout, r


def test_status_reports_an_invalid_state_file_and_still_exits_zero(f):
    with config_and_tokens_restored(f), state_restored(f):
        ready_config(f)
        f.set_active(False)
        f.state_dir.mkdir(parents=True, exist_ok=True)
        for text, want in (("{not json", "is not valid JSON ("), ("[1]", "must contain a JSON object")):
            f.state.write_text(text)
            r = f.run("status")
            assert r.returncode == 0 and r.stderr == "", (text, r)
            line = status_line(r, "state: ERROR ")
            assert line.startswith(f"state: ERROR {f.state} ") and want in line, line
            assert "active account: ready (from default (first account))" in r.stdout, r
            report = json.loads(f.run("status", "--json").stdout)
            assert line == f"state: ERROR {report['state_error']}", (line, report)
            assert f.state.read_text() == text


def test_status_reports_a_broken_config_and_exits_one(f):
    with config_and_tokens_restored(f):
        f.config.write_text("{not json")
        r = f.run("status")
        assert r.returncode == 1 and r.stderr == "", r
        line = status_line(r, "config: ERROR ")
        assert line.startswith(f"config: ERROR {f.config} is not valid JSON (") and "Expecting" in line, line
        assert "accounts (token validity" not in r.stdout and "devices:" not in r.stdout, r
        assert f.config.read_text() == "{not json"


def test_status_follows_the_current_release_link_to_its_marker(f):
    with config_and_tokens_restored(f):
        ready_config(f)
        f.set_active(False)
        opt = f.prefix / "opt/claude-rotate"
        opt_existed = opt.exists()
        release, current = opt / "releases/9.9.9", opt / "current"
        marker = release / ".claude-rotate-release"
        installed = {"installed_at": "2026-01-02T03:04:05Z", "version": "9.9.9"}

        def release_line():
            r = f.run("status")
            assert r.returncode == 0, r
            return status_line(r, "release: ")

        def report():
            return json.loads(f.run("status", "--json").stdout)

        try:
            assert release_line() == "release: none"
            assert report()["current"] is None and report()["release"] is None
            release.mkdir(parents=True)
            current.symlink_to("releases/9.9.9")  # relative to the directory holding the link
            assert release_line() == "release: releases/9.9.9"
            assert report()["current"] == "releases/9.9.9" and report()["release"] is None
            marker.write_text(json.dumps(installed))
            assert release_line() == "release: releases/9.9.9 (installed 2026-01-02T03:04:05Z)"
            assert report()["release"] == installed
            current.unlink()
            current.symlink_to(release)  # an absolute target is used as it is
            assert release_line() == f"release: {release} (installed 2026-01-02T03:04:05Z)"
            assert report()["current"] == str(release) and report()["release"] == installed
            marker.write_text("{nope")
            assert report()["release"] == {"error": "unreadable marker"}
            assert release_line().startswith(f"release: {release}")
        finally:
            current.unlink(missing_ok=True)
            shutil.rmtree(opt / "releases" if opt_existed else opt.parent, ignore_errors=True)


def test_status_queries_loopback_for_a_wildcard_empty_or_non_string_bind(f):
    with config_and_tokens_restored(f):
        ready_config(f)
        f.set_active(True)
        for bind in ("0.0.0.0", "::", "", 5):
            with FakeService(f) as service:
                cfg = f.cfg()
                cfg["bind"] = bind
                f.config.write_text(json.dumps(cfg))
                r = f.run("status")
                assert r.returncode == 0 and "live /rotate/status: reachable" in r.stdout, (bind, r)
                assert service.hits == 1, (bind, service.hits)
                report = json.loads(f.run("status", "--json").stdout)
                assert service.hits == 2, (bind, service.hits)
                assert report["bind"][0] == "127.0.0.1", (bind, report["bind"])
                assert report["live"]["active_account"] == "a", (bind, report["live"])


def main():
    f = Fixture()
    order = ["test_prefix_keeps_the_callers_ids_even_if_the_host_has_the_service_account",
             "test_init_creates_empty_config_and_modes", "test_invalid_names_do_nothing",
             "test_add_token_and_renewal", "test_add_user_activation_and_duplicates",
             "test_private_to_flag_and_device_removal_guard", "test_readiness_needs_enabled_nonempty_token", "test_inherited_lock_descriptor",
             "test_restart_failure_is_reported", "test_concurrent_add_users_are_serialized",
             "test_status_has_no_secrets", "test_status_live_query_uses_header",
             "test_reload_is_confirmed_through_status",
             "test_rotate_rejections_do_not_mutate", "test_rotate_switches_after_stop_and_preserves_state",
             "test_rotate_stop_timeout_restarts_the_service", "test_rotate_failed_stop_refuses_without_a_restart",
             "test_rotate_unwritable_state_restarts_and_exits_not_live",
             "test_remove_token_and_setup_needed", "test_set_url_and_symlinked_config",
             "test_unexpected_errors_are_reported_without_a_traceback",
             "test_unreadable_config_directory_is_reported_as_permission_denied",
             "test_add_token_config_write_failure_removes_the_new_token_file",
             "test_add_token_refuses_a_new_name_whose_token_path_is_taken",
             "test_add_token_refuses_an_existing_account_with_a_token_file_outside_the_tokens_directory",
             "test_add_token_refuses_a_missing_tokens_directory",
             "test_add_token_refuses_a_token_file_that_is_not_a_regular_file",
             "test_add_token_from_a_terminal_prints_the_hint_and_hides_the_typed_token",
             "test_check_refuses_a_bad_config_and_changes_nothing", "test_refuses_without_root_or_a_prefix",
             "test_container_mode_adds_a_token_and_a_device_as_the_caller",
             "test_container_mode_refuses_a_symlinked_config_and_rotate",
             "test_service_identity_missing_is_refused_in_process",
             "test_symlinked_ancestor_of_a_managed_path_is_refused",
             "test_set_account", "test_menu",
             "test_inherited_lock_descriptor_is_reused_and_kept_by_a_locking_command",
             "test_inherited_lock_descriptor_that_the_caller_does_not_hold_is_refused",
             "test_readiness_reports_each_problem_through_check",
             "test_add_user_removes_placeholder_devices_and_names_each",
             "test_init_on_a_complete_configuration_reports_it_complete",
             "test_init_without_a_config_or_an_example_refuses",
             "test_init_sets_token_modes_and_leaves_symbolic_links_alone",
             "test_rotate_refuses_each_ineligible_target_with_its_reason_and_keeps_state",
             "test_rotate_counts_a_passed_reset_as_zero_utilisation",
             "test_rotate_creates_a_missing_state_directory_private_to_the_service_identity",
             "test_status_prints_a_failed_reload_and_the_settings_that_need_a_restart",
             "test_status_reports_a_live_query_that_nothing_answers",
             "test_status_lists_the_last_five_events_with_their_fields",
             "test_status_reports_an_invalid_state_file_and_still_exits_zero",
             "test_status_reports_a_broken_config_and_exits_one",
             "test_status_follows_the_current_release_link_to_its_marker",
             "test_status_queries_loopback_for_a_wildcard_empty_or_non_string_bind"]
    tests = [(n, globals()[n]) for n in order]  # the fixture is built up step by step
    assert sorted(order) == sorted(n for n, g in globals().items() if n.startswith("test_") and callable(g))
    try:
        for name, fn in tests:
            fn(f)
            print(f"ok   {name}")
    finally:
        f.cleanup()
    print(f"{len(tests)} admin checks passed")


if __name__ == "__main__":
    main()
