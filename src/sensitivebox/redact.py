"""Redaction for everything Sensitive Box prints (docs/BIBLE.md, "Command line": private by
default). Doctor output goes into public bug reports, so it must never carry a hardware address.

``redact`` is applied as the last step of rendering, after all the text is put together. It
removes:

* MAC addresses written with ``:`` or ``-`` separators, wherever they appear (free text included,
  such as an ``unavailable (not a number: '…')`` reason);
* systemd's MAC-based interface names, ``<prefix>x<12 hex digits>`` such as ``wlx001122334455``
  (see systemd.net-naming-scheme(7)).

Bluetooth device names can't be recognised in free text, so they are never put into output in the
first place: ``device_label`` describes a device by its class icon and battery level only. Wi-Fi
network names are never read.
"""

import re
from dataclasses import dataclass
from typing import Any, cast

import msgspec

from sensitivebox.sources.bluez import BtDevice

MAC_MASK = "XX:XX:XX:XX:XX:XX"

_MAC_RE = re.compile(
    r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{2}([:-])(?:[0-9A-Fa-f]{2}\1){4}[0-9A-Fa-f]{2}(?![0-9A-Fa-f])"
)
_MAC_IFACE_RE = re.compile(r"\b(en|wl|ww|sl|ib)x[0-9A-Fa-f]{12}\b")


# A space can belong to a path. Unquoted free text is ambiguous, so redact conservatively
# through the rest of the field/line, stopping only at a structural delimiter.
_HOME_RE = re.compile(r"(?:/home/|/root/|(?<!\w)~/)[^\r\n\t\"'<>]+")


def redact(text: str) -> str:
    text = _HOME_RE.sub("<home>", text)
    text = _MAC_RE.sub(MAC_MASK, text)
    return _MAC_IFACE_RE.sub(lambda m: f"{m[1]}x<MAC>", text)


def device_label(device: BtDevice) -> str:
    """A Bluetooth device as its class and battery, never its name or address."""
    battery = f" (battery {device.battery} %)" if device.battery is not None else ""
    return (device.icon or "device") + battery


@dataclass(frozen=True, slots=True)
class Redactor:
    """Known private labels supplement pattern redaction for exports."""

    private: tuple[str, ...] = ()

    def __call__(self, text: str) -> str:
        for name in self.private:
            if name:
                text = text.replace(name, "<redacted>")
        return redact(text)


def redacted_json(
    value: object, *, private: tuple[str, ...] = (), stable_ids: frozenset[str] = frozenset()
) -> str:
    """API/SSE boundary: redact text fields while retaining explicitly known signal IDs.

    A hardware ID can itself contain a MAC-based interface name. Only typed ID fields whose
    value is in the collector's registry are exempt; labels, reasons and errors never are.
    """
    redactor = Redactor(private)

    def clean(item: Any, key: str = "") -> Any:
        if isinstance(item, str):
            if key in {"id", "signal", "sensor_id"} and item in stable_ids:
                return item
            return redactor(item)
        if isinstance(item, dict):
            # API mapping keys are fixed schema/group names, never private labels.
            return {k: clean(v, k) for k, v in cast(dict[str, Any], item).items()}
        if isinstance(item, (list, tuple)):
            return [clean(v) for v in cast(list[Any] | tuple[Any, ...], item)]
        return item

    return msgspec.json.encode(clean(msgspec.to_builtins(value))).decode()
