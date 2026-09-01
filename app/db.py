from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

import app.config as config
from app.config import DB_PATH

config.ensure_dirs()
DATABASE_URL = f"sqlite:///{DB_PATH}"

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
# expire_on_commit=False：commit 后对象属性保留在内存，session 关闭后对象虽 detached
# 但可读（run_sync 返回对象给调度器/测试时避免 DetachedInstanceError）。
SessionLocal = sessionmaker(
    bind=engine, autoflush=False, autocommit=False, expire_on_commit=False
)
Base = declarative_base()


def init_db():
    import app.models  # noqa: F401 确保模型注册到 Base
    Base.metadata.create_all(engine)
    # 轻量迁移：为已存在的旧库补上新增列（SQLite 不支持 ALTER 加列自动完成）
    _migrate_columns()


def _migrate_columns():
    from sqlalchemy import text

    from app.config import SYNC_WINDOW_OPTIONS

    # 每个表需要补的列（键->类型）。SQLite ALTER 只加缺失列。
    expected = {
        "settings": {
            "sync_window": f"VARCHAR DEFAULT '{SYNC_WINDOW_OPTIONS[-1]}'",
            "fetch_history": "BOOLEAN DEFAULT 0",
            "weight_enabled": "BOOLEAN DEFAULT 1",
            "weight_sync_window": "VARCHAR DEFAULT '3months'",
            "health_enabled": "BOOLEAN DEFAULT 0",
            "health_sync_window": "VARCHAR DEFAULT '1month'",
        },
        "sync_log": {
            "read_count": "INTEGER DEFAULT 0",
            "fetched_count": "INTEGER DEFAULT 0",
            "upload_failed": "INTEGER DEFAULT 0",
            "history_count": "INTEGER DEFAULT 0",
            "fetch_failed": "INTEGER DEFAULT 0",
            "intl_synced": "INTEGER DEFAULT 0",
            "intl_failed": "INTEGER DEFAULT 0",
            "intl_history_count": "INTEGER DEFAULT 0",
            "china_synced": "INTEGER DEFAULT 0",
            "china_failed": "INTEGER DEFAULT 0",
            "igp_synced": "INTEGER DEFAULT 0",
            "igp_failed": "INTEGER DEFAULT 0",
            "igp_history_count": "INTEGER DEFAULT 0",
            "igp_done": "INTEGER DEFAULT 0",
            "kind": "VARCHAR DEFAULT 'activity'",
        },
        "synced_activities": {
            "igp_activity_id": "VARCHAR DEFAULT ''",
        },
        "accounts": {
            "avatar_url": "VARCHAR DEFAULT ''",
        },
        "igpsport_account": {
            "avatar_url": "VARCHAR DEFAULT ''",
            "nick_name": "VARCHAR DEFAULT ''",
            "igp_diag": "VARCHAR DEFAULT ''",
        },
    }
    try:
        with engine.begin() as conn:
            for table, cols_def in expected.items():
                cols = {
                    r[1]
                    for r in conn.execute(
                        text(f"PRAGMA table_info({table})")
                    ).fetchall()
                }
                for name, dtype in cols_def.items():
                    if name not in cols:
                        conn.execute(
                            text(f"ALTER TABLE {table} ADD COLUMN {name} {dtype}")
                        )
    except Exception:
        # 表可能尚不存在或结构异常，交给 create_all 兜底
        pass
