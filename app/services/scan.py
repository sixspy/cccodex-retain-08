from datetime import date, datetime

import sqlite3

from app.db import write_tx
from app.services.grace import get_open_grace, with_effective_state
from app.services.policies import get_active_policy


def _parse_day(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def _today(as_of: str | None) -> date:
    return _parse_day(as_of) if as_of else date.today()


def run_expire_scan(conn: sqlite3.Connection, as_of_date: str) -> dict:
    """扫描在线分区：超过策略保留天数则生成待执行裁决。整单串行写。"""
    as_of = _parse_day(as_of_date)
    with write_tx(conn):
        cur = conn.execute(
            "INSERT INTO scan_jobs (as_of_date, status, message) VALUES (?,?,?)",
            (as_of_date, "进行中", ""),
        )
        job_id = int(cur.lastrowid)
        expired = 0

        online = conn.execute(
            "SELECT id, domain_code, biz_date, storage_state FROM partitions "
            "WHERE storage_state = '在线'"
        ).fetchall()

        for part in online:
            policy = get_active_policy(conn, part["domain_code"])
            if not policy:
                continue
            age = (as_of - _parse_day(part["biz_date"])).days
            if age < int(policy["keep_days"]):
                continue
            # 同一分区若已有未完成裁决，不再重复开单（预留后续题：重裁/作废重开）
            exists = conn.execute(
                "SELECT id FROM judgments WHERE partition_id = ? "
                "AND status IN ('待执行','执行中')",
                (part["id"],),
            ).fetchone()
            if exists:
                continue
            conn.execute(
                "INSERT INTO judgments "
                "(scan_job_id, partition_id, domain_code, biz_date, policy_version, "
                "keep_days, decided_action, status) "
                "VALUES (?,?,?,?,?,?,?, '待执行')",
                (
                    job_id,
                    part["id"],
                    part["domain_code"],
                    part["biz_date"],
                    policy["version"],
                    policy["keep_days"],
                    policy["action_on_expire"],
                ),
            )
            expired += 1

        conn.execute(
            "UPDATE scan_jobs SET status = ?, expired_count = ?, message = ? "
            "WHERE id = ?",
            ("已完成", expired, f"生成待执行裁决 {expired} 条", job_id),
        )
        job = get_scan_job(conn, job_id)
    return job


def get_scan_job(conn: sqlite3.Connection, job_id: int) -> dict | None:
    row = conn.execute(
        "SELECT id, as_of_date, status, expired_count, message, created_at "
        "FROM scan_jobs WHERE id = ?",
        (job_id,),
    ).fetchone()
    return dict(row) if row else None


def list_scan_jobs(
    conn: sqlite3.Connection, as_of: str | None = None
) -> list[dict]:
    """扫描列表：每条任务带出其名下被宽限保护拦下的裁决数（三块并动同数据）。"""
    today = _today(as_of)
    rows = conn.execute(
        "SELECT id, as_of_date, status, expired_count, message, created_at "
        "FROM scan_jobs ORDER BY id DESC"
    ).fetchall()
    jobs = [dict(r) for r in rows]
    for job in jobs:
        blocked = 0
        judged = conn.execute(
            "SELECT j.partition_id FROM judgments j WHERE j.scan_job_id = ?",
            (job["id"],),
        ).fetchall()
        for jr in judged:
            open_row = get_open_grace(conn, jr["partition_id"])
            if open_row and with_effective_state(open_row, today)["can_block_destroy"]:
                blocked += 1
        job["grace_blocked_count"] = blocked
    return jobs


def list_judgments(
    conn: sqlite3.Connection,
    status: str | None = None,
    scan_job_id: int | None = None,
    as_of: str | None = None,
) -> list[dict]:
    """裁决队列：左联宽限登记，带宽限截止与可否执行（销毁受宽限硬拦）。"""
    today = _today(as_of)
    clauses: list[str] = []
    params: list[object] = []
    if status:
        clauses.append("j.status = ?")
        params.append(status)
    if scan_job_id is not None:
        clauses.append("j.scan_job_id = ?")
        params.append(scan_job_id)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(
        "SELECT j.id, j.scan_job_id, j.partition_id, j.domain_code, j.biz_date, "
        "j.policy_version, j.keep_days, j.decided_action, j.status, j.fail_reason, "
        "j.created_at AS judgment_created_at, "
        "g.id AS grace_id, g.grace_deadline, g.backfill_status, "
        "g.backfill_task_code, g.status AS grace_status, g.reason AS grace_reason "
        "FROM judgments j "
        "LEFT JOIN grace_registrations g "
        "  ON g.id = (SELECT id FROM grace_registrations "
        "             WHERE partition_id = j.partition_id "
        "             ORDER BY id DESC LIMIT 1)"
        + where
        + " ORDER BY j.id DESC",
        params,
    ).fetchall()

    items: list[dict] = []
    for r in rows:
        item = dict(r)
        grace_effective = None
        can_block = 0
        executable = 1 if item["status"] in ("待执行", "已失败") else 0
        block_reason = ""
        if item["grace_id"] is not None:
            grace_effective = item["grace_status"]
            if item["grace_status"] == "宽限中":
                deadline = _parse_day(item["grace_deadline"])
                if today > deadline:
                    grace_effective = "已过期"
                elif item["decided_action"] == "销毁":
                    can_block = 1
                    executable = 0
                    block_reason = (
                        f"宽限保护中（截止 {item['grace_deadline']}，"
                        f"补采：{item['backfill_status']}），禁止提前销毁"
                    )
        item["grace_effective_status"] = grace_effective
        item["grace_can_block"] = can_block
        item["can_execute"] = executable
        item["block_reason"] = block_reason
        items.append(item)
    return items
