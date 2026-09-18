"""SQLite 持久化层。

保存每次模拟（成功或失败）的完整结果。数据库路径由环境变量
``POOLING_DB_PATH`` 指定，默认使用工作目录下的 ``data/pooling.db``。
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

SCHEMA = """
CREATE TABLE IF NOT EXISTS simulations (
    id          TEXT PRIMARY KEY,
    name        TEXT,
    status      TEXT NOT NULL CHECK (status IN ('success', 'failed')),
    error_code  TEXT,
    step_count  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    result      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_simulations_created_at
    ON simulations (created_at DESC);
"""


def default_db_path() -> str:
    return os.environ.get(
        "POOLING_DB_PATH", os.path.join("data", "pooling.db")
    )


def connect(db_path: Optional[str] = None) -> sqlite3.Connection:
    """打开连接并确保目录与表结构存在。"""
    path = db_path or default_db_path()
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    # 多 worker / 容器重启场景下更稳妥的写入配置
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.executescript(SCHEMA)
    return conn


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def insert_simulation(conn: sqlite3.Connection, result: dict) -> dict:
    """落库一次模拟结果，补全 id / 时间戳后返回完整记录。"""
    record = dict(result)
    record["id"] = uuid4().hex
    record["created_at"] = utc_now_iso()

    conn.execute(
        """
        INSERT INTO simulations
            (id, name, status, error_code, step_count, created_at, result)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            record["id"],
            record.get("name"),
            record["status"],
            (record.get("error") or {}).get("code"),
            len(record.get("steps", [])),
            record["created_at"],
            json.dumps(record, ensure_ascii=False),
        ),
    )
    conn.commit()
    return record


def get_simulation(conn: sqlite3.Connection, sim_id: str) -> Optional[dict]:
    row = conn.execute(
        "SELECT result FROM simulations WHERE id = ?", (sim_id,)
    ).fetchone()
    if row is None:
        return None
    return json.loads(row["result"])


def list_simulations(
    conn: sqlite3.Connection, limit: int = 50, offset: int = 0
) -> list[dict]:
    rows = conn.execute(
        """
        SELECT id, name, status, error_code, step_count, created_at
        FROM simulations
        ORDER BY created_at DESC, id
        LIMIT ? OFFSET ?
        """,
        (limit, offset),
    ).fetchall()
    return [dict(row) for row in rows]
