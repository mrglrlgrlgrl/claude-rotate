"""claude-rotate-admin under a temporary prefix with a fake systemctl and dummy secrets.

  python3 tests/test_admin.py

Nothing here touches /etc, /opt, /var or a real service: CLAUDE_ROTATE_PREFIX points
every managed path into a temporary directory and a recording systemctl stub is
first on PATH.
"""
import http.server
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

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
             "test_remove_token_and_setup_needed", "test_set_url_and_symlinked_config",
             "test_set_account", "test_menu"]
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
