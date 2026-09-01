"""同步编排：将中国区活动下载（抓取）并上传到国际区与 iGPSPORT（迹驰）。

下载与上传实现对齐开源项目 dailysync（@gooin/garmin-connect）：
- 下载：GET {connectapi}/download-service/files/activity/{id} 返回的是**原始数据 zip**，
  需解压后取其中的 .fit 文件（dailysync 的 downloadOriginalActivityData + 解压）。
- 上传：POST {connectapi}/upload-service/upload/.fit（URL 带 .fit 后缀），
  multipart 字段名为 userfile（dailysync 的 uploadActivity）。

去重以目标平台真实数据为准（国际区按 beginTimestamp、迹驰按开始时间分钟级比对），
本地 SyncedActivity 标记仅作平台查询失败/未登录时的兜底。
"""
import io
import logging
import time
import zipfile
from datetime import datetime

import app.config as config
from app.models import SyncedActivity, SyncLog, get_setting, prune_sync_logs
from app.services.igpsport import (
    _cn_start_key,
    _igp_start_key,
    _to_meters,
    get_service,
)

logger = logging.getLogger("garminsync")

# 迹驰防重复保护窗口（秒）：本地已标记上传过、但平台活动列表查不到这条时，
# 若距上次上传仍在此窗口内，判定为平台入库/列表同步滞后，本轮不重传，
# 避免"刚传完又传一遍"。超过窗口仍未收录才按换号/清库处理重新上传。
IGP_REUPLOAD_GUARD_SECONDS = 6 * 3600

# 距离配对容差：两个平台对同一活动的里程计算会有小幅差异，
# 取「绝对值 200 米」与「相对 2%」中较大的一个。
IGP_DISTANCE_TOL_ABS = 200.0
IGP_DISTANCE_TOL_REL = 0.02


def _claim_day_distance(day_dist: dict, day: str, dist) -> bool:
    """日期命中时，用里程判断平台当天的活动是否已全部"认领"完。

    返回 True 表示平台已有对应活动（跳过上传）。
    里程可用时消耗最接近的那条：同一天骑多次时，平台已有的几条会被逐条
    认领掉，多出来的那条（平台确实没有）才会被上传。
    里程缺失或对不上时保守返回 True——宁可漏传，也不重复上传。
    """
    lst = day_dist.get(day)
    if not lst:
        return False  # 平台当天的活动都已对上号 → 这条是新的，应当上传
    if dist is None:
        lst.pop(0)
        return True
    # 单位兜底：佳明若用公里、平台用米（相差约 1000 倍），按量级自动换算，
    # 避免单位不一致导致配对全部失效（失效只会退回保守跳过，不会重复上传）。
    vals = [d for d in lst if d]
    if vals:
        ratio = (sum(vals) / len(vals)) / dist
        if 800 < ratio < 1250:
            dist *= 1000
        elif 0.0008 < ratio < 0.00125:
            dist /= 1000
    best_i, best_diff = -1, None
    for i, d in enumerate(lst):
        if d is None:
            continue
        diff = abs(d - dist)
        tol = max(IGP_DISTANCE_TOL_ABS, dist * IGP_DISTANCE_TOL_REL, d * IGP_DISTANCE_TOL_REL)
        if diff <= tol and (best_diff is None or diff < best_diff):
            best_i, best_diff = i, diff
    lst.pop(best_i if best_i >= 0 else 0)
    return True


def _upload_fit(intl, fp):
    """将 FIT 文件上传到国际区 Garmin Connect（对齐 dailysync）。

    - URL 带 `.fit` 后缀：`/upload-service/upload/.fit`；
    - multipart 字段名是 `userfile`；
    - part 显式指定 application/octet-stream（form-data 对文件流的默认类型），
      避免 garth Client.upload() 用 mimetypes 猜不出 .fit 类型导致 415。
    """
    return intl.connectapi(
        "/upload-service/upload/.fit",
        method="POST",
        files={"userfile": (fp.name, fp, "application/octet-stream")},
    )


def _download_fit(cn, activity_id: str) -> bytes:
    """从中国区下载活动原始数据并解出 FIT 字节（对齐 dailysync）。

    下载接口返回的是原始数据 **zip**（内含 .fit），取其中第一个 .fit 成员；
    若返回的已是裸 FIT 则直接使用。
    """
    data = cn.download(f"/download-service/files/activity/{activity_id}")
    if not data:
        raise RuntimeError("下载活动文件为空")
    if data[:2] == b"PK":  # zip 压缩包
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = z.namelist()
            fit_name = next(
                (n for n in names if n.lower().endswith(".fit")), names[0]
            )
            logger.info("活动 [%s] 下载为 zip，取成员 %s", activity_id, fit_name)
            return z.read(fit_name)
    if len(data) >= 12 and data[8:12] != b".FIT":
        logger.warning(
            "活动 [%s] 下载内容既非 zip 也非 FIT，前16字节: %s",
            activity_id, data[:16].hex(),
        )
    return data


def _fetch_activities_in_window(cn, window_days: int) -> list:
    """分页拉取中国区活动，仅保留最近 window_days 天内的（活动按时间倒序返回）。"""
    cutoff = time.time() * 1000 - window_days * 86400 * 1000  # beginTimestamp 为毫秒
    result = []
    start = 0
    page = 50
    safety = 2000  # 最多翻 2000 条，防止异常死循环
    while start <= safety:
        batch = cn.connectapi(
            "/activitylist-service/activities",
            params={"start": start, "limit": page},
        )
        if not batch:
            break
        # 兼容多种响应结构：数组直接处理；对象尝试常见列表键；否则视为接口异常
        if isinstance(batch, dict):
            items = None
            for key in ("results", "activities", "items", "activityList"):
                if isinstance(batch.get(key), list):
                    items = batch[key]
                    break
            if items is None:
                raise RuntimeError(
                    "活动接口返回异常结构(非数组/未找到列表键): " + str(batch)[:500]
                )
            batch = items
        elif not isinstance(batch, list):
            # 接口返回了字符串等异常内容（如错误提示页），把内容带进错误信息便于排查
            raise RuntimeError(
                "活动接口返回异常类型 %s(内容): %s"
                % (type(batch).__name__, str(batch)[:500])
            )
        if not batch:
            break
        hit_older = False
        for act in batch:
            if not isinstance(act, dict):
                logger.warning("抓取阶段：活动条目类型异常，跳过: %s", str(act)[:200])
                continue
            ts = act.get("beginTimestamp") or 0
            if ts >= cutoff:
                result.append(act)
            else:
                # 已越过窗口下界（倒序），后续都更旧
                hit_older = True
        if hit_older:
            break
        start += page
    return result


def _fetch_intl_timestamps(intl, window_days: int) -> dict:
    """拉取国际区窗口内活动的 {beginTimestamp: activityId} 映射，用于上传前去重。

    与抓取端同样的分页与结构兼容逻辑：活动按时间倒序返回，直到越过窗口下界。
    国际区已存在的活动（相同 beginTimestamp）不再重复上传。
    """
    cutoff = time.time() * 1000 - window_days * 86400 * 1000  # 毫秒
    mapping = {}
    start = 0
    page = 100
    safety = 2000  # 最多翻 2000 条，防止异常死循环
    while start <= safety:
        batch = intl.connectapi(
            "/activitylist-service/activities",
            params={"start": start, "limit": page},
        )
        if not batch:
            break
        if isinstance(batch, dict):
            items = None
            for key in ("results", "activities", "items", "activityList"):
                if isinstance(batch.get(key), list):
                    items = batch[key]
                    break
            batch = items if items is not None else []
        elif not isinstance(batch, list):
            batch = []
        if not batch:
            break
        hit_older = False
        for act in batch:
            if not isinstance(act, dict):
                continue
            ts = act.get("beginTimestamp") or 0
            if ts >= cutoff:
                mapping[ts] = str(act.get("activityId", ""))
            else:
                hit_older = True
        if hit_older:
            break
        start += page
    return mapping


def run_sync(session_factory, garmin) -> SyncLog:
    session = session_factory()
    log = SyncLog(started_at=datetime.utcnow(), status="running")
    session.add(log)
    session.commit()
    try:
        # 中国区登录是抓取的前提（必须）；国际区可选，未登录则只抓取、不上传
        cn = garmin.get_client("china", "garmin.cn")
        if cn is None:
            log.status = "error"
            log.error = "中国区账户未登录"
            log.finished_at = datetime.utcnow()
            session.commit()
            logger.error("同步中止：中国区账户未登录")
            return log

        intl = garmin.get_client("intl", "garmin.com")  # 可选

        window = config.SYNC_WINDOW_DAYS.get(get_setting(session).sync_window, 90)
        activities = _fetch_activities_in_window(cn, window)
        # 国际区窗口内已知活动（beginTimestamp -> intl activityId），用于上传前去重
        intl_known = _fetch_intl_timestamps(intl, window) if intl else {}
        # 一次性加载本地同步记录，供国际区/迹驰去重复用
        activity_ids = [str(a.get("activityId")) for a in activities]
        synced_map = {
            s.china_activity_id: s
            for s in session.query(SyncedActivity)
            .filter(SyncedActivity.china_activity_id.in_(activity_ids))
        }
        logger.info(
            "抓取阶段：窗口内活动 %d 条（国际区已登录=%s，国际区已知活动 %d 条）",
            len(activities), intl is not None, len(intl_known),
        )

        fetched = 0          # 抓取（下载 FIT）成功条数
        synced = 0           # 同步（上传国际区）成功条数
        upload_failed = 0    # 同步失败条数
        history = 0          # 历史（之前已同步过）条数
        skipped = 0          # 抓取成功但国际区未登录、暂未上传条数
        intl_dup = 0         # 国际区已存在、跳过上传条数
        intl_reup = 0        # 国际区本地标记失效（平台无此活动）、重新上传条数
        fetch_errors = []    # 抓取失败原因
        upload_errors = []   # 同步失败原因
        # 迹驰（iGPSPORT）：已登录时随活动同步一并执行（同一日志）
        igp_synced = 0       # 上传迹驰成功条数
        igp_failed = 0       # 上传迹驰失败条数
        igp_history = 0      # 迹驰平台已存在、跳过条数
        igp_done = 0         # 本地已有迹驰标记：平台无则重传 / 平台查询失败则跳过
        igp_pending = 0      # 平台暂未收录（入库延迟窗口内），本轮暂不重传
        igp_errors = []      # 迹驰同步失败原因

        for act in activities:
            activity_id = str(act.get("activityId"))
            existing = synced_map.get(activity_id)

            # ---- 国际区去重：以国际区平台真实数据为准（与迹驰同策略） ----
            if intl is not None:
                ts = act.get("beginTimestamp") or 0
                intl_id = intl_known.get(ts)
                if intl_id:
                    # 平台已有同一时刻活动：跳过，并同步修正本地标记
                    if existing is None:
                        existing = SyncedActivity(
                            china_activity_id=activity_id,
                            intl_activity_id=intl_id,
                        )
                        session.add(existing)
                        synced_map[activity_id] = existing
                    elif not existing.intl_activity_id:
                        existing.intl_activity_id = intl_id
                    session.commit()
                    intl_dup += 1
                    logger.info(
                        "国际区已存在活动 [%s]（intl id=%s），跳过上传",
                        activity_id, intl_id,
                    )
                    continue
                if existing is not None and existing.intl_activity_id:
                    # 平台没有但本地标记有：标记失效（国际区被删/换号），
                    # 以平台为准重新上传
                    intl_reup += 1
                    logger.info(
                        "国际区平台无活动 [%s] 但本地有标记，重新上传",
                        activity_id,
                    )
            else:
                # 国际区未登录：按本地记录保守跳过（避免重复上传）
                if existing is not None:
                    history += 1
                    continue

            # ---- 抓取：从中国区下载原始数据并解出 FIT ----
            try:
                fit_bytes = _download_fit(cn, activity_id)
                fetched += 1
            except Exception as e:
                fetch_errors.append(f"{activity_id}:{e}")
                logger.warning("抓取失败 [%s]: %s", activity_id, e)
                continue  # 没拿到文件，无法上传

            # ---- 同步：上传到国际区（未登录则跳过） ----
            if intl is None:
                skipped += 1
                logger.info("国际区未登录，跳过上传 [%s]", activity_id)
                continue

            try:
                fp = io.BytesIO(fit_bytes)
                fp.name = f"{activity_id}.fit"
                result = _upload_fit(intl, fp)
                intl_id = ""
                try:
                    det = result.get("detailedImportResult", {})
                    if det.get("successes"):
                        intl_id = str(det["successes"][0].get("activityId", ""))
                except Exception:
                    pass

                if existing is None:
                    existing = SyncedActivity(
                        china_activity_id=activity_id,
                        intl_activity_id=intl_id,
                    )
                    session.add(existing)
                    synced_map[activity_id] = existing
                else:
                    existing.intl_activity_id = intl_id
                session.commit()
                synced += 1
            except Exception as e:
                upload_failed += 1
                upload_errors.append(f"{activity_id}:{e}")
                logger.warning("同步失败 [%s]: %s", activity_id, e)

        # ---- 迹驰（iGPSPORT）上传：已登录时随活动同步一并执行 ----
        igp_svc = get_service()
        igp_logged_in = bool(igp_svc.status().get("logged_in"))
        if igp_logged_in:
            # 平台级去重：拉取迹驰平台真实活动比对，以平台为准（避免本地标记
            # 失效/换号后出现"平台没有却跳过不传"的死区）。
            igp_known = None  # None 表示平台查询失败，退化为本地标记兜底
            igp_day_dist: dict = {}  # 日期 -> 平台里程列表（元素可能是 None）
            try:
                igp_acts = igp_svc.get_activities()
                igp_known = {_igp_start_key(a["start_time"]) for a in igp_acts}
                igp_known.discard("")
                for a in igp_acts:
                    k = _igp_start_key(a["start_time"])
                    if k:
                        igp_day_dist.setdefault(k[:10], []).append(a.get("distance"))
            except Exception as e:
                igp_errors.append(f"迹驰活动查询失败：{e}")
                logger.error("迹驰活动查询失败：%s", e)

            # 两边时间点样例写入运行日志：平台与 Garmin 的时间格式/时区对不上时
            # （表现为平台永远匹配不上、每轮都重传），从运行日志就能对比。
            if igp_known is None:
                logger.warning("迹驰去重：平台活动列表不可用，本轮退化为本地标记兜底")
            else:
                cn_sample = [
                    _cn_start_key(a.get("beginTimestamp") or 0) for a in activities[:3]
                ]
                igp_sample = sorted(igp_known)[-3:]
                logger.info(
                    "迹驰去重：平台已有 %d 个时间点，最新 %s；本轮中国区时间点 %s",
                    len(igp_known),
                    igp_sample,
                    cn_sample,
                )
                raw = getattr(igp_svc, "last_raw_sample", None)
                if raw:
                    logger.info("迹驰活动原始字段：%s", raw)
                # 预检：本地已标记上传过的活动，在平台列表里应当能匹配上。
                # 若一条都匹配不上，说明两边时间口径不一致（时区/格式），
                # 此时"平台没有"是假象，重传只会造成重复上传，
                # 故退化为本地标记兜底，宁可不传也不重复。
                marked_keys = []
                for a in activities:
                    m = synced_map.get(str(a.get("activityId")))
                    if m is not None and m.igp_activity_id:
                        k = _cn_start_key(a.get("beginTimestamp") or 0)
                        if k:
                            marked_keys.append(k)
                            marked_keys.append(k[:10])  # 日期级，同上
                if marked_keys and not any(k in igp_known for k in marked_keys):
                    logger.warning(
                        "迹驰去重：已上传的 %d 条在平台列表中一条都匹配不上，"
                        "疑似时间口径不一致，本轮改按本地标记判断，避免重复上传",
                        len(marked_keys),
                    )
                    igp_known = None

            for act in activities:
                activity_id = str(act.get("activityId"))
                row = synced_map.get(activity_id)
                key = _cn_start_key(act.get("beginTimestamp") or 0)
                day_key = key[:10]  # 日期级：平台列表可能只返回日期（如 "2026.08.29"）
                dist = _to_meters(act.get("distance") or act.get("Distance"))
                if igp_known is not None:
                    # ① 平台已有同一时刻活动：跳过，并同步修正本地标记。
                    #    分钟级精确匹配直接命中；否则退化为日期级，并用里程配对，
                    #    这样同一天骑多次时也能正确区分哪条平台已有。
                    hit = bool(key and key in igp_known)
                    if not hit and day_key and day_key in igp_known:
                        hit = _claim_day_distance(igp_day_dist, day_key, dist)
                    if hit:
                        if row is None:
                            row = SyncedActivity(
                                china_activity_id=activity_id,
                                igp_activity_id="(exists)",
                            )
                            session.add(row)
                        elif not row.igp_activity_id:
                            row.igp_activity_id = "(exists)"
                        session.commit()
                        igp_history += 1
                        continue
                    # ② 平台没有但本地标记有：两种可能——换号/平台数据被清（应重传），
                    #    或平台列表滞后、刚上传的活动尚未收录（重传会重复）。
                    #    用时间窗区分：仍在入库延迟窗口内则等平台收录，不重传。
                    if row is not None and row.igp_activity_id:
                        if (
                            row.synced_at
                            and (datetime.utcnow() - row.synced_at).total_seconds()
                            < IGP_REUPLOAD_GUARD_SECONDS
                        ):
                            igp_pending += 1
                            logger.info(
                                "迹驰平台暂未收录 [%s]，距上次上传 %.1f 小时内，暂不重传",
                                activity_id,
                                IGP_REUPLOAD_GUARD_SECONDS / 3600,
                            )
                            continue
                        igp_done += 1
                else:
                    # 平台查询失败：保守处理，信任本地标记与历史挡板，避免重复上传
                    if row is not None and row.igp_activity_id:
                        igp_done += 1
                        continue
                # 平台没有此活动 → 上传迹驰
                try:
                    fit_bytes = _download_fit(cn, activity_id)
                except Exception as e:
                    igp_failed += 1
                    igp_errors.append(f"{activity_id}:{e}")
                    logger.warning("iGPSPORT 抓取失败 [%s]: %s", activity_id, e)
                    continue
                try:
                    igp_id = igp_svc.upload_fit(
                        fit_bytes, f"garmin_{activity_id}.fit"
                    )
                    if not igp_id:
                        # 平台已接收但响应未返回 rideId：写占位标记，
                        # 保证平台查询失败时的本地兜底去重仍然生效，避免重复上传
                        igp_id = "(uploaded)"
                    if row is None:
                        row = SyncedActivity(
                            china_activity_id=activity_id,
                            igp_activity_id=igp_id,
                        )
                        session.add(row)
                    else:
                        row.igp_activity_id = igp_id
                    # 记录本次迹驰上传时间：防重复保护窗口以此为基准
                    row.synced_at = datetime.utcnow()
                    session.commit()
                    igp_synced += 1
                except Exception as e:
                    igp_failed += 1
                    igp_errors.append(f"{activity_id}:{e}")
                    logger.warning("iGPSPORT 上传失败 [%s]: %s", activity_id, e)

        # 汇总原因明细
        detail = []
        if intl is None and skipped:
            detail.append(
                f"国际区未登录，已抓取 {skipped} 条但未上传"
                f"（登录国际区后再次同步即可上传）"
            )
        if intl_reup:
            detail.append(f"国际区本地标记失效（平台无此活动），重新上传 {intl_reup} 条")
        # 迹驰"平台已存在"属正常情况，与国际区一样只在汇总行体现，不额外说明
        if igp_pending:
            detail.append(
                f"迹驰平台暂未收录 {igp_pending} 条（刚上传，等待入库，暂不重传）"
            )
        if igp_done:
            detail.append(f"迹驰平台未收录、本地标记过期，重新上传 {igp_done} 条")
        if not igp_logged_in and activities:
            detail.append("迹驰未登录，未执行迹驰同步")
        if fetch_errors:
            detail.append("抓取失败(" + str(len(fetch_errors)) + "): " + "; ".join(fetch_errors))
        if upload_errors:
            detail.append("同步失败(" + str(len(upload_errors)) + "): " + "; ".join(upload_errors))
        if igp_errors:
            detail.append("迹驰失败(" + str(len(igp_errors)) + "): " + "; ".join(igp_errors))
        log.read_count = len(activities)
        log.fetched_count = fetched
        log.synced_count = synced
        log.upload_failed = upload_failed
        log.history_count = history
        log.intl_history_count = history + intl_dup
        log.intl_synced = synced
        log.intl_failed = upload_failed
        log.igp_synced = igp_synced
        log.igp_failed = igp_failed
        log.igp_history_count = igp_history
        log.igp_done = igp_done
        log.error = "; ".join(detail)[:4000]

        if intl is None:
            log.status = "partial" if fetched > 0 else "error"
        elif upload_failed == 0 and not fetch_errors and not igp_errors:
            log.status = "success"
        elif synced > 0 or fetched > 0 or igp_synced > 0:
            log.status = "partial"
        else:
            log.status = "error"

        log.finished_at = datetime.utcnow()
        session.commit()
        logger.info(
            "同步结束：读取 %d 条，国际区 历史 %d 成功 %d 失败 %d"
            "（抓取 %d，未登录跳过 %d），迹驰 已传 %d 历史 %d 成功 %d 失败 %d%s",
            len(activities), history + intl_dup + intl_reup, synced, upload_failed,
            fetched, skipped, igp_done, igp_history, igp_synced, igp_failed,
            f"，原因：{log.error}" if log.error else "",
        )
    except Exception as e:
        session.rollback()
        log.status = "error"
        log.error = str(e)[:4000]
        log.finished_at = datetime.utcnow()
        session.commit()
        logger.exception("同步过程异常：%s", e)
    finally:
        # expunge 使 log 脱离 session 但仍保留已加载的内存属性，
        # 这样返回的 log 对象可在其 session 关闭后被安全读取。
        try:
            session.expunge(log)
        except Exception:
            pass
        # 活动日志只保留最近 500 条，超出自动删除最旧的，防止无限增长
        try:
            prune_sync_logs(session, "activity")
        except Exception:
            pass
        session.close()
    return log
