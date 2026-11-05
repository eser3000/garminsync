"""模块⑤编排：佳明中国区 ⇄ 国际区**全量数据**对比、互相补全。

覆盖两类可通过 FIT 通道互补的数据：
- 活动（activities）：见 app/services/region_activity.py，按窗口内两区全量
  活动列表（beginTimestamp）对比，互相上传缺失的原始 FIT；
- 健康（wellness）：睡眠 / 心率 / 压力 / 身体电量 / 血氧 / 呼吸 / 步数等
  每日监控原始 FIT，见下方说明。

两类结果汇总到同一条日志（kind="health"），设置项与其它模块均不受影响。

健康数据「双区全量对比、互相补全」：
- 下载：GET {connectapi}/download-service/files/wellness/{YYYY-MM-DD}（按 UTC
  日期逐日请求，返回 zip，内含当日全部监控 FIT）。中国区与国际区各下载一份，
  得到两区在所选周期内的**全量**文件集合；双时间戳 /wellness/{start}/{end}
  要求毫秒级时间戳，传秒级会被解析成 1970 年导致全部 404，故仍按单日请求。
- 对比：以文件内容 SHA256 作为数据身份，两区集合的并集＝全量数据；只存在于
  A 区的文件即判定 B 区缺失，从 A 区取原始 FIT 上传到 B 区，双向互补，
  最终两区都持有全量数据。
- 设置项不变：仍受 health_enabled 控制、固定每天 03:00、周期仍由
  health_sync_window 决定（1day/1week/1month/3months），中国区必须登录；
  国际区未登录时降级为「仅下载中国区、不上传」。
- 去重：以平台真实数据对比为主判据；另按 (内容哈希, 目标区) 记录已上传过的
  文件，避免 Garmin 归档不收录原始 FIT（此时服务端只会返回 409 Duplicate）
  造成每轮重复 POST。服务端明确返回 Duplicate 也补记为已同步。
"""
import hashlib
import io
import logging
import time
import zipfile
from datetime import datetime

import app.config as config
from app.models import (
    SyncedHealthTarget,
    SyncLog,
    get_setting,
    prune_sync_logs,
)
from app.time_utils import cn_date
from garth.exc import GarthHTTPError

logger = logging.getLogger("garminsync")

TARGET_NAMES = {"china": "中国区", "intl": "国际区"}


def _download_wellness(client, start: int, end: int) -> list:
    """从一个 Garmin 区域下载单日（UTC 日期）的每日监控数据 zip。

    参数 start/end 为秒级时间戳，仅用于确定日期（同一天），实际请求按
    GarminDB 验证过的单日格式：/download-service/files/wellness/{YYYY-MM-DD}。

    返回 [(filename, fit_bytes), ...]；zip 内仅取 .fit 成员。

    Garmin 在无健康数据的时间段（未佩戴/无记录）返回 404，属于正常情况，
    此时返回空列表而不是抛异常，避免整个同步被标记为失败。
    """
    # 请求按 UTC 日期（Garmin 归档按 UTC 自然日，GarminDB 验证过的格式）；
    # 日志展示统一用东八区日期（cn_date），与窗口日志一致。
    date = datetime.utcfromtimestamp(start).strftime("%Y-%m-%d")
    try:
        data = client.download(f"/download-service/files/wellness/{date}")
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


def _collect_region(client, start: int, end: int, region_name: str):
    """逐日下载一个区域在窗口内的全量健康 FIT。

    返回 (files, downloaded, no_data_days, error_days, errors)：
    - files: {sha256: (filename, fit_bytes)}，同一内容的文件只保留一份；
    - downloaded: 下载到的文件总数（按内容去重前）；
    - no_data_days: Garmin 返回 404 的天数；
    - error_days/errors: 非 404 的下载失败天数与明细。

    单日失败只记录并继续；调用方据此判定该区域集合是否完整——集合不完整时
    不向该区域补全，避免把「没读到」误判成「对方独有」而重复上传。
    """
    files: dict[str, tuple[str, bytes]] = {}
    downloaded = 0
    no_data_days = 0
    error_days = 0
    errors = []
    for day_start in range(start, end, 86400):
        day_end = min(day_start + 86400, end)
        try:
            day_files = _download_wellness(client, day_start, day_end)
        except Exception as e:  # noqa: BLE001
            error_days += 1
            errors.append(f"{region_name} {cn_date(day_start)}: {str(e)[:160]}")
            logger.warning(
                "健康数据 %s %s 下载失败：%s", region_name, cn_date(day_start), e
            )
            continue
        if not day_files:
            no_data_days += 1
            continue
        downloaded += len(day_files)
        for name, fit_bytes in day_files:
            digest = hashlib.sha256(fit_bytes).hexdigest()
            files.setdefault(digest, (name, fit_bytes))
    logger.info(
        "健康数据：%s 下载 %d 个文件，按内容去重后 %d 个（无数据 %d 天，失败 %d 天）",
        region_name, downloaded, len(files), no_data_days, error_days,
    )
    return files, downloaded, no_data_days, error_days, errors


def _upload_fit(client, fit_bytes: bytes, name: str):
    """将 FIT 文件上传到指定区域 Garmin Connect（与活动同步同一通道）。"""
    fp = io.BytesIO(fit_bytes)
    fp.name = name
    return client.connectapi(
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
    "Duplicate Wellness File."，即服务端已存在该文件，应视为“已同步过”。
    """
    return "duplicate" in (text or "").lower()


def _try_upload(client, fit_bytes: bytes, name: str) -> tuple[bool, bool, str]:
    """上传单个 FIT，返回 (ok, duplicate, message)。

    duplicate=True 表示服务端已存在该文件（无需再次上传），ok 仍为 True。
    """
    try:
        result = _upload_fit(client, fit_bytes, name)
    except GarthHTTPError as exc:
        # HTTP 层错误（如 409 Conflict）：响应体里通常带着 Garmin
        # 拒绝导入的真实原因，必须带进日志才能定位。
        resp = getattr(getattr(exc, "error", None), "response", None)
        body = ""
        if resp is not None:
            try:
                body = resp.text[:500]
            except Exception:
                body = ""
        if _is_duplicate_error(body):
            return True, True, "服务端已存在（重复导入）"
        return False, False, f"{exc} {body}"[:300]
    except Exception as e:  # noqa: BLE001
        return False, False, str(e)[:300]

    if not _upload_succeeded(result):
        det = (result or {}).get("detailedImportResult", {}) or {}
        msgs = " ".join(str(m) for m in (det.get("failures") or []))
        if _is_duplicate_error(msgs):
            return True, True, "服务端已存在（重复导入）"
        return False, False, msgs[:300]
    return True, False, ""


def run_health_sync(session_factory, garmin) -> SyncLog:
    session = session_factory()
    log = SyncLog(started_at=datetime.utcnow(), status="running", kind="health")
    session.add(log)
    session.commit()
    try:
        # 中国区登录是下载的前提（必须）；国际区未登录时降级为仅下载、不补全
        cn = garmin.get_client("china", "garmin.cn")
        if cn is None:
            log.status = "error"
            log.error = "中国区账户未登录"
            log.finished_at = datetime.utcnow()
            session.commit()
            logger.error("健康数据同步中止：中国区账户未登录")
            return log

        intl = garmin.get_client("intl", "garmin.com")  # 可选

        # 同步周期：仍使用健康数据自己的周期设置（选项与取值均不变）
        st = get_setting(session)
        window = config.SYNC_WINDOW_DAYS.get(
            st.health_sync_window, config.HEALTH_WINDOW_DAYS
        )
        end = int(time.time())
        start = end - window * 86400

        logger.info(
            "健康数据同步：窗口 %d 天（%s ~ %s，国际区已登录=%s），双区全量对比补全",
            window,
            cn_date(start),
            cn_date(end),
            intl is not None,
        )

        # ---- ① 两区各自下载窗口内全量健康数据 ----
        cn_files, cn_downloaded, cn_no_data, cn_err_days, cn_errors = (
            _collect_region(cn, start, end, "中国区")
        )
        if intl is not None:
            intl_files, intl_downloaded, intl_no_data, intl_err_days, intl_errors = (
                _collect_region(intl, start, end, "国际区")
            )
        else:
            intl_files, intl_downloaded = {}, 0
            intl_no_data, intl_err_days, intl_errors = 0, 0, []

        # ---- ② 对比全量：并集＝全量数据，交集＝两区均已完整 ----
        cn_hashes = set(cn_files)
        intl_hashes = set(intl_files)
        all_hashes = cn_hashes | intl_hashes
        both = cn_hashes & intl_hashes
        log.read_count = len(all_hashes)
        log.fetched_count = cn_downloaded + intl_downloaded
        log.history_count = len(both)

        # 读取失败过的区域集合不完整：不向该区域补全，避免重复上传
        trusted = {
            "china": cn_err_days == 0,
            "intl": intl is not None and intl_err_days == 0,
        }

        # ---- ③ 组装互相补全任务（只存在于 A 区 → 上传到 B 区） ----
        pending = []
        for digest in sorted(all_hashes):
            if digest in both:
                continue
            if digest in cn_files:
                if trusted["intl"]:
                    name, fit_bytes = cn_files[digest]
                    pending.append(("intl", intl, name, fit_bytes, digest))
            elif trusted["china"]:
                name, fit_bytes = intl_files[digest]
                pending.append(("china", cn, name, fit_bytes, digest))
        logger.info(
            "健康数据对比：全量 %d 个（中国区 %d / 国际区 %d），两区均已完整 %d 个，待互相补全 %d 个",
            len(all_hashes), len(cn_hashes), len(intl_hashes), len(both),
            len(pending),
        )

        # ---- ④ 逐区补全；按 (hash, region) 去重，避免重复 POST ----
        marks = {
            (h, r)
            for (h, r) in session.query(
                SyncedHealthTarget.file_hash, SyncedHealthTarget.region
            ).all()
        }
        counters = {
            "china": {"synced": 0, "failed": 0},
            "intl": {"synced": 0, "failed": 0},
        }
        errors = []
        skipped_marked = 0   # 已补全过、平台归档暂未收录，跳过重复上传
        dup_remote = 0       # 服务端已存在（Duplicate），补记为已同步

        for region, client, name, fit_bytes, digest in pending:
            key = (digest, region)
            if key in marks:
                skipped_marked += 1
                continue
            ok, duplicate, msg = _try_upload(client, fit_bytes, name)
            if ok:
                session.add(
                    SyncedHealthTarget(
                        file_hash=digest, region=region, filename=name
                    )
                )
                session.commit()
                marks.add(key)
                counters[region]["synced"] += 1
                if duplicate:
                    dup_remote += 1
                logger.info(
                    "健康数据 [%s] 补全到%s%s",
                    name, TARGET_NAMES[region],
                    "（服务端已存在，补记去重）" if duplicate else "",
                )
            else:
                counters[region]["failed"] += 1
                errors.append(f"{name}→{TARGET_NAMES[region]}:{msg}")
                logger.warning(
                    "健康数据补全失败 [%s → %s]: %s",
                    name, TARGET_NAMES[region], msg,
                )

        log.china_synced = counters["china"]["synced"]
        log.china_failed = counters["china"]["failed"]
        log.intl_synced = counters["intl"]["synced"]
        log.intl_failed = counters["intl"]["failed"]
        log.synced_count = log.china_synced + log.intl_synced
        log.upload_failed = log.china_failed + log.intl_failed
        log.intl_history_count = skipped_marked

        detail = []
        if cn_no_data or intl_no_data:
            detail.append(
                f"无数据跳过 中国区 {cn_no_data} 天 / 国际区 {intl_no_data} 天"
            )
        if intl is None:
            detail.append(
                "国际区未登录，仅下载中国区数据，未执行对比补全"
                "（登录国际区后再次同步即可双向补全）"
            )
        if cn_err_days or intl_err_days:
            detail.append(
                f"下载失败 中国区 {cn_err_days} 天 / 国际区 {intl_err_days} 天"
                "（失败区域本轮不向其补全，避免重复上传）"
            )
        if cn_errors or intl_errors:
            detail.append(
                "下载失败明细: " + "; ".join((cn_errors + intl_errors)[:10])
            )
        if both:
            detail.append(f"两区均已完整 {len(both)} 个")
        if skipped_marked:
            detail.append(
                f"已补全过、平台归档暂未收录 {skipped_marked} 个（跳过重复上传）"
            )
        if dup_remote:
            detail.append(f"服务端已存在 {dup_remote} 个（补记已同步）")
        if errors:
            detail.append("补全失败(" + str(len(errors)) + "): " + "; ".join(errors))
        # ---- ⑤ 活动数据：两区全量对比、互相补全（与健康数据汇总到同一日志） ----
        from app.services.region_activity import sync_activities

        try:
            act = sync_activities(session, cn, intl, window)
        except Exception as e:  # noqa: BLE001
            logger.exception("活动数据互补过程异常：%s", e)
            act = {
                "total": 0, "both": 0,
                "china_synced": 0, "intl_synced": 0, "failed": 0,
                "unavailable": True,
                "detail": [f"活动数据互补异常：{str(e)[:200]}"],
            }
        log.act_count = act["total"]
        log.act_both = act["both"]
        log.act_china_synced = act["china_synced"]
        log.act_intl_synced = act["intl_synced"]
        log.act_failed = act["failed"]
        detail.extend(act["detail"])

        if len(all_hashes) == 0 and not (cn_err_days or intl_err_days):
            # 整个窗口两区都无健康数据（404 已在下载层吞掉），正常完成
            detail.append("该时间段两区均无健康数据文件（Garmin 返回 404）")
        log.error = "; ".join(detail)[:4000]

        if len(all_hashes) == 0 and (cn_err_days or intl_err_days):
            status = "error"
        elif intl is None or cn_err_days or intl_err_days:
            status = "partial"
        elif log.upload_failed == 0:
            status = "success"
        elif log.synced_count > 0:
            status = "partial"
        else:
            status = "error"
        # 活动互补失败或未执行（未登录/列表读取失败）时整体降为 partial；
        # 反过来健康部分全失败但活动有补全成功，也不应误标成 error。
        if act.get("unavailable") or act.get("failed"):
            if status == "success":
                status = "partial"
        if status == "error" and (
            act.get("china_synced") or act.get("intl_synced")
        ):
            status = "partial"
        log.status = status

        log.finished_at = datetime.utcnow()
        session.commit()
        logger.info(
            "模块⑤全量同步结束：健康 全量 %d 个（下载 %d 个，已完整 %d 个），"
            "补全 中国区 %d 失败 %d / 国际区 %d 失败 %d，已传过跳过 %d 个；"
            "活动 全量 %d 条（已完整 %d 条），补全 中国区 %d / 国际区 %d，"
            "失败 %d 条%s",
            len(all_hashes), log.fetched_count, len(both),
            log.china_synced, log.china_failed,
            log.intl_synced, log.intl_failed, skipped_marked,
            log.act_count, log.act_both,
            log.act_china_synced, log.act_intl_synced, log.act_failed,
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
