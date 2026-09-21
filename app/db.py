"""SQLite 存储层。

数据库路径通过环境变量 ``DATABASE_PATH`` 配置，默认为当前目录下的
``experiments.db``。表结构在应用启动时通过 :func:`init_db` 创建，
新增表均使用 ``CREATE TABLE IF NOT EXISTS``，因此已有数据库在启动时
会自动补齐表结构，无需手工迁移。

表说明：
* ``experiments`` / ``assignments``：实验与受试者的完整条件序列，
  序列只保存在服务端，不通过创建 / 详情接口下发；
* ``subject_progress``：每个受试者下一待揭盲周期 ``next_period``；
* ``unblindings``：已揭盲记录（周期 -> 条件）；
* ``advance_requests``：推进接口的幂等结果，按 ``(实验, request_id)`` 去重。
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

# 写事务排队等待锁的时间，配合 BEGIN IMMEDIATE 保证并发推进串行化
BUSY_TIMEOUT_MS = 10_000

SCHEMA = """
CREATE TABLE IF NOT EXISTS experiments (
    id           TEXT PRIMARY KEY,
    conditions   TEXT NOT NULL,          -- JSON 数组
    seed         INTEGER NOT NULL,
    group_size   INTEGER NOT NULL,       -- 一组完整序列的条数
    num_subjects INTEGER NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS assignments (
    experiment_id  TEXT NOT NULL REFERENCES experiments(id),
    subject_id     TEXT NOT NULL,
    sequence_index INTEGER NOT NULL,     -- 命中的 Williams 序列下标
    sequence       TEXT NOT NULL,        -- JSON 数组，映射后的完整条件序列（服务端持有）
    PRIMARY KEY (experiment_id, subject_id)
);

CREATE TABLE IF NOT EXISTS subject_progress (
    experiment_id TEXT NOT NULL REFERENCES experiments(id),
    subject_id    TEXT NOT NULL,
    next_period   INTEGER NOT NULL,      -- 下一待揭盲周期（从 1 开始）
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (experiment_id, subject_id)
);

CREATE TABLE IF NOT EXISTS unblindings (
    experiment_id TEXT NOT NULL REFERENCES experiments(id),
    subject_id    TEXT NOT NULL,
    period        INTEGER NOT NULL,      -- 已揭盲周期（从 1 开始）
    condition     TEXT NOT NULL,         -- 该周期揭盲的条件
    request_id    TEXT NOT NULL,         -- 首次成功推进的幂等键
    revealed_at   TEXT NOT NULL,
    PRIMARY KEY (experiment_id, subject_id, period)
);

CREATE TABLE IF NOT EXISTS advance_requests (
    experiment_id   TEXT NOT NULL REFERENCES experiments(id),
    request_id      TEXT NOT NULL,
    subject_id      TEXT NOT NULL,       -- 首次请求绑定的受试者
    expected_period INTEGER NOT NULL,    -- 首次请求绑定的预期周期
    period          INTEGER NOT NULL,    -- 首次结果揭盲的周期
    condition       TEXT NOT NULL,       -- 首次结果揭盲的条件
    revealed_at     TEXT NOT NULL,
    PRIMARY KEY (experiment_id, request_id)
);
"""


class StalePeriodError(Exception):
    """expected_period 与受试者下一周期不一致（或实验已结束）。"""

    def __init__(self, next_period: int, complete: bool) -> None:
        self.next_period = next_period
        self.complete = complete
        super().__init__(
            f"expected period mismatch: next_period={next_period}, complete={complete}"
        )


class RequestIdConflictError(Exception):
    """request_id 已被另一条请求（不同 subject_id / expected_period）占用。"""


def _database_path() -> str:
    return os.environ.get("DATABASE_PATH", "experiments.db")


def get_connection() -> sqlite3.Connection:
    path = Path(_database_path())
    if str(path.parent) not in ("", "."):
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    return conn


def init_db() -> None:
    """建表（幂等），在应用启动时调用；旧库会自动补齐新增表。"""
    with closing(get_connection()) as conn:
        with conn:
            conn.executescript(SCHEMA)


def insert_experiment(
    experiment_id: str,
    conditions: list[str],
    seed: int,
    group_size: int,
    created_at: str,
    assignments: list[dict[str, Any]],
) -> None:
    with closing(get_connection()) as conn:
        with conn:
            conn.execute(
                "INSERT INTO experiments "
                "(id, conditions, seed, group_size, num_subjects, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    experiment_id,
                    json.dumps(conditions, ensure_ascii=False),
                    seed,
                    group_size,
                    len(assignments),
                    created_at,
                ),
            )
            conn.executemany(
                "INSERT INTO assignments "
                "(experiment_id, subject_id, sequence_index, sequence) "
                "VALUES (?, ?, ?, ?)",
                [
                    (
                        experiment_id,
                        a["subject_id"],
                        a["sequence_index"],
                        json.dumps(a["sequence"], ensure_ascii=False),
                    )
                    for a in assignments
                ],
            )


def advance_period(
    experiment_id: str,
    subject_id: str,
    expected_period: int,
    request_id: str,
    revealed_at: str,
) -> dict[str, Any] | None:
    """原子推进一个受试者的揭盲进度。

    在单个 ``BEGIN IMMEDIATE`` 事务内完成：

    1. 幂等检查：``(experiment_id, request_id)`` 已存在时直接返回首次结果，
       不再次推进；若同一 request_id 绑定了不同的 subject_id /
       expected_period，抛出 :class:`RequestIdConflictError`；
    2. 周期校验：``expected_period`` 必须等于受试者当前 ``next_period``，
       且实验尚未结束，否则抛出 :class:`StalePeriodError`（409），
       失败路径不写入任何数据、不揭露任何条件；
    3. 揭盲：写入 ``unblindings``、推进 ``subject_progress``、
       落盘 ``advance_requests``，三者一起提交或一起回滚。

    返回首次 / 重放的揭盲结果；实验或受试者不存在时返回 ``None``。
    """
    conn = get_connection()
    try:
        # 显式事务，避免 sqlite3 隐式的 deferred 事务
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        committed = False
        try:
            result = _advance_locked(
                conn,
                experiment_id,
                subject_id,
                expected_period,
                request_id,
                revealed_at,
            )
            if result is not None and not result["_replayed"]:
                conn.execute("COMMIT")
                committed = True
            if result is not None:
                result.pop("_replayed", None)
            return result
        finally:
            if not committed:
                # 重放（只读）、409、404 或异常时结束事务
                conn.rollback()
    finally:
        conn.close()


def _advance_locked(
    conn: sqlite3.Connection,
    experiment_id: str,
    subject_id: str,
    expected_period: int,
    request_id: str,
    revealed_at: str,
) -> dict[str, Any] | None:
    # 1) 幂等命中：返回首次结果，绝不重复推进
    request_row = conn.execute(
        "SELECT subject_id, expected_period, period, condition, revealed_at "
        "FROM advance_requests WHERE experiment_id = ? AND request_id = ?",
        (experiment_id, request_id),
    ).fetchone()
    if request_row is not None:
        if (
            request_row["subject_id"] != subject_id
            or request_row["expected_period"] != expected_period
        ):
            raise RequestIdConflictError()
        return {
            "experiment_id": experiment_id,
            "subject_id": subject_id,
            "period": request_row["period"],
            "condition": request_row["condition"],
            "request_id": request_id,
            "revealed_at": request_row["revealed_at"],
            "_replayed": True,
        }

    # 2) 定位受试者的服务端序列（不做任何下发）
    assignment_row = conn.execute(
        "SELECT sequence FROM assignments "
        "WHERE experiment_id = ? AND subject_id = ?",
        (experiment_id, subject_id),
    ).fetchone()
    if assignment_row is None:
        return None

    sequence = json.loads(assignment_row["sequence"])
    num_periods = len(sequence)

    progress_row = conn.execute(
        "SELECT next_period FROM subject_progress "
        "WHERE experiment_id = ? AND subject_id = ?",
        (experiment_id, subject_id),
    ).fetchone()
    next_period = progress_row["next_period"] if progress_row is not None else 1

    # 3) 仅当请求的正是下一周期且实验未结束时才揭盲
    if next_period > num_periods or expected_period != next_period:
        raise StalePeriodError(
            next_period=next_period, complete=next_period > num_periods
        )

    condition = sequence[next_period - 1]

    conn.execute(
        "INSERT INTO unblindings "
        "(experiment_id, subject_id, period, condition, request_id, revealed_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            experiment_id,
            subject_id,
            next_period,
            condition,
            request_id,
            revealed_at,
        ),
    )
    conn.execute(
        "INSERT INTO subject_progress "
        "(experiment_id, subject_id, next_period, updated_at) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(experiment_id, subject_id) DO UPDATE SET "
        "next_period = excluded.next_period, updated_at = excluded.updated_at",
        (experiment_id, subject_id, next_period + 1, revealed_at),
    )
    conn.execute(
        "INSERT INTO advance_requests "
        "(experiment_id, request_id, subject_id, expected_period, "
        "period, condition, revealed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            experiment_id,
            request_id,
            subject_id,
            expected_period,
            next_period,
            condition,
            revealed_at,
        ),
    )

    return {
        "experiment_id": experiment_id,
        "subject_id": subject_id,
        "period": next_period,
        "condition": condition,
        "request_id": request_id,
        "revealed_at": revealed_at,
        "_replayed": False,
    }


def get_progress(experiment_id: str, subject_id: str) -> dict[str, Any] | None:
    """查询受试者揭盲进度。

    只返回已揭盲记录与 next_period，不包含完整序列或后续安排。
    实验或受试者不存在时返回 ``None``。
    """
    with closing(get_connection()) as conn:
        assignment_row = conn.execute(
            "SELECT sequence FROM assignments "
            "WHERE experiment_id = ? AND subject_id = ?",
            (experiment_id, subject_id),
        ).fetchone()
        if assignment_row is None:
            return None
        num_periods = len(json.loads(assignment_row["sequence"]))

        reveal_rows = conn.execute(
            "SELECT period, condition, revealed_at FROM unblindings "
            "WHERE experiment_id = ? AND subject_id = ? ORDER BY period",
            (experiment_id, subject_id),
        ).fetchall()
        progress_row = conn.execute(
            "SELECT next_period FROM subject_progress "
            "WHERE experiment_id = ? AND subject_id = ?",
            (experiment_id, subject_id),
        ).fetchone()

    next_period = progress_row["next_period"] if progress_row is not None else 1
    return {
        "experiment_id": experiment_id,
        "subject_id": subject_id,
        "next_period": next_period,
        "num_periods": num_periods,
        "completed": next_period > num_periods,
        "reveals": [
            {
                "period": row["period"],
                "condition": row["condition"],
                "revealed_at": row["revealed_at"],
            }
            for row in reveal_rows
        ],
    }


def get_experiment(experiment_id: str) -> dict[str, Any] | None:
    with closing(get_connection()) as conn:
        row = conn.execute(
            "SELECT * FROM experiments WHERE id = ?", (experiment_id,)
        ).fetchone()
        if row is None:
            return None
        assignment_rows = conn.execute(
            "SELECT subject_id FROM assignments "
            "WHERE experiment_id = ? ORDER BY rowid",
            (experiment_id,),
        ).fetchall()
    return _experiment_dict(row, assignment_rows)


def list_experiments() -> list[dict[str, Any]]:
    with closing(get_connection()) as conn:
        rows = conn.execute(
            "SELECT * FROM experiments ORDER BY created_at, id"
        ).fetchall()
    return [_experiment_dict(row, None) for row in rows]


def _experiment_dict(
    row: sqlite3.Row, assignment_rows: list[sqlite3.Row] | None
) -> dict[str, Any]:
    conditions = json.loads(row["conditions"])
    experiment: dict[str, Any] = {
        "experiment_id": row["id"],
        "conditions": conditions,
        "seed": row["seed"],
        "group_size": row["group_size"],
        "num_subjects": row["num_subjects"],
        "num_periods": len(conditions),
        "created_at": row["created_at"],
    }
    if assignment_rows is not None:
        # 只回传受试者名册，完整条件序列保留在服务端，按周期受控揭盲
        experiment["assignments"] = [
            {"subject_id": a["subject_id"]} for a in assignment_rows
        ]
    return experiment
