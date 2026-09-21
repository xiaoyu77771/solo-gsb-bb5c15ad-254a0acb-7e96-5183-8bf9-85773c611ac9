"""API 与 Williams 序列的自动化测试。"""

from __future__ import annotations

import sqlite3
import threading
from collections import Counter
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import main
from app.main import app
from app.williams import group_size, williams_sequences


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    with TestClient(app) as test_client:
        yield test_client


def assert_balanced(sequences: list[list[str]], n: int) -> None:
    """校验位置平衡与有向相邻对平衡。"""
    size = group_size(n)
    assert len(sequences) % size == 0
    k = len(sequences) // size  # 完整组数
    per_position = k * (1 if n % 2 == 0 else 2)

    # 每个条件在每个位置出现次数相等
    for pos in range(n):
        counts = Counter(seq[pos] for seq in sequences)
        assert len(counts) == n
        assert all(v == per_position for v in counts.values())

    # 每个有向相邻条件对出现次数相等
    pairs: Counter = Counter()
    for seq in sequences:
        for a, b in zip(seq, seq[1:]):
            pairs[(a, b)] += 1
    assert len(pairs) == n * (n - 1)
    assert len(set(pairs.values())) == 1


def create_experiment(
    client: TestClient,
    conditions: list[str] | None = None,
    subject_ids: list[str] | None = None,
    seed: int = 42,
    washout_seconds: int = 0,
):
    conditions = conditions or ["A", "B", "C"]
    subject_ids = subject_ids or [f"s{i}" for i in range(6)]
    resp = client.post(
        "/experiments",
        json={
            "conditions": conditions,
            "subject_ids": subject_ids,
            "seed": seed,
            "washout_seconds": washout_seconds,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json(), subject_ids


class FakeClock:
    """可控的服务端时钟，替代 main.server_now 以模拟洗脱期流逝。"""

    def __init__(self, start: datetime | None = None) -> None:
        self.current = start or datetime(2026, 9, 21, 8, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)


@pytest.fixture()
def fake_clock(monkeypatch) -> FakeClock:
    clock = FakeClock()
    monkeypatch.setattr(main, "server_now", clock)
    return clock


def complete_period(
    client: TestClient,
    experiment_id: str,
    subject_id: str,
    period: int,
    request_id: str,
) -> dict:
    resp = client.post(
        f"/experiments/{experiment_id}/complete",
        json={
            "subject_id": subject_id,
            "expected_period": period,
            "request_id": request_id,
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def reveal_sequence(
    client: TestClient,
    experiment_id: str,
    subject_id: str,
    num_periods: int,
    req_prefix: str = "",
) -> list[str]:
    """通过推进接口逐周期揭盲，重建该受试者的条件序列。"""
    sequence: list[str] = []
    for period in range(1, num_periods + 1):
        resp = client.post(
            f"/experiments/{experiment_id}/advance",
            json={
                "subject_id": subject_id,
                "expected_period": period,
                "request_id": f"{req_prefix}{subject_id}-p{period}",
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["period"] == period
        sequence.append(body["condition"])
    return sequence


def assert_no_sequence_leak(payload: object) -> None:
    """响应中任何层级都不得出现条件序列或序列下标。"""
    forbidden = {"sequence", "sequence_index"}
    if isinstance(payload, dict):
        assert forbidden.isdisjoint(payload.keys())
        for value in payload.values():
            assert_no_sequence_leak(value)
    elif isinstance(payload, list):
        for item in payload:
            assert_no_sequence_leak(item)


# ---------- Williams 序列单元测试 ----------

@pytest.mark.parametrize("n", range(2, 9))
def test_williams_sequences_balanced(n: int) -> None:
    sequences = [[str(i) for i in seq] for seq in williams_sequences(n)]
    assert len(sequences) == group_size(n)
    assert_balanced(sequences, n)


# ---------- 创建 / 查询接口（不再下发完整序列） ----------

def test_create_even_conditions(client: TestClient) -> None:
    body, subject_ids = create_experiment(
        client,
        conditions=["A", "B", "C", "D"],
        subject_ids=[f"s{i}" for i in range(8)],
    )
    assert body["group_size"] == 4
    assert body["num_subjects"] == 8
    assert body["num_periods"] == 4
    assert len(body["assignments"]) == 8
    # 名册只含受试者 ID，且顺序与提交一致
    assert [a["subject_id"] for a in body["assignments"]] == subject_ids
    assert set(body["assignments"][0].keys()) == {"subject_id"}
    assert_no_sequence_leak(body)

    # 序列只能通过逐周期揭盲重建，且仍满足 Williams 平衡
    sequences = [
        reveal_sequence(client, body["experiment_id"], s, 4)
        for s in subject_ids
    ]
    assert_balanced(sequences, 4)
    # 2 个完整组：每条 Williams 序列恰好被使用 2 次
    assert sorted(Counter(tuple(seq) for seq in sequences).values()) == [
        2, 2, 2, 2
    ]


def test_create_odd_conditions(client: TestClient) -> None:
    body, subject_ids = create_experiment(
        client,
        conditions=["A", "B", "C"],
        subject_ids=[f"s{i}" for i in range(6)],
        seed=7,
    )
    assert body["group_size"] == 6
    assert body["num_periods"] == 3
    assert_no_sequence_leak(body)
    sequences = [
        reveal_sequence(client, body["experiment_id"], s, 3)
        for s in subject_ids
    ]
    assert_balanced(sequences, 3)


def test_same_input_and_seed_gives_same_assignment(client: TestClient) -> None:
    payload = {
        "conditions": ["A", "B", "C"],
        "subject_ids": [f"s{i}" for i in range(12)],
        "seed": 123,
    }
    first = client.post("/experiments", json=payload).json()
    second = client.post("/experiments", json=payload).json()
    assert first["assignments"] == second["assignments"]
    # 逐周期揭盲重建的序列也完全一致（确定性分配）
    first_seqs = [
        reveal_sequence(client, first["experiment_id"], s, 3, "first-")
        for s in payload["subject_ids"]
    ]
    second_seqs = [
        reveal_sequence(client, second["experiment_id"], s, 3, "second-")
        for s in payload["subject_ids"]
    ]
    assert first_seqs == second_seqs


def test_subject_count_not_multiple_returns_422(client: TestClient) -> None:
    resp = client.post(
        "/experiments",
        json={
            "conditions": ["A", "B", "C"],
            "subject_ids": [f"s{i}" for i in range(7)],
            "seed": 1,
        },
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["group_size"] == 6
    assert detail["subject_count"] == 7
    assert detail["nearest_acceptable"] == [6, 12]


def test_subject_count_below_one_group_returns_422(client: TestClient) -> None:
    resp = client.post(
        "/experiments",
        json={
            "conditions": ["A", "B", "C", "D"],
            "subject_ids": ["s1", "s2"],
            "seed": 1,
        },
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["nearest_acceptable"] == [4]


def test_duplicate_conditions_rejected(client: TestClient) -> None:
    resp = client.post(
        "/experiments",
        json={
            "conditions": ["A", "A", "B"],
            "subject_ids": [f"s{i}" for i in range(6)],
            "seed": 1,
        },
    )
    assert resp.status_code == 422


def test_duplicate_subjects_rejected(client: TestClient) -> None:
    resp = client.post(
        "/experiments",
        json={
            "conditions": ["A", "B", "C"],
            "subject_ids": ["s1", "s1", "s2", "s3", "s4", "s5"],
            "seed": 1,
        },
    )
    assert resp.status_code == 422


def test_too_few_conditions_rejected(client: TestClient) -> None:
    resp = client.post(
        "/experiments",
        json={"conditions": ["A"], "subject_ids": ["s1"], "seed": 1},
    )
    assert resp.status_code == 422


def test_get_experiment_does_not_leak_sequence(client: TestClient) -> None:
    created, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], seed=5
    )
    experiment_id = created["experiment_id"]

    resp = client.get(f"/experiments/{experiment_id}")
    assert resp.status_code == 200
    assert resp.json() == created
    assert_no_sequence_leak(resp.json())

    listing = client.get("/experiments").json()
    entry = next(e for e in listing if e["experiment_id"] == experiment_id)
    assert entry["num_periods"] == 2
    assert_no_sequence_leak(entry)
    assert "assignments" not in entry


def test_get_unknown_experiment_returns_404(client: TestClient) -> None:
    assert client.get("/experiments/nope").status_code == 404


# ---------- 按周期受控揭盲 ----------

def test_advance_reveals_one_period_and_progresses(client: TestClient) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B", "C"], subject_ids=[f"s{i}" for i in range(6)]
    )
    eid = body["experiment_id"]

    progress = client.get(f"/experiments/{eid}/subjects/s0/progress").json()
    assert progress["next_period"] == 1
    assert progress["num_periods"] == 3
    assert progress["completed"] is False
    assert progress["reveals"] == []

    first = client.post(
        f"/experiments/{eid}/advance",
        json={"subject_id": "s0", "expected_period": 1, "request_id": "r1"},
    )
    assert first.status_code == 200
    first_body = first.json()
    assert first_body["period"] == 1
    assert first_body["condition"] in {"A", "B", "C"}
    assert first_body["request_id"] == "r1"
    assert "revealed_at" in first_body

    progress = client.get(f"/experiments/{eid}/subjects/s0/progress").json()
    assert progress["next_period"] == 2
    assert progress["reveals"] == [
        {
            "period": 1,
            "condition": first_body["condition"],
            "revealed_at": first_body["revealed_at"],
        }
    ]
    assert_no_sequence_leak(progress)


def test_stale_or_skipped_period_returns_409_and_reveals_nothing(
    client: TestClient,
) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], seed=3
    )
    eid = body["experiment_id"]

    # 跳过周期 1 直接请求周期 2
    resp = client.post(
        f"/experiments/{eid}/advance",
        json={"subject_id": "s1", "expected_period": 2, "request_id": "skip"},
    )
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["reason"] == "period_mismatch"
    assert detail["next_period"] == 1
    assert "condition" not in detail

    # 失败不推进、不留幂等记录：同一 request_id 在正确周期上仍可首次成功
    ok = client.post(
        f"/experiments/{eid}/advance",
        json={"subject_id": "s1", "expected_period": 1, "request_id": "skip"},
    )
    assert ok.status_code == 200

    # 周期 1 已揭盲后，过期请求周期 1 再次冲突
    stale = client.post(
        f"/experiments/{eid}/advance",
        json={"subject_id": "s1", "expected_period": 1, "request_id": "late"},
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["next_period"] == 2

    progress = client.get(f"/experiments/{eid}/subjects/s1/progress").json()
    assert progress["next_period"] == 2
    assert len(progress["reveals"]) == 1


def test_same_request_id_retry_is_idempotent(client: TestClient) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], seed=9
    )
    eid = body["experiment_id"]
    payload = {
        "subject_id": "s1",
        "expected_period": 1,
        "request_id": "idem-1",
    }

    first = client.post(f"/experiments/{eid}/advance", json=payload)
    assert first.status_code == 200
    first_body = first.json()

    for _ in range(3):
        retry = client.post(f"/experiments/{eid}/advance", json=payload)
        assert retry.status_code == 200
        assert retry.json() == first_body

    # 重试不重复推进
    progress = client.get(f"/experiments/{eid}/subjects/s1/progress").json()
    assert progress["next_period"] == 2
    assert len(progress["reveals"]) == 1

    # 推进到周期 2 后，用旧 request_id 重放仍返回首次（周期 1）结果
    second = client.post(
        f"/experiments/{eid}/advance",
        json={"subject_id": "s1", "expected_period": 2, "request_id": "idem-2"},
    )
    assert second.status_code == 200
    assert second.json()["period"] == 2

    replay = client.post(f"/experiments/{eid}/advance", json=payload)
    assert replay.status_code == 200
    assert replay.json() == first_body


def test_request_id_reused_with_different_payload_returns_409(
    client: TestClient,
) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], seed=4
    )
    eid = body["experiment_id"]

    first = client.post(
        f"/experiments/{eid}/advance",
        json={"subject_id": "s1", "expected_period": 1, "request_id": "shared"},
    )
    assert first.status_code == 200

    # 同一 request_id 绑定不同受试者
    other_subject = client.post(
        f"/experiments/{eid}/advance",
        json={"subject_id": "s2", "expected_period": 1, "request_id": "shared"},
    )
    assert other_subject.status_code == 409
    assert other_subject.json()["detail"]["reason"] == "request_id_conflict"

    # s2 未因此被推进或揭盲
    progress = client.get(f"/experiments/{eid}/subjects/s2/progress").json()
    assert progress["next_period"] == 1
    assert progress["reveals"] == []


def test_concurrent_advance_has_single_winner(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "conc.db"))
    with TestClient(app) as setup:
        created = setup.post(
            "/experiments",
            json={
                "conditions": ["A", "B"],
                "subject_ids": ["s1", "s2"],
                "seed": 11,
            },
        ).json()
    eid = created["experiment_id"]

    n_threads = 8
    barrier = threading.Barrier(n_threads)
    statuses: list[int] = []
    statuses_lock = threading.Lock()

    def worker(i: int) -> None:
        # 每个线程使用独立 client（独立门户线程），共享同一个 SQLite 文件
        with TestClient(app) as local_client:
            barrier.wait()
            resp = local_client.post(
                f"/experiments/{eid}/advance",
                json={
                    "subject_id": "s1",
                    "expected_period": 1,
                    "request_id": f"race-{i}",
                },
            )
            with statuses_lock:
                statuses.append(resp.status_code)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert statuses.count(200) == 1
    assert statuses.count(409) == n_threads - 1

    with TestClient(app) as verify:
        progress = verify.get(
            f"/experiments/{eid}/subjects/s1/progress"
        ).json()
    assert progress["next_period"] == 2
    assert len(progress["reveals"]) == 1


def test_advance_after_final_period_returns_409(client: TestClient) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], seed=8
    )
    eid = body["experiment_id"]

    sequence = reveal_sequence(client, eid, "s1", 2)
    assert len(sequence) == 2

    done = client.post(
        f"/experiments/{eid}/advance",
        json={"subject_id": "s1", "expected_period": 3, "request_id": "over"},
    )
    assert done.status_code == 409
    assert done.json()["detail"]["reason"] == "experiment_complete"
    assert done.json()["detail"]["next_period"] == 3

    progress = client.get(f"/experiments/{eid}/subjects/s1/progress").json()
    assert progress["completed"] is True
    assert progress["next_period"] == 3
    assert [r["period"] for r in progress["reveals"]] == [1, 2]


def test_advance_unknown_experiment_or_subject_returns_404(
    client: TestClient,
) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], seed=6
    )
    eid = body["experiment_id"]
    payload = {
        "subject_id": "ghost",
        "expected_period": 1,
        "request_id": "x",
    }

    assert client.post("/experiments/missing/advance", json=payload).status_code == 404
    assert client.post(f"/experiments/{eid}/advance", json=payload).status_code == 404
    assert client.get(f"/experiments/{eid}/subjects/ghost/progress").status_code == 404


def test_invalid_advance_payload_rejected(client: TestClient) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], seed=2
    )
    eid = body["experiment_id"]

    resp = client.post(
        f"/experiments/{eid}/advance",
        json={"subject_id": "s1", "expected_period": 0, "request_id": "r"},
    )
    assert resp.status_code == 422

    resp = client.post(
        f"/experiments/{eid}/advance",
        json={"subject_id": "s1", "expected_period": 1, "request_id": ""},
    )
    assert resp.status_code == 422


# ---------- 创建实验接收 washout_seconds ----------

def test_create_experiment_persists_washout_seconds(client: TestClient) -> None:
    body, _ = create_experiment(
        client,
        conditions=["A", "B"],
        subject_ids=["s1", "s2"],
        washout_seconds=3600,
    )
    assert body["washout_seconds"] == 3600

    detail = client.get(f"/experiments/{body['experiment_id']}").json()
    assert detail["washout_seconds"] == 3600

    listing = client.get("/experiments").json()
    entry = next(e for e in listing if e["experiment_id"] == body["experiment_id"])
    assert entry["washout_seconds"] == 3600


def test_washout_seconds_defaults_to_zero(client: TestClient) -> None:
    # 不传 washout_seconds 时按 0 处理，且响应包含该字段
    resp = client.post(
        "/experiments",
        json={
            "conditions": ["A", "B"],
            "subject_ids": ["s1", "s2"],
            "seed": 1,
        },
    )
    assert resp.status_code == 201
    assert resp.json()["washout_seconds"] == 0


def test_negative_washout_seconds_rejected(client: TestClient) -> None:
    resp = client.post(
        "/experiments",
        json={
            "conditions": ["A", "B"],
            "subject_ids": ["s1", "s2"],
            "seed": 1,
            "washout_seconds": -1,
        },
    )
    assert resp.status_code == 422


# ---------- 周期完成登记 ----------

def test_complete_registers_completed_at(client: TestClient, fake_clock: FakeClock) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"]
    )
    eid = body["experiment_id"]

    # 尚无揭盲周期时不能完成
    early = client.post(
        f"/experiments/{eid}/complete",
        json={"subject_id": "s1", "expected_period": 1, "request_id": "c-early"},
    )
    assert early.status_code == 409
    assert early.json()["detail"]["reason"] == "no_revealed_period"
    assert early.json()["detail"]["current_period"] == 0

    # 揭盲周期 1 后登记完成
    assert client.post(
        f"/experiments/{eid}/advance",
        json={"subject_id": "s1", "expected_period": 1, "request_id": "a1"},
    ).status_code == 200

    result = complete_period(client, eid, "s1", 1, "c1")
    assert result["period"] == 1
    assert result["request_id"] == "c1"
    assert result["completed_at"] == fake_clock().isoformat()

    progress = client.get(f"/experiments/{eid}/subjects/s1/progress").json()
    assert progress["completions"] == [
        {"period": 1, "completed_at": result["completed_at"]}
    ]
    # 完成记录不包含任何条件信息
    assert "condition" not in progress["completions"][0]


def test_complete_is_idempotent_same_request_id(
    client: TestClient, fake_clock: FakeClock
) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"]
    )
    eid = body["experiment_id"]
    assert client.post(
        f"/experiments/{eid}/advance",
        json={"subject_id": "s1", "expected_period": 1, "request_id": "a1"},
    ).status_code == 200

    payload = {"subject_id": "s1", "expected_period": 1, "request_id": "c1"}
    first = client.post(f"/experiments/{eid}/complete", json=payload)
    assert first.status_code == 200
    first_body = first.json()

    # 即使服务端时钟推进、且后续周期已经走完，重放仍返回首次结果
    fake_clock.advance(9999)
    assert client.post(
        f"/experiments/{eid}/advance",
        json={"subject_id": "s1", "expected_period": 2, "request_id": "a2"},
    ).status_code == 200
    assert complete_period(client, eid, "s1", 2, "c2")["period"] == 2

    replay = client.post(f"/experiments/{eid}/complete", json=payload)
    assert replay.status_code == 200
    assert replay.json() == first_body

    progress = client.get(f"/experiments/{eid}/subjects/s1/progress").json()
    assert [c["period"] for c in progress["completions"]] == [1, 2]


def test_complete_request_id_conflict_returns_409(client: TestClient) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"]
    )
    eid = body["experiment_id"]
    for sid in ("s1", "s2"):
        assert client.post(
            f"/experiments/{eid}/advance",
            json={"subject_id": sid, "expected_period": 1, "request_id": f"a-{sid}"},
        ).status_code == 200

    assert client.post(
        f"/experiments/{eid}/complete",
        json={"subject_id": "s1", "expected_period": 1, "request_id": "shared"},
    ).status_code == 200

    # 同一 request_id 绑定不同受试者
    conflict = client.post(
        f"/experiments/{eid}/complete",
        json={"subject_id": "s2", "expected_period": 1, "request_id": "shared"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["reason"] == "request_id_conflict"

    # 绑定不同周期也冲突
    conflict2 = client.post(
        f"/experiments/{eid}/complete",
        json={"subject_id": "s1", "expected_period": 2, "request_id": "shared"},
    )
    assert conflict2.status_code == 409
    assert conflict2.json()["detail"]["reason"] == "request_id_conflict"


def test_complete_out_of_order_returns_409(
    client: TestClient, fake_clock: FakeClock
) -> None:
    body, _ = create_experiment(
        client,
        conditions=["A", "B", "C"],
        subject_ids=[f"s{i}" for i in range(6)],
        washout_seconds=0,
    )
    eid = body["experiment_id"]

    # 超前：周期 1 尚未揭盲
    resp = client.post(
        f"/experiments/{eid}/complete",
        json={"subject_id": "s0", "expected_period": 1, "request_id": "x"},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "no_revealed_period"

    assert client.post(
        f"/experiments/{eid}/advance",
        json={"subject_id": "s0", "expected_period": 1, "request_id": "a1"},
    ).status_code == 200

    # 超前：周期 2 尚未揭盲
    ahead = client.post(
        f"/experiments/{eid}/complete",
        json={"subject_id": "s0", "expected_period": 2, "request_id": "ahead"},
    )
    assert ahead.status_code == 409
    assert ahead.json()["detail"]["reason"] == "period_not_revealed"
    assert ahead.json()["detail"]["current_period"] == 1

    assert complete_period(client, eid, "s0", 1, "c1")["period"] == 1

    # 重复完成周期 1
    duplicate = client.post(
        f"/experiments/{eid}/complete",
        json={"subject_id": "s0", "expected_period": 1, "request_id": "dup"},
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"]["reason"] == "period_already_completed"

    # 推进并揭盲周期 2 后，落后 / 超前均拒绝
    assert client.post(
        f"/experiments/{eid}/advance",
        json={"subject_id": "s0", "expected_period": 2, "request_id": "a2"},
    ).status_code == 200
    stale = client.post(
        f"/experiments/{eid}/complete",
        json={"subject_id": "s0", "expected_period": 1, "request_id": "stale"},
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["reason"] == "period_already_completed"
    too_far = client.post(
        f"/experiments/{eid}/complete",
        json={"subject_id": "s0", "expected_period": 3, "request_id": "far"},
    )
    assert too_far.status_code == 409
    assert too_far.json()["detail"]["reason"] == "period_not_revealed"
    assert too_far.json()["detail"]["current_period"] == 2

    # 只有周期 1 一条完成记录，失败路径没有写入
    progress = client.get(f"/experiments/{eid}/subjects/s0/progress").json()
    assert [c["period"] for c in progress["completions"]] == [1]


def test_complete_unknown_experiment_or_subject_returns_404(
    client: TestClient,
) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"]
    )
    eid = body["experiment_id"]
    assert client.post(
        f"/experiments/{eid}/advance",
        json={"subject_id": "s1", "expected_period": 1, "request_id": "a"},
    ).status_code == 200
    payload = {"subject_id": "ghost", "expected_period": 1, "request_id": "x"}
    assert client.post(
        "/experiments/missing/complete", json=payload
    ).status_code == 404
    assert client.post(
        f"/experiments/{eid}/complete", json=payload
    ).status_code == 404


def test_invalid_complete_payload_rejected(client: TestClient) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"]
    )
    eid = body["experiment_id"]
    assert client.post(
        f"/experiments/{eid}/complete",
        json={"subject_id": "s1", "expected_period": 0, "request_id": "r"},
    ).status_code == 422
    assert client.post(
        f"/experiments/{eid}/complete",
        json={"subject_id": "s1", "expected_period": 1, "request_id": ""},
    ).status_code == 422


# ---------- 洗脱期门禁 ----------

def _reveal(client: TestClient, eid: str, sid: str, period: int, rid: str):
    resp = client.post(
        f"/experiments/{eid}/advance",
        json={
            "subject_id": sid,
            "expected_period": period,
            "request_id": rid,
        },
    )
    return resp


def test_advance_blocked_until_completed_and_washout_elapsed(
    client: TestClient, fake_clock: FakeClock
) -> None:
    washout = 300
    body, _ = create_experiment(
        client,
        conditions=["A", "B", "C"],
        subject_ids=[f"s{i}" for i in range(6)],
        washout_seconds=washout,
    )
    eid = body["experiment_id"]

    # 周期 1 不受门禁限制
    first = _reveal(client, eid, "s0", 1, "a1")
    assert first.status_code == 200
    first_condition = first.json()["condition"]

    # 未完成周期 1：推进被拒，且不给出可凭空等待的时间
    blocked = _reveal(client, eid, "s0", 2, "a2-blocked")
    assert blocked.status_code == 409
    detail = blocked.json()["detail"]
    assert detail["reason"] == "previous_period_not_completed"
    assert detail["next_period"] == 2
    assert "retry_after_seconds" not in detail
    assert "next_available_at" not in detail
    # 绝不泄露下一周期条件
    assert "condition" not in detail

    # 登记完成
    completion = complete_period(client, eid, "s0", 1, "c1")
    completed_at = datetime.fromisoformat(completion["completed_at"])

    # 洗脱期未届满：409 + retry_after_seconds，仍不泄露下一条件
    fake_clock.advance(120)
    early = _reveal(client, eid, "s0", 2, "a2-early")
    assert early.status_code == 409
    early_detail = early.json()["detail"]
    assert early_detail["reason"] == "washout_not_elapsed"
    assert early_detail["retry_after_seconds"] == washout - 120
    expected_available = (
        completed_at + timedelta(seconds=washout)
    ).isoformat()
    assert early_detail["next_available_at"] == expected_available
    assert "condition" not in early_detail

    # 进度接口同样给出 next_available_at
    progress = client.get(f"/experiments/{eid}/subjects/s0/progress").json()
    assert progress["next_period"] == 2
    assert progress["next_available_at"] == expected_available
    assert progress["washout_seconds"] == washout

    # 精确届满：放行，揭盲周期 2（条件不由门禁响应提前泄露）
    fake_clock.advance(washout - 120)
    second = _reveal(client, eid, "s0", 2, "a2")
    assert second.status_code == 200, second.text
    assert second.json()["period"] == 2
    assert second.json()["condition"] in {"A", "B", "C"}
    # 与周期 1 是同一条件序列中的不同位置
    assert second.json()["condition"] != first_condition

    # 届满后 next_available_at 清空
    progress = client.get(f"/experiments/{eid}/subjects/s0/progress").json()
    assert progress["next_available_at"] is None

    # 周期 2 未完成，周期 3 同样被门禁拦截
    blocked3 = _reveal(client, eid, "s0", 3, "a3-blocked")
    assert blocked3.status_code == 409
    assert blocked3.json()["detail"]["reason"] == (
        "previous_period_not_completed"
    )


def test_washout_retry_after_rounds_up(
    client: TestClient, fake_clock: FakeClock
) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], washout_seconds=10
    )
    eid = body["experiment_id"]
    assert _reveal(client, eid, "s1", 1, "a1").status_code == 200
    complete_period(client, eid, "s1", 1, "c1")

    # 仅过去 0.5 秒，剩余 9.5 秒 -> 向上取整为 10
    fake_clock.advance(0.5)
    resp = _reveal(client, eid, "s1", 2, "a2")
    assert resp.status_code == 409
    assert resp.json()["detail"]["retry_after_seconds"] == 10


def test_blocked_advance_does_not_consume_request_id(
    client: TestClient, fake_clock: FakeClock
) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], washout_seconds=60
    )
    eid = body["experiment_id"]
    assert _reveal(client, eid, "s1", 1, "a1").status_code == 200

    # 门禁拦截不留幂等记录：届满后同一 request_id 可首次成功
    blocked = _reveal(client, eid, "s1", 2, "same-rid")
    assert blocked.status_code == 409
    complete_period(client, eid, "s1", 1, "c1")
    fake_clock.advance(60)
    ok = _reveal(client, eid, "s1", 2, "same-rid")
    assert ok.status_code == 200

    # 重放仍返回首次结果
    assert _reveal(client, eid, "s1", 2, "same-rid").json() == ok.json()


def test_washout_zero_keeps_legacy_flow(
    client: TestClient, fake_clock: FakeClock
) -> None:
    # washout_seconds = 0：不强制完成登记，推进行为与旧版一致
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], washout_seconds=0
    )
    eid = body["experiment_id"]
    assert _reveal(client, eid, "s1", 1, "a1").status_code == 200
    assert _reveal(client, eid, "s1", 2, "a2").status_code == 200
    progress = client.get(f"/experiments/{eid}/subjects/s1/progress").json()
    assert progress["completions"] == []
    assert progress["next_available_at"] is None
    assert progress["completed"] is True

    # 实验结束后仍可补登记每个周期的完成时间
    c1 = complete_period(client, eid, "s1", 1, "c1")
    c2 = complete_period(client, eid, "s1", 2, "c2")
    assert [c["period"] for c in (c1, c2)] == [1, 2]
    progress = client.get(f"/experiments/{eid}/subjects/s1/progress").json()
    assert progress["next_available_at"] is None
    assert progress["completed"] is True


def test_progress_next_available_at_after_completion(
    client: TestClient, fake_clock: FakeClock
) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], washout_seconds=60
    )
    eid = body["experiment_id"]
    progress = client.get(f"/experiments/{eid}/subjects/s1/progress").json()
    assert progress["next_available_at"] is None
    assert progress["completions"] == []
    assert progress["washout_seconds"] == 60

    assert _reveal(client, eid, "s1", 1, "a1").status_code == 200
    # 揭盲但未完成：没有可等待的时间点
    assert (
        client.get(f"/experiments/{eid}/subjects/s1/progress").json()[
            "next_available_at"
        ]
        is None
    )

    completion = complete_period(client, eid, "s1", 1, "c1")
    available = (
        datetime.fromisoformat(completion["completed_at"])
        + timedelta(seconds=60)
    ).isoformat()
    progress = client.get(f"/experiments/{eid}/subjects/s1/progress").json()
    assert progress["next_available_at"] == available

    fake_clock.advance(60)
    assert (
        client.get(f"/experiments/{eid}/subjects/s1/progress").json()[
            "next_available_at"
        ]
        is None
    )


def test_concurrent_complete_has_single_winner(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "conc-complete.db"))
    with TestClient(app) as setup:
        created = setup.post(
            "/experiments",
            json={
                "conditions": ["A", "B"],
                "subject_ids": ["s1", "s2"],
                "seed": 11,
                "washout_seconds": 0,
            },
        ).json()
        assert setup.post(
            f"/experiments/{created['experiment_id']}/advance",
            json={
                "subject_id": "s1",
                "expected_period": 1,
                "request_id": "a1",
            },
        ).status_code == 200
    eid = created["experiment_id"]

    n_threads = 8
    barrier = threading.Barrier(n_threads)
    statuses: list[int] = []
    statuses_lock = threading.Lock()

    def worker(i: int) -> None:
        with TestClient(app) as local_client:
            barrier.wait()
            resp = local_client.post(
                f"/experiments/{eid}/complete",
                json={
                    "subject_id": "s1",
                    "expected_period": 1,
                    "request_id": f"crace-{i}",
                },
            )
            with statuses_lock:
                statuses.append(resp.status_code)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert statuses.count(200) == 1
    assert statuses.count(409) == n_threads - 1

    with TestClient(app) as verify:
        progress = verify.get(
            f"/experiments/{eid}/subjects/s1/progress"
        ).json()
    assert len(progress["completions"]) == 1
    assert progress["completions"][0]["period"] == 1


def test_concurrent_advance_after_gate_opens_single_winner(
    tmp_path, monkeypatch
) -> None:
    """门禁刚放行时并发推进：只有一个请求揭盲，其余 409。"""
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "conc-gate.db"))
    fixed = datetime(2026, 9, 21, 8, 0, tzinfo=timezone.utc)

    class Clock:
        def __init__(self) -> None:
            self.value = fixed

        def __call__(self) -> datetime:
            return self.value

    clock = Clock()
    monkeypatch.setattr(main, "server_now", clock)

    with TestClient(app) as setup:
        created = setup.post(
            "/experiments",
            json={
                "conditions": ["A", "B"],
                "subject_ids": ["s1", "s2"],
                "seed": 13,
                "washout_seconds": 60,
            },
        ).json()
        eid = created["experiment_id"]
        assert setup.post(
            f"/experiments/{eid}/advance",
            json={
                "subject_id": "s1",
                "expected_period": 1,
                "request_id": "a1",
            },
        ).status_code == 200
        assert setup.post(
            f"/experiments/{eid}/complete",
            json={
                "subject_id": "s1",
                "expected_period": 1,
                "request_id": "c1",
            },
        ).status_code == 200

    # 时间跳过洗脱期，所有线程同时推进周期 2
    clock.value = fixed + timedelta(seconds=60)
    n_threads = 6
    barrier = threading.Barrier(n_threads)
    statuses: list[int] = []
    statuses_lock = threading.Lock()

    def worker(i: int) -> None:
        with TestClient(app) as local_client:
            barrier.wait()
            resp = local_client.post(
                f"/experiments/{eid}/advance",
                json={
                    "subject_id": "s1",
                    "expected_period": 2,
                    "request_id": f"gate-{i}",
                },
            )
            with statuses_lock:
                statuses.append(resp.status_code)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert statuses.count(200) == 1
    assert statuses.count(409) == n_threads - 1


# ---------- 旧库启动时自动补齐表结构 ----------

def test_startup_migrates_legacy_database(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "legacy.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))

    # 模拟旧版本数据库：只有 experiments / assignments 两张表
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE experiments (
                id TEXT PRIMARY KEY,
                conditions TEXT NOT NULL,
                seed INTEGER NOT NULL,
                group_size INTEGER NOT NULL,
                num_subjects INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE assignments (
                experiment_id TEXT NOT NULL REFERENCES experiments(id),
                subject_id TEXT NOT NULL,
                sequence_index INTEGER NOT NULL,
                sequence TEXT NOT NULL,
                PRIMARY KEY (experiment_id, subject_id)
            );
            """
        )
        conn.execute(
            "INSERT INTO experiments "
            "(id, conditions, seed, group_size, num_subjects, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("legacy-1", '["A","B","C"]', 1, 6, 6, "2026-01-01T00:00:00+00:00"),
        )
        conn.execute(
            "INSERT INTO assignments "
            "(experiment_id, subject_id, sequence_index, sequence) "
            "VALUES (?, ?, ?, ?)",
            ("legacy-1", "s0", 0, '["A","C","B"]'),
        )

    # 应用启动（lifespan）自动补齐表结构，旧数据仍可查询与揭盲
    with TestClient(app) as migrated:
        detail = migrated.get("/experiments/legacy-1").json()
        assert detail["num_subjects"] == 6
        assert detail["washout_seconds"] == 0  # 旧实验按 0 秒洗脱处理
        assert_no_sequence_leak(detail)

        progress = migrated.get(
            "/experiments/legacy-1/subjects/s0/progress"
        ).json()
        assert progress["next_period"] == 1
        assert progress["num_periods"] == 3
        assert progress["washout_seconds"] == 0
        assert progress["reveals"] == []
        assert progress["completions"] == []
        assert progress["next_available_at"] is None

        resp = migrated.post(
            "/experiments/legacy-1/advance",
            json={
                "subject_id": "s0",
                "expected_period": 1,
                "request_id": "boot-1",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["condition"] == "A"

        # 旧实验 washout=0：不登记完成也可继续推进（旧版行为不变）
        resp2 = migrated.post(
            "/experiments/legacy-1/advance",
            json={
                "subject_id": "s0",
                "expected_period": 2,
                "request_id": "boot-2",
            },
        )
        assert resp2.status_code == 200
        assert resp2.json()["condition"] == "C"


def test_startup_migrates_v11_database_with_progress(tmp_path, monkeypatch) -> None:
    """上一版本（已含揭盲表、无 washout 列）的库：迁移后进行中的旧实验可继续。"""
    db_path = tmp_path / "legacy-v11.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))

    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE experiments (
                id TEXT PRIMARY KEY,
                conditions TEXT NOT NULL,
                seed INTEGER NOT NULL,
                group_size INTEGER NOT NULL,
                num_subjects INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE assignments (
                experiment_id TEXT NOT NULL REFERENCES experiments(id),
                subject_id TEXT NOT NULL,
                sequence_index INTEGER NOT NULL,
                sequence TEXT NOT NULL,
                PRIMARY KEY (experiment_id, subject_id)
            );
            CREATE TABLE subject_progress (
                experiment_id TEXT NOT NULL,
                subject_id TEXT NOT NULL,
                next_period INTEGER NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (experiment_id, subject_id)
            );
            CREATE TABLE unblindings (
                experiment_id TEXT NOT NULL,
                subject_id TEXT NOT NULL,
                period INTEGER NOT NULL,
                condition TEXT NOT NULL,
                request_id TEXT NOT NULL,
                revealed_at TEXT NOT NULL,
                PRIMARY KEY (experiment_id, subject_id, period)
            );
            CREATE TABLE advance_requests (
                experiment_id TEXT NOT NULL,
                request_id TEXT NOT NULL,
                subject_id TEXT NOT NULL,
                expected_period INTEGER NOT NULL,
                period INTEGER NOT NULL,
                condition TEXT NOT NULL,
                revealed_at TEXT NOT NULL,
                PRIMARY KEY (experiment_id, request_id)
            );
            """
        )
        conn.execute(
            "INSERT INTO experiments "
            "(id, conditions, seed, group_size, num_subjects, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("v11-1", '["A","B"]', 2, 2, 2, "2026-01-01T00:00:00+00:00"),
        )
        conn.executemany(
            "INSERT INTO assignments "
            "(experiment_id, subject_id, sequence_index, sequence) "
            "VALUES (?, ?, ?, ?)",
            [
                ("v11-1", "s1", 0, '["A","B"]'),
                ("v11-1", "s2", 1, '["B","A"]'),
            ],
        )
        # s1 已在旧版本中揭盲周期 1、推进到周期 2，但从未有“完成”概念
        conn.execute(
            "INSERT INTO unblindings "
            "(experiment_id, subject_id, period, condition, request_id, revealed_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("v11-1", "s1", 1, "A", "old-rid", "2026-01-02T00:00:00+00:00"),
        )
        conn.execute(
            "INSERT INTO subject_progress "
            "(experiment_id, subject_id, next_period, updated_at) "
            "VALUES (?, ?, ?, ?)",
            ("v11-1", "s1", 2, "2026-01-02T00:00:00+00:00"),
        )
        conn.execute(
            "INSERT INTO advance_requests "
            "(experiment_id, request_id, subject_id, expected_period, "
            "period, condition, revealed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("v11-1", "old-rid", "s1", 1, 1, "A", "2026-01-02T00:00:00+00:00"),
        )

    with TestClient(app) as migrated:
        # 迁移补齐 washout_seconds 列与完成表，旧揭盲历史保留
        progress = migrated.get(
            "/experiments/v11-1/subjects/s1/progress"
        ).json()
        assert progress["next_period"] == 2
        assert progress["washout_seconds"] == 0
        assert progress["completions"] == []
        assert progress["next_available_at"] is None
        assert progress["reveals"][0]["condition"] == "A"

        # 旧 request_id 重放仍返回原结果
        replay = migrated.post(
            "/experiments/v11-1/advance",
            json={
                "subject_id": "s1",
                "expected_period": 1,
                "request_id": "old-rid",
            },
        )
        assert replay.status_code == 200
        assert replay.json()["condition"] == "A"
        assert replay.json()["revealed_at"] == "2026-01-02T00:00:00+00:00"

        # washout=0：无需完成登记即可推进周期 2
        resp = migrated.post(
            "/experiments/v11-1/advance",
            json={
                "subject_id": "s1",
                "expected_period": 2,
                "request_id": "new-rid",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["condition"] == "B"
