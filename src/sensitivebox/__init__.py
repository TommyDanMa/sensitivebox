"""Sensitive Box: a read-only Linux hwmon sensor daemon with alerting and a local web UI."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("sensitivebox")
except PackageNotFoundError:  # running from a source tree that was never installed
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
