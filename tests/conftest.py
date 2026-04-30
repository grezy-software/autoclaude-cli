"""Top-level pytest config: keep autoclaude's logger out of the operator's real log.

``autoclaude.logger._init_logger`` attaches a ``RotatingFileHandler`` pointing
at ``$XDG_CONFIG_HOME/autoclaude/logs/autoclaude.log`` (default
``~/.config/autoclaude/logs/autoclaude.log``) the first time any module calls
``get_logger`` -- which happens **at import time** when test files do
``from autoclaude.<x> import ...``. Without this conftest, every ``pytest`` run
on a developer machine pollutes the operator's production log with
test-fixture noise (rc=1 from fake claude binaries, exec-failure traces from
unit tests of the failure path, ...), making real tick-debugging much harder.

The fix is to redirect ``XDG_CONFIG_HOME`` to a throwaway session-scoped tmp
directory **before any autoclaude module is imported**. Top-level statements
in ``conftest.py`` run before pytest collects test files, so this assignment
wins the race against the logger's lazy init.

The tmp dir is left on disk for the OS to garbage-collect; it is throwaway and
small (just an empty ``logs/autoclaude.log``).
"""

from __future__ import annotations

import os
import tempfile

# Must come before any ``from autoclaude...`` import in this file or in the
# test files pytest is about to collect. Top-level conftest assignments run
# during pytest startup, before test-module collection -- which is when
# autoclaude's logger first fires.
_AUTOCLAUDE_TEST_CONFIG_HOME = tempfile.mkdtemp(prefix="autoclaude-pytest-config-")
os.environ["XDG_CONFIG_HOME"] = _AUTOCLAUDE_TEST_CONFIG_HOME
