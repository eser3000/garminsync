"""活动数据双区互补：中国区 ⇄ 国际区全量活动对比，互相上传缺失的 FIT。

模块⑤「双区全量数据互补」的活动部分。复用 app.services.sync 中已验证过的
活动列表拉取与 FIT 下载实现：
- 列表：GET /activitylist-service/activities（分页、倒序、越过窗口下界停止）
- 下载：GET /download-service/files/activity/{id} → 原始数据 zip 内的 .fit
- 上传：POST /upload-service/upload/.fit（multipart 字段 userfile）

身份判据：活动开始时间 beginTimestamp（毫秒）在两区一致，作为全量集合的键
（与活动同步模块①的去重口径相同）。
去重：synced_region_activities 按 (活动键, 目标区) 记录已上传过的活动，
避免平台列表尚未收录、或服务端返回 Duplicate 时每轮重复 POST。

本模块只返回统计、不写 SyncLog；由 health_sync.run_health_sync 统一汇总到
模块⑤的一条日志里。
"""
import io
import logging

from app.models import SyncedRegionActivity
from app.services.sync import _download_fit, _fetch_activities_in_window
from garth.exc import GarthHTTPError

logger = logging.getLogger("garminsync")

TARGET_NAMES = {"china": "中国区", "intl": "国际区"}


def _activity_key(act: dict) -> str:
    """活动身份：开始时间（毫秒）。同一时刻视为同一条活动。"""
    return str(act.get("beginTimestamp") or 0)


def _collect(client, window_days: int, region_name: str):
    """拉取一个区域窗口内的全量活动，返回 ({key: 活动条目}, 错误信息)。

    列表拉取失败时返回空集合与错误：读取不完整时调用方会跳过本轮互补，
    避免把「没读到」误判成「对方独有」而重复上传。
    """
    try:
        acts = _fetch_activities_in_window(client, window_days)
    except Exception as e:  # noqa: BLE001
        msg = f"{region_name}活动列表拉取失败：{str(e)[:200]}"
        logger.warning(msg)
        return {}, msg
    items = {}
    for act in acts:
        if not isinstance(act, dict):
            continue
        key = _activity_key(act)
        if key == "0":
            continue
        items.setdefault(key, act)
    logger.info(
        "活动数据：%s 窗口内 %d 条（按开始时间去重后 %d 条）",
        region_name, len(acts), len(items),
    )
    return items, ""


def _upload_fit(client, fit_bytes: bytes, name: str):
    """将原始活动 FIT 上传到指定区域（与模块①同一条上传通道）。"""
    fp = io.BytesIO(fit_bytes)
    fp.name = name
    return client.connectapi(
        "/upload-service/upload/.fit",
        method="POST",
        files={"userfile": (fp.name, fp, "application/octet-stream")},
    )


def _try_upload(client, fit_bytes: bytes, name: str) -> tuple[bool, bool, str]:
    """上传单条活动 FIT，返回 (ok, duplicate, message)。"""
    try:
        result = _upload_fit(client, fit_bytes, name)
    except GarthHTTPError as exc:
        resp = getattr(getattr(exc, "error", None), "response", None)
        body = ""
        if resp is not None:
            try:
                body = resp.text[:500]
            except Exception:
                body = ""
        if "duplicate" in (body or "").lower():
            return True, True, "服务端已存在（重复导入）"
        return False, False, f"{exc} {body}"[:300]
    except Exception as e:  # noqa: BLE001
        return False, False, str(e)[:300]

    det = {}
    if isinstance(result, dict):
        det = result.get("detailedImportResult", {}) or {}
    msgs = " ".join(str(m) for m in (det.get("failures") or []))
    if msgs and "duplicate" not in msgs.lower():
        return False, False, msgs[:300]
    return True, "duplicate" in msgs.lower(), ""


def sync_activities(session, cn, intl, window_days: int) -> dict:
    """执行活动数据的双区全量对比、互相补全，返回统计 dict（不写日志）。

    两区都需登录；任一区未登录或活动列表读取失败时返回 unavailable=True，
    本轮不执行互补，避免重复上传。
    """
    result = {
        "total": 0,
        "both": 0,
        "china_synced": 0,
        "intl_synced": 0,
        "failed": 0,
        "downloaded": 0,
        "skipped": 0,
        "dup": 0,
        "unavailable": False,
        "detail": [],
        "errors": [],
    }
    if cn is None or intl is None:
        result["unavailable"] = True
        result["detail"].append("国际区未登录，未执行活动数据互补")
        return result

    cn_items, cn_err = _collect(cn, window_days, "中国区")
    intl_items, intl_err = _collect(intl, window_days, "国际区")
    if cn_err or intl_err:
        result["unavailable"] = True
        result["detail"].extend([m for m in (cn_err, intl_err) if m])
        result["detail"].append("活动列表读取不完整，本轮不执行互补以避免重复上传")
        return result

    cn_keys = set(cn_items)
    intl_keys = set(intl_items)
    all_keys = cn_keys | intl_keys
    both = cn_keys & intl_keys
    result["total"] = len(all_keys)
    result["both"] = len(both)

    # (活动键, 目标区, 源客户端, 目标客户端, 源活动条目)
    pending = []
    for key in sorted(all_keys):
        if key in both:
            continue
        if key in cn_items:
            pending.append((key, "intl", cn, intl, cn_items[key]))
        else:
            pending.append((key, "china", intl, cn, intl_items[key]))
    logger.info(
        "活动对比：全量 %d 条（中国区 %d / 国际区 %d），两区已完整 %d 条，待互补 %d 条",
        len(all_keys), len(cn_keys), len(intl_keys), len(both), len(pending),
    )

    marks = {
        (k, r)
        for (k, r) in session.query(
            SyncedRegionActivity.activity_key, SyncedRegionActivity.region
        ).all()
    }
    for key, region, src_client, tgt_client, act in pending:
        if (key, region) in marks:
            result["skipped"] += 1
            continue
        activity_id = str(act.get("activityId") or "")
        if not activity_id:
            result["failed"] += 1
            result["errors"].append(f"{key}: 源活动缺少 activityId")
            continue
        try:
            fit_bytes = _download_fit(src_client, activity_id)
        except Exception as e:  # noqa: BLE001
            result["failed"] += 1
            result["errors"].append(f"{activity_id} 下载失败：{str(e)[:160]}")
            logger.warning("活动互补下载失败 [%s]: %s", activity_id, e)
            continue
        result["downloaded"] += 1
        ok, duplicate, msg = _try_upload(
            tgt_client, fit_bytes, f"{activity_id}.fit"
        )
        if ok:
            session.add(
                SyncedRegionActivity(
                    activity_key=key,
                    region=region,
                    source_activity_id=activity_id,
                )
            )
            session.commit()
            marks.add((key, region))
            result[region + "_synced"] += 1
            if duplicate:
                result["dup"] += 1
            logger.info(
                "活动 [%s] 补全到%s%s",
                activity_id, TARGET_NAMES[region],
                "（服务端已存在，补记去重）" if duplicate else "",
            )
        else:
            result["failed"] += 1
            result["errors"].append(f"{activity_id}→{TARGET_NAMES[region]}:{msg}")
            logger.warning(
                "活动互补上传失败 [%s → %s]: %s",
                activity_id, TARGET_NAMES[region], msg,
            )

    result["detail"].append(
        f"活动：全量 {result['total']} 条（中国区 {len(cn_keys)} / 国际区 "
        f"{len(intl_keys)}），两区已完整 {result['both']} 条，"
        f"补全 中国区 {result['china_synced']} / 国际区 {result['intl_synced']}，"
        f"失败 {result['failed']} 条"
    )
    if result["skipped"]:
        result["detail"].append(
            f"活动已补全过、平台列表暂未收录 {result['skipped']} 条（跳过重复上传）"
        )
    if result["dup"]:
        result["detail"].append(
            f"活动服务端已存在 {result['dup']} 条（补记已同步）"
        )
    if result["errors"]:
        result["detail"].append(
            "活动失败明细: " + "; ".join(result["errors"][:10])
        )
    return result
