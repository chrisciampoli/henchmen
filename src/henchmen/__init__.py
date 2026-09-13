"""Henchmen - Agent factory development system on GCP."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _version

try:
    # Single source of truth: the `version` field in pyproject.toml, read back
    # from the installed distribution metadata. Hard-coding it here is what let
    # pyproject (0.2.1), __version__ (0.1.0) and the CHANGELOG drift apart.
    __version__ = _version("henchmen")
except PackageNotFoundError:  # pragma: no cover - running from a source tree
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
