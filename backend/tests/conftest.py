import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# 保证从任意工作目录都能 import app 包。
BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """每个用例独立临时库：补丁 app.db.DB_PATH 后重新初始化种子。"""
    db_file = tmp_path / "case.db"
    monkeypatch.setenv("SHIPGATE_DB", str(db_file))

    import app.db as db_mod
    import app.config as config_mod
    monkeypatch.setattr(config_mod, "DB_PATH", db_file)
    monkeypatch.setattr(db_mod, "DB_PATH", db_file)

    from app.seed import seed_if_empty
    seed_if_empty()

    from app.main import app
    with TestClient(app) as c:
        yield c
