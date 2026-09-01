"""体重同步编排：从小米体脂秤读取体重记录，生成 FIT 并上传到 Garmin。

- 数据源：小米健康 API（key=weight），凭据为 userId + passToken（抓包获取）
- 上传：固定同时上传国际区（garmin.com）与中国区（garmin.cn），无需选择目标；
  未登录的区域自动跳过（至少需登录一个区域）
- 去重：按记录时间戳（秒）记录在 synced_weights 表
"""
import io
import logging
import time
from datetime import datetime

import app.config as config
from app.models import (
    SyncedWeight,
    SyncLog,
    XiaomiAccount,
    get_setting,
    prune_sync_logs,
)
from app.services import fit_files
from app.services.xiaomi import XiaomiClient, XiaomiError

logger = logging.getLogger("garminsync")

# 体重上传目标：固定双区（国际区 + 中国区），写死无需配置
WEIGHT_TARGETS = (("intl", "garmin.com"), ("china", "garmin.cn"))
TARGET_NAMES = {"intl": "国际区", "china": "中国区"}


def _upload_fit(client, fit_bytes, name):
    """将 FIT 文件上传到目标 Garmin（与活动同步同一条通道）。"""
    fp = io.BytesIO(fit_bytes)
    fp.name = name
    return client.connectapi(
        "/upload-service/upload/.fit",
        method="POST",
        files={"userfile": (fp.name, fp, "application/octet-stream")},
    )


def _upload_success(result) -> bool:
    """判断上传响应是否成功，兼容多种返回结构。"""
    if result is None:
        return False
    if isinstance(result, bool):
        return result
    if not isinstance(result, dict):
        return True
    try:
        det = result.get("detailedImportResult") or {}
        if det.get("successes"):
            return True
        if det.get("failures"):
            return False
        # 无 detailedImportResult 时退化为直接 successes / uploadId 判断
        if result.get("successes"):
            return True
        return "uploadId" in result or "uploadId" in det
    except AttributeError:
        return True


def run_weight_sync(session_factory, garmin) -> SyncLog:
    session = session_factory()
    log = SyncLog(started_at=datetime.utcnow(), status="running", kind="weight")
    session.add(log)
    session.commit()
    try:
        st = get_setting(session)
        account = session.query(XiaomiAccount).filter_by(logged_in=True).first()
        if account is None:
            raise XiaomiError(
                "小米账户未绑定，请先在设置中填写 userId / passToken 并验证"
            )

        # 时间窗口：独立于活动同步，使用体重同步自己的周期设置
        window_days = config.SYNC_WINDOW_DAYS.get(st.weight_sync_window, 90)
        end = int(time.time())
        start = end - window_days * 86400

        # ---- 小米拉取 ----
        client = XiaomiClient()
        try:
            client.set_credentials(
                account.user_id, account.ssecurity, account.pass_token
            )
            records = client.get_weight_data(start, end)
        except XiaomiError:
            # ssecurity 可能失效（长期未用），重新登录后重试一次
            logger.info("小米凭据可能失效，重新登录…")
            creds = client.login_from_token(account.user_id, account.pass_token)
            account.ssecurity = creds["ssecurity"]
            account.pass_token = creds["pass_token"]
            account.updated_at = datetime.utcnow()
            session.commit()
            client.set_credentials(
                account.user_id, account.ssecurity, account.pass_token
            )
            records = client.get_weight_data(start, end)

        valid = [r for r in records if r.get("weight") and r["weight"] > 0]
        log.fetched_count = len(valid)  # 抓取（有效）条数
        log.fetch_failed = len(records) - len(valid)  # 读取到但无效被过滤的条数
        logger.info(
            "体重同步：窗口内记录 %d 条（有效 %d 条，无效 %d 条）",
            len(records), len(valid), log.fetch_failed,
        )

        # ---- 按时间戳去重 ----
        existing = {
            ts
            for (ts,) in session.query(SyncedWeight.ts)
            .filter(SyncedWeight.ts >= start)
            .all()
        }
        pending = [r for r in valid if int(r["timestamp"]) not in existing]
        # 历史（之前已同步过、本次跳过）条数，双区相同，供日志展示
        log.history_count = len(valid) - len(pending)
        if existing:
            logger.info(
                "体重同步：已同步过 %d 条，本次待上传 %d 条",
                len(valid) - len(pending), len(pending),
            )

        if not pending:
            log.status = "success"
            log.finished_at = datetime.utcnow()
            session.commit()
            logger.info("体重同步：无新增记录，跳过上传")
            return log

        # ---- 固定双区上传：国际区 + 中国区（未登录的区域自动跳过） ----
        fit_bytes = fit_files.build_weight_fit(pending)
        uploaded: list[str] = []  # 上传成功的区域
        failed: list[str] = []  # 已登录但上传失败的区域
        skipped: list[str] = []  # 未登录被跳过的区域
        for region, domain in WEIGHT_TARGETS:
            tgt = garmin.get_client(region, domain)
            if tgt is None:
                skipped.append(region)
                continue
            try:
                result = _upload_fit(
                    tgt, fit_bytes, f"weight_{start}_{region}.fit"
                )
            except Exception as e:
                failed.append(region)
                setattr(log, f"{region}_failed", len(pending))
                logger.exception("体重上传到 %s 失败：%s", region, e)
                continue
            if _upload_success(result):
                uploaded.append(region)
                setattr(log, f"{region}_synced", len(pending))
            else:
                failed.append(region)
                setattr(log, f"{region}_failed", len(pending))
                logger.warning(
                    "体重上传到 %s 失败：%s", region, str(result)[:500]
                )

        def _region_detail(region: str) -> str:
            """构造区域结果明细，如「中国区失败 2 条」。"""
            if region in failed:
                return f"{TARGET_NAMES[region]}失败 {getattr(log, region + '_failed')} 条"
            if region in skipped:
                return f"{TARGET_NAMES[region]}未登录跳过"
            return f"{TARGET_NAMES[region]}成功 {getattr(log, region + '_synced')} 条"

        if not uploaded:
            if skipped and not failed:
                raise XiaomiError(
                    "Garmin 账户未登录（国际区/中国区至少需登录一个），无法上传体重数据"
                )
            log.upload_failed = len(pending)
            log.status = "error"
            log.error = "全部目标区上传失败: " + "；".join(
                _region_detail(r) for r in failed
            )
        else:
            # 至少一个区域上传成功 → 记录去重，避免下次重复上传
            for r in pending:
                session.add(SyncedWeight(ts=int(r["timestamp"]), weight=r["weight"]))
            session.commit()
            log.synced_count = len(pending)
            if failed:
                log.status = "partial"
                log.upload_failed = len(pending)
                log.error = "部分目标区上传失败: " + "；".join(
                    _region_detail(r) for r in failed
                )
            else:
                log.status = "success"
                log.error = ""

        log.finished_at = datetime.utcnow()
        session.commit()
        logger.info(
            "体重同步结束：读取 %d 条（有效 %d / 无效 %d），"
            "国际区成功 %d 失败 %d，中国区成功 %d 失败 %d，跳过：%s",
            len(records), len(valid), log.fetch_failed,
            log.intl_synced, log.intl_failed,
            log.china_synced, log.china_failed,
            ",".join(TARGET_NAMES[r] for r in skipped) or "-",
        )
    except Exception as e:
        session.rollback()
        log.status = "error"
        log.error = str(e)[:4000]
        log.finished_at = datetime.utcnow()
        session.commit()
        logger.exception("体重同步过程异常：%s", e)
    finally:
        try:
            session.expunge(log)
        except Exception:
            pass
        # 体重日志只保留最近 500 条，超出自动删除最旧的，防止无限增长
        try:
            prune_sync_logs(session, "weight")
        except Exception:
            pass
        session.close()
    return log
