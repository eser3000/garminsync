"""时间工具：对外展示统一使用东八区（Asia/Shanghai）。

数据库中的时间字段统一按 UTC（datetime.utcnow()）存储，仅在做
API 序列化 / 日志输出 / 前端展示时才转换为东八区，避免时区混乱。
东八区无夏令时，固定 +8 小时，不依赖系统时区与 tzdata。
"""
from datetime import datetime, timedelta, timezone

CN_TZ = timezone(timedelta(hours=8))
CN_OFFSET = timedelta(hours=8)


def now_cn() -> datetime:
    """当前东八区时间（naive），用于调度判断与下次运行时间计算。"""
    return datetime.now(CN_TZ).replace(tzinfo=None)


def to_cn(dt: datetime) -> datetime:
    """UTC（或任意 aware）datetime -> 东八区 naive datetime。

    - naive：按 UTC 处理，直接 +8 小时；
    - aware：先转东八区再去掉时区信息。
    """
    if dt.tzinfo is not None:
        return dt.astimezone(CN_TZ).replace(tzinfo=None)
    return dt + CN_OFFSET


def cn_iso(dt) -> str:
    """UTC datetime -> 东八区 isoformat 字符串（naive，供 API/前端展示）。"""
    if not dt:
        return None
    return to_cn(dt).isoformat()


def cn_date(ts: float) -> str:
    """秒级时间戳 -> 东八区日期字符串（YYYY-MM-DD，供日志展示）。"""
    return datetime.fromtimestamp(ts, tz=CN_TZ).strftime("%Y-%m-%d")
