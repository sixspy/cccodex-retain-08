"""宽限登记 / 补采 / 提前销毁拦截 专项测试。

固定基准日 2026-11-16，全程走 API 或服务层，不依赖真实时钟。
"""
import sqlite3

import pytest

from app.db import connect, get_db
from app.services.grace import register_grace


BASE = "2026-11-16"


def _destroy_partition_id(authed) -> tuple[int, int]:
    """跑扫描并取一条「销毁」裁决，返回 (judgment_id, partition_id)。"""
    authed.post("/api/scan-jobs", json={"as_of_date": BASE})
    items = authed.get("/api/judgments").json()["items"]
    target = next(j for j in items if j["decided_action"] == "销毁")
    return target["id"], target["partition_id"]


def test_policy_carries_grace_days(authed):
    policies = authed.get("/api/policies").json()["items"]
    events = next(p for p in policies if p["domain_code"] == "events")
    assert events["grace_days"] >= 1
    assert all(p["grace_days"] >= 1 for p in policies)


def test_grace_register_blocks_premature_destroy(authed):
    judgment_id, partition_id = _destroy_partition_id(authed)

    reg = authed.post(
        "/api/grace",
        json={"partition_id": partition_id, "reason": "补采任务 BF-001 未完"},
    )
    assert reg.status_code == 200
    item = reg.json()["item"]
    assert item["status"] == "宽限中"
    assert item["effective_status"] == "宽限中"
    assert item["grace_deadline"] > BASE
    assert item["backfill_status"] == "待补采"

    # 提前毁即拦：执行入口 409，分区一行不动
    blocked = authed.post(f"/api/judgments/{judgment_id}/execute")
    assert blocked.status_code == 409
    assert "宽限" in blocked.json()["detail"]

    part = next(
        p for p in authed.get("/api/partitions").json()["items"]
        if p["id"] == partition_id
    )
    assert part["storage_state"] == "在线"
    assert part["can_destroy"] == 0
    assert part["grace_effective_status"] == "宽限中"

    # 裁决队列同步显示拦截与截止
    j = next(
        x for x in authed.get("/api/judgments").json()["items"]
        if x["id"] == judgment_id
    )
    assert j["can_execute"] == 0
    assert j["grace_can_block"] == 1
    assert j["grace_deadline"] == item["grace_deadline"]
    assert "禁止提前销毁" in j["block_reason"]


def test_duplicate_grace_registration_rejected(authed):
    _, partition_id = _destroy_partition_id(authed)
    first = authed.post("/api/grace", json={"partition_id": partition_id})
    assert first.status_code == 200
    second = authed.post("/api/grace", json={"partition_id": partition_id})
    assert second.status_code == 409
    assert "已有生效中的宽限登记" in second.json()["detail"]


def test_backfill_verify_releases_and_allows_destroy(authed):
    judgment_id, partition_id = _destroy_partition_id(authed)
    grace_id = authed.post(
        "/api/grace",
        json={"partition_id": partition_id, "reason": "BF-002"},
    ).json()["item"]["id"]

    # 补采中：任务号可核，仍拦截
    mid = authed.post(
        f"/api/grace/{grace_id}/backfill",
        json={"backfill_status": "补采中", "backfill_task_code": "BF-002"},
    )
    assert mid.status_code == 200
    assert mid.json()["item"]["backfill_task_code"] == "BF-002"
    assert (
        authed.post(f"/api/judgments/{judgment_id}/execute").status_code == 409
    )

    # 核验完成：自动放行，裁决恢复可执行
    done = authed.post(
        f"/api/grace/{grace_id}/backfill",
        json={"backfill_status": "已补采", "backfill_task_code": "BF-002",
              "remark": "补采入库行数核对一致"},
    )
    assert done.status_code == 200
    released = done.json()["item"]
    assert released["status"] == "已放行"
    assert released["backfill_verified_at"] == BASE
    assert released["can_block_destroy"] == 0

    exe = authed.post(f"/api/judgments/{judgment_id}/execute")
    assert exe.status_code == 200
    assert exe.json()["run"]["result_state"] == "已销毁"


def test_grace_expiry_restores_execution(authed):
    judgment_id, partition_id = _destroy_partition_id(authed)
    grace_id = authed.post(
        "/api/grace",
        json={"partition_id": partition_id, "grace_days": 2},
    ).json()["item"]["id"]
    deadline = authed.get("/api/grace").json()["items"][0]["grace_deadline"]

    # 截止日内拦截
    assert authed.post(f"/api/judgments/{judgment_id}/execute").status_code == 409

    # 未到截止不能置过期
    early = authed.post(f"/api/grace/{grace_id}/expire")
    assert early.status_code == 409

    # 越过截止日：登记置过期，销毁放行
    after = "2026-11-20"
    expired = authed.post(
        f"/api/grace/{grace_id}/expire", params={"as_of_date": after}
    )
    assert expired.status_code == 200
    assert expired.json()["item"]["status"] == "已过期"
    assert deadline < after

    exe = authed.post(
        f"/api/judgments/{judgment_id}/execute",
        params={"as_of_date": after},
    )
    assert exe.status_code == 200
    assert exe.json()["run"]["result_state"] == "已销毁"


def test_lazy_expiry_blocks_reregistration_until_settled(authed):
    """读时已过期但行仍为宽限中：不可再登记，先置过期再恢复。"""
    _, partition_id = _destroy_partition_id(authed)
    grace_id = authed.post(
        "/api/grace",
        json={"partition_id": partition_id, "grace_days": 1},
    ).json()["item"]["id"]
    after = "2026-11-19"

    # 过期后登记仍占着唯一索引 → 重复登记被拒
    dup = authed.post(
        "/api/grace",
        json={"partition_id": partition_id, "as_of_date": after},
    )
    assert dup.status_code == 409

    authed.post(f"/api/grace/{grace_id}/expire", params={"as_of_date": after})
    again = authed.post(
        "/api/grace",
        json={"partition_id": partition_id, "as_of_date": after},
    )
    assert again.status_code == 200


def test_archive_judgment_not_eligible_for_grace(authed):
    authed.post("/api/scan-jobs", json={"as_of_date": BASE})
    archive = next(
        j for j in authed.get("/api/judgments").json()["items"]
        if j["decided_action"] == "归档"
    )
    resp = authed.post(
        "/api/grace", json={"partition_id": archive["partition_id"]}
    )
    assert resp.status_code == 409
    assert "仅「销毁」裁决可申请宽限" in resp.json()["detail"]


def test_scan_list_grace_blocked_count(authed):
    judgment_id, partition_id = _destroy_partition_id(authed)
    authed.post("/api/grace", json={"partition_id": partition_id})

    jobs = authed.get("/api/scan-jobs").json()["items"]
    assert jobs[0]["grace_blocked_count"] >= 1

    # 放行后计数回落
    grace_id = authed.get("/api/grace").json()["items"][0]["id"]
    authed.post(
        f"/api/grace/{grace_id}/backfill",
        json={"backfill_status": "已补采"},
    )
    jobs_after = authed.get("/api/scan-jobs").json()["items"]
    assert jobs_after[0]["grace_blocked_count"] == jobs[0]["grace_blocked_count"] - 1


def test_reconcile_counts_open_grace(authed):
    _, partition_id = _destroy_partition_id(authed)
    authed.post("/api/grace", json={"partition_id": partition_id})
    report = authed.post(
        "/api/reconcile-reports", json={"as_of_date": BASE}
    ).json()["report"]
    assert report["grace_open_count"] >= 1
    assert "生效宽限" in report["summary"]


def test_grace_close_allows_destroy(authed):
    judgment_id, partition_id = _destroy_partition_id(authed)
    grace_id = authed.post(
        "/api/grace", json={"partition_id": partition_id}
    ).json()["item"]["id"]
    assert authed.post(f"/api/judgments/{judgment_id}/execute").status_code == 409

    closed = authed.post(
        f"/api/grace/{grace_id}/close", json={"note": "确认无需补采"}
    )
    assert closed.json()["item"]["status"] == "已关闭"
    assert authed.post(f"/api/judgments/{judgment_id}/execute").status_code == 200


def test_missing_lock_dual_write_clobbers_at_unique_index(tmp_path, monkeypatch):
    """缺锁旁路双写：两条连接并发登记同一分区，唯一索引必须拦下第二条。"""
    db = tmp_path / "race.db"
    monkeypatch.setenv("DATABASE_PATH", str(db))
    from app.db import init_db

    init_db(str(db))

    c1, c2 = connect(str(db)), connect(str(db))
    part_id = c1.execute(
        "SELECT id FROM partitions WHERE domain_code = 'events' LIMIT 1"
    ).fetchone()["id"]

    # 模拟「两侧同时要写」：两连接各自 BEGIN IMMEDIATE，SQLite 保留锁串行化；
    # 即便都拿到锁，部分唯一索引也拒绝同一分区第二条生效登记。
    c1.execute("BEGIN IMMEDIATE")
    with pytest.raises(sqlite3.OperationalError):
        c2.execute("BEGIN IMMEDIATE")  # 拿不到保留锁 → database is locked
    c1.execute(
        "INSERT INTO grace_registrations "
        "(partition_id, domain_code, biz_date, grace_days, grace_deadline, status) "
        "VALUES (?,?,?,2, '2026-11-18', '宽限中')",
        (part_id, "events", "2026-11-12"),
    )
    c1.execute("COMMIT")

    # 第二条连接的重复登记被唯一索引硬冲突，不会互盖
    with pytest.raises(sqlite3.IntegrityError):
        c2.execute(
            "INSERT INTO grace_registrations "
            "(partition_id, domain_code, biz_date, grace_days, grace_deadline, status) "
            "VALUES (?,?,?,2, '2026-11-19', '宽限中')",
            (part_id, "events", "2026-11-12"),
        )
    c1.close()
    c2.close()


def test_service_register_is_serialized_under_write_lock(tmp_path, monkeypatch):
    """服务层登记走串行写事务：同分区第二条明确 409 语义而非脏覆盖。"""
    db = tmp_path / "ser.db"
    monkeypatch.setenv("DATABASE_PATH", str(db))
    from app.db import init_db

    init_db(str(db))
    with get_db(str(db)) as conn:
        part_id = conn.execute(
            "SELECT id FROM partitions WHERE domain_code = 'events' LIMIT 1"
        ).fetchone()["id"]
        register_grace(conn, part_id, reason="first")
        with pytest.raises(ValueError, match="已有生效中的宽限登记"):
            register_grace(conn, part_id, reason="second")
        rows = conn.execute(
            "SELECT COUNT(*) AS c FROM grace_registrations WHERE partition_id = ?",
            (part_id,),
        ).fetchone()["c"]
        assert rows == 1


def test_grace_list_effective_state_after_deadline(authed):
    _, partition_id = _destroy_partition_id(authed)
    authed.post(
        "/api/grace",
        json={"partition_id": partition_id, "grace_days": 2},
    )
    current = authed.get("/api/grace").json()["items"][0]
    assert current["effective_status"] == "宽限中"

    later = authed.get(
        "/api/grace", params={"as_of_date": "2026-11-19"}
    ).json()["items"][0]
    assert later["effective_status"] == "已过期"
    assert later["can_block_destroy"] == 0
    # 原始登记行状态未被读操作偷偷改写
    assert later["status"] == "宽限中"
