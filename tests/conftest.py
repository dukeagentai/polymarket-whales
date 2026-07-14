import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import db


@pytest.fixture
def session_factory(tmp_path):
    """A fresh file-based SQLite DB per test (not :memory: — the pool hands
    out a new connection per `with Session()`, which would each see an
    empty in-memory DB)."""
    db_path = tmp_path / "test.db"
    Session = db.init_db(f"sqlite:///{db_path}")
    yield Session
