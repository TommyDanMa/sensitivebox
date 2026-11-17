"""Default-browser launch with Chromium app mode and no API availability probe."""

import os
import shlex
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from sensitivebox.process import run as run_process
from sensitivebox.redact import redact

CHROMIUM = {
    "chromium",
    "chromium-browser",
    "google-chrome",
    "google-chrome-stable",
    "brave",
    "brave-browser",
    "microsoft-edge",
    "microsoft-edge-stable",
    "vivaldi",
    "vivaldi-stable",
}


@dataclass(frozen=True, slots=True)
class BrowserView:
    url: str
    message: str


def launch(args: Sequence[str]) -> None:
    # Browsers can stay open indefinitely. Capture their output in an unnamed file and inspect
    # immediate failures; a healthy long-running browser is left running after a short deadline.
    with tempfile.TemporaryFile(mode="w+") as errors:
        process = subprocess.Popen(  # noqa: S603 - argv only; no shell and no inherited output
            list(args),
            stdin=subprocess.DEVNULL,
            stdout=errors,
            stderr=errors,
            text=True,
            start_new_session=True,
        )
        try:
            code = process.wait(timeout=0.25)
        except subprocess.TimeoutExpired:
            return
        if code:
            errors.seek(0)
            raise OSError(redact(errors.read(4096) or f"browser launch failed ({code})"))


def _data_dirs() -> list[Path]:
    return [
        Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share"),
        *(
            Path(p)
            for p in os.environ.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share").split(":")
            if p
        ),
    ]


def open_page(
    page: str = "",
    rule: str = "",
    *,
    run: Callable[[Sequence[str]], str] = run_process,
    launch: Callable[[Sequence[str]], None] = launch,
    data_dirs: Sequence[Path] | None = None,
) -> BrowserView:
    if page not in ("", "dashboard", "log", "rule", "settings") or (page == "rule" and not rule):
        raise ValueError("open accepts log, rule <id> or settings")
    if page != "rule" and rule:
        raise ValueError("only open rule accepts a rule ID")
    suffix = (
        ("/rule/" + quote(rule, safe=""))
        if page == "rule"
        else ("/" + page if page not in ("", "dashboard") else "/")
    )
    url = "http://127.0.0.1:7878" + suffix
    try:
        desktop = run(["xdg-settings", "get", "default-web-browser"]).strip()
    except OSError:
        desktop = ""  # xdg-open still knows the user's platform default
    args = ["xdg-open", url]
    if desktop and Path(desktop).name == desktop and desktop.endswith(".desktop"):
        for directory in data_dirs if data_dirs is not None else _data_dirs():
            path = directory / "applications" / desktop
            if not path.is_file():
                continue
            try:
                lines = path.read_text().splitlines()
                entry = next(line[5:] for line in lines if line.startswith("Exec="))
                command = shlex.split(entry)
            except (OSError, ValueError, StopIteration):
                break
            if command and Path(command[0]).name in CHROMIUM:
                args = [part for part in command if not part.startswith("%")]
                args.append("--app=" + url)
            break
    launch(args)
    return BrowserView(url, f"Opened {url}. The page needs the Sensitive Box service.")
