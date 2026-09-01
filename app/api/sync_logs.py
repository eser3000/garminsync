"""同步日志公共查询/清空/序列化（activity/weight/health 三类共用）。

三类同步日志结构一致（同一张 SyncLog 表按 kind 区分），只有序列化字段
子集不同；统一返回全部字段，多余字段对前端无影响，避免三份重复代码。
"""
from app.db import SessionLocal
from app.models import SyncLog
from app.time_utils import cn_iso


def _serialize_log(r: SyncLog) -> dict:
    return {
        "id": r.id,
        "started_at": cn_iso(r.started_at),
        "finished_at": cn_iso(r.finished_at),
        "status": r.status,
        "read_count": r.read_count,
        "fetched_count": r.fetched_count,
        "synced_count": r.synced_count,
        "upload_failed": r.upload_failed,
        "fetch_failed": r.fetch_failed,
        "history_count": r.history_count,
        "intl_history_count": r.intl_history_count,
        "intl_synced": r.intl_synced,
        "intl_failed": r.intl_failed,
        "china_synced": r.china_synced,
        "china_failed": r.china_failed,
        "igp_history_count": r.igp_history_count,
        "igp_synced": r.igp_synced,
        "igp_failed": r.igp_failed,
        "igp_done": r.igp_done,
        "error": r.error,
    }


def query_sync_logs(kind: str, limit: int = 20) -> list[dict]:
    """最近 limit 条 kind 类型的同步日志（倒序）。"""
    with SessionLocal() as s:
        rows = (
            s.query(SyncLog)
            .filter_by(kind=kind)
            .order_by(SyncLog.id.desc())
            .limit(limit)
            .all()
        )
        return [_serialize_log(r) for r in rows]


def clear_sync_logs(kind: str) -> None:
    """清空 kind 类型的同步日志。"""
    with SessionLocal() as s:
        s.query(SyncLog).filter_by(kind=kind).delete()
        s.commit()
