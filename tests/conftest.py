"""pytest 공용 픽스처."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src import db as db_module  # noqa: E402


@pytest.fixture
def conn() -> sqlite3.Connection:
    """스키마가 적용된 인메모리 DB."""
    c = db_module.connect(":memory:")
    db_module.init_schema(c)
    yield c
    c.close()
