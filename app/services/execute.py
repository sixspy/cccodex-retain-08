import sqlite3

from app.db import write_tx
from app.services.grace import (
    assert_destroy_allowed,
    get_grace,
    get_open_grace,
    with_effective_state,
)
from app.services.partitions import get_partition


def _finalize_open_grace(conn: sqlite3.Connection, partition_id: int, today) -> None:
    """分区走到终态时对齐宽限登记：能走到这里的在拦登记必为已过期，落库置过期。"""
    open_row = get_open_grace(conn, partition_id)
    if not open_row:
        return
    item = with_effective_state(open_row, today)
    if item["effective_status"] == "已过期":
        conn.execute(
            "UPDATE grace_registrations SET status = '已过期', "
            "release_note = COALESCE(NULLIF(release_note,''), "
            "'裁决已执行（宽限截止后放行）') WHERE id = ?",
            (open_row["id"],),
        )


def execute_judgment(
    conn: sqlite3.Connection,
    judgment_id: int,
    as_of: str | None = None,
) -> dict:
    """执行单条裁决（串行写）：归档写入 archive_uri；销毁清空在线态。

    销毁动作先过宽限硬拦：宽限生效中直接拒绝，分区一行不动。
    整单失败则在同事务内恢复在线并记失败流水，事务照常提交后再抛出。
    """
    from datetime import date, datetime

    today = (
        datetime.strptime(as_of, "%Y-%m-%d").date() if as_of else date.today()
    )
    failure: Exception | None = None

    with write_tx(conn):
        row = conn.execute(
            "SELECT * FROM judgments WHERE id = ?", (judgment_id,)
        ).fetchone()
        if not row:
            raise ValueError("裁决不存在")
        judgment = dict(row)
        if judgment["status"] not in ("待执行", "已失败"):
            raise ValueError(f"当前状态不可执行：{judgment['status']}")

        part = get_partition(conn, judgment["partition_id"])
        if not part:
            raise ValueError("分区不存在")
        if part["storage_state"] not in ("在线", "裁撤中"):
            raise ValueError(f"分区状态不可裁撤：{part['storage_state']}")

        action = judgment["decided_action"]
        if action == "销毁":
            # 提前毁即拦：宽限登记生效中（未截止、未放行）在此硬挡。
            assert_destroy_allowed(conn, part["id"], as_of)

        conn.execute(
            "UPDATE judgments SET status = '执行中', fail_reason = '' WHERE id = ?",
            (judgment_id,),
        )
        conn.execute(
            "UPDATE partitions SET storage_state = '裁撤中' WHERE id = ?",
            (part["id"],),
        )

        try:
            if action == "归档":
                uri = f"archive://{part['domain_code']}/{part['biz_date']}"
                conn.execute(
                    "UPDATE partitions SET storage_state = '已归档', archive_uri = ? "
                    "WHERE id = ?",
                    (uri, part["id"]),
                )
                result_state = "已归档"
                archive_uri = uri
                detail = "分区已归档并保留可查归档地址"
            elif action == "销毁":
                conn.execute(
                    "UPDATE partitions SET storage_state = '已销毁', archive_uri = NULL, "
                    "row_count = 0 WHERE id = ?",
                    (part["id"],),
                )
                result_state = "已销毁"
                archive_uri = None
                detail = "分区已销毁，在线行数清零"
            else:
                raise ValueError(f"未知动作：{action}")

            _finalize_open_grace(conn, part["id"], today)
            conn.execute(
                "UPDATE judgments SET status = '已完成' WHERE id = ?",
                (judgment_id,),
            )
            conn.execute(
                "INSERT OR REPLACE INTO execute_runs "
                "(judgment_id, action, result_state, archive_uri, ok, detail) "
                "VALUES (?,?,?,?,1,?)",
                (judgment_id, action, result_state, archive_uri, detail),
            )
        except Exception as exc:  # noqa: BLE001 — 失败闭：恢复在线并记失败
            conn.execute(
                "UPDATE partitions SET storage_state = '在线', archive_uri = NULL "
                "WHERE id = ?",
                (part["id"],),
            )
            conn.execute(
                "UPDATE judgments SET status = '已失败', fail_reason = ? WHERE id = ?",
                (str(exc), judgment_id),
            )
            conn.execute(
                "INSERT OR REPLACE INTO execute_runs "
                "(judgment_id, action, result_state, archive_uri, ok, detail) "
                "VALUES (?,?,?,?,0,?)",
                (judgment_id, action, "在线", None, str(exc)),
            )
            failure = exc

    if failure is not None:
        raise failure
    return get_run_by_judgment(conn, judgment_id)


def get_run_by_judgment(conn: sqlite3.Connection, judgment_id: int) -> dict | None:
    row = conn.execute(
        "SELECT id, judgment_id, action, result_state, archive_uri, ok, detail, created_at "
        "FROM execute_runs WHERE judgment_id = ?",
        (judgment_id,),
    ).fetchone()
    return dict(row) if row else None


def list_runs(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT id, judgment_id, action, result_state, archive_uri, ok, detail, created_at "
        "FROM execute_runs ORDER BY id DESC"
    ).fetchall()
    return [dict(r) for r in rows]
