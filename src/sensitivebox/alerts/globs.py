"""Signal globs: ``fnmatch`` syntax plus named wildcards.

``*``, ``?`` and ``[...]`` mean what they mean to :func:`fnmatch.fnmatchcase` (``*`` may cross a
``/``). A named wildcard such as ``{battery}`` matches exactly one path segment and *binds* it, so a
rule's guard can name the same device as its sensor (docs/BIBLE.md, "Alerting"; review ADV-002)::

    sensor  = "power_supply/{battery}/capacity"
    only_if = { signal = "power_supply/{battery}/status", equals = "Discharging" }

The translation to a regular expression is written out here rather than taken from
``fnmatch.translate``, whose output format changes between Python versions.
"""

import re
from functools import cache

_NAME_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_GLOB_META_RE = re.compile(r"([*?\[])")


def names(glob: str) -> frozenset[str]:
    """The named wildcards in ``glob``."""
    return frozenset(_NAME_RE.findall(glob))


def _bracket(glob: str, start: int) -> tuple[str, int] | None:
    """The regex for a ``[...]`` set starting at ``start``, and the index after it."""
    end = start + 1
    if end < len(glob) and glob[end] == "!":
        end += 1
    if end < len(glob) and glob[end] == "]":
        end += 1
    end = glob.find("]", end)
    if end < 0:
        return None
    body = glob[start + 1 : end].replace("\\", "\\\\")
    if body.startswith("!"):
        body = "^" + body[1:]
    elif body.startswith(("^", "[")):
        body = "\\" + body
    return f"[{body}]", end + 1


@cache
def compile_glob(glob: str) -> re.Pattern[str]:
    parts: list[str] = []
    seen: set[str] = set()
    i = 0
    while i < len(glob):
        char = glob[i]
        if char == "{" and (m := _NAME_RE.match(glob, i)):
            name = m[1]
            parts.append(f"(?P={name})" if name in seen else f"(?P<{name}>[^/]+)")
            seen.add(name)
            i = m.end()
        elif char == "*":
            parts.append(".*")
            i += 1
        elif char == "?":
            parts.append(".")
            i += 1
        elif char == "[" and (bracket := _bracket(glob, i)):
            parts.append(bracket[0])
            i = bracket[1]
        else:
            parts.append(re.escape(char))
            i += 1
    return re.compile("(?s:" + "".join(parts) + r")\Z")


def match(glob: str, signal: str) -> dict[str, str] | None:
    """The bindings of ``glob``'s named wildcards if ``signal`` matches it, else ``None``."""
    m = compile_glob(glob).match(signal)
    return None if m is None else m.groupdict()


def fill(glob: str, bindings: dict[str, str]) -> str:
    """``glob`` with its named wildcards replaced by their bound (literal) segments."""

    def literal(m: re.Match[str]) -> str:
        value = bindings.get(m[1])
        return m[0] if value is None else _GLOB_META_RE.sub(r"[\1]", value)

    return _NAME_RE.sub(literal, glob)
