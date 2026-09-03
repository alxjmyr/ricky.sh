"""ricky — a personal agentic assistant and agent harness."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("ricky")
except PackageNotFoundError:  # pragma: no cover - only during uninstalled use
    __version__ = "0.0.0"

__all__ = ["__version__"]
