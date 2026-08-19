from __future__ import annotations

import pytest

from bot.db import Database


@pytest.fixture()
def db(tmp_path) -> Database:
    database = Database(tmp_path / "test.sqlite3")
    database.connect()
    yield database
    database.close()
