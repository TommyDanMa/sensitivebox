"""Stable sensor IDs: ``<chip name>/<bus id>/<channel>``, never ``hwmonN``.

``hwmonN`` numbers are assigned in probe order and change between boots. The device a chip hangs
off (a PCI address, a platform device name, a power supply) does not.

Some drivers hang the chip off a *class* device numbered in probe order, under its physical
device: an NVMe drive's ``…/0000:61:00.0/nvme/nvme0``. ``nvme0`` can become ``nvme1`` when the
controllers probe in another order, so the ID uses the physical device instead (review ADV-008).
"""

import re
from pathlib import Path

NO_DEVICE = "virtual"


def _is_numbered_class_device(device: Path) -> bool:
    """``<class>/<class><N>``, e.g. ``nvme/nvme0``: an instance number, not an identity."""
    return re.fullmatch(re.escape(device.parent.name) + r"\d+", device.name) is not None


def bus_id(hwmon_dir: Path) -> str:
    """Where the chip's ``device`` symlink resolves, e.g. ``0000:00:18.3``; for a numbered class
    device, the physical device above it."""
    device = hwmon_dir / "device"
    if not device.is_symlink():
        return NO_DEVICE
    resolved = device.resolve()
    if _is_numbered_class_device(resolved) and resolved.parent.parent.name:
        return resolved.parent.parent.name
    return resolved.name


def sensor_id(chip: str, bus: str, channel: str) -> str:
    return f"{chip}/{bus}/{channel}"
