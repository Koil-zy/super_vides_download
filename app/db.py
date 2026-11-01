"""SQLite 轻量存储：连接管理与建表。

数据库文件固定放在项目内 data/app.db，随项目一起拷贝即可；
建表语句留档在 db/schema.sql，应用启动时自动执行（幂等）。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / 'data'
DB_PATH = DATA_DIR / 'app.db'
SCHEMA_PATH = ROOT / 'db' / 'schema.sql'

# 存量数据库需要补齐的列（SQLite 不支持 ADD COLUMN IF NOT EXISTS）
_USER_COLUMNS = (
    ('merchant_id', 'INTEGER REFERENCES admins(id)'),
    ('blacklisted_at', 'TEXT'),
    ('blacklist_reason', "TEXT NOT NULL DEFAULT ''"),
)


def now_iso() -> str:
    """统一的 UTC 时间串：2026-11-01T08:30:00Z。"""
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def iso_after(days: float = 0, seconds: float = 0) -> str:
    moment = datetime.now(timezone.utc) + timedelta(days=days, seconds=seconds)
    return moment.strftime('%Y-%m-%dT%H:%M:%SZ')


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    conn.execute('PRAGMA journal_mode = WAL')
    return conn


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """给已有库补齐 users 表新增列（幂等）。"""
    existing = {row['name'] for row in conn.execute('PRAGMA table_info(users)')}
    for name, ddl in _USER_COLUMNS:
        if name not in existing:
            conn.execute(f'ALTER TABLE users ADD COLUMN {name} {ddl}')


def init_db() -> None:
    """启动时调用：建表 + 迁移 + 清理过期会话（失败不影响启动）。"""
    DATA_DIR.mkdir(exist_ok=True)
    sql = SCHEMA_PATH.read_text(encoding='utf-8')
    with connect() as conn:
        conn.executescript(sql)
        _ensure_columns(conn)
        conn.execute('DELETE FROM sessions WHERE expires_at <= ?', (now_iso(),))


@contextmanager
def get_db():
    """按请求打开一个连接，退出时自动关闭（with 块正常结束会提交）。"""
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
