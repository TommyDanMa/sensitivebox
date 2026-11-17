"""Install and manage the user service, preserving all differing user-owned files."""

import os
import shutil
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

from sensitivebox.process import run as run_process
from sensitivebox.redact import redact


@dataclass(frozen=True, slots=True)
class ServicePaths:
    config: Path
    data: Path
    executable: Path

    @property
    def unit(self) -> Path:
        return self.config / "systemd/user/sensitivebox.service"

    @property
    def launcher(self) -> Path:
        return self.data / "applications/sensitivebox.desktop"


def default_paths() -> ServicePaths:
    executable = shutil.which("sbox") or str(Path(sys.executable).with_name("sbox"))
    return ServicePaths(
        Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"),
        Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share"),
        Path(executable).absolute(),
    )


def quoted(executable: Path, *, desktop: bool = False) -> str:
    value = str(executable)
    if any(c in value for c in "\n\r\0") or not executable.is_absolute():
        raise OSError("service executable needs an absolute path without control characters")
    if desktop:
        value = value.replace("\\", "\\\\\\\\").replace('"', '\\\\"')
        value = value.replace("`", "\\\\`").replace("$", "\\\\$")
    else:
        value = value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "$$")
    return '"' + value.replace("%", "%%") + '"'


ICON_SIZES = (128, 256, 512)


def _matches(path: Path, content: str | bytes) -> bool:
    if not path.is_file():
        return False
    if isinstance(content, bytes):
        return path.read_bytes() == content
    return path.read_text() == content


def _create(path: Path, content: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = content.encode() if isinstance(content, str) else content
    with path.open("xb") as stream:
        stream.write(data)


def manage(
    enabled: bool,
    *,
    paths: ServicePaths | None = None,
    run: Callable[[Sequence[str]], str] = run_process,
) -> str:
    paths = paths or default_paths()
    resource = files("sensitivebox").joinpath("data")
    expected: tuple[tuple[Path, str | bytes], ...] = (
        (
            paths.unit,
            resource.joinpath("sensitivebox.service.in")
            .read_text()
            .replace("@EXECUTABLE@", quoted(paths.executable)),
        ),
        (
            paths.launcher,
            resource.joinpath("sensitivebox.desktop.in")
            .read_text()
            .replace("@EXECUTABLE@", quoted(paths.executable, desktop=True)),
        ),
        *(
            (
                paths.data / f"icons/hicolor/{size}x{size}/apps/sensitivebox.png",
                resource.joinpath(f"icons/{size}.png").read_bytes(),
            )
            for size in ICON_SIZES
        ),
    )
    # Check every destination before creating any. Symlinks are never installed over.
    for path, content in expected:
        if path.is_symlink() or (path.exists() and not _matches(path, content)):
            raise OSError(redact(f"{path} differs from generated file; refusing to overwrite"))
    if enabled:
        for path, content in expected:
            if not path.exists():
                _create(path, content)
        run(["systemctl", "--user", "daemon-reload"])
    run(
        ["systemctl", "--user", "enable" if enabled else "disable", "--now", "sensitivebox.service"]
    )
    return (
        "Sensitive Box enabled and started." if enabled else "Sensitive Box stopped and disabled."
    )
