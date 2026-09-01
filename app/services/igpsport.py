"""iGPSPORT（迹驰）集成：OAuth 登录、token 刷新、活动查询（去重）与 FIT 上传。

技术来源（开源社区逆向并多版本实测验证）：
- 登录：OAuth 2.0 password grant，客户端标识 `qiwu.mobile`（iGPSPORT App 内部
  移动端客户端），拿到 Bearer access_token + refresh_token（offline_access）。
- 上传：网页版同款 OSS 直传三段式：
  1) GET getSignedUrl?fileExtension=.fit  -> {data:{ossId, signedUrl}}
  2) PUT 文件字节到阿里云 OSS（不带 Content-Type，否则签名校验 403）
  3) POST uploadByOss {fileName, ossName}  -> code 200/0，data 为活动 id
- 去重：GET queryMyActivity 分页拉已有活动，按开始时间与本地记录双重比对。

⚠️ 非官方公开 API，随 App 更新可能变动；个人自用等价于官方 App 手动上传。
"""
from __future__ import annotations  # Python 3.9 兼容（PEP 604 联合类型注解）

import json
import logging
import re
import threading
from datetime import datetime, timedelta, timezone

import requests

from app.db import SessionLocal
from app.models import IgpsportAccount

logger = logging.getLogger("garminsync")

_CN_TZ = timezone(timedelta(hours=8))

# ---- 接口常量（来自开源社区逆向验证） ----
OAUTH_URL = "https://oauth.zh.igpsport.com/connect/token"
CLIENT_ID = "qiwu.mobile"
CLIENT_SECRET = "0d9b5544-6871-2999-13a9-59198788054b"
SCOPE = (
    "openid offline_access mobile.api user.api device.api activity.api "
    "IdentityServerApi"
)
API_BASE = "https://prod.zh.igpsport.com/service"
QUERY_ACTIVITY_URL = (
    API_BASE + "/web-gateway/web-analyze/activity/queryMyActivity"
)
GET_SIGNED_URL = API_BASE + "/sportg/third-party-server/oss/getSignedUrl"
UPLOAD_BY_OSS = (
    API_BASE + "/web-gateway/web-analyze/activity/uploadByOss"
)
PAGE_SIZE = 20  # iGPSPORT 每页最多 20
# 用户资料（含头像）：移动端接口，返回 data.avatar / data.nickName 等
USER_INFO_URL = API_BASE + "/mobile/api/User/UserInfo"

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class IgpsportError(RuntimeError):
    pass


class LoginExpired(IgpsportError):
    """登录态失效（401/403 或刷新失败），需要用户重新登录。"""


def _auth_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "User-Agent": _UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Origin": "https://app.igpsport.cn",
        "Referer": "https://app.igpsport.cn/",
    }


def _cn_start_key(ms: int) -> str:
    """Garmin beginTimestamp（毫秒，UTC）-> 东八区分钟级 key "YYYY-MM-DD HH:MM"。"""
    try:
        return datetime.fromtimestamp(ms / 1000, tz=_CN_TZ).strftime(
            "%Y-%m-%d %H:%M"
        )
    except (OverflowError, OSError, ValueError):
        return ""


# 匹配 "2026.08.29" / "2026-08-29 10:00:00" / "2026/8/9" 等写法，
# 时分可选（平台实测有两种：精确到秒，或只有日期且用点分隔）。
_DATE_RE = re.compile(
    r"(\d{4})\D+(\d{1,2})\D+(\d{1,2})(?:\D+(\d{1,2})\D+(\d{1,2}))?"
)


def _igp_start_key(s) -> str:
    """iGPSPORT 开始时间归一化为可与 Garmin 比对的 key。

    平台返回格式不固定，实测两种：
    - 精确到秒："2026-08-29 10:00:00" -> 分钟级 key "2026-08-29 10:00"
    - 只有日期且用点分隔："2026.08.29" -> 日期级 key "2026-08-29"

    只有日期时无法做到分钟级，返回日期级 key，由调用方按日期比对。
    """
    s = str(s or "").strip()
    if not s:
        return ""
    if s.isdigit():
        ms = int(s)
        if ms > 10 ** 12:  # 毫秒
            ms //= 1000
        try:
            return datetime.fromtimestamp(ms, tz=_CN_TZ).strftime(
                "%Y-%m-%d %H:%M"
            )
        except (OverflowError, OSError, ValueError):
            return ""
    m = _DATE_RE.match(s)
    if not m:
        return ""
    year, month, day, hour, minute = m.groups()
    date_key = f"{year}-{int(month):02d}-{int(day):02d}"
    if hour is not None and minute is not None:
        return f"{date_key} {int(hour):02d}:{int(minute):02d}"
    return date_key


def _pick_start_time(item: dict) -> str:
    """从活动条目中挑选信息最完整的开始时间原始值。

    平台不同版本字段名与精度不一：实测新版列表接口只给日期（"2026.08.29"），
    旧版网页接口另有 StartTimeString 可能带时分。这里遍历候选字段，
    优先返回能解析出分钟级的那个（key 长度 16），其次是日期级（长度 10）。
    """
    best, best_key = "", ""
    for k in (
        "startTimeString",
        "StartTimeString",
        "startTimeStr",
        "StartTime",
        "startTime",
        "rideStartTime",
        "beginTime",
        "startDate",
        "createTime",
    ):
        key = _igp_start_key(item.get(k))
        if not key:
            continue
        if len(key) > len(best_key):
            best, best_key = str(item.get(k)), key
    return best


def _to_meters(v) -> float | None:
    """距离统一为米（平台 rideDistance 即米）；无法解析或非正数返回 None。"""
    try:
        d = float(v)
    except (TypeError, ValueError):
        return None
    return d if d > 0 else None


class IgpsportService:
    # 最近一次活动查询的首条原始字段（排障用：确认平台到底返回了哪些时间字段）
    last_raw_sample: dict | None = None

    def __init__(self, session_factory=SessionLocal):
        self.Session = session_factory
        self._token_lock = threading.Lock()

    # ---------- 凭据存取 ----------
    def _load(self) -> IgpsportAccount | None:
        with self.Session() as s:
            acc = s.get(IgpsportAccount, 1)
            if acc is None:
                return None
            # 拷贝为普通对象，避免 session 关闭后访问 detached 属性报错
            return IgpsportAccount(
                id=1,
                username=acc.username,
                access_token=acc.access_token,
                refresh_token=acc.refresh_token,
                expires_at=acc.expires_at,
                logged_in=acc.logged_in,
                avatar_url=acc.avatar_url,
                nick_name=acc.nick_name,
            )

    def _save(
        self,
        username: str,
        access_token: str,
        refresh_token: str,
        expires_at: datetime | None,
        logged_in: bool,
        avatar_url: str | None = None,
        nick_name: str | None = None,
        igp_diag: str | None = None,
    ):
        with self.Session() as s:
            acc = s.get(IgpsportAccount, 1)
            if acc is None:
                acc = IgpsportAccount(id=1)
                s.add(acc)
            acc.username = username
            acc.access_token = access_token
            acc.refresh_token = refresh_token
            acc.expires_at = expires_at
            acc.logged_in = logged_in
            if avatar_url is not None:
                acc.avatar_url = avatar_url
            if nick_name is not None:
                acc.nick_name = nick_name
            if igp_diag is not None:
                acc.igp_diag = igp_diag
            acc.updated_at = datetime.utcnow()
            s.commit()

    # ---------- OAuth ----------
    def _token_request(self, payload: dict) -> dict:
        """发一次 OAuth 请求，解析出 tokens；失败抛 IgpsportError。"""
        try:
            r = requests.post(
                OAUTH_URL,
                auth=(CLIENT_ID, CLIENT_SECRET),
                data=payload,
                timeout=30,
            )
        except requests.RequestException as e:
            raise IgpsportError(f"iGPSPORT 网络请求失败: {e}")
        try:
            res = r.json()
        except ValueError:
            raise IgpsportError(f"iGPSPORT 登录返回异常: HTTP {r.status_code}")
        if r.status_code != 200 or "access_token" not in res:
            err = res.get("error_description") or res.get("error") or res
            raise IgpsportError(f"iGPSPORT 登录失败(HTTP {r.status_code}): {err}")
        return res

    def login(self, username: str, password: str) -> dict:
        """密码登录（OAuth password grant），成功即持久化 tokens。"""
        res = self._token_request(
            {
                "grant_type": "password",
                "username": username,
                "password": password,
                "scope": SCOPE,
            }
        )
        expires_in = int(res.get("expires_in", 3600))
        expires_at = datetime.utcnow() + timedelta(seconds=expires_in)
        access_token = res["access_token"]
        refresh_token = res.get("refresh_token", "")
        display = username
        if not refresh_token:
            logger.warning("iGPSPORT 登录未返回 refresh_token，token 过期后需重新登录")
        # 登录成功后拉一次用户资料，拿到头像与昵称（失败不影响登录）
        avatar_url, nick_name, igp_diag = "", "", ""
        try:
            info = self.get_user_info(access_token)
            avatar_url = info.get("avatar") or ""
            nick_name = info.get("nickName") or ""
            igp_diag = (
                f"OK avatar={avatar_url!r} nickName={nick_name!r} "
                f"keys={sorted(info.keys())}"
            )
        except (IgpsportError, LoginExpired) as e:
            logger.warning("iGPSPORT 获取用户资料失败（不影响登录）: %s", e)
            igp_diag = f"FAIL {e}"
        self._save(
            username=display,
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=expires_at,
            logged_in=True,
            avatar_url=avatar_url,
            nick_name=nick_name,
            igp_diag=igp_diag,
        )
        logger.info("iGPSPORT 登录成功：%s", nick_name or display)
        return {"ok": True, "username": nick_name or display, "avatar_url": avatar_url}

    def _invalidate(self, username: str, reason: str):
        """清空登录态并落库：token 失效后页面立即显示"未登录"，避免假登录误导。"""
        try:
            self._save(
                username=username,
                access_token="",
                refresh_token="",
                expires_at=None,
                logged_in=False,
            )
        except Exception:
            pass
        logger.warning("iGPSPORT 登录已失效（%s），已自动登出", reason)

    def _refresh(self, acc: IgpsportAccount) -> str:
        """用 refresh_token 换新 access_token；失败清空登录态并抛 LoginExpired。"""
        if not acc.refresh_token:
            self._invalidate(acc.username, "refresh_token 缺失")
            raise LoginExpired("iGPSPORT refresh_token 缺失，登录已失效，请重新登录")
        try:
            res = self._token_request(
                {
                    "grant_type": "refresh_token",
                    "refresh_token": acc.refresh_token,
                    "scope": SCOPE,
                }
            )
        except IgpsportError as e:
            self._invalidate(acc.username, f"token 刷新失败: {e}")
            raise LoginExpired(f"iGPSPORT 登录已失效，请重新登录（{e}）")
        expires_in = int(res.get("expires_in", 3600))
        self._save(
            username=acc.username,
            access_token=res["access_token"],
            refresh_token=res.get("refresh_token", acc.refresh_token),
            expires_at=datetime.utcnow() + timedelta(seconds=expires_in),
            logged_in=True,
        )
        logger.info("iGPSPORT token 已刷新")
        return res["access_token"]

    def _ensure_token(self) -> str:
        """返回有效 access_token；过期则自动刷新，失效则抛 LoginExpired。"""
        acc = self._load()
        if acc is None or not acc.access_token or not acc.logged_in:
            raise LoginExpired("iGPSPORT 未登录")
        if acc.expires_at and datetime.utcnow() < acc.expires_at - timedelta(seconds=60):
            return acc.access_token
        with self._token_lock:
            # 双重检查：另一个线程可能已刷新
            acc = self._load()
            if acc.expires_at and datetime.utcnow() < acc.expires_at - timedelta(seconds=60):
                return acc.access_token
            try:
                return self._refresh(acc)
            except IgpsportError as e:
                raise LoginExpired(f"iGPSPORT 登录已过期，请重新登录（{e}）")

    def get_user_info(self, token: str) -> dict:
        """拉取用户资料（昵称 / 头像等）。失败抛 IgpsportError / LoginExpired。"""
        try:
            r = requests.get(
                USER_INFO_URL, headers=_auth_headers(token), timeout=30
            )
        except requests.RequestException as e:
            raise IgpsportError(f"iGPSPORT 用户资料请求失败: {e}")
        if r.status_code in (401, 403):
            raise LoginExpired(
                f"iGPSPORT 登录已过期，请重新登录（HTTP {r.status_code}）"
            )
        if r.status_code != 200:
            raise IgpsportError(
                f"iGPSPORT 用户资料 HTTP {r.status_code}: {r.text[:300]}"
            )
        try:
            body = r.json()
        except ValueError:
            raise IgpsportError(f"iGPSPORT 用户资料返回非 JSON: {r.text[:300]}")
        code = body.get("code")
        if code not in (0, 200):
            msg = body.get("message", "未知错误")
            if "token" in str(msg).lower() or "登录" in str(msg):
                raise LoginExpired(f"iGPSPORT 登录已过期，请重新登录（{msg}）")
            raise IgpsportError(
                f"iGPSPORT 用户资料失败: {msg} (code={code}); body={r.text[:300]}"
            )
        return body.get("data") or {}

    def logout(self):
        with self.Session() as s:
            acc = s.get(IgpsportAccount, 1)
            if acc is not None:
                acc.logged_in = False
                acc.access_token = ""
                acc.refresh_token = ""
                s.commit()
        logger.info("iGPSPORT 已登出")

    def status(self) -> dict:
        acc = self._load()
        return {
            "logged_in": bool(acc and acc.logged_in and acc.access_token),
            "username": (acc.nick_name or acc.username) if acc else "",
            "avatar_url": acc.avatar_url if acc else "",
            "igp_diag": acc.igp_diag if acc else "",
        }

    # ---------- 活动查询（去重用） ----------
    def get_activities(self, max_pages: int = 50) -> list[dict]:
        """分页拉取 iGPSPORT 已有活动，返回 [{start_time, ride_id}]。

        服务端不支持 offset，只能按页翻（每页最多 20 条）。
        返回项 start_time 为平台原始字符串（如 "2025-08-29 10:00:00"）。
        """
        token = self._ensure_token()
        headers = _auth_headers(token)
        result: list[dict] = []
        for page in range(1, max_pages + 1):
            try:
                r = requests.get(
                    QUERY_ACTIVITY_URL,
                    headers=headers,
                    params={
                        "pageNo": page,
                        "pageSize": PAGE_SIZE,
                        "reqType": 0,
                        "sort": 1,
                    },
                    timeout=30,
                )
            except requests.RequestException as e:
                raise IgpsportError(f"iGPSPORT 活动查询请求失败: {e}")
            if r.status_code in (401, 403):
                raise LoginExpired("iGPSPORT 登录已过期，请重新登录")
            if r.status_code != 200:
                raise IgpsportError(
                    f"iGPSPORT 活动查询 HTTP {r.status_code}: {r.text[:200]}"
                )
            body = r.json()
            code = body.get("code")
            if code not in (0, 200):
                msg = body.get("message", body.get("data", "未知错误"))
                if "token" in str(msg).lower() or "登录" in str(msg):
                    raise LoginExpired("iGPSPORT 登录已过期，请重新登录")
                raise IgpsportError(f"iGPSPORT 活动查询失败: {msg} (code={code})")
            data = body.get("data") or {}
            rows = data.get("rows") or []
            for item in rows:
                ride_id = (
                    item.get("RideId")
                    or item.get("rideId")
                    or item.get("id")
                    or ""
                )
                if not ride_id:
                    continue
                start_time = _pick_start_time(item)
                if not self.last_raw_sample:
                    # 留一份首条原始字段，便于确认平台到底给了哪些时间字段
                    self.last_raw_sample = {
                        k: str(v)[:40] for k, v in item.items()
                    }
                result.append(
                    {
                        "ride_id": str(ride_id),
                        "start_time": str(start_time),
                        "distance": _to_meters(
                            item.get("rideDistance") or item.get("distance")
                        ),
                    }
                )
            if len(rows) < PAGE_SIZE:
                break
        # 平台字段与预期不符（取不到开始时间）时，宁可判定查询失败让上层退化为
        # 本地兜底，也不能把"时间未知"的活动当成"平台没有"，否则每轮都会重复上传。
        if result:
            valid = sum(1 for a in result if _igp_start_key(a["start_time"]))
            if valid < len(result) // 2:
                raise IgpsportError(
                    "iGPSPORT 活动列表未能解析出开始时间，无法用于去重"
                )
        logger.info("iGPSPORT 已有活动 %d 条", len(result))
        return result

    def _extract_ride_id(self, data) -> str:
        """从 uploadByOss 响应的 data 字段提取平台活动 id。

        兼容字符串 / 数字 / 字典（键名因平台版本不同而有差异）。
        无法提取时返回空串，调用方据此判定"平台未真正入库"。
        """
        if data is None:
            return ""
        if isinstance(data, dict):
            for key in ("rideId", "RideId", "id", "activityId", "ActivityId", "data"):
                v = data.get(key)
                if v is not None and str(v).strip():
                    return str(v).strip()
            return ""
        return str(data).strip()

    # ---------- FIT 上传（OSS 直传三段式） ----------
    def upload_fit(self, fit_bytes: bytes, filename: str) -> str:
        """上传 FIT 到 iGPSPORT，成功返回平台活动 id（rideId）。

        流程：getSignedUrl -> PUT 到阿里云 OSS -> uploadByOss 通知落库。
        失败抛 IgpsportError / LoginExpired。
        """
        # 按文件头判断真实类型（FIT 魔数在 offset 8-9 为 ".F"）
        is_fit = len(fit_bytes) >= 12 and fit_bytes[8:10] == b".F"
        ext = "fit" if is_fit else "gpx"
        if not is_fit:
            # 我们只会上传 FIT，非 FIT 内容直接拒绝，避免平台静默解析失败
            raise IgpsportError("iGPSPORT 仅支持 FIT 文件，拒绝上传非 FIT 内容")
        token = self._ensure_token()
        headers = _auth_headers(token)

        # 1) 获取 OSS 签名直传地址
        try:
            r = requests.get(
                GET_SIGNED_URL,
                headers=headers,
                params={"fileExtension": f".{ext}"},
                timeout=30,
            )
        except requests.RequestException as e:
            raise IgpsportError(f"iGPSPORT 获取上传地址失败: {e}")
        if r.status_code in (401, 403):
            raise LoginExpired("iGPSPORT 登录已过期，请重新登录")
        if r.status_code != 200:
            raise IgpsportError(f"iGPSPORT 获取上传地址 HTTP {r.status_code}")
        try:
            data = r.json().get("data") or {}
            signed_url = data.get("signedUrl") or ""
            oss_id = data.get("ossId") or ""
        except ValueError:
            signed_url, oss_id = "", ""
        if not signed_url or not oss_id:
            raise IgpsportError(f"iGPSPORT 未获取到上传地址: {r.text[:200]}")

        # 2) PUT 文件到 OSS。签名 URL 绑定 Content-Type 校验，带 Content-Type 会
        #    SignatureDoesNotMatch(403)，故先不带；403/400 时降级带 octet-stream 重试。
        put_headers = {}
        try:
            put = requests.put(signed_url, data=fit_bytes, headers=put_headers, timeout=180)
        except requests.RequestException as e:
            raise IgpsportError(f"iGPSPORT OSS 上传失败: {e}")
        if put.status_code in (403, 400):
            try:
                put = requests.put(
                    signed_url,
                    data=fit_bytes,
                    headers={"Content-Type": "application/octet-stream"},
                    timeout=180,
                )
            except requests.RequestException as e:
                raise IgpsportError(f"iGPSPORT OSS 上传失败: {e}")
        if not 200 <= put.status_code < 300:
            raise IgpsportError(
                f"iGPSPORT OSS 上传失败 HTTP {put.status_code}: {put.text[:200]}"
            )

        # 3) 通知平台解析落库
        try:
            r2 = requests.post(
                UPLOAD_BY_OSS,
                headers={**headers, "Content-Type": "application/json"},
                json={"fileName": filename, "ossName": oss_id},
                timeout=60,
            )
        except requests.RequestException as e:
            raise IgpsportError(f"iGPSPORT 上传确认请求失败: {e}")
        if r2.status_code in (401, 403):
            raise LoginExpired("iGPSPORT 登录已过期，请重新登录")
        if r2.status_code != 200:
            raise IgpsportError(f"iGPSPORT 上传确认 HTTP {r2.status_code}: {r2.text[:200]}")
        try:
            body = r2.json()
        except ValueError:
            raise IgpsportError(f"iGPSPORT 上传确认返回异常: {r2.text[:200]}")
        code = body.get("code")
        resp_short = json.dumps(body, ensure_ascii=False)[:400]
        if code not in (0, 200):
            raise IgpsportError(f"iGPSPORT 上传失败(code={code}): {resp_short}")
        # 平台接受上传但响应未带回活动 id（如 data=null）时，文件其实已投递入库，
        # 只是响应体没给 rideId。此类情况按成功处理，避免误报失败并反复重传；
        # 下一轮同步以平台实际活动列表为准去重，不受缺 id 影响。
        ride_id = self._extract_ride_id(body.get("data"))
        if ride_id:
            logger.info("iGPSPORT 上传成功 (%s) -> rideId=%s", filename, ride_id)
        else:
            logger.warning(
                "iGPSPORT 上传已被平台接受但未返回活动 id，按成功处理 (%s)"
                "（响应: %s）",
                filename,
                resp_short,
            )
        return ride_id


# 模块级单例：api / 调度器 / 编排统一走 get_service()，避免多实例状态不同步
service: IgpsportService | None = None


def get_service() -> IgpsportService:
    global service
    if service is None:
        service = IgpsportService()
    return service
