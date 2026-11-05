"""后台调度器：按设置频率自动触发同步，并支持手动立即触发。

支持三类任务（互不干扰，各自独立记录状态与频率）：
- activity：中国区活动 -> 国际区 + iGPSPORT 迹驰（受 settings.enabled 控制；
  iGPSPORT 已登录时随活动同步一并执行，统计并入同一日志）
- weight：小米体脂秤体重 -> 固定双区（国际区 + 中国区，受 settings.weight_enabled
  控制，固定每天本地 00:00 自动执行一次）
- health：佳明中国区 ⇄ 国际区全量健康数据对比、互相补全（睡眠/心率/压力/
  身体电量/血氧/呼吸/步数，受 settings.health_enabled 控制，固定每天本地
  03:00 自动执行一次）
"""
import asyncio
import logging
import time
from datetime import datetime, timedelta
from typing import Optional

from app.models import SyncLog, get_setting
from app.time_utils import CN_OFFSET, cn_iso, now_cn

logger = logging.getLogger("garminsync")

KINDS = ("activity", "weight", "health")
WEIGHT_SCHEDULE_LABEL = "每天 00:00"
HEALTH_SCHEDULE_LABEL = "每天 03:00"


def _aligned_slot_ts(ts: float, minutes: int) -> float:
    """把时间戳对齐到频率周期的起点（整点基准）。

    频率都是 60 的因子（10/15/30/60/120/180/240/360），按 UTC 时间戳
    对齐即可落在整点/半点/刻钟等固定刻度上，与东八区展示一致。
    """
    period = minutes * 60
    return (ts // period) * period


class SyncScheduler:
    def __init__(self, garmin, session_factory):
        self.garmin = garmin
        self.Session = session_factory
        self._task = None
        self._trigger = {k: False for k in KINDS}
        self._last: dict[str, Optional[SyncLog]] = {k: None for k in KINDS}
        self._lock = asyncio.Lock()
        self._event = None
        self._event_loop = None

    def start(self):
        self._event_loop = asyncio.get_running_loop()
        self._event = asyncio.Event()
        self._task = asyncio.create_task(self._loop())

    def stop(self):
        if self._task:
            self._task.cancel()
            self._task = None

    def trigger(self, kind: str = "activity"):
        if kind not in KINDS:
            kind = "activity"
        self._trigger[kind] = True
        # 立即唤醒轮询循环，让手动同步不用等最长 30 秒
        if self._event is not None and self._event_loop is not None:
            self._event_loop.call_soon_threadsafe(self._event.set)

    async def _loop(self):
        while True:
            try:
                await asyncio.wait_for(self._event.wait(), timeout=30)
            except asyncio.TimeoutError:
                pass
            self._event.clear()
            try:
                await self._maybe_run()
            except Exception:
                logger.exception("调度循环执行异常")

    async def _maybe_run(self):
        async with self._lock:
            s = self._read_settings()
            # 1) 手动触发优先：把已触发的任务依次执行完（本轮不再走自动调度）
            triggered = False
            for kind in KINDS:
                if self._trigger[kind]:
                    self._trigger[kind] = False
                    await self._run_once(kind)
                    triggered = True
            if triggered:
                return
            # 2) 活动自动同步（需要中国区登录）
            if s.enabled and self.garmin.get_client("china", "garmin.cn") is not None:
                if self._due("activity", s.frequency_minutes):
                    await self._run_once("activity")
            # 3) 体重自动同步（独立开关，固定每天本地 00:00 执行）
            if s.weight_enabled:
                if self._weight_due():
                    await self._run_once("weight")
            # 4) 健康数据自动同步（独立开关，固定每天本地 03:00 执行）
            if s.health_enabled:
                if self._health_due():
                    await self._run_once("health")
            # iGPSPORT 迹驰不再独立调度：已登录时由活动同步（activity）一并执行，
            # 统计与日志并入活动同步（见 app/services/sync.py::run_sync）。

    def _due(self, kind: str, frequency_minutes: int) -> bool:
        """频率同步是否到期：以整点为基准，而非从上次完成时刻起算。

        当前时间对齐到频率周期起点（如 30 分钟频率 -> 01:00/01:30/02:00…），
        若最近一次完成时间早于该周期起点，说明本周期还没跑过，则到期。
        首次运行（无记录）立即执行。
        """
        last = self._last[kind]
        if last is None or not last.finished_at:
            return True
        slot_ts = _aligned_slot_ts(time.time(), frequency_minutes)
        return last.finished_at < datetime.utcfromtimestamp(slot_ts)

    def _daily_due(self, kind: str, hour: int) -> bool:
        """kind 任务是否到期：固定每天本地 hour 点执行一次。

        到期条件：最近一次完成时间早于「今天本地 hour 点对应的 UTC 时刻」。
        内存无记录（如进程重启）时从 DB 兜底读取最近一次该 kind 的日志。
        """
        last = self._last[kind]
        if last is None or not last.finished_at:
            with self.Session() as s:
                last = (
                    s.query(SyncLog)
                    .filter_by(kind=kind)
                    .order_by(SyncLog.id.desc())
                    .first()
                )
        # 固定按东八区计算「今天 hour 点」，再换算成 UTC 与 last.finished_at（UTC）比较，
        # 不依赖容器/宿主机系统时区。
        local_target = now_cn().replace(
            hour=hour, minute=0, second=0, microsecond=0
        )
        target_utc = local_target - CN_OFFSET
        if last is None or not last.finished_at:
            return True  # 从未同步过：部署后立即执行一次
        return last.finished_at < target_utc

    def _weight_due(self) -> bool:
        """体重同步是否到期：固定每天本地 00:00 执行一次。"""
        return self._daily_due("weight", 0)

    def _health_due(self) -> bool:
        """健康数据同步是否到期：固定每天本地 03:00 执行一次。"""
        return self._daily_due("health", 3)

    @staticmethod
    def _next_daily_run(hour: int) -> str:
        """下一次固定每日执行时刻：今天东八区 hour 点已过则明天。"""
        now = now_cn()
        target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if now >= target:
            target = target + timedelta(days=1)
        return target.isoformat()

    def _next_weight_run(self) -> str:
        return self._next_daily_run(0)

    def _next_health_run(self) -> str:
        return self._next_daily_run(3)

    async def _run_once(self, kind: str = "activity"):
        if kind == "weight":
            from app.services.weight_sync import run_weight_sync

            self._last["weight"] = await asyncio.to_thread(
                run_weight_sync, self.Session, self.garmin
            )
        elif kind == "health":
            from app.services.health_sync import run_health_sync

            self._last["health"] = await asyncio.to_thread(
                run_health_sync, self.Session, self.garmin
            )
        else:
            from app.services.sync import run_sync

            self._last["activity"] = await asyncio.to_thread(
                run_sync, self.Session, self.garmin
            )

    def _read_settings(self):
        with self.Session() as s:
            return get_setting(s)

    def state(self, kind: str = "activity") -> dict:
        if kind not in KINDS:
            kind = "activity"
        s = self._read_settings()
        last = self._last[kind]
        next_run_at = None
        # 下一次运行时刻 = 下一个整点对齐周期起点（与 _due 的整点基准一致）
        next_ts = (
            _aligned_slot_ts(time.time(), s.frequency_minutes)
            + s.frequency_minutes * 60
        )
        next_run_at = cn_iso(datetime.utcfromtimestamp(next_ts))
        last_dict = None
        if last:
            last_dict = {
                "started_at": cn_iso(last.started_at),
                "finished_at": cn_iso(last.finished_at),
                "status": last.status,
                "read_count": last.read_count,
                "fetched_count": last.fetched_count,
                "synced_count": last.synced_count,
                "upload_failed": last.upload_failed,
                "history_count": last.history_count,
                "intl_history_count": last.intl_history_count,
                "intl_synced": last.intl_synced,
                "intl_failed": last.intl_failed,
                "china_synced": last.china_synced,
                "china_failed": last.china_failed,
                "igp_history_count": last.igp_history_count,
                "igp_synced": last.igp_synced,
                "igp_failed": last.igp_failed,
                "error": last.error,
            }
        base = {
            "status": last.status if last else "idle",
            "frequency_minutes": s.frequency_minutes,
            "last_sync": last_dict,
            "next_run_at": next_run_at,
        }
        if kind == "weight":
            return {
                **base,
                "enabled": s.weight_enabled,
                "targets": ["intl", "china"],
                "schedule": WEIGHT_SCHEDULE_LABEL,
                "next_run_at": self._next_weight_run(),
            }
        if kind == "health":
            return {
                **base,
                "enabled": s.health_enabled,
                "target": "中国区 ⇄ 国际区",
                "schedule": HEALTH_SCHEDULE_LABEL,
                "next_run_at": self._next_health_run(),
            }
        return {
            **base,
            "enabled": s.enabled,
        }
