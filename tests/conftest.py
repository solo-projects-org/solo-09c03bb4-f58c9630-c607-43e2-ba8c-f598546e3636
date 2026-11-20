"""pytest 公共夹具：每个测试使用独立的临时 SQLite 库。"""

import os
import tempfile

_tmp = tempfile.NamedTemporaryFile(prefix="scenic-test-", suffix=".db", delete=False)
_tmp.close()
os.environ["SCENIC_DB_PATH"] = _tmp.name

import pytest  # noqa: E402

from app import database  # noqa: E402
from app.main import app, service  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_db():
    database.Base.metadata.drop_all(database.engine)
    database.Base.metadata.create_all(database.engine)
    service._invalidate()
    yield
    database.Base.metadata.drop_all(database.engine)
    service._invalidate()


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        yield c
