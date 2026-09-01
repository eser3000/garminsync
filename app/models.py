from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Account(Base):
    __tablename__ = "accounts"

    region = mapped_column(String, primary_key=True)  # 'china' | 'intl'
    email = mapped_column(String, default="")
    username = mapped_column(String, default="")
    avatar_url = mapped_column(String, default="")  # Garmin 头像 URL（登录时获取）
    token = mapped_column(Text, default="")  # garth client.dumps() 的 base64 串
    logged_in = mapped_column(Boolean, default=False)
    mfa_pending = mapped_column(Boolean, default=False)
    updated_at = mapped_column(DateTime, default=datetime.utcnow)


class Setting(Base):
    __tablename__ = "settings"

    id = mapped_column(Integer, primary_key=True)
    frequency_minutes = mapped_column(Integer, default=60)  # 10..360
    enabled = mapped_column(Boolean, default=False)
    sync_window = mapped_column(String, default="3months")  # 1day|1week|1month|3months
    fetch_history = mapped_column(Boolean, default=False)  # 已废弃：时间范围已覆盖历史数据，无需单独开关（保留列仅为兼容旧数据库）
    weight_enabled = mapped_column(Boolean, default=True)  # 体重自动同步开关（固定每天 00:00）
    weight_sync_window = mapped_column(String, default="3months")  # 体重同步时间范围 1day|1week|1month|3months
    health_enabled = mapped_column(Boolean, default=False)  # 健康数据自动同步开关（固定每天 03:00）
    health_sync_window = mapped_column(String, default="1month")  # 健康同步时间范围 1day|1week|1month|3months
    updated_at = mapped_column(DateTime, default=datetime.utcnow)


class SyncLog(Base):
    __tablename__ = "sync_log"

    id = mapped_column(Integer, primary_key=True)
    kind = mapped_column(String, default="activity")  # activity|weight|health|igpsport
    started_at = mapped_column(DateTime, default=datetime.utcnow)
    finished_at = mapped_column(DateTime)
    status = mapped_column(String, default="running")  # running|success|partial|error
    read_count = mapped_column(Integer, default=0)  # 读取（中国区窗口内全量活动/记录）条数
    fetched_count = mapped_column(Integer, default=0)  # 抓取（下载 FIT）成功条数
    synced_count = mapped_column(Integer, default=0)  # 同步（上传国际区）成功条数
    upload_failed = mapped_column(Integer, default=0)  # 同步失败条数
    history_count = mapped_column(Integer, default=0)  # 历史（之前已同步过）条数
    fetch_failed = mapped_column(Integer, default=0)  # 抓取失败（读取到但无效被过滤）条数
    intl_synced = mapped_column(Integer, default=0)  # 国际区上传成功条数
    intl_failed = mapped_column(Integer, default=0)  # 国际区上传失败条数
    intl_history_count = mapped_column(Integer, default=0)  # 国际区历史存在（不重复同步）条数
    china_synced = mapped_column(Integer, default=0)  # 中国区上传成功条数
    china_failed = mapped_column(Integer, default=0)  # 中国区上传失败条数
    igp_synced = mapped_column(Integer, default=0)  # iGPSPORT 上传成功条数
    igp_failed = mapped_column(Integer, default=0)  # iGPSPORT 上传失败条数
    igp_history_count = mapped_column(Integer, default=0)  # iGPSPORT 历史存在（不重复同步）条数
    igp_done = mapped_column(Integer, default=0)  # iGPSPORT 已上传过（去重跳过）条数
    error = mapped_column(Text, default="")  # 抓取失败 + 同步失败的原因明细


class SyncedActivity(Base):
    __tablename__ = "synced_activities"

    id = mapped_column(Integer, primary_key=True)
    china_activity_id = mapped_column(String, unique=True)
    intl_activity_id = mapped_column(String, default="")
    igp_activity_id = mapped_column(String, default="")  # iGPSPORT rideId
    synced_at = mapped_column(DateTime, default=datetime.utcnow)


class IgpsportAccount(Base):
    """iGPSPORT（迹驰）账户凭据：OAuth access/refresh token。"""

    __tablename__ = "igpsport_account"

    id = mapped_column(Integer, primary_key=True)
    username = mapped_column(String, default="")
    access_token = mapped_column(Text, default="")
    refresh_token = mapped_column(Text, default="")
    expires_at = mapped_column(DateTime)
    logged_in = mapped_column(Boolean, default=False)
    avatar_url = mapped_column(String, default="")  # 迹驰头像 URL（登录时由 mobile/api/User/UserInfo 获取）
    nick_name = mapped_column(String, default="")  # 迹驰昵称（优先于 username 展示）
    igp_diag = mapped_column(String, default="")  # 调试：最近一次用户资料获取诊断（确认后移除）
    updated_at = mapped_column(DateTime, default=datetime.utcnow)


class XiaomiAccount(Base):
    """小米体脂秤账户凭据（userId + passToken 抓包获取）。"""

    __tablename__ = "xiaomi_account"

    id = mapped_column(Integer, primary_key=True)
    user_id = mapped_column(String, default="")
    pass_token = mapped_column(Text, default="")
    ssecurity = mapped_column(Text, default="")  # base64 编码，签名用
    logged_in = mapped_column(Boolean, default=False)
    updated_at = mapped_column(DateTime, default=datetime.utcnow)


class SyncedWeight(Base):
    """已同步体重记录（按秒级时间戳去重）。"""

    __tablename__ = "synced_weights"

    id = mapped_column(Integer, primary_key=True)
    ts = mapped_column(Integer, unique=True)  # 记录时间戳（秒）
    weight = mapped_column(Float, default=0.0)  # kg
    synced_at = mapped_column(DateTime, default=datetime.utcnow)


class SyncedHealth(Base):
    """已同步的健康数据 FIT 文件（按内容 SHA256 去重）。"""

    __tablename__ = "synced_health"

    id = mapped_column(Integer, primary_key=True)
    file_hash = mapped_column(String, unique=True)  # FIT 内容 SHA256
    filename = mapped_column(String, default="")  # zip 内文件名
    synced_at = mapped_column(DateTime, default=datetime.utcnow)


def prune_sync_logs(session, kind: str, keep: int = 500) -> int:
    """每类同步日志最多保留 keep 条，超出自动删除最旧的，防止无限增长。

    在每次同步写入日志后调用；返回删除的条数。
    """
    ids = (
        session.query(SyncLog.id)
        .filter_by(kind=kind)
        .order_by(SyncLog.id.desc())
        .offset(keep)
        .limit(100000)
        .all()
    )
    if not ids:
        return 0
    old_ids = [i for (i,) in ids]
    session.query(SyncLog).filter(SyncLog.id.in_(old_ids)).delete(
        synchronize_session=False
    )
    session.commit()
    return len(old_ids)


def get_setting(session) -> Setting:
    s = session.get(Setting, 1)
    if s is None:
        s = Setting(
            id=1,
            frequency_minutes=60,
            enabled=False,
            sync_window="3months",
            fetch_history=False,
            weight_sync_window="3months",
            health_sync_window="1month",
        )
        session.add(s)
        session.commit()
    return s



