"""Question-shaped commands; both installed executable names share this entry point."""

import argparse
import asyncio
import math
import os
import re
import signal
import sqlite3
import sys
import time
from collections.abc import Sequence
from dataclasses import replace
from difflib import get_close_matches
from pathlib import Path
from typing import Any, NoReturn

from dbus_fast.aio import MessageBus

from sensitivebox import __version__, alerts
from sensitivebox.alerts.mute import boot_id
from sensitivebox.browser import open_page
from sensitivebox.completion import PUBLIC_COMMANDS, completion
from sensitivebox.fixtures import FixtureError, MachineRoot, demo_root
from sensitivebox.history import (
    follow,
    journal,
    log_view,
    parse_since,
    render_log,
    render_weekly,
    rule_replay,
)
from sensitivebox.inspect import AppsView, doctor_view, public_sources, render_apps
from sensitivebox.operations import control
from sensitivebox.output import Output, logging_output
from sensitivebox.redact import redact
from sensitivebox.report import export
from sensitivebox.runtime import Buses, RunOptions, run_daemon
from sensitivebox.sample import sample
from sensitivebox.service import manage
from sensitivebox.simulate import SCENARIOS, SimulationError
from sensitivebox.sources.dbus import connect_session_bus, connect_system_bus
from sensitivebox.status import RuleView, render_rules, render_status, rule_rows, status
from sensitivebox.store.status import read_history, read_mutes

EXIT_ERROR = 3
DEFAULT_SYSFS_ROOT = Path("/sys")
DEFAULT_PROC_ROOT = Path("/proc")


class UsageError(ValueError):
    pass


class Arguments(argparse.Namespace):
    command: str | None = None
    root: Path | None = None
    json: bool = False
    interval: float = 1.0
    db: Path | None = None
    verbose: bool = False
    scenario: str = "idle"
    no_notify: bool = False
    shell: str = "bash"
    rule_id: str = ""
    duration: str = ""
    no_renotify: bool = False
    since: str | None = None
    limit: int = 100
    follow: bool = False
    daemon: bool = False
    weekly: bool = False
    page: str = ""
    fixture: bool = False
    keep_app_names: bool = False
    output_path: Path | None = None


class Parser(argparse.ArgumentParser):
    def __init__(self, *args: Any, output: Output | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.output = output or Output()

    def error(self, message: str) -> NoReturn:
        match = re.search(r"invalid choice: (.+?) \(choose from", message)
        if match:
            word = match[1].strip("'\"")
            suggestion = get_close_matches(word, (*PUBLIC_COMMANDS, *SCENARIOS), n=1)
            if suggestion:
                message = f"{message}. Did you mean {suggestion[0]}?"
        raise UsageError(f"{message}; use sbox --help")

    def _print_message(self, message: str, file: Any = None) -> None:
        if message:
            if self.output.json:
                kind = "help" if message.startswith("usage:") else "version"
                self.output.emit(kind, message.strip(), message.strip(), error=file is sys.stderr)
            else:
                (file or sys.stdout).write(redact(message))


def _shared(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="print JSON (NDJSON for live commands)",
    )
    parser.add_argument("--root", type=Path, default=argparse.SUPPRESS, help=argparse.SUPPRESS)


def build_parser(output: Output | None = None) -> Parser:
    parser = Parser(
        output=output, prog="sbox", description="Read-only Linux sensor monitoring and alerting."
    )
    _shared(parser)
    parser.add_argument(
        "--db", type=Path, default=argparse.SUPPRESS, metavar="PATH", help="history database"
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND", parser_class=Parser)
    watch = sub.add_parser("watch", output=output, help="start the daemon in the foreground")
    run = sub.add_parser(
        "run", output=output
    )  # No help argument: omit this alias from the command list.
    for command in (watch, run):
        _shared(command)
        command.add_argument(
            "--interval",
            type=float,
            default=1.0,
            metavar="SECONDS",
            help="poll interval (default: 1 second)",
        )
        command.add_argument(
            "--db", type=Path, default=argparse.SUPPRESS, metavar="PATH", help="history database"
        )
    diagnostic = sub.add_parser(
        "doctor", output=output, help="show sensors, default-rule coverage and sources"
    )
    _shared(diagnostic)
    diagnostic.add_argument("-v", "--verbose", action="store_true", help="show empty chip details")
    demo = sub.add_parser(
        "demo", output=output, help="play a packaged scenario with real desktop popups"
    )
    _shared(demo)
    demo.add_argument("scenario", choices=tuple(SCENARIOS))
    demo.add_argument("--no-notify", action="store_true", help="disable desktop popups")
    demo.add_argument("--interval", type=float, default=1.0, metavar="SECONDS")
    demo.add_argument("--db", type=Path, default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    rules = sub.add_parser(
        "rules", output=output, help="list every rule, or replay recorded inputs for one rule"
    )
    _shared(rules)
    rules.add_argument("rule_id", nargs="?", default="", metavar="RULE")
    rules.add_argument("--db", type=Path, default=argparse.SUPPRESS, metavar="PATH")
    apps = sub.add_parser(
        "apps", output=output, help="show top apps while memory is tight, with PSI"
    )
    _shared(apps)
    report = sub.add_parser("report", output=output, help="write a private diagnostic archive")
    _shared(report)
    report.add_argument("--fixture", action="store_true")
    report.add_argument("--keep-app-names", action="store_true")
    report.add_argument("--output", type=Path, dest="output_path")
    browser = sub.add_parser("open", output=output, help="open dashboard, log, rule or settings")
    _shared(browser)
    browser.add_argument("page", nargs="?", default="")
    browser.add_argument("rule_id", nargs="?", default="")
    for name in ("enable", "disable"):
        command = sub.add_parser(name, output=output, help=f"{name} the systemd user service")
        _shared(command)
    rule = sub.add_parser("rule", output=output, help="replay recorded inputs for a rule")
    _shared(rule)
    rule.add_argument("rule_id", nargs="?", default="", metavar="RULE")
    rule.add_argument("--db", type=Path, default=argparse.SUPPRESS)
    history = sub.add_parser("log", output=output, help="inspect incidents or weekly quality")
    _shared(history)
    history.add_argument("rule_id", nargs="?", default="")
    history.add_argument("--db", type=Path, default=argparse.SUPPRESS)
    history.add_argument("-f", "--follow", action="store_true")
    history.add_argument("--since")
    history.add_argument("--limit", type=int, default=100)
    history.add_argument("--daemon", action="store_true")
    history.add_argument("--weekly", action="store_true")
    for name, description in (
        ("mute", "hush notifications for one rule"),
        ("unmute", "release notifications for one rule"),
        ("quiet", "set quiet hours for every rule; critical still gets through"),
    ):
        command = sub.add_parser(name, output=output, help=description, description=description)
        _shared(command)
        command.add_argument("--db", type=Path, default=argparse.SUPPRESS)
        if name != "quiet":
            command.add_argument("rule_id", metavar="RULE")
        if name != "unmute":
            command.add_argument("duration", metavar="DURATION" if name == "mute" else "SCHEDULE")
            command.add_argument("--no-renotify", action="store_true")
    complete = sub.add_parser("completion", output=output, help="print bash or zsh completion")
    _shared(complete)
    complete.add_argument("shell", choices=("bash", "zsh"))
    return parser


def cmd_doctor(
    sysfs_root: Path,
    proc_root: Path,
    *,
    verbose: bool = False,
    root: MachineRoot | None = None,
    output: Output | None = None,
) -> int:
    current = asyncio.run(sample(sysfs_root, proc_root, root))
    view = doctor_view(current, sysfs_root, proc_root, verbose)
    (output or Output()).emit("doctor", view, view.text)
    return 0


def default_db_path() -> Path:
    state = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(state) / "sensitivebox" / "sensitivebox.db"


def default_rules_path() -> Path:
    config = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(config) / "sensitivebox" / "rules.toml"


def cmd_run(options: RunOptions, rules_file: Path | None = None) -> int:
    return asyncio.run(_serve(options, alerts.load_rule_set(rules_file)))


async def _serve(options: RunOptions, rules: list[alerts.Rule]) -> int:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    for sig in previous:
        loop.add_signal_handler(sig, stop.set)
    system: MessageBus | None = None
    session: MessageBus | None = None
    try:
        if options.root is None and options.simulate is None:
            system = await connect_system_bus()
        if options.desktop and (options.root is None or options.fixture_notifications):
            session = await connect_session_bus()
        buses = Buses(options.root.bus if options.root is not None else system, session)
        await run_daemon(options, rules, buses, stop, Output().line)
    finally:
        for bus in (system, session):
            if bus is not None:
                bus.disconnect()
        for sig, handler in previous.items():
            loop.remove_signal_handler(sig)
            signal.signal(sig, handler)
    return 0


def _dispatch(args: Arguments, output: Output) -> int:
    if args.command == "completion":
        view = completion(args.shell, alerts.load_rule_set(default_rules_path()))
        output.emit("completion", view, view.script)
        return 0
    if args.command == "open":
        if args.root is not None:
            raise UsageError("open rejects --root; it launches a host browser")
        try:
            view = open_page(args.page, args.rule_id)
        except ValueError as exc:
            raise UsageError(str(exc)) from exc
        output.emit("browser", view, view.message)
        return 0
    if args.command in ("enable", "disable"):
        if args.root is not None:
            raise UsageError(f"{args.command} rejects --root; it manages the host user service")
        message = manage(args.command == "enable")
        output.emit("service", {"enabled": args.command == "enable", "message": message}, message)
        return 0
    if args.root is not None and args.command == "log" and args.daemon:
        raise UsageError("log --daemon rejects --root; it reads the host journal")
    if args.command == "rule" or (args.command == "rules" and args.rule_id):
        if not args.rule_id:
            raise UsageError("rule needs a rule ID; use sbox rules to list every rule")
        view = rule_replay(args.db or default_db_path(), args.rule_id)
        if not view.evaluations:
            known = [r.id for r in alerts.load_rule_set(default_rules_path())]
            if args.rule_id not in known:
                nearest = get_close_matches(args.rule_id, known, n=1, cutoff=0)
                suggestion = f" Did you mean {nearest[0]}?" if nearest else ""
                raise UsageError(
                    f"unknown rule {args.rule_id}.{suggestion} Use sbox rules to list rules"
                )
        output.emit(
            "rule_replay",
            view,
            "\n".join(f"{e.at:g} {e.signal} {e.state.value}: {e.reason}" for e in view.evaluations)
            or view.reason
            or "No history",
        )
        return 0
    if args.command == "log":
        if args.limit < 1 or args.limit > 10000:
            raise UsageError("--limit must be between 1 and 10000")
        path = args.db or default_db_path()
        try:
            since = parse_since(args.since, time.time())
        except ValueError as exc:
            raise UsageError(str(exc)) from exc
        if args.daemon:
            if args.weekly:
                raise UsageError("--weekly cannot be combined with --daemon")
            return journal(args.since, args.limit, args.follow, output)
        elif args.weekly:
            if args.follow:
                raise UsageError("--weekly cannot be combined with --follow")
            from sensitivebox.store.events import Events
            from sensitivebox.store.read import reader

            with reader(path) as db:
                rows = Events(db, time.time, writer=False).weekly() if db is not None else []
                output.emit(
                    "weekly",
                    {"rows": rows[: args.limit], "truncated": len(rows) > args.limit},
                    render_weekly(rows[: args.limit], len(rows) > args.limit),
                )
        elif args.follow:
            follow(path, since, args.limit, args.rule_id or None, output)
        else:
            view = log_view(path, since, args.limit, args.rule_id or None)
            output.emit("log", view, render_log(view, time.time()))
        return 0
    root = MachineRoot.load(args.root) if args.root is not None else None
    sysfs, proc = (
        (root.sys, root.proc) if root is not None else (DEFAULT_SYSFS_ROOT, DEFAULT_PROC_ROOT)
    )
    if args.command == "report":
        destination = args.output_path or Path(
            "sensitivebox-" + time.strftime("%Y%m%d-%H%M%S") + ".tar.gz"
        )
        view = asyncio.run(
            export(
                destination,
                sysfs,
                proc,
                root=root,
                fixture=args.fixture,
                db=args.db or (default_db_path() if root is None else None),
                keep_app_names=args.keep_app_names,
            )
        )
        output.emit("report", view, f"Wrote {view.output} ({view.members} members)")
        return 0
    if args.command in ("mute", "unmute", "quiet"):
        if args.command != "quiet" and args.rule_id not in {
            r.id for r in alerts.load_rule_set(default_rules_path())
        }:
            raise UsageError(f"unknown rule {args.rule_id}; use sbox rules")
        if root is not None and args.db is None:
            raise UsageError("mute controls with --root require an explicit --db")
        try:
            view = control(
                args.db or default_db_path(),
                proc,
                args.command,
                args.rule_id,
                args.duration,
                not args.no_renotify,
            )
        except ValueError as exc:
            raise UsageError(str(exc)) from exc
        output.emit("mute", view, view.message)
        return 0
    if args.command == "demo":
        if args.db is not None:
            raise UsageError("demo uses in-memory history; remove --db")
        if not math.isfinite(args.interval) or args.interval <= 0:
            raise UsageError("--interval must be positive and finite; use --interval 1")
        machine = root if root is not None else demo_root()
        options = RunOptions(
            machine.sys,
            machine.proc,
            args.interval,
            None,
            simulate="proart-p16",
            scenario=args.scenario,
            desktop=root is None and not args.no_notify,
            root=machine,
            json=args.json,
            fixture_notifications=root is None,
        )
        try:
            return asyncio.run(_serve(options, alerts.load_defaults()))
        finally:
            if machine.temporary is not None:
                machine.temporary.cleanup()
    if args.command in ("watch", "run"):
        if not math.isfinite(args.interval) or args.interval <= 0:
            raise UsageError("--interval must be positive and finite; use e.g. --interval 1")
        options = RunOptions(
            sysfs,
            proc,
            args.interval,
            args.db if args.db is not None else (None if root else default_db_path()),
            desktop=root is None,
            root=root,
            json=args.json,
        )
        return cmd_run(options, default_rules_path())
    if args.command == "doctor":
        return cmd_doctor(sysfs, proc, verbose=args.verbose, root=root, output=output)
    if args.command == "apps":
        current = asyncio.run(sample(sysfs, proc, root))
        output.emit("apps", AppsView(public_sources(current.sources)), render_apps(current.sources))
        return 0
    if args.command in (None, "rules"):
        rules = alerts.load_rule_set(default_rules_path())
        current = asyncio.run(sample(sysfs, proc, root))
        history = read_history(args.db if args.db is not None else default_db_path())
        rows = rule_rows(rules, current, history, time.time())
        if args.command == "rules":
            output.emit("rules", RuleView(rows), render_rules(rows))
            return 0
        view = status(current, rows)
        try:
            boot = boot_id(proc)
        except OSError:
            pass
        else:
            view = replace(
                view, muted=read_mutes(args.db or default_db_path(), boot, time.monotonic())
            )
        output.emit("status", view, render_status(current, view))
        return view.health
    raise UsageError("unknown command; use sbox --help")


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    output = Output("--json" in arguments)
    try:
        if any(a.split("=")[0] in ("--sysfs-root", "--proc-root", "--no-dbus") for a in arguments):
            raise UsageError("old root flags were replaced; use --root with sys/, proc/, dbus/")
        if any(a.split("=")[0] in ("--simulate", "--scenario", "--notify") for a in arguments):
            raise UsageError("simulation flags were replaced; use sbox demo heatwave")
        args = build_parser(output).parse_args(arguments, namespace=Arguments())
        with logging_output(output):
            return _dispatch(args, output)
    except KeyboardInterrupt:
        return 0
    except (
        UsageError,
        FixtureError,
        SimulationError,
        alerts.RuleError,
        OSError,
        sqlite3.Error,
        ValueError,
    ) as exc:
        message = str(exc)
        if isinstance(exc, (OSError, sqlite3.Error)):
            message += "; check the selected paths and permissions, then run sbox doctor"
        output.error(message)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
