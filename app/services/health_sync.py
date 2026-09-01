"""健康数据同步编排：将中国区每日监控数据原始 FIT 转发上传到国际区。

同步范围：睡眠 / 心率 / 压力 / 身体电量 / 血氧 / 呼吸 / 步数等每日监控数据
（即 Garmin 手表记录的 wellness / daily monitoring 数据）。

技术方案对齐开源项目 GarminDB / garmin-daily-monitor 等社区验证过的做法：
- 下载：GET {connectapi}/download-service/files/wellness/{YYYY-MM-DD}
  按单日（UTC 日期）逐日请求，返回 zip，内含每日监控（monitor 类型）的
  原始 FIT（睡眠/心率/压力/血氧/身体电量/呼吸/步数均在其中）。
  注意：双时间戳参数格式 /wellness/{start}/{end} 要求毫秒级时间戳，
  传秒级时间戳会被解析成 1970 年导致全部 404（此前在中国区持续复现）。
- 上传：POST {connectapi}/upload-service/upload/.fit（与活动同步同一通道，
  直接转发原始 FIT，避免自行构造消息导致数据失真）。
- 去重：按文件内容 SHA256 记录在 synced_health 表，重复文件不再上传。
"""
import hashlib
import io
import logging
import time
import zipfile
from datetime import datetime

import app.config as config
from app.models import SyncedHealth, SyncLog, get_setting, prune_sync_logs
from app.time_utils import cn_date
from garth.exc import GarthHTTPError

logger = logging.getLogger("garminsync")


def _download_wellness(cn, start: int, end: int) -> list:
    """从中国区下载单日（UTC 日期）的每日监控数据 zip。

    参数为秒级时间戳，仅用于确定日期（同一天），实际请求按 GarminDB
    验证过的单日格式：/download-service/files/wellness/{YYYY-MM-DD}。

    返回 [(filename, fit_bytes), ...]；zip 内仅取 .fit 成员。

    Garmin 在无健康数据的时间段（未佩戴/无记录）返回 404，属于正常情况，
    此时返回空列表而不是抛异常，避免整个同步被标记为失败。
    """
    # 请求按 UTC 日期（Garmin 归档按 UTC 自然日，GarminDB 验证过的格式）；
    # 日志展示统一用东八区日期（cn_date），与窗口日志一致。
    date = datetime.utcfromtimestamp(start).strftime("%Y-%m-%d")
    try:
        data = cn.download(f"/download-service/files/wellness/{date}")
    except GarthHTTPError as exc:
        resp = getattr(getattr(exc, "error", None), "response", None)
        if resp is not None and resp.status_code == 404:
            logger.info(
                "健康数据：%s 无数据（404），跳过该天",
                cn_date(start),
            )
            return []
        raise
    if not data:
        raise RuntimeError("健康数据下载为空")
    files = []
    if data[:2] == b"PK":  # zip 压缩包
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for n in z.namelist():
                if n.lower().endswith(".fit"):
                    files.append((n.split("/")[-1], z.read(n)))
    else:
        raise RuntimeError(
            "健康数据接口返回异常内容（非 zip），前16字节: " + data[:16].hex()
        )
    if not files:
        raise RuntimeError("健康数据 zip 中未找到 FIT 文件")
    return files


def _upload_fit(intl, fp):
    """将 FIT 文件上传到国际区 Garmin Connect（与活动同步同通道）。"""
    return intl.connectapi(
        "/upload-service/upload/.fit",
        method="POST",
        files={"userfile": (fp.name, fp, "application/octet-stream")},
    )


def _upload_succeeded(result) -> bool:
    """上传响应中无 failures 视为成功（garth 不抛异常且 Garmin 未拒绝）。"""
    if not isinstance(result, dict):
        return True
    det = result.get("detailedImportResult", {}) or {}
    return not (det.get("failures") or [])


def _is_duplicate_error(text: str) -> bool:
    """判断 Garmin 响应是否为“文件已存在”的重复导入错误（409 Duplicate）。

    健康数据重复上传时 Garmin 返回 409 + "Duplicate Metrics File." /
    "Duplicate Wellness File."，即服务端已存在该文件。此时若本地
    synced_health 没有对应 SHA256 记录（例如数据库记录曾丢失），
    文件会被反复重试上传并一直报错，应视为“已同步过”并补记去重。
    """
    return "duplicate" in (text or "").lower()


def run_health_sync(session_factory, garmin) -> SyncLog:
    session = session_factory()
    log = SyncLog(started_at=datetime.utcnow(), status="running", kind="health")
    session.add(log)
    session.commit()
    try:
        # 中国区登录是下载的前提（必须）；国际区可选，未登录则只下载、不上传
        cn = garmin.get_client("china", "garmin.cn")
        if cn is None:
            log.status = "error"
            log.error = "中国区账户未登录"
            log.finished_at = datetime.utcnow()
            session.commit()
            logger.error("健康数据同步中止：中国区账户未登录")
            return log

        intl = garmin.get_client("intl", "garmin.com")  # 可选

        # 同步周期：独立于活动同步，使用健康数据自己的周期设置（默认近 1 个月）
        st = get_setting(session)
        window = config.SYNC_WINDOW_DAYS.get(
            st.health_sync_window, config.HEALTH_WINDOW_DAYS
        )
        end = int(time.time())
        start = end - window * 86400

        synced = 0          # 上传成功
        upload_failed = 0   # 上传失败
        skipped = 0         # 国际区未登录未上传
        dup = 0             # 已同步过跳过
        dup_remote = 0      # 国际区已存在（重复文件），补记去重标记已同步
        errors = []
        fetched = 0         # 下载到的文件总数（含重复/跳过）
        no_data_days = 0    # 无数据的天数（Garmin 404）

        logger.info(
            "健康数据同步：窗口 %d 天（%s ~ %s，国际区已登录=%s）",
            window,
            cn_date(start),
            cn_date(end),
            intl is not None,
        )

        # 按天切分下载：每天一个请求，某天无数据（404）只跳过当天，不影响其他天；
        # 每个文件按内容 SHA256 去重，已同步过的不再上传。
        for day_start in range(start, end, 86400):
            day_end = min(day_start + 86400, end)
            files = _download_wellness(cn, day_start, day_end)
            if not files:
                no_data_days += 1
                continue
            fetched += len(files)
            logger.info(
                "健康数据：%s 下载 %d 个文件",
                cn_date(day_start),
                len(files),
            )
            for name, fit_bytes in files:
                digest = hashlib.sha256(fit_bytes).hexdigest()
                if (
                    session.query(SyncedHealth)
                    .filter_by(file_hash=digest)
                    .first()
                ):
                    dup += 1
                    continue

                if intl is None:
                    skipped += 1
                    continue

                try:
                    fp = io.BytesIO(fit_bytes)
                    fp.name = name
                    result = _upload_fit(intl, fp)
                    if not _upload_succeeded(result):
                        det = (result or {}).get("detailedImportResult", {}) or {}
                        msgs = " ".join(str(m) for m in (det.get("failures") or []))
                        if _is_duplicate_error(msgs):
                            # 服务端已存在（重复导入）：补记去重，避免下次再试
                            dup_remote += 1
                            session.add(SyncedHealth(file_hash=digest, filename=name))
                            session.commit()
                            logger.warning(
                                "健康数据 [%s] 国际区已存在（重复），标记为已同步",
                                name,
                            )
                            continue
                        upload_failed += 1
                        errors.append(f"{name}:{msgs[:200]}")
                        logger.warning("健康数据上传被拒 [%s]: %s", name, msgs[:200])
                        continue
                    session.add(SyncedHealth(file_hash=digest, filename=name))
                    session.commit()
                    synced += 1
                except GarthHTTPError as exc:
                    # HTTP 层错误（如 409 Conflict）：响应体里通常带着 Garmin
                    # 拒绝导入的真实原因（文件类型不支持 / 数据冲突 / 重复导入），
                    # 必须打进日志才能定位，仅打 str(exc) 只有状态码信息不足。
                    resp = getattr(getattr(exc, "error", None), "response", None)
                    body = ""
                    if resp is not None:
                        try:
                            body = resp.text[:500]
                        except Exception:
                            body = ""
                    if _is_duplicate_error(body):
                        # 服务端已存在该文件（本地记录可能曾丢失）：补记去重，
                        # 标记为已同步，避免每次同步都重复尝试上传同一文件。
                        dup_remote += 1
                        session.add(SyncedHealth(file_hash=digest, filename=name))
                        session.commit()
                        logger.warning(
                            "健康数据 [%s] 国际区已存在（重复），标记为已同步%s",
                            name, f" | 响应体: {body[:120]}" if body else "",
                        )
                        continue
                    upload_failed += 1
                    errors.append(f"{name}:{exc} {body}")
                    logger.warning(
                        "健康数据上传失败 [%s]: %s%s", name, exc,
                        f" | 响应体: {body}" if body else "",
                    )
                except Exception as e:
                    upload_failed += 1
                    errors.append(f"{name}:{e}")
                    logger.warning("健康数据上传失败 [%s]: %s", name, e)

        detail = []
        if no_data_days:
            detail.append(f"无数据跳过 {no_data_days} 天")
        if intl is None and skipped:
            detail.append(
                f"国际区未登录，已下载 {skipped} 个文件但未上传"
                f"（登录国际区后再次同步即可上传）"
            )
        if dup:
            detail.append(f"已同步过，跳过 {dup} 个")
        if dup_remote:
            detail.append(
                f"国际区已存在 {dup_remote} 个重复文件（本地记录缺失），已标记为已同步"
            )
        if errors:
            detail.append("上传失败(" + str(len(errors)) + "): " + "; ".join(errors))
        log.fetched_count = fetched
        log.synced_count = synced
        log.upload_failed = upload_failed
        log.error = "; ".join(detail)[:4000]

        if fetched == 0:
            # 整个窗口无健康数据（404 已在下载层吞掉），正常完成
            log.status = "success"
            log.error = "该时间段无健康数据文件（Garmin 返回 404）"
        elif intl is None:
            log.status = "partial"
        elif upload_failed == 0:
            log.status = "success"
        elif synced > 0:
            log.status = "partial"
        else:
            log.status = "error"

        log.finished_at = datetime.utcnow()
        session.commit()
        logger.info(
            "健康数据同步结束：下载 %d 个，上传成功 %d 个，失败 %d 个，"
            "未上传 %d 个，重复跳过 %d 个，服务端已存在补记 %d 个%s",
            fetched, synced, upload_failed, skipped, dup, dup_remote,
            f"，原因：{log.error}" if log.error else "",
        )
    except Exception as e:
        session.rollback()
        log.status = "error"
        log.error = str(e)[:4000]
        log.finished_at = datetime.utcnow()
        session.commit()
        logger.exception("健康数据同步过程异常：%s", e)
    finally:
        # expunge 使 log 脱离 session 但仍保留已加载的内存属性，
        # 这样返回的 log 对象可在其 session 关闭后被安全读取。
        try:
            session.expunge(log)
        except Exception:
            pass
        # 健康日志只保留最近 500 条，超出自动删除最旧的，防止无限增长
        try:
            prune_sync_logs(session, "health")
        except Exception:
            pass
        session.close()
    return log
