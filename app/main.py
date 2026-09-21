"""交叉实验顺序平衡 API。

使用 Williams 平衡拉丁方为受试者分配条件顺序，保证：
* 每个条件在每个位置上出现次数相等；
* 每个有向相邻条件对出现次数相等。

完整条件序列仅保存在服务端，通过推进接口按周期受控揭盲：
创建与详情接口只返回受试者名册，不暴露任何后续周期安排。
"""

from __future__ import annotations

import random
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException

from . import db
from .schemas import (
    AdvanceOut,
    AdvanceRequest,
    ExperimentCreate,
    ExperimentOut,
    ExperimentSummary,
    ProgressOut,
)
from .williams import group_size, williams_sequences


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 容器 / 进程启动时建表（旧库自动补齐新增表）
    db.init_db()
    yield


app = FastAPI(
    title="Crossover Experiment Order Balancer",
    description="基于 Williams 平衡拉丁方的交叉实验顺序分配与按周期受控揭盲服务",
    version="1.1.0",
    lifespan=lifespan,
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post(
    "/experiments",
    status_code=201,
    response_model=ExperimentOut,
    summary="创建实验并生成分配结果（不返回条件序列）",
)
def create_experiment(payload: ExperimentCreate) -> dict:
    conditions = payload.conditions
    subject_ids = payload.subject_ids

    if any(not c.strip() for c in conditions):
        raise HTTPException(
            status_code=422, detail="conditions 不能为空字符串"
        )
    if len(set(conditions)) != len(conditions):
        raise HTTPException(
            status_code=422, detail="conditions 必须互异（存在重复条件）"
        )
    if any(not s.strip() for s in subject_ids):
        raise HTTPException(
            status_code=422, detail="subject_ids 不能为空字符串"
        )
    if len(set(subject_ids)) != len(subject_ids):
        raise HTTPException(
            status_code=422, detail="subject_ids 必须互异（存在重复受试者）"
        )

    n = len(conditions)
    size = group_size(n)
    count = len(subject_ids)

    if count % size != 0:
        lower = (count // size) * size
        upper = lower + size
        nearest = [lower, upper] if lower > 0 else [upper]
        raise HTTPException(
            status_code=422,
            detail={
                "message": (
                    f"受试者数量 {count} 无法整分为完整的序列组"
                    f"（每组 {size} 条序列）"
                ),
                "subject_count": count,
                "group_size": size,
                "nearest_acceptable": nearest,
            },
        )

    # Williams 平衡序列（下标形式），再映射为实际条件。
    # 完整序列写入服务端存储，不在响应中返回。
    index_sequences = williams_sequences(n)
    condition_sequences = [
        [conditions[i] for i in seq] for seq in index_sequences
    ]

    # 用种子确定性地打乱“序列池”，再依次分配给受试者。
    # 池中每条序列恰好出现 count // size 次，保证整组完整。
    rng = random.Random(payload.seed)
    pool = list(range(size)) * (count // size)
    rng.shuffle(pool)

    assignments = [
        {
            "subject_id": subject_id,
            "sequence_index": pool[i],
            "sequence": condition_sequences[pool[i]],
        }
        for i, subject_id in enumerate(subject_ids)
    ]

    experiment_id = uuid.uuid4().hex
    created_at = datetime.now(timezone.utc).isoformat()
    db.insert_experiment(
        experiment_id, conditions, payload.seed, size, created_at, assignments
    )

    return {
        "experiment_id": experiment_id,
        "conditions": conditions,
        "seed": payload.seed,
        "group_size": size,
        "num_subjects": count,
        "num_periods": n,
        "created_at": created_at,
        "assignments": [{"subject_id": s} for s in subject_ids],
    }


@app.get(
    "/experiments",
    response_model=list[ExperimentSummary],
    summary="列出全部实验",
)
def list_experiments() -> list[dict]:
    return db.list_experiments()


@app.get(
    "/experiments/{experiment_id}",
    response_model=ExperimentOut,
    summary="查询实验（只返回受试者名册，不含条件序列）",
)
def get_experiment(experiment_id: str) -> dict:
    experiment = db.get_experiment(experiment_id)
    if experiment is None:
        raise HTTPException(status_code=404, detail="experiment not found")
    return experiment


@app.post(
    "/experiments/{experiment_id}/advance",
    response_model=AdvanceOut,
    summary="按周期受控揭盲：仅当 expected_period 为下一周期时返回当前条件并原子推进",
)
def advance_period(experiment_id: str, payload: AdvanceRequest) -> dict:
    revealed_at = datetime.now(timezone.utc).isoformat()
    try:
        result = db.advance_period(
            experiment_id,
            payload.subject_id,
            payload.expected_period,
            payload.request_id,
            revealed_at,
        )
    except db.RequestIdConflictError:
        # 同一幂等键却提交了不同的受试者或周期
        raise HTTPException(
            status_code=409,
            detail={
                "message": "request_id 已被其他请求占用",
                "reason": "request_id_conflict",
                "request_id": payload.request_id,
            },
        )
    except db.StalePeriodError as exc:
        # 并发抢先推进或过期周期：不揭露任何条件
        raise HTTPException(
            status_code=409,
            detail={
                "message": "expected_period 与该受试者的下一周期不一致"
                if not exc.complete
                else "该受试者已完成全部周期",
                "reason": "experiment_complete"
                if exc.complete
                else "period_mismatch",
                "next_period": exc.next_period,
            },
        )

    if result is None:
        raise HTTPException(
            status_code=404,
            detail={
                "message": "实验或受试者不存在",
                "experiment_id": experiment_id,
                "subject_id": payload.subject_id,
            },
        )
    return result


@app.get(
    "/experiments/{experiment_id}/subjects/{subject_id}/progress",
    response_model=ProgressOut,
    summary="查询受试者揭盲进度（只含已揭盲记录与 next_period）",
)
def get_progress(experiment_id: str, subject_id: str) -> dict:
    progress = db.get_progress(experiment_id, subject_id)
    if progress is None:
        raise HTTPException(
            status_code=404,
            detail={
                "message": "实验或受试者不存在",
                "experiment_id": experiment_id,
                "subject_id": subject_id,
            },
        )
    return progress
