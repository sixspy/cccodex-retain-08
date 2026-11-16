import sqlite3
from datetime import date, datetime

from app.db import default_deadline, write_tx
from app.services.partitions import get_partition
from app.services.policies import get_active_policy


def _parse_day(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def _today(as_of: str | None) -> date:
    return _parse_day(as_of) if as_of else date.today()


def get_open_grace(
    conn: sqlite3.Connection, partition_id: int
) -> dict | None:
    row = conn.execute(
        "SELECT * FROM grace_registrations "
        "WHERE partition_id = ? AND status = '宽限中' "
        "ORDER BY id DESC LIMIT 1",
        (partition_id,),
    ).fetchone()
    return dict(row) if row else None


def get_grace(conn: sqlite3.Connection, grace_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM grace_registrations WHERE id = ?", (grace_id,)
    ).fetchone()
    return dict(row) if row else None


def with_effective_state(row: dict, today: date) -> dict:
    """读时计算生效态：登记行状态为「宽限中」但已过截止日 → 生效态「已过期」。"""
    item = dict(row)
    effective = item["status"]
    can_block = False
    if item["status"] == "宽限中":
        deadline = _parse_day(item["grace_deadline"])
        if today > deadline:
            effective = "已过期"
        else:
            can_block = True
    item["effective_status"] = effective
    item["can_block_destroy"] = 1 if can_block else 0
    item["backfill_done"] = 1 if item.get("backfill_status") == "已补采" else 0
    return item


def list_grace(
    conn: sqlite3.Connection,
    status: str | None = None,
    as_of: str | None = None,
) -> list[dict]:
    today = _today(as_of)
    sql = (
        "SELECT g.*, p.storage_state, j.status AS judgment_status, "
        "j.decided_action FROM grace_registrations g "
        "JOIN partitions p ON p.id = g.partition_id "
        "LEFT JOIN judgments j ON j.id = g.judgment_id"
    )
    params: list[object] = []
    if status:
        sql += " WHERE g.status = ?"
        params.append(status)
    sql += " ORDER BY g.id DESC"
    rows = conn.execute(sql, params).fetchall()
    return [with_effective_state(dict(r), today) for r in rows]


def register_grace(
    conn: sqlite3.Connection,
    partition_id: int,
    reason: str = "",
    grace_days: int | None = None,
    as_of: str | None = None,
) -> dict:
    """宽限登记（串行写）。同一分区存在生效中登记则拒绝，杜绝双写互盖。"""
    today = _today(as_of)
    with write_tx(conn):
        part = get_partition(conn, partition_id)
        if not part:
            raise ValueError("分区不存在")
        if part["storage_state"] in ("已销毁", "已归档"):
            raise ValueError(f"分区已终态（{part['storage_state']}），不可登记宽限")
        existing = get_open_grace(conn, partition_id)
        if existing:
            raise ValueError(
                f"该分区已有生效中的宽限登记 #{existing['id']}，"
                f"截止 {existing['grace_deadline']}，不可重复登记"
            )

        judgment_row = conn.execute(
            "SELECT * FROM judgments WHERE partition_id = ? "
            "AND status IN ('待执行','执行中','已失败') "
            "ORDER BY id DESC LIMIT 1",
            (partition_id,),
        ).fetchone()
        judgment = dict(judgment_row) if judgment_row else None
        if judgment and judgment["decided_action"] != "销毁":
            raise ValueError(
                "仅「销毁」裁决可申请宽限保护，当前裁决动作："
                f"{judgment['decided_action']}"
            )

        if grace_days is None:
            policy = get_active_policy(conn, part["domain_code"])
            grace_days = int(policy["grace_days"]) if policy else 3
        if int(grace_days) < 1:
            raise ValueError("宽限天数必须 ≥ 1")

        deadline = default_deadline(today, int(grace_days))
        cur = conn.execute(
            "INSERT INTO grace_registrations "
            "(partition_id, judgment_id, domain_code, biz_date, reason, "
            "grace_days, grace_deadline, backfill_status, status) "
            "VALUES (?,?,?,?,?,?,?, '待补采', '宽限中')",
            (
                partition_id,
                judgment["id"] if judgment else None,
                part["domain_code"],
                part["biz_date"],
                reason.strip(),
                int(grace_days),
                deadline,
            ),
        )
        # 宽限登记与裁决态对齐：登记即把未完成裁决标注失败原因为空但受保护，
        # 状态保持「待执行」，由执行入口按登记拦截，两侧通过 judgment_id 强绑定。
        return with_effective_state(get_grace(conn, int(cur.lastrowid)), today)


def update_backfill(
    conn: sqlite3.Connection,
    grace_id: int,
    backfill_status: str,
    task_code: str = "",
    remark: str = "",
    as_of: str | None = None,
) -> dict:
    """补采任务可核：登记补采任务号与进度；标记「已补采」即自动放行宽限。"""
    today = _today(as_of)
    if backfill_status not in ("待补采", "补采中", "已补采"):
        raise ValueError(f"非法补采状态：{backfill_status}")
    with write_tx(conn):
        row = get_grace(conn, grace_id)
        if not row:
            raise ValueError("宽限登记不存在")
        if row["status"] != "宽限中":
            raise ValueError(f"宽限登记已结束（{row['status']}），不可更新补采")

        verified_at = None
        new_status = "宽限中"
        released_at = None
        note = row["release_note"]
        if backfill_status == "已补采":
            verified_at = today.isoformat()
            new_status = "已放行"
            released_at = verified_at
            note = "补采入库核验完成，提前放行"
        conn.execute(
            "UPDATE grace_registrations SET backfill_status = ?, "
            "backfill_task_code = ?, backfill_verified_at = COALESCE(?, backfill_verified_at), "
            "backfill_remark = ?, status = ?, released_at = COALESCE(?, released_at), "
            "release_note = ? WHERE id = ?",
            (
                backfill_status,
                task_code.strip() or row["backfill_task_code"],
                verified_at,
                remark.strip(),
                new_status,
                released_at,
                note,
                grace_id,
            ),
        )
        return with_effective_state(get_grace(conn, grace_id), today)


def close_grace(
    conn: sqlite3.Connection,
    grace_id: int,
    note: str = "人工关闭",
    as_of: str | None = None,
) -> dict:
    """人工关闭一条宽限中登记（未补采但确认不再保护）。"""
    today = _today(as_of)
    with write_tx(conn):
        row = get_grace(conn, grace_id)
        if not row:
            raise ValueError("宽限登记不存在")
        if row["status"] != "宽限中":
            raise ValueError(f"宽限登记已结束（{row['status']}），无需关闭")
        conn.execute(
            "UPDATE grace_registrations SET status = '已关闭', "
            "released_at = ?, release_note = ? WHERE id = ?",
            (today.isoformat(), note.strip() or "人工关闭", grace_id),
        )
        return with_effective_state(get_grace(conn, grace_id), today)


def assert_destroy_allowed(
    conn: sqlite3.Connection, partition_id: int, as_of: str | None = None
) -> None:
    """执行入口硬拦：宽限生效（未截止且未放行）即禁止销毁。"""
    today = _today(as_of)
    open_row = get_open_grace(conn, partition_id)
    if not open_row:
        return
    item = with_effective_state(open_row, today)
    if item["can_block_destroy"]:
        raise ValueError(
            f"宽限期内禁止提前销毁：宽限登记 #{item['id']}，"
            f"截止 {item['grace_deadline']}，"
            f"补采状态 {item['backfill_status']}"
        )


def expire_if_due(
    conn: sqlite3.Connection, grace_id: int, as_of: str | None = None
) -> dict:
    """到达或越过截止日仍未补采：登记置「已过期」，裁决恢复可执行。"""
    today = _today(as_of)
    with write_tx(conn):
        row = get_grace(conn, grace_id)
        if not row:
            raise ValueError("宽限登记不存在")
        item = with_effective_state(row, today)
        if row["status"] != "宽限中":
            return item
        if item["effective_status"] != "已过期":
            raise ValueError(
                f"尚未到宽限截止日 {row['grace_deadline']}，不可置过期"
            )
        conn.execute(
            "UPDATE grace_registrations SET status = '已过期', "
            "release_note = '宽限截止，补采未完成，恢复裁决执行' WHERE id = ?",
            (grace_id,),
        )
        return with_effective_state(get_grace(conn, grace_id), today)


def open_grace_count(conn: sqlite3.Connection, as_of: str | None = None) -> int:
    today = _today(as_of)
    rows = conn.execute(
        "SELECT id, status, grace_deadline FROM grace_registrations "
        "WHERE status = '宽限中'"
    ).fetchall()
    return sum(
        1 for r in rows if with_effective_state(dict(r), today)["can_block_destroy"]
    )
