"""Load rules from TOML: the user's ``rules.toml`` and the shipped ``defaults.toml``."""

import tomllib
from collections.abc import Sequence
from importlib import resources
from pathlib import Path
from typing import Any, cast

import msgspec

from sensitivebox.alerts.model import Rule


class RuleError(ValueError):
    """A rules file that can't be used, with the rule it came from in the message."""


def parse_rules(data: Any) -> list[Rule]:
    """Validate an already-parsed ``{"rule": [...]}`` mapping."""
    if not isinstance(data, dict):
        raise RuleError("expected a table with [[rule]] entries")
    table = cast(dict[str, Any], data)
    for key in table:
        if key != "rule":
            hint = " (did you mean [[rule]]?)" if key.rstrip("s") == "rule" else ""
            raise RuleError(f"unknown top-level key {key!r}{hint}: a rules file has only [[rule]]")
    raw: Any = table.get("rule", [])
    if not isinstance(raw, list):
        raise RuleError("`rule` must be an array of tables: write [[rule]]")
    rules: list[Rule] = []
    for n, entry in enumerate(raw, start=1):  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
        label = f"rule {n}"
        if isinstance(entry, dict) and isinstance(entry.get("id"), str):  # pyright: ignore[reportUnknownMemberType]
            label += f" ({entry['id']})"
        try:
            rules.append(msgspec.convert(entry, Rule))
        except msgspec.ValidationError as exc:
            raise RuleError(f"{label}: {exc}") from None
    _check_unique(rules)
    return rules


def _check_unique(rules: Sequence[Rule]) -> None:
    seen: set[str] = set()
    for rule in rules:
        if rule.id in seen:
            raise RuleError(f"duplicate rule id {rule.id!r}")
        seen.add(rule.id)


def load_rules(text: str) -> list[Rule]:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise RuleError(f"not valid TOML: {exc}") from None
    return parse_rules(data)


def load_rules_file(path: Path) -> list[Rule]:
    try:
        return load_rules(path.read_text(encoding="utf-8"))
    except RuleError as exc:
        raise RuleError(f"{path}: {exc}") from None


def load_defaults() -> list[Rule]:
    """The default rules shipped inside the package."""
    text = resources.files("sensitivebox.alerts").joinpath("defaults.toml").read_text("utf-8")
    return load_rules(text)


def load_rule_set(user_file: Path | None = None) -> list[Rule]:
    """The default rules, overridden by id from the user's ``rules.toml`` when it exists.

    A rule in the user's file with the same ``id`` replaces the default; a new ``id`` is added.
    A missing file is fine; one that can't be read or parsed raises :class:`RuleError`.
    """
    rules = {rule.id: rule for rule in load_defaults()}
    if user_file is not None and user_file.is_file():
        try:
            mine = load_rules_file(user_file)
        except OSError as exc:
            raise RuleError(f"{user_file}: {exc.strerror or exc}") from None
        rules.update({rule.id: rule for rule in mine})
    return list(rules.values())
