"""Tests for the bounded self-heal package."""

from pathlib import Path

# ``tests/`` is not itself a package in this repository. Pytest's default
# import mode therefore imports this directory as top-level ``selfheal`` during
# collection, which would hide the production package bearing the same name.
# Extending this test package's search path keeps the required package marker
# while allowing ``selfheal.config`` and later Rock modules to resolve from the
# repository's real package directory.
__path__.append(str(Path(__file__).resolve().parents[2] / "selfheal"))

__all__ = ["config", "notion_issue_loop", "reasons"]
