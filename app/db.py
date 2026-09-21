"""SQLite 存储层。

数据库路径通过环境变量 ``DATABASE_PATH`` 配置，默认为当前目录下的
``experiments.db``。表结构在应用启动时通过 :func:`init_db` 创建，
新增表均使用 ``CREATE TABLE IF NOT EXISTS``，新增列通过
``ALTER TABLE ... ADD COLUMN`` 补齐，因此已有数据库在启动时会自动
迁移，无需手工操作；未配置洗脱期的旧实验按 ``washout_seconds = 0`` 处理。

表说明：
* ``experiments`` / ``assignments``：实验与受试者的完整条件序列，
  序列只保存在服务端，不通过创建 / 详情接口下发；
  ``experiments.washout_seconds`` 为相邻周期间的最小洗脱秒数；
* ``subject_progress``：每个受试者下一待揭盲周期 ``next_period``；
* ``unblindings``：已揭盲记录（周期 -> 条件）；
* ``advance_requests``：推进接口的幂等结果，按 ``(实验, request_id)`` 去重；
* ``period_completions``：周期完成登记（周期 -> 服务端完成时间）；
* ``complete_requests``：完成接口的幂等结果，按 ``(实验, request_id)`` 去重。
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# 写事务排队等待锁的时间，配合 BEGIN IMMEDIATE 保证并发推进串行化
BUSY_TIMEOUT_MS = 10_000

SCHEMA = """
CREATE TABLE IF NOT EXISTS experiments (
    id              TEXT PRIMARY KEY,
    conditions      TEXT NOT NULL,     -- JSON 数组
    seed            INTEGER NOT NULL,
    group_size      INTEGER NOT NULL,  -- 一组完整序列的条数
    num_subjects    INTEGER NOT NULL,
    washout_seconds INTEGER NOT NULL DEFAULT 0,  -- 相邻周期间最小洗脱秒数
    created_at      TEXT NOT NULL
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

CREATE TABLE IF NOT EXISTS period_completions (
    experiment_id TEXT NOT NULL REFERENCES experiments(id),
    subject_id    TEXT NOT NULL,
    period        INTEGER NOT NULL,      -- 已完成并登记的周期（从 1 开始）
    request_id    TEXT NOT NULL,         -- 首次成功完成登记的幂等键
    completed_at  TEXT NOT NULL,         -- 服务端登记的完成时间（参与门禁判断）
    PRIMARY KEY (experiment_id, subject_id, period)
);

CREATE TABLE IF NOT EXISTS complete_requests (
    experiment_id   TEXT NOT NULL REFERENCES experiments(id),
    request_id      TEXT NOT NULL,
    subject_id      TEXT NOT NULL,       -- 首次请求绑定的受试者
    expected_period INTEGER NOT NULL,    -- 首次请求绑定的完成周期
    period          INTEGER NOT NULL,    -- 首次结果登记完成的周期
    completed_at    TEXT NOT NULL,
    PRIMARY KEY (experiment_id, request_id)
);
"""

# 旧库迁移：为 experiments 补齐 washout_seconds 列，缺省按 0 秒处理。
# 已存在时会报错并被忽略（SQLite 不支持 ADD COLUMN IF NOT EXISTS）。
_MIGRATIONS = (
    "ALTER TABLE experiments "
    "ADD COLUMN washout_seconds INTEGER NOT NULL DEFAULT 0",
)


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


class WashoutNotElapsedError(Exception):
    """推进下一周期时，上一周期尚未完成或洗脱期尚未届满。

    :param reason: ``previous_period_not_completed``（上一周期无完成记录）
        或 ``washout_not_elapsed``（洗脱期未到期）。
    :param next_period: 调用方试图揭盲的周期（= 上一周期 + 1）。
    :param retry_after_seconds: 还需等待的整秒数（向上取整）；
        上一周期未完成（无 next_available_at）时为 ``None``。
    :param next_available_at: 服务端计算出的下一周期最早可揭盲时间（ISO 字符串）。
    """

    def __init__(
        self,
        reason: str,
        next_period: int,
        retry_after_seconds: int | None,
        next_available_at: str | None,
    ) -> None:
        self.reason = reason
        self.next_period = next_period
        self.retry_after_seconds = retry_after_seconds
        self.next_available_at = next_available_at
        super().__init__(
            f"washout gate blocked: reason={reason}, next_period={next_period}"
        )


class CompletionStateError(Exception):
    """完成登记越序：目标周期不是“已揭盲且尚未完成”的当前周期。

    :param reason: ``no_revealed_period``（尚无已揭盲周期）、
        ``period_not_revealed``（目标周期还未揭盲，超前）、
        ``period_already_completed``（目标周期已登记完成）或
        ``period_mismatch``（目标周期落后于当前周期）。
    :param current_period: 最近已揭盲且可登记完成的周期（无揭盲时为 0）。
    """

    def __init__(self, reason: str, current_period: int) -> None:
        self.reason = reason
        self.current_period = current_period
        super().__init__(
            f"completion out of order: reason={reason}, "
            f"current_period={current_period}"
        )


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
    """建表（幂等），在应用启动时调用；旧库会自动补齐新增表与列。"""
    with closing(get_connection()) as conn:
        with conn:
            conn.executescript(SCHEMA)
            for statement in _MIGRATIONS:
                try:
                    conn.execute(statement)
                except sqlite3.OperationalError:
                    # 列已存在（新建的库或此前已迁移过的库），忽略即可
                    pass


def insert_experiment(
    experiment_id: str,
    conditions: list[str],
    seed: int,
    group_size: int,
    created_at: str,
    assignments: list[dict[str, Any]],
    washout_seconds: int = 0,
) -> None:
    with closing(get_connection()) as conn:
        with conn:
            conn.execute(
                "INSERT INTO experiments "
                "(id, conditions, seed, group_size, num_subjects, "
                "washout_seconds, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    experiment_id,
                    json.dumps(conditions, ensure_ascii=False),
                    seed,
                    group_size,
                    len(assignments),
                    washout_seconds,
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
    now: datetime,
) -> dict[str, Any] | None:
    """原子推进一个受试者的揭盲进度。

    在单个 ``BEGIN IMMEDIATE`` 事务内完成：

    1. 幂等检查：``(experiment_id, request_id)`` 已存在时直接返回首次结果，
       不再次推进；若同一 request_id 绑定了不同的 subject_id /
       expected_period，抛出 :class:`RequestIdConflictError`；
    2. 周期校验：``expected_period`` 必须等于受试者当前 ``next_period``，
       且实验尚未结束，否则抛出 :class:`StalePeriodError`（409），
       失败路径不写入任何数据、不揭露任何条件；
    3. 完成 / 洗脱门禁（``next_period > 1`` 且实验配置了洗脱期）：
       上一周期必须存在完成登记，且服务端当前时间已达到
       ``completed_at + washout_seconds``，否则抛出
       :class:`WashoutNotElapsedError`（409，带 ``retry_after_seconds``），
       响应不包含任何条件信息；
    4. 揭盲：写入 ``unblindings``、推进 ``subject_progress``、
       落盘 ``advance_requests``，三者一起提交或一起回滚。

    时间判断全部使用服务端时钟（``now``），整个门禁与揭盲在同一写事务内
    完成，因此并发完成登记 / 推进看到的状态串行一致。

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
                now,
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
    now: datetime,
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

    # 4) 完成与洗脱门禁：进入下一周期前，上一周期必须已完成且洗脱期届满。
    #    washout_seconds = 0（含未配置的旧实验）时门禁等价于放行，
    #    保持旧版行为：不强制完成登记也可连续推进。
    if next_period > 1:
        washout_seconds = _washout_seconds(conn, experiment_id)
        if washout_seconds > 0:
            previous = next_period - 1
            completion_row = conn.execute(
                "SELECT completed_at FROM period_completions "
                "WHERE experiment_id = ? AND subject_id = ? AND period = ?",
                (experiment_id, subject_id, previous),
            ).fetchone()
            if completion_row is None:
                # 不泄露下一周期条件，也不给出可凭空等待的时间点
                raise WashoutNotElapsedError(
                    reason="previous_period_not_completed",
                    next_period=next_period,
                    retry_after_seconds=None,
                    next_available_at=None,
                )
            completed_at = datetime.fromisoformat(completion_row["completed_at"])
            next_available_at = completed_at + timedelta(seconds=washout_seconds)
            if now < next_available_at:
                raise WashoutNotElapsedError(
                    reason="washout_not_elapsed",
                    next_period=next_period,
                    retry_after_seconds=math.ceil(
                        (next_available_at - now).total_seconds()
                    ),
                    next_available_at=next_available_at.isoformat(),
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


def _washout_seconds(conn: sqlite3.Connection, experiment_id: str) -> int:
    """读取实验洗脱秒数；旧行缺列 / 缺值时按 0 秒处理。"""
    row = conn.execute(
        "SELECT washout_seconds FROM experiments WHERE id = ?",
        (experiment_id,),
    ).fetchone()
    if row is None:
        return 0
    value = row["washout_seconds"]
    return int(value) if value is not None else 0


def complete_period(
    experiment_id: str,
    subject_id: str,
    expected_period: int,
    request_id: str,
    completed_at: str,
) -> dict[str, Any] | None:
    """原子登记一个周期的完成时间。

    在单个 ``BEGIN IMMEDIATE`` 事务内完成：

    1. 幂等检查：``(experiment_id, request_id)`` 已存在时直接返回首次结果；
       若同一 request_id 绑定了不同的 subject_id / expected_period，
       抛出 :class:`RequestIdConflictError`；
    2. 越序校验：目标周期必须**已揭盲且尚未完成**（即该受试者最近揭盲、
       尚未登记完成的当前周期），超前（未揭盲）、落后（已过周期）、
       重复完成或尚无揭盲均抛出 :class:`CompletionStateError`（409），
       不写入任何数据；
    3. 完成登记：写入 ``period_completions`` 与 ``complete_requests``，
       两者一起提交或一起回滚。

    完成时间由服务端时钟生成并持久化，作为后续洗脱期门禁的判断基准；
    与推进接口共享写锁，并发完成 / 推进看到的状态串行一致。

    返回首次 / 重放的完成结果；实验或受试者不存在时返回 ``None``。
    """
    conn = get_connection()
    try:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        committed = False
        try:
            result = _complete_locked(
                conn,
                experiment_id,
                subject_id,
                expected_period,
                request_id,
                completed_at,
            )
            if result is not None and not result["_replayed"]:
                conn.execute("COMMIT")
                committed = True
            if result is not None:
                result.pop("_replayed", None)
            return result
        finally:
            if not committed:
                conn.rollback()
    finally:
        conn.close()


def _complete_locked(
    conn: sqlite3.Connection,
    experiment_id: str,
    subject_id: str,
    expected_period: int,
    request_id: str,
    completed_at: str,
) -> dict[str, Any] | None:
    # 1) 幂等命中：返回首次结果，绝不重复登记
    request_row = conn.execute(
        "SELECT subject_id, expected_period, period, completed_at "
        "FROM complete_requests WHERE experiment_id = ? AND request_id = ?",
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
            "request_id": request_id,
            "completed_at": request_row["completed_at"],
            "_replayed": True,
        }

    # 2) 实验 / 受试者必须存在（完成登记不下发任何条件，只确认存在性）
    assignment_row = conn.execute(
        "SELECT 1 FROM assignments "
        "WHERE experiment_id = ? AND subject_id = ?",
        (experiment_id, subject_id),
    ).fetchone()
    if assignment_row is None:
        return None

    # 3) 当前可登记完成的周期 = 最近揭盲、且尚未登记完成的周期
    latest_revealed_row = conn.execute(
        "SELECT MAX(period) AS period FROM unblindings "
        "WHERE experiment_id = ? AND subject_id = ?",
        (experiment_id, subject_id),
    ).fetchone()
    latest_revealed = latest_revealed_row["period"]
    if latest_revealed is None:
        raise CompletionStateError(reason="no_revealed_period", current_period=0)

    latest_completed_row = conn.execute(
        "SELECT MAX(period) AS period FROM period_completions "
        "WHERE experiment_id = ? AND subject_id = ?",
        (experiment_id, subject_id),
    ).fetchone()
    latest_completed = latest_completed_row["period"] or 0
    current_period = latest_completed + 1
    # 最近揭盲周期必然 >= 最近完成周期 + 1（完成接口只允许登记已揭盲周期）
    if current_period > latest_revealed:  # 理论上不应出现，防御性处理
        raise CompletionStateError(
            reason="period_already_completed", current_period=latest_completed
        )

    if expected_period != current_period:
        if expected_period <= latest_completed:
            reason = "period_already_completed"
        elif expected_period > latest_revealed:
            reason = "period_not_revealed"
        else:
            reason = "period_mismatch"
        raise CompletionStateError(reason=reason, current_period=current_period)

    # 4) 登记完成（主键约束兜底并发：同周期只有一个请求能成功）
    try:
        conn.execute(
            "INSERT INTO period_completions "
            "(experiment_id, subject_id, period, request_id, completed_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                experiment_id,
                subject_id,
                current_period,
                request_id,
                completed_at,
            ),
        )
    except sqlite3.IntegrityError:  # pragma: no cover - 事务锁已串行化，兜底
        raise CompletionStateError(
            reason="period_already_completed", current_period=latest_completed
        )
    try:
        conn.execute(
            "INSERT INTO complete_requests "
            "(experiment_id, request_id, subject_id, expected_period, "
            "period, completed_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                experiment_id,
                request_id,
                subject_id,
                expected_period,
                current_period,
                completed_at,
            ),
        )
    except sqlite3.IntegrityError:  # pragma: no cover - 事务锁已串行化，兜底
        raise RequestIdConflictError()

    return {
        "experiment_id": experiment_id,
        "subject_id": subject_id,
        "period": current_period,
        "request_id": request_id,
        "completed_at": completed_at,
        "_replayed": False,
    }


def get_progress(
    experiment_id: str, subject_id: str, now: datetime | None = None
) -> dict[str, Any] | None:
    """查询受试者揭盲与完成进度。

    只返回已揭盲记录、已完成周期与下一周期可揭盲时间 ``next_available_at``，
    不包含完整序列或后续安排。时间判断使用服务端时钟（``now``，缺省时取
    当前 UTC 时间），与推进 / 完成门禁的判断基准保持一致。

    ``next_available_at`` 语义：

    * 已完成全部周期：``None``；
    * 下一周期已满足完成 / 洗脱门禁（含洗脱期为 0 的实验）：``None``；
    * 上一周期已完成但洗脱期未届满：``completed_at + washout_seconds``；
    * 上一周期尚未完成（实验配置了洗脱期）：``None``，调用方需先登记完成。

    实验或受试者不存在时返回 ``None``。
    """
    if now is None:
        now = datetime.now(timezone.utc)
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
        completion_rows = conn.execute(
            "SELECT period, completed_at FROM period_completions "
            "WHERE experiment_id = ? AND subject_id = ? ORDER BY period",
            (experiment_id, subject_id),
        ).fetchall()
        progress_row = conn.execute(
            "SELECT next_period FROM subject_progress "
            "WHERE experiment_id = ? AND subject_id = ?",
            (experiment_id, subject_id),
        ).fetchone()
        washout_seconds = _washout_seconds(conn, experiment_id)

    next_period = progress_row["next_period"] if progress_row is not None else 1
    completed_all = next_period > num_periods

    next_available_at: str | None = None
    if not completed_all and next_period > 1 and washout_seconds > 0:
        previous_completion = next(
            (row for row in completion_rows if row["period"] == next_period - 1),
            None,
        )
        if previous_completion is not None:
            available_at = datetime.fromisoformat(
                previous_completion["completed_at"]
            ) + timedelta(seconds=washout_seconds)
            # 已届满则门禁放行，无需再回传时间点
            if now < available_at:
                next_available_at = available_at.isoformat()

    return {
        "experiment_id": experiment_id,
        "subject_id": subject_id,
        "next_period": next_period,
        "num_periods": num_periods,
        "washout_seconds": washout_seconds,
        "completed": completed_all,
        "reveals": [
            {
                "period": row["period"],
                "condition": row["condition"],
                "revealed_at": row["revealed_at"],
            }
            for row in reveal_rows
        ],
        "completions": [
            {"period": row["period"], "completed_at": row["completed_at"]}
            for row in completion_rows
        ],
        "next_available_at": next_available_at,
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
        "washout_seconds": row["washout_seconds"]
        if "washout_seconds" in row.keys()
        else 0,
        "created_at": row["created_at"],
    }
    if assignment_rows is not None:
        # 只回传受试者名册，完整条件序列保留在服务端，按周期受控揭盲
        experiment["assignments"] = [
            {"subject_id": a["subject_id"]} for a in assignment_rows
        ]
    return experiment
