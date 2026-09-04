from __future__ import annotations

import pytest

from backend.app import database


def test_normalize_douyin_flags():
    assert database.normalize_douyin_auto_publish(None) is False
    assert database.normalize_douyin_auto_publish(True) is True
    assert database.normalize_douyin_auto_publish("false") is False
    assert database.resolve_douyin_generate_meta(False, douyin_auto_publish=True) is True
    assert database.resolve_douyin_generate_meta(False, douyin_auto_publish=False) is False
    with pytest.raises(ValueError):
        database.normalize_douyin_auto_publish("maybe")


def test_create_task_stores_douyin_flags(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "test.sqlite")
    database.init_db()
    task_id = database.create_task(
        "https://www.youtube.com/watch?v=douyintest01",
        douyin_auto_publish=True,
        douyin_generate_meta=False,
    )
    task = database.get_task(task_id)
    assert task["douyin_auto_publish"] is True
    assert task["douyin_generate_meta"] is True  # forced when auto publish on
