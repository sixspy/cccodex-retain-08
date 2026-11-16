import sqlite3
import threading
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path

from app.config import Config

# 进程内串行写锁：宽限登记与裁决执行两侧共用一把锁，
# 配合 SQLite BEGIN IMMEDIATE 杜绝并发双写互盖。
_write_lock = threading.RLock()


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or Config.database()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        path, check_same_thread=False, timeout=5.0, isolation_level=None
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


@contextmanager
def get_db(db_path: str | None = None):
    """读连接：autocommit 模式，只读不隐式开事务。"""
    conn = connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def write_tx(conn: sqlite3.Connection):
    """串行写事务：先抢进程写锁，再 BEGIN IMMEDIATE 拿库级保留锁。

    所有写路径（扫描、裁决执行、宽限登记/补采、对账）必须经过这里，
    保证两侧同时到达时严格串行，缺锁的旁路写会在唯一索引处撞冲突。
    """
    with _write_lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


SCHEMA = """
CREATE TABLE IF NOT EXISTS retain_policies (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  domain_code TEXT NOT NULL,
  domain_name TEXT NOT NULL,
  keep_days INTEGER NOT NULL,
  grace_days INTEGER NOT NULL DEFAULT 3,
  action_on_expire TEXT NOT NULL CHECK (action_on_expire IN ('归档','销毁')),
  version INTEGER NOT NULL DEFAULT 1,
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(domain_code, version)
);

CREATE TABLE IF NOT EXISTS partitions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  domain_code TEXT NOT NULL,
  biz_date TEXT NOT NULL,
  row_count INTEGER NOT NULL DEFAULT 0,
  storage_state TEXT NOT NULL CHECK (
    storage_state IN ('在线','已归档','已销毁','裁撤中')
  ),
  archive_uri TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(domain_code, biz_date)
);

CREATE TABLE IF NOT EXISTS scan_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  as_of_date TEXT NOT NULL,
  status TEXT NOT NULL,
  expired_count INTEGER NOT NULL DEFAULT 0,
  message TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS judgments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scan_job_id INTEGER NOT NULL,
  partition_id INTEGER NOT NULL,
  domain_code TEXT NOT NULL,
  biz_date TEXT NOT NULL,
  policy_version INTEGER NOT NULL,
  keep_days INTEGER NOT NULL,
  decided_action TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('待执行','执行中','已完成','已失败','已作废')),
  fail_reason TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY(scan_job_id) REFERENCES scan_jobs(id),
  FOREIGN KEY(partition_id) REFERENCES partitions(id)
);

CREATE TABLE IF NOT EXISTS execute_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  judgment_id INTEGER NOT NULL UNIQUE,
  action TEXT NOT NULL,
  result_state TEXT NOT NULL,
  archive_uri TEXT,
  ok INTEGER NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY(judgment_id) REFERENCES judgments(id)
);

CREATE TABLE IF NOT EXISTS reconcile_reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  as_of_date TEXT NOT NULL,
  online_count INTEGER NOT NULL,
  archived_count INTEGER NOT NULL,
  destroyed_count INTEGER NOT NULL,
  pending_judgment_count INTEGER NOT NULL,
  grace_open_count INTEGER NOT NULL DEFAULT 0,
  mismatch_count INTEGER NOT NULL,
  summary TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 宽限登记快照表（形态一：独立快照，不向分区状态机注入宽限态）。
-- 一行一次宽限生命周期：登记 → 补采中 → 已补采放行 / 过期 / 人工关闭。
CREATE TABLE IF NOT EXISTS grace_registrations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  partition_id INTEGER NOT NULL,
  judgment_id INTEGER,
  domain_code TEXT NOT NULL,
  biz_date TEXT NOT NULL,
  reason TEXT NOT NULL DEFAULT '',
  grace_days INTEGER NOT NULL,
  registered_at TEXT NOT NULL DEFAULT (datetime('now')),
  grace_deadline TEXT NOT NULL,
  backfill_status TEXT NOT NULL CHECK (
    backfill_status IN ('待补采','补采中','已补采')
  ) DEFAULT '待补采',
  backfill_task_code TEXT NOT NULL DEFAULT '',
  backfill_verified_at TEXT,
  backfill_remark TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL CHECK (
    status IN ('宽限中','已放行','已过期','已关闭')
  ) DEFAULT '宽限中',
  released_at TEXT,
  release_note TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(partition_id) REFERENCES partitions(id),
  FOREIGN KEY(judgment_id) REFERENCES judgments(id)
);

-- 同一分区同时只允许一条生效中的宽限登记，双写在此硬冲突。
CREATE UNIQUE INDEX IF NOT EXISTS ux_grace_open_per_partition
ON grace_registrations(partition_id)
WHERE status = '宽限中';

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""


SEED_POLICIES = [
    # domain_code, domain_name, keep_days, grace_days, action
    ("orders", "订单明细域", 7, 3, "归档"),
    ("payments", "支付流水域", 14, 3, "归档"),
    ("events", "埋点事件域", 3, 2, "销毁"),
    ("inventory", "库存快照域", 10, 3, "归档"),
]


SEED_PARTITIONS = [
    ("orders", "2026-11-01", 1200, "在线"),
    ("orders", "2026-11-05", 980, "在线"),
    ("orders", "2026-11-10", 1100, "在线"),
    ("orders", "2026-11-14", 1050, "在线"),
    ("payments", "2026-10-20", 800, "在线"),
    ("payments", "2026-11-01", 760, "在线"),
    ("payments", "2026-11-12", 790, "在线"),
    ("events", "2026-11-08", 5000, "在线"),
    ("events", "2026-11-12", 5200, "在线"),
    ("events", "2026-11-15", 5100, "在线"),
    ("inventory", "2026-10-30", 300, "在线"),
    ("inventory", "2026-11-11", 310, "在线"),
]


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _migrate(conn: sqlite3.Connection) -> None:
    """老库平滑加列，不破坏既有数据。"""
    policy_cols = _columns(conn, "retain_policies")
    if "grace_days" not in policy_cols:
        conn.execute(
            "ALTER TABLE retain_policies ADD COLUMN grace_days INTEGER NOT NULL DEFAULT 3"
        )
    report_cols = _columns(conn, "reconcile_reports")
    if "grace_open_count" not in report_cols:
        conn.execute(
            "ALTER TABLE reconcile_reports "
            "ADD COLUMN grace_open_count INTEGER NOT NULL DEFAULT 0"
        )


def default_deadline(registered_day: date, grace_days: int) -> str:
    return (registered_day + timedelta(days=int(grace_days))).isoformat()


def init_db(db_path: str | None = None) -> None:
    conn = connect(db_path)
    try:
        # executescript 会先隐式提交，DDL 直接走 autocommit；
        # 迁移与播种再进串行写事务。
        conn.executescript(SCHEMA)
        with write_tx(conn):
            _migrate(conn)
            n = conn.execute(
                "SELECT COUNT(*) AS c FROM retain_policies"
            ).fetchone()["c"]
            if n == 0:
                for domain_code, domain_name, keep_days, grace_days, action in SEED_POLICIES:
                    conn.execute(
                        "INSERT INTO retain_policies "
                        "(domain_code, domain_name, keep_days, grace_days, "
                        "action_on_expire, version, active) "
                        "VALUES (?,?,?,?,?,1,1)",
                        (domain_code, domain_name, keep_days, grace_days, action),
                    )
                for domain_code, biz_date, row_count, state in SEED_PARTITIONS:
                    conn.execute(
                        "INSERT INTO partitions "
                        "(domain_code, biz_date, row_count, storage_state) "
                        "VALUES (?,?,?,?)",
                        (domain_code, biz_date, row_count, state),
                    )
                conn.execute(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES ('lab_name', ?)",
                    (Config.LAB_NAME,),
                )
                conn.execute(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES ('skeleton', ?)",
                    (
                        "分区留存策略版本→到期扫描裁决→归档或销毁执行→策略与实物分区对账",
                    ),
                )
    finally:
        conn.close()
