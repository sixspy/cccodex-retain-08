import sqlite3
from datetime import date, datetime


def _parse_day(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def list_partitions(
    conn: sqlite3.Connection,
    domain_code: str | None = None,
    storage_state: str | None = None,
    as_of: str | None = None,
) -> list[dict]:
    """实物分区：关联生效中宽限登记，带出截止日、补采状态与可否销毁。"""
    today = (
        datetime.strptime(as_of, "%Y-%m-%d").date() if as_of else date.today()
    )
    clauses: list[str] = []
    params: list[object] = []
    if domain_code:
        clauses.append("p.domain_code = ?")
        params.append(domain_code)
    if storage_state:
        clauses.append("p.storage_state = ?")
        params.append(storage_state)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(
        "SELECT p.id, p.domain_code, p.biz_date, p.row_count, p.storage_state, "
        "p.archive_uri, p.created_at, "
        "g.id AS grace_id, g.grace_deadline, g.backfill_status, "
        "g.backfill_task_code, g.status AS grace_status "
        "FROM partitions p "
        "LEFT JOIN grace_registrations g "
        "  ON g.id = (SELECT id FROM grace_registrations "
        "             WHERE partition_id = p.id "
        "             ORDER BY id DESC LIMIT 1)"
        + where
        + " ORDER BY p.domain_code, p.biz_date",
        params,
    ).fetchall()
    items: list[dict] = []
    for r in rows:
        item = dict(r)
        effective = None
        can_destroy = 1 if item["storage_state"] == "在线" else 0
        if item["grace_id"] is not None and item["grace_status"] == "宽限中":
            if today > _parse_day(item["grace_deadline"]):
                effective = "已过期"
            else:
                effective = "宽限中"
                can_destroy = 0
        elif item["grace_id"] is not None:
            effective = item["grace_status"]
        item["grace_effective_status"] = effective
        item["can_destroy"] = can_destroy
        items.append(item)
    return items


def get_partition(conn: sqlite3.Connection, partition_id: int) -> dict | None:
    row = conn.execute(
        "SELECT id, domain_code, biz_date, row_count, storage_state, "
        "archive_uri, created_at FROM partitions WHERE id = ?",
        (partition_id,),
    ).fetchone()
    return dict(row) if row else None
