"""SQLite 持久层：保存模型定义、检验运行与反例轨迹。

只做持久化，不缓存检验结论（结论由当前模型即时计算）。
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from typing import Any

DB_PATH = os.environ.get("CW_DB_PATH", "/data/workbench.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS models (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    spec        TEXT NOT NULL,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id          TEXT PRIMARY KEY,
    model_id    TEXT NOT NULL REFERENCES models(id) ON DELETE CASCADE,
    bound       INTEGER NOT NULL,
    verdict     TEXT NOT NULL,
    result      TEXT NOT NULL,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_model ON runs(model_id, created_at DESC);
CREATE TABLE IF NOT EXISTS risk_runs (
    id          TEXT PRIMARY KEY,
    model_id    TEXT NOT NULL REFERENCES models(id) ON DELETE CASCADE,
    bound       INTEGER NOT NULL,
    top_n       INTEGER NOT NULL,
    verdict     TEXT NOT NULL,
    params      TEXT NOT NULL,
    result      TEXT NOT NULL,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_risk_runs_model ON risk_runs(model_id, created_at DESC);
CREATE TABLE IF NOT EXISTS conc_runs (
    id          TEXT PRIMARY KEY,
    model_id    TEXT NOT NULL REFERENCES models(id) ON DELETE CASCADE,
    bound       INTEGER NOT NULL,
    verdict     TEXT NOT NULL,
    result      TEXT NOT NULL,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conc_runs_model ON conc_runs(model_id, created_at DESC);
CREATE TABLE IF NOT EXISTS diff_runs (
    id              TEXT PRIMARY KEY,
    baseline_id     TEXT,
    candidate_id    TEXT,
    baseline_name   TEXT NOT NULL,
    candidate_name  TEXT NOT NULL,
    bound           INTEGER NOT NULL,
    verdict         TEXT NOT NULL,
    params          TEXT NOT NULL,
    baseline_spec   TEXT NOT NULL,
    candidate_spec  TEXT NOT NULL,
    result          TEXT NOT NULL,
    created_at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_diff_runs_created ON diff_runs(created_at DESC);
"""


def init_db(path: str | None = None) -> None:
    target = path or DB_PATH
    os.makedirs(os.path.dirname(os.path.abspath(target)), exist_ok=True)
    with connect(target) as conn:
        conn.executescript(_SCHEMA)


@contextmanager
def connect(path: str | None = None):
    conn = sqlite3.connect(path or DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _row_to_model(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "spec": json.loads(row["spec"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def create_model(name: str, spec: dict) -> dict:
    mid = uuid.uuid4().hex[:12]
    now = time.time()
    with connect() as conn:
        conn.execute(
            "INSERT INTO models (id, name, spec, created_at, updated_at) VALUES (?,?,?,?,?)",
            (mid, name, json.dumps(spec, ensure_ascii=False), now, now),
        )
    return get_model(mid)


def list_models() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM models ORDER BY updated_at DESC"
        ).fetchall()
    return [_row_to_model(r) for r in rows]


def get_model(mid: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM models WHERE id = ?", (mid,)).fetchone()
    return _row_to_model(row) if row else None


def update_model(mid: str, name: str | None, spec: dict) -> dict | None:
    now = time.time()
    with connect() as conn:
        cur = conn.execute(
            "UPDATE models SET name = COALESCE(?, name), spec = ?, updated_at = ? WHERE id = ?",
            (name, json.dumps(spec, ensure_ascii=False), now, mid),
        )
        if cur.rowcount == 0:
            return None
    return get_model(mid)


def delete_model(mid: str) -> bool:
    with connect() as conn:
        cur = conn.execute("DELETE FROM models WHERE id = ?", (mid,))
        return cur.rowcount > 0


def create_run(model_id: str, bound: int, result: dict) -> dict:
    rid = uuid.uuid4().hex[:12]
    now = time.time()
    with connect() as conn:
        conn.execute(
            "INSERT INTO runs (id, model_id, bound, verdict, result, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (rid, model_id, bound, result.get("verdict", "unknown"),
             json.dumps(result, ensure_ascii=False), now),
        )
    return {"id": rid, "model_id": model_id, "bound": bound,
            "verdict": result.get("verdict"), "result": result, "created_at": now}


def list_runs(model_id: str, limit: int = 50) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, model_id, bound, verdict, created_at FROM runs "
            "WHERE model_id = ? ORDER BY created_at DESC LIMIT ?",
            (model_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def get_run(rid: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM runs WHERE id = ?", (rid,)).fetchone()
    if not row:
        return None
    return {
        "id": row["id"], "model_id": row["model_id"], "bound": row["bound"],
        "verdict": row["verdict"], "created_at": row["created_at"],
        "result": json.loads(row["result"]),
    }


# ---------------------------------------------------------------------------
# 随机故障风险分析记录
# ---------------------------------------------------------------------------

def create_risk_run(model_id: str, bound: int, top_n: int,
                    params: dict, result: dict) -> dict:
    rid = uuid.uuid4().hex[:12]
    now = time.time()
    with connect() as conn:
        conn.execute(
            "INSERT INTO risk_runs (id, model_id, bound, top_n, verdict, params, result, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (rid, model_id, bound, top_n, result.get("verdict", "unknown"),
             json.dumps(params, ensure_ascii=False),
             json.dumps(result, ensure_ascii=False), now),
        )
    return {"id": rid, "model_id": model_id, "bound": bound, "top_n": top_n,
            "verdict": result.get("verdict"), "params": params,
            "result": result, "created_at": now}


def list_risk_runs(model_id: str, limit: int = 50) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, model_id, bound, top_n, verdict, created_at FROM risk_runs "
            "WHERE model_id = ? ORDER BY created_at DESC LIMIT ?",
            (model_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def get_risk_run(rid: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM risk_runs WHERE id = ?", (rid,)).fetchone()
    if not row:
        return None
    return {
        "id": row["id"], "model_id": row["model_id"], "bound": row["bound"],
        "top_n": row["top_n"], "verdict": row["verdict"],
        "created_at": row["created_at"],
        "params": json.loads(row["params"]),
        "result": json.loads(row["result"]),
    }


# ---------------------------------------------------------------------------
# 并发调度安全分析记录
# ---------------------------------------------------------------------------

def create_conc_run(model_id: str, bound: int, result: dict) -> dict:
    rid = uuid.uuid4().hex[:12]
    now = time.time()
    with connect() as conn:
        conn.execute(
            "INSERT INTO conc_runs (id, model_id, bound, verdict, result, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (rid, model_id, bound, result.get("verdict", "unknown"),
             json.dumps(result, ensure_ascii=False), now),
        )
    return {"id": rid, "model_id": model_id, "bound": bound,
            "verdict": result.get("verdict"), "result": result, "created_at": now}


def list_conc_runs(model_id: str, limit: int = 50) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, model_id, bound, verdict, created_at FROM conc_runs "
            "WHERE model_id = ? ORDER BY created_at DESC LIMIT ?",
            (model_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def get_conc_run(rid: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM conc_runs WHERE id = ?", (rid,)).fetchone()
    if not row:
        return None
    return {
        "id": row["id"], "model_id": row["model_id"], "bound": row["bound"],
        "verdict": row["verdict"], "created_at": row["created_at"],
        "result": json.loads(row["result"]),
    }


# ---------------------------------------------------------------------------
# 版本行为对比记录（保存对比参数 + 两个模型快照；模型被删除后记录仍可复现）
# ---------------------------------------------------------------------------

def create_diff_run(baseline_id: str | None, candidate_id: str | None,
                    baseline_name: str, candidate_name: str, bound: int,
                    params: dict, baseline_spec: dict, candidate_spec: dict,
                    result: dict) -> dict:
    rid = uuid.uuid4().hex[:12]
    now = time.time()
    with connect() as conn:
        conn.execute(
            "INSERT INTO diff_runs (id, baseline_id, candidate_id, baseline_name, "
            "candidate_name, bound, verdict, params, baseline_spec, candidate_spec, "
            "result, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (rid, baseline_id, candidate_id, baseline_name, candidate_name, bound,
             result.get("verdict", "unknown"),
             json.dumps(params, ensure_ascii=False),
             json.dumps(baseline_spec, ensure_ascii=False),
             json.dumps(candidate_spec, ensure_ascii=False),
             json.dumps(result, ensure_ascii=False), now),
        )
    return {"id": rid, "baseline_id": baseline_id, "candidate_id": candidate_id,
            "baseline_name": baseline_name, "candidate_name": candidate_name,
            "bound": bound, "verdict": result.get("verdict"),
            "params": params, "baseline_spec": baseline_spec,
            "candidate_spec": candidate_spec, "result": result, "created_at": now}


def list_diff_runs(limit: int = 50, model_id: str | None = None) -> list[dict]:
    with connect() as conn:
        if model_id is not None:
            rows = conn.execute(
                "SELECT id, baseline_id, candidate_id, baseline_name, candidate_name, "
                "bound, verdict, created_at FROM diff_runs "
                "WHERE baseline_id = ? OR candidate_id = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (model_id, model_id, limit)).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, baseline_id, candidate_id, baseline_name, candidate_name, "
                "bound, verdict, created_at FROM diff_runs "
                "ORDER BY created_at DESC LIMIT ?",
                (limit,)).fetchall()
    return [dict(r) for r in rows]


def get_diff_run(rid: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM diff_runs WHERE id = ?", (rid,)).fetchone()
    if not row:
        return None
    return {
        "id": row["id"], "baseline_id": row["baseline_id"],
        "candidate_id": row["candidate_id"], "baseline_name": row["baseline_name"],
        "candidate_name": row["candidate_name"], "bound": row["bound"],
        "verdict": row["verdict"], "created_at": row["created_at"],
        "params": json.loads(row["params"]),
        "baseline_spec": json.loads(row["baseline_spec"]),
        "candidate_spec": json.loads(row["candidate_spec"]),
        "result": json.loads(row["result"]),
    }
