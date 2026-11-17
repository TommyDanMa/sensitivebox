# Sensitive Box

A read-only Linux hardware monitor that **alerts you before something overheats**, with memory
pressure and network activity alongside thermals, fans and power.

> **v0.1.0.** The first shareable release: discovery, alerts, the daemon, the command line,
> and the local dashboard.

## Install

```sh
uv tool install sensitivebox
sbox enable
sbox open
```

`uv tool install sensitivebox` is the PyPI install. The same wheel is attached to the
[v0.1.0 GitHub release](https://github.com/TommyDanMa/sensitivebox/releases/tag/v0.1.0):

```sh
uv tool install "https://github.com/TommyDanMa/sensitivebox/releases/download/v0.1.0/sensitivebox-0.1.0-py3-none-any.whl"
```

End users do not need Node. The release wheel contains the built dashboard.

From a checkout, for development:

```sh
uv sync
uv run sbox --help
```

Both `sbox` and `sensitivebox` are installed and accept the same commands.

## Web UI

The dashboard is at `/`. Alerts (acknowledge, and mute labelled Snooze) are at `/log`. A rule's
replay is at `/rule/<id>`. Settings, including the remembered theme and rule edits, are at
`/settings`. Click a reading to open its history. The design system is at `/styleguide`.

Build the UI, then run `sbox watch` or `sbox demo heatwave --no-notify` and open
`http://127.0.0.1:7878/`. Without a build, the server explains how to build it. `pnpm dev` answers
the API from a scripted machine, including the rule routes the daemon does not have yet.

```sh
cd ui
pnpm i
pnpm dev        # http://localhost:5173/  (the styleguide is /styleguide)
pnpm test       # unit tests
pnpm test:e2e   # Playwright snapshots, behaviour and accessibility checks
pnpm build      # writes to src/sensitivebox/api/static (not committed)
```

Only people working on the UI need Node and pnpm.

## Commands

| Command | Purpose |
|---|---|
| `sbox` | One status report: host, key readings, stored alert states and freshness. |
| `sbox watch` | Run the daemon in the foreground, with desktop notifications and history. |
| `sbox demo heatwave` | Run a packaged scenario with popups and in-memory history. |
| `sbox doctor` | List every sensor, stable ID, group, limits, default rules and unavailable reasons. |
| `sbox apps` | Show current memory PSI and the top five apps while memory is tight. |
| `sbox rules` | Show each configured rule's matched signals, state and reason. |
| `sbox rule cpu-hot` / `sbox rules cpu-hot` | Replay retained input frames through the real engine. |
| `sbox mute cpu-hot 30m` / `sbox unmute cpu-hot` | Hold or release every notification severity for a rule. |
| `sbox quiet 22:00-07:00` / `sbox quiet off` | Recurring local quiet hours for every rule; critical still gets through. |
| `sbox log` / `sbox log -f` | Group incidents, or follow their individual events. |
| `sbox log --weekly` | Per-rule ISO-week alert quality measurements. |
| `sbox log --daemon` | Read the user-service journal; combine with `-f` to follow. |
| `sbox enable` / `sbox disable` | Enable/start or disable/stop the systemd user service now. |
| `sbox open [log\|settings]` / `sbox open rule cpu-hot` | Launch a loopback dashboard URL in the default browser. |
| `sbox report [--fixture]` | Export a private diagnostic archive or reusable machine fixture. |
| `sbox completion bash` | Print a sourceable bash completion script; `zsh` is also supported. |

Use `uv run` before these commands when working from the checkout.

`run` remains a hidden alias of `watch` for one release, with exactly the same arguments.
Old simulation flags now explain how to use `demo`. The old `dump` and `install-service` stubs
have been removed. The dashboard is the next milestone; `open` launches its URL
without checking for a listener and explains that the page needs the service.

## Status and scripting

```sh
sbox
sbox --json
sbox rules --json
sbox doctor --json
```

Readings come from one fresh sample. Alert state comes from the newest stored evaluations;
a one-shot command never starts a new alert engine or creates/migrates the history database.
An evaluation is fresh for three times its signal's poll interval plus the store's 5-second
flush allowance. Missing or stale state shows **Unknown**, with a hint to start `sbox watch`.
A changed rule configuration requires restarting the daemon before its state is trusted.

| Exit code from plain `sbox` | Meaning |
|---|---|
| 0 | Alert state is fresh and no warning or critical alert is open. |
| 1 | A warning is open, or alert state is stale or missing. |
| 2 | A critical alert is open; this wins over other stale state. |
| 3 | Operational failure: invocation, configuration, fixture or database error. |

Info alerts, Pending, Warming and a fresh Unknown signal do not raise the health code.
Host uptime comes from `/proc/uptime` and is labelled **host uptime**. Status and heartbeats
show explicit mute counts and remaining durations when their boot identity is available. Today's
and this month's stored byte totals remain **unavailable**; unavailable never means zero. Network counters in doctor are explicitly labelled **since boot**.

Every command accepts `--json` before or after the command. One-shot results have the shape
`{"version":1,"kind":"status","data":{...}}`; live commands emit newline-delimited JSON
(NDJSON), including startup, alert, status and stopped events. In JSON mode, errors and diagnostic
logs are JSON on stderr; stdout contains no prose. Usage errors return 3, including typos, which
suggest a command without running the correction. Text output has no colour escapes, so pipes
and `NO_COLOR` work without special handling.

## Foreground monitoring

```sh
sbox watch
sbox watch --interval 2
sbox watch --db ./history.db --json
```

The daemon polls every second by default, feeds the alert engine, stores history in
`~/.local/state/sensitivebox/sensitivebox.db` (or `$XDG_STATE_HOME/sensitivebox/sensitivebox.db`),
and sends desktop notifications. Critical alerts use critical urgency; resolved alerts use low
urgency. Output retains startup and alert lines, followed by the 30-second heartbeat and one
status summary line. There is no full-screen redraw. Ctrl-C or SIGTERM stops it cleanly.

Memory and network have a 2-second sampling tier, batteries and chargers 5 seconds, and app
memory at most every 20 seconds while memory is tight. Sleep and shutdown pause evaluation;
resume warms rules again without closing an open alert or sending a duplicate notification.

User rules in `~/.config/sensitivebox/rules.toml` (or `$XDG_CONFIG_HOME/sensitivebox/rules.toml`)
override shipped defaults by ID. Rule states are shown per matched signal, so one critical
signal cannot disappear behind a rule summary. An unmatched rule is Unknown.

## Local API

`watch`, its hidden `run` alias, and `demo` serve the API on **127.0.0.1:7878** inside the same
asyncio loop that polls and alerts. An occupied port prints **API unavailable** while monitoring
continues normally; stopping that daemon still returns 0. No external bind is supported.

| Route | Response |
|---|---|
| `GET /api/sensors` | Timestamp, freshness, grouped readings with labels/units/limits/missing reasons/evaluations, redacted apps, Bluetooth and Wi-Fi status. |
| `GET /api/host` | Hostname, Ubuntu release, kernel, package version; absent facts are null. |
| `GET /api/history?id=<signal>&range=1h` | `{t, avg}` raw samples, Unix seconds and null gaps. |
| `GET /api/history?id=<signal>&range=6h` (or `24h`) | `{t, avg, min, max}` minute buckets, including the partial current minute. |
| `GET /api/alerts?limit=100` | `{incidents, truncated}`, open and recently closed (24h), newest first; limit 1–10000. |
| `POST /api/alerts/<incident-id>/ack` | `{}` body; updated incident. Repeat acknowledgement of an open incident succeeds. |
| `POST /api/alerts/<incident-id>/mute` | `{"duration":"15m","renotify":true}`; updated incident, with its rule muted. |
| `GET /api/stream` | SSE `snapshot` and `alert` events with increasing IDs. Current snapshot first, then future events. |

Sensor reads return 503 until the first poll. Afterward a stale snapshot remains available with
`fresh:false`. Missing readings stay null; real zero readings stay zero. Minute averages exclude
missing readings; empty minutes are null. Every history column has the same length. Today's and
this month's network totals remain unavailable until persistent totals are implemented.

Each signal's evaluations carry `rule_id`, lowercase `state`, `when`, resolved threshold `value`,
effective recovery `clear`, and the rule's `severity` (even when OK). Unavailable thresholds and
clears are null. The signal's own `value` remains the reading. GET and SSE use the same shape.

API JSON has no CLI envelope. Unknown incidents return 404, closed incidents 409. Ack changes no
evaluation or notification. Mute uses the CLI's duration syntax, including `15m`, `30m`, `1h`,
and `until-reboot`, with re-notification on expiry enabled by default. Mutations return 200;
bad JSON or unknown fields return 400 and a non-JSON content type returns 415.

For POST, first GET any API route to receive the `sbox_csrf` cookie, then send its value as
`X-CSRF-Token` with `Content-Type: application/json` and the exact page origin in `Origin`.
POSTs require a loopback Host, same-origin Origin and the session token. Cross-origin CORS is
not enabled. API responses are no-store. JSON/SSE/errors redact private text, including app
names and home paths; stable sensor IDs remain unchanged. Built UI files are served unchanged.

Each SSE subscriber has a 64-event queue. Slow clients are disconnected on overflow; reconnecting
receives the current snapshot, with no replay (even with Last-Event-ID). Fetch chart history from
`/api/history`. Built files are served directly, missing assets return 404, and client routes
such as `/styleguide` and `/rule/cpu-hot` receive index.html. API routes never fall back to HTML.

Only the writer upgrades older history schemas. New evaluations preserve open-alert severity
and incident kind, including during missing readings and resume. Legacy evaluations stay
Unknown until refreshed. The 400-day 15-minute rollups are retained unchanged.

## Mutes, history and weekly measurements

```sh
sbox mute cpu-hot 30m
sbox mute cpu-hot until-reboot --no-renotify
sbox unmute cpu-hot
sbox quiet 22:00-07:00 --no-renotify
sbox quiet off
sbox log cpu-hot --since yesterday --limit 50
sbox log --since 2026-11-01T09:00:00+01:00
sbox log -f --json
sbox log --weekly
sbox rule cpu-hot --json
```

Mutes live in SQLite and are visible to the running daemon. They survive daemon restarts in the
same boot. Timed and until-reboot mutes clear on reboot. Expiry and cooldown use monotonic time,
which stops during suspend. Explicit rule mutes hold all severities. Quiet schedules use local
civil time, follow timezone/DST changes, and hold info/warning while allowing critical through.
Comma-separated quiet intervals are supported.

Muting changes notification delivery only: alerts still evaluate, appear in history and affect
health. Automatic release re-notifies a still-open alert unless `--no-renotify` was selected.
Manual `unmute` and `quiet off` re-notify regardless of that preference. Releases pass through
cooldown and, when held, wait until cooldown ends while the same alert remains open.

One-shot `log` groups incidents newest first, showing severity, rule, firing duration, peak and
resolved time, with held/delivered reasons on the same line (for example, `muted (quiet hours)`).
Peaks survive raw-history pruning; older incidents without a recorded peak show `unavailable`.
For below rules the peak is the lowest reading; missing data never becomes a numeric peak. Its default
range starts at local midnight yesterday, with Today and Yesterday headings. `--since` also
accepts durations, ISO dates and timestamps. `--limit` defaults to 100, accepts 1–10000, and
reports truncation. `-f` appends individual events as NDJSON with `--json`; Ctrl-C stops it.
`log --daemon` reads only the Sensitive Box user-service journal.

Weekly rows record the computation zone and use ISO weeks beginning Monday at local midnight.
Fires belong to the opening week; active firing minutes split across occupied weeks. Median
and percent closed under five minutes describe incidents closed that week. Suspend and daemon
downtime do not count; incomplete durations are flagged. Muted percentage is separate from
acknowledgement, which stays unavailable until an acknowledgement producer records a fact.
The alerts page marks a noisy rule and asks whether an alert was useful.

Alert events, weekly measurements and change markers are kept for 400 days, preserving older
facts needed to identify still-open incidents. Kernel, BIOS, package-version and power-profile
observations begin as baselines. Apt tracking starts at the current readable EOF and records
new completed upgrades across rotation and restart; older upgrades are not imported.

`rules` without an ID lists every configured rule. `rule <id>` and `rules <id>` replay the same
retained history; bare `rule` errors with a `sbox rules` hint. An unknown ID suggests the nearest
configured rule without running a correction or the list. Both commands complete rule IDs.

Replay includes guards, chip limits, rule edits and sleep
lifecycle events. One checkpoint at the ring boundary preserves live engine state across pruning;
daemon-session events reproduce Warming on restart. History readers never create or migrate a
database. Raw inputs remain one hour and the 400-day 15-minute rollups remain unchanged.

## Service and diagnostic archives

```sh
sbox enable
sbox disable
sbox open
sbox open rule cpu-hot
sbox report --output ./diagnostics.tar.gz
sbox report --fixture --output ./machine.tar.gz
```

Enable writes the user unit under `$XDG_CONFIG_HOME/systemd/user/` and the launcher under
`$XDG_DATA_HOME/applications/` (using the standard home defaults when unset), then enables and
starts the service. Disable stops/disables it and leaves both files. Both commands refuse a
differing existing unit or launcher. Generated commands refer to the installed `sbox` executable;
install at its intended location before enabling. Subprocesses use argument vectors and bounded
waits. The default browser opens an app window when its command is recognized as Chromium-based;
otherwise the platform default opener handles the URL. The API is not served until Prompt 4.

Reports are mode-0600 tarballs and refuse to overwrite any existing destination. Both forms strip
app names by default; `--keep-app-names` permits them. MACs, Wi-Fi network names, Bluetooth device
names and home paths are stripped. Ownership metadata is cleared. Existing alert titles may
retain their hostname. Archives include structured diagnostics and bounded operations history
when a database is selected, never a raw database or journal. Fixture-mode reports also include
allowlisted `sys/`, `proc/`, `dbus/` inputs with safe relative links, readable by `doctor` after
extraction. Missing inputs stay unavailable. Collection is bounded to 64 KiB per file, 4096 members
and 16 MiB of content; omissions are listed in the manifest and budget failures exit 3.

The hidden `--root` can collect either report form without host fallback. `enable`, `disable`,
`open` and `log --daemon` reject it before accessing the host. Fixture mute controls require an
explicit `--db`; reports include only an explicitly selected database when `--root` is used.

## Demos

```sh
sbox demo idle
sbox demo heatwave
sbox demo memory-crunch --no-notify
sbox demo suspend-resume --json
```

A canonical machine fixture ships inside the Python package, so demos work after wheel
installation outside the checkout. They use the real collector, engine, store and notifiers,
with scripted readings and shipped default rules, independent of personal overrides.

| Scenario | Behavior |
|---|---|
| `idle` | Nothing fires. |
| `heatwave` | CPU 40 → 95 → 84 °C; CPU hot fires at about 65 s and resolves at about 113 s. |
| `memory-crunch` | Memory stall, pressure, OOM and low-memory alerts, followed by recovery. |
| `suspend-resume` | Sleep at 55–75 s with CPU hot open; no no-data alert or duplicate notification. |

Popups are enabled by default; `--no-notify` disables them. Without a session bus, the scenario
continues and explains that notifications were not sent. Demo history is always in memory;
`--db` is rejected. No hardware files are changed.

## Doctor and apps

Doctor reports unavailable and junk sensors as well as working ones, including ENODATA, bad
values and dropped junk limits. Stable IDs use `<chip>/<bus id>/<channel>`, never `hwmonN`.
Its **DEFAULT RULES** column deliberately describes shipped coverage; `rules` instead uses the
effective defaults plus user overrides. Doctor JSON includes the same diagnostic information
as text, with structured sensor and source fields.

Memory used means **MemTotal − MemAvailable**, never “free”. Pressure is Linux PSI, not usage
percentage. `apps` reads the current user's cgroup v2 `app.slice`, only above 80% memory used or
when pressure is high/rising. Healthy memory prints current PSI and says apps were not sampled.
Missing cgroup support is reported with a reason.

All command output, including JSON, errors and live diagnostics, passes through shared
redaction. MAC addresses, MAC-derived interface names, Wi-Fi network names and Bluetooth device
names are omitted or masked. Bluetooth devices are described by class and battery level.

## Shell completion

For bash:

```sh
source <(sbox completion bash)
```

For zsh, after completion initialization:

```sh
autoload -Uz compinit
compinit
source <(sbox completion zsh)
```

The script supports both executable names, public commands, scenarios and options. It includes
configured rule-ID candidates for rule inspection, muting and log filters. Regenerate/re-source it after changing your rules. Hidden `run` and
test flags are excluded. No `eval` is needed.

## Development and fixture roots

```sh
uv sync
scripts/check.sh
uv run sbox demo heatwave --no-notify
uv run sbox doctor --root tests/fixtures/proart-p16
```

The hidden test-only `--root` works before or after a command. Each machine/case contains
`sys/`, `proc/` and `dbus/`; empty components represent unavailable sources. Readers never fall
back to host hardware or buses when a root is selected, and fixture mode never connects real
notification buses. D-Bus replies are JSON snapshots adapted to the normal bus protocol;
missing replies mean unavailable, malformed JSON is an operational error. There is no fixture
inheritance. Counter-reset frames remain grouped within their case.

Install bash and zsh to run shell completion tests locally. CI installs zsh for the same tests.
The full check runs locked dependency sync, lint, formatting, strict types and pytest, including
installed-wheel checks. Tests use fixtures, fake buses and injected clocks, never real hardware.

## License

[MIT](LICENSE)
