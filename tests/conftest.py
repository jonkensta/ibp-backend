"""Shared pytest fixtures."""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

TOPLEVEL = Path(__file__).resolve().parent.parent
MIGRATE_SCRIPT = TOPLEVEL / "scripts" / "migrate.py"

if str(TOPLEVEL) not in sys.path:
    sys.path.insert(0, str(TOPLEVEL))


@pytest.fixture(scope="session")
def migrate() -> ModuleType:
    """Import scripts/migrate.py (not a package) as a module."""
    spec = importlib.util.spec_from_file_location("ibp_migrate", MIGRATE_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
