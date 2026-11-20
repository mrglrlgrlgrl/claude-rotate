# Service operations: Debian 12

Manage the installed service over SSH. Accounts, device keys and the active
upstream account are administered with `claude-rotate-admin`, installed by
`install.sh`; the web dashboard is read-only. For installation, upgrades and
removal, see [the deployment guide](deploy/README.md).

## Install, upgrade, remove

Run from a checkout of this repository, as root. The commands are repeatable.

```sh
sudo ./install.sh                      # install or upgrade; leaves the service stopped until configured
sudo claude-rotate-admin add-token acct-1      # paste the output of: claude setup-token
sudo claude-rotate-admin add-user alice-laptop # prints the two export lines for that device
sudo claude-rotate-admin status
```

`install.sh` builds a root-owned release under `/opt/claude-rotate/releases/`,
installs the unit and `/usr/local/sbin/claude-rotate-admin`, and keeps an
existing `/etc/claude-rotate` as it is. A fresh install has no accounts or
devices, so the unit stays disabled and stopped and the script prints
`installed: setup needed`. The first `add-user` after the first `add-token`
enables and starts the service. Running `install.sh` again from a newer
checkout builds a new release, switches `current` and restarts the service.

```sh
sudo ./uninstall.sh                  # removes service, code and admin command; keeps config, tokens, state, logs, identity
sudo ./uninstall.sh --purge          # also deletes /etc/claude-rotate, /var/lib/claude-rotate, /var/log/claude-rotate and the service user
sudo ./uninstall-legacy.sh           # same as uninstall.sh, plus the manual releases 89ec74a and 89ec74a-recovery.*
```

Both uninstall scripts stop the service first and refuse to run when the unit,
the admin wrapper or the `current` link is not the one this project installs.
Home checkouts, staging directories, Tailscale Serve routes and other services
are never touched. See [Removal](deploy/README.md#removal) for details.

## Everyday commands

Run these on the VPS. Each table row is a separate action, not a script to paste
in full.

| Action | Command |
| --- | --- |
| Status | `sudo systemctl status claude-rotate --no-pager` |
| Follow logs (Ctrl-C exits the viewer) | `sudo journalctl -u claude-rotate -f` |
| Last 100 log lines | `sudo journalctl -u claude-rotate -n 100 --no-pager` |
| Reload config.json and the token files (open streams carry on) | `sudo systemctl reload claude-rotate` |
| Restart | `sudo systemctl restart claude-rotate` |
| Stop | `sudo systemctl stop claude-rotate` |
| Start | `sudo systemctl start claude-rotate` |
| Stop and disable startup at boot | `sudo systemctl disable --now claude-rotate` |
| Enable startup at boot and start | `sudo systemctl enable --now claude-rotate` |

The unit retries failures after 10 seconds and has a 45-second event-loop
watchdog. An explicit stop stays stopped until started again, or until reboot
if the service is enabled. Stops/restarts allow a bounded drain but can interrupt
requests. Invalid configuration can cause repeated startup failures; check logs.
`claude-rotate-admin` stops and disables the service when the configuration has
no enabled account token or no device key, so an unconfigured install does not
restart in a loop.

## Health and limits

```sh
sudo systemctl show claude-rotate \
  -p ActiveState -p NRestarts -p MemoryCurrent -p MemoryMax
df -h /var/lib/claude-rotate /var/log/claude-rotate
```

Shipped defaults: memory pressure threshold 384 MiB, memory ceiling 512 MiB,
swap allowance 128 MiB, eight active proxy requests, 4 MiB request bodies and
8 MiB buffered responses. SSE responses remain streamed. Overload returns 503;
oversized bodies return 413 and oversized buffered responses return 502.

The audit log rotates at 8 MiB with four backups. The statistics cache retains
at most 10,000 records and 8 MiB of encoded record data; Python object overhead
is additional. Dashboard totals can represent partial history. Journal retention
is separate. Monitor disk space, increasing restart counts and `storage_errors`
in the authenticated `/rotate/status` response. An active process does not prove
upstream token validity; also test a small Claude request through the proxy.

## Installed files

| Purpose | Path |
| --- | --- |
| Accounts, device keys and rotation policy | `/etc/claude-rotate/config.json` |
| Account tokens | `/etc/claude-rotate/tokens/` |
| Saved rotation state | `/var/lib/claude-rotate/state.json` |
| Request audit and rotated backups | `/var/log/claude-rotate/audit.jsonl*` |
| Active release symlink | `/opt/claude-rotate/current` |
| Versioned code and virtual environments | `/opt/claude-rotate/releases/` |
| Installed systemd unit | `/etc/systemd/system/claude-rotate.service` |
| Admin command | `/usr/local/sbin/claude-rotate-admin` |
| Lock shared by install, uninstall and admin | `/run/claude-rotate.lock` |

Config and token files are `0640 root:claude-rotate`; their directories are
`0750 root:claude-rotate`. State/log directories are private to the service.
Keep a protected backup of `/etc/claude-rotate`; it contains bearer credentials.
For a consistent backup of mutable state/logs, stop the service before copying.

## Accounts and devices

`claude-rotate-admin` works from any directory and needs root. It validates the
configuration, writes files atomically with the modes above, holds a lock so
concurrent commands cannot lose each other's changes, and then reloads the
running service so the change is live without dropping open streams (a stopped
service is enabled and started instead). After a reload it asks the service
whether it accepted the new configuration, and prints `is live` only if it did.
If the service kept its previous configuration because the new one did not
validate, or does not answer, it says `NOT live` and exits 2.

| Action | Command |
| --- | --- |
| Show service, accounts, devices, active account | `sudo claude-rotate-admin status` (`--json` for scripts) |
| Add an account, or replace an existing account's token | `sudo claude-rotate-admin add-token NAME` |
| Add an account reserved for one or more devices (comma list), written with priority 1 | `sudo claude-rotate-admin add-token NAME --private-to DEVICE[,DEVICE...]` |
| Change an account's settings, any of `--disable`/`--enable`, `--priority N`, `--weight X`, `--weekly-cap F`/`--no-weekly-cap`, `--weekly-pacing`/`--no-weekly-pacing`, `--pace-lookahead 12h`/`--no-pace-lookahead`, `--final-window 12h`/`--no-final-window` (needs `weekly_pacing`; `--no-weekly-pacing` needs `--no-final-window` while a window above zero is set), `--private-to DEVICE[,DEVICE...]`/`--shared` (`--shared` leaves the `priority: 1` that `--private-to` wrote; `--priority 100` undoes it) | `sudo claude-rotate-admin set-account NAME --FLAG ...` |
| Manage accounts from a numbered prompt menu (select, add, change settings, replace token, make active, remove); answers are read one line at a time from stdin | `sudo claude-rotate-admin menu` |
| Add a device and print its two `export` lines | `sudo claude-rotate-admin add-user NAME` |
| Revoke a device (refused while it is the only owner of a private account) | `sudo claude-rotate-admin remove-user NAME` |
| Remove an account and delete its token file | `sudo claude-rotate-admin remove-token NAME` |
| Set the URL printed for new devices | `sudo claude-rotate-admin set-url https://HOST` |
| Switch to the next eligible account, or to ACCOUNT | `sudo claude-rotate-admin rotate [ACCOUNT]` |
| Validate config.json after a manual edit and reload | `sudo claude-rotate-admin activate` |

`add-account`, `add-device`, `remove-account` and `remove-device` are aliases.
Names are 1-64 ASCII letters, digits, `_` or `-`, starting with a letter or
digit. `add-token` asks for the token with a hidden prompt; in a script, pipe it
on stdin or pass `--token-file PATH`. Tokens never appear on the command line or
in the output. Adding a device whose name exists is refused; use `remove-user`
then `add-user` to issue a new key. `--no-restart` stores a change without
signalling the service; the service still notices the changed files itself
within `reload_poll_s` seconds (5 by default).

Manual edits remain possible with `sudoedit /etc/claude-rotate/config.json`
followed by `sudo claude-rotate-admin activate`, which checks the JSON and the
readiness rules before reloading. Per-account settings have `set-account`
instead; manual edits cover the rotation policy keys and the other top-level
settings.

The service reloads `config.json` and the token files without a restart; the
[README](README.md#reloading-the-configuration) lists what a reload covers. An
invalid file is rejected and the previous configuration keeps running, so
`sudo claude-rotate-admin status` shows `live config: ... LAST RELOAD FAILED`
until it is fixed. Only `bind`, `port`, `reload_poll_s` and the resource and
audit limits (set by the unit's environment on this service) need a restart,
and `status` lists any that changed. A device revoked by a reload loses new
requests at once, but a stream it already has open finishes; restart the
service to cut it off.

## Switching the upstream account

`rotate` makes another account active. Without an argument it picks the next
account in configuration order that exists, is not disabled, has a non-empty
token file and is under both utilization thresholds according to the saved
state; with an argument it checks the same rules for that account and refuses
otherwise. Nothing is written when a request is refused.

The command stops the service, re-reads the state the service saved while
draining, re-checks the target, writes the new active account with a `manual`
switch event, and starts the service again. In-flight requests get the normal
drain period. If the service is not running, the file is updated and the switch
applies at the next start.

The manual choice is protected by the normal switch cooldown
(`switch_cooldown_s`, 300 s by default). After that the configured policy
applies again: `consume-first`, `priority` and the thresholds can move traffic
to another account, and a quota exhaustion or a disabled account moves it
immediately. To prefer one account durably, give it a lower `priority` in the
configuration. `status` shows the active account from the saved state and,
while the service runs, from the live `/rotate/status` response together with
`storage_errors`, per-account last-seen times and upstream status. A present
token file says nothing about whether the token is still valid; only real
upstream traffic does.

## Tailscale and dashboard

```sh
sudo tailscale serve status
```

Use its reported HTTPS URL as clients' `ANTHROPIC_BASE_URL`. Their
`CLAUDE_CODE_OAUTH_TOKEN` is a **device key**. Keep the application bound to
`127.0.0.1:8484`; Tailscale Serve handles tailnet access. Serve is configured
separately from this service. See [Tailscale Serve](https://tailscale.com/docs/reference/tailscale-cli/serve).

Open the read-only dashboard on a tailnet-connected client:

```text
https://YOUR-TAILSCALE-HOST/rotate/panel?key=YOUR-DEVICE-KEY
```

Treat that URL as a password because it includes the key. Every device key can
view the dashboard; there is no separate administrator role. JSON endpoints
`/rotate/status` and `/rotate/stats` also accept `Authorization: Bearer KEY`,
which avoids putting the key in a URL.

To open the dashboard without a key from any tailnet user's device, set
`"panel_tailnet": true` in the config and reload the service
(`sudo claude-rotate-admin activate`); then
`https://YOUR-TAILSCALE-HOST/rotate/panel` works on its own. It relies on the
`Tailscale-User-Login` header that Serve adds, so tagged devices and Funnel
traffic still need a key, and the service refuses to start with it unless
`bind` is loopback. The API itself always requires a device key.

## Updates and rollback

Run `sudo ./install.sh` from the reviewed newer checkout. It builds a new
release and virtual environment at a new final directory from
`requirements.lock`, switches `current` atomically and restarts the service.
Previous releases are kept for rollback; nothing is installed into the active
release. Check real client traffic after switching. Dependency upgrades,
especially uvicorn, require the runner tests and service acceptance checks.

To roll back, follow the [manual rollback procedure](deploy/README.md#immutable-upgrades-and-rollback)
with a previous release directory.
The [Debian systemctl reference](https://manpages.debian.org/bookworm/systemd/systemctl.1.en.html)
documents service-control commands.
