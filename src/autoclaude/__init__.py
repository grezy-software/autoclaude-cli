"""AutoClaude local runner."""

from importlib.metadata import PackageNotFoundError, version

try:
    # semantic-release bumps pyproject.toml; read installed metadata so we
    # don't have to hand-sync a string in this file on every release.
    __version__ = version("autoclaude-cli")
except PackageNotFoundError:
    __version__ = "0.0.0+dev"
