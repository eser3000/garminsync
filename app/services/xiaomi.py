"""小米体脂秤 API 客户端（仅体重数据）。

签名算法与 https://github.com/XiaoSiHwang/garmin-weight-sync 一致：
- 登录：account.xiaomi.com 服务登录，凭 userId + passToken 换取 ssecurity
- 签名：nonce + ssecurity -> sha256 生成 signed_nonce，参数用 RC4 加密，signature 为 sha1 摘要
"""
from __future__ import annotations

import base64
import email.utils
import hashlib
import json
import logging
import os
import struct
import time

import requests

logger = logging.getLogger(__name__)

APP_ID = "miothealth"
USER_AGENT = "MisFit/2.0.0 (iPhone; iOS 13.0; Scale/2.0.0)"
LOGIN_URL = "https://account.xiaomi.com/pass/serviceLogin?_json=true&sid=miothealth"
API_BASE = "https://hlth.io.mi.com"


class XiaomiError(Exception):
    """小米 API 异常。"""


def parse_any_float(v):
    if v is None:
        return 0.0
    if isinstance(v, (float, int)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def parse_any_int(v):
    if v is None:
        return 0
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, str):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return 0
    return 0


def unmarshal_fitness_data(data_list):
    """解析新 API /app/v1/data/get_fitness_data_by_time 返回的体重记录。

    每条记录格式: {sid, key, time(秒), value(JSON 字符串), zone_offset, update_time, zone_name}
    """
    weights = []
    for item in data_list:
        if item.get("key") != "weight":
            continue
        time_stamp = item.get("time", 0)
        value_str = item.get("value", "{}")
        try:
            value_data = json.loads(value_str)
        except (TypeError, ValueError):
            continue

        w = {
            "timestamp": time_stamp,
            "date": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time_stamp)),
            "weight": parse_any_float(value_data.get("weight")),
            "bmi": parse_any_float(value_data.get("bmi")),
            "body_fat_rate": parse_any_float(value_data.get("body_fat_rate")),
            "moisture_rate": parse_any_float(value_data.get("moisture_rate")),
            "bone_mass": parse_any_float(value_data.get("bone_mass")),
            "muscle_mass": parse_any_float(value_data.get("muscle_rate"))
            / 100 * parse_any_float(value_data.get("weight")),
            "visceral_fat": parse_any_int(value_data.get("visceral_fat")),
            "metabolic_age": parse_any_int(value_data.get("ma")),
            "basal_metabolism": parse_any_int(value_data.get("basal_metabolism")),
            "heart_rate": parse_any_int(value_data.get("heartRate")),
        }
        weights.append(w)
    return weights


class XiaomiClient:
    """小米健康 API 客户端（仅体重数据，region 固定为 cn 区）。"""

    def __init__(self):
        self.sid = APP_ID
        self.session = requests.Session()
        self.user_id = None
        self.ssecurity = None
        self.pass_token = None
        self.time_offset = 0

    # ---------- 登录 ----------

    def set_credentials(self, user_id, ssecurity_encoded, pass_token):
        """从已保存的凭据恢复会话。ssecurity_encoded 为 base64 字符串。"""
        self.user_id = user_id
        self.pass_token = pass_token
        if ssecurity_encoded:
            try:
                self.ssecurity = base64.b64decode(ssecurity_encoded)
            except (TypeError, ValueError) as exc:
                raise XiaomiError("ssecurity 不是合法的 base64 字符串") from exc
        else:
            self.ssecurity = None

    def login_from_token(self, user_id, pass_token):
        """用 userId + passToken 验证登录，获取 ssecurity 并同步服务器时间。

        返回: {"user_id", "pass_token", "ssecurity"}（ssecurity 为 base64 字符串，用于持久化）
        """
        if not user_id or not pass_token:
            raise XiaomiError("缺少小米凭据")

        headers = {
            "Cookie": f"userId={user_id}; passToken={pass_token}",
            "User-Agent": USER_AGENT,
        }
        try:
            resp = self.session.get(LOGIN_URL, headers=headers, timeout=20)
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise XiaomiError(f"小米登录网络错误: {exc}") from exc

        try:
            txt = resp.text
            if txt.startswith("&&&START&&&"):
                txt = txt[11:]
            data = json.loads(txt)
        except (ValueError, TypeError) as exc:
            raise XiaomiError(f"小米登录响应解析失败: {resp.text[:200]}") from exc

        if data.get("code") != 0:
            raise XiaomiError(
                f"小米登录失败: code={data.get('code')} desc={data.get('desc', '')} "
                f"(请检查 userId / passToken 是否有效)"
            )

        self.user_id = str(data.get("userId") or user_id)
        if data.get("passToken"):
            self.pass_token = data["passToken"]
        ssecurity_b64 = data.get("ssecurity")
        if not ssecurity_b64:
            raise XiaomiError("小米登录响应缺少 ssecurity")
        self.ssecurity = base64.b64decode(ssecurity_b64)

        # 跟随 location 完成会话并同步服务器时间偏移
        auth_location = data.get("location")
        if auth_location:
            try:
                resp2 = self.session.get(auth_location, timeout=20)
                server_date = resp2.headers.get("Date")
                if server_date:
                    server_ts = email.utils.mktime_tz(email.utils.parsedate_tz(server_date))
                    if server_ts:
                        self.time_offset = server_ts - time.time()
                else:
                    logger.warning("小米登录跳转响应缺少 Date 头，服务器时间未同步")
            except requests.RequestException:
                logger.warning("跟随小米登录跳转失败，忽略")
        else:
            logger.warning("小米登录响应缺少 location，服务器时间未同步，nonce 时间戳可能偏差")

        return {
            "user_id": self.user_id,
            "pass_token": self.pass_token,
            "ssecurity": base64.b64encode(self.ssecurity).decode("utf-8"),
        }

    # ---------- 签名与请求 ----------

    def _gen_nonce(self):
        rand_bytes = os.urandom(8)
        ts = int((time.time() + self.time_offset) / 60)
        return rand_bytes + struct.pack(">I", ts)

    @staticmethod
    def _gen_signed_nonce(ssecurity, nonce):
        m = hashlib.sha256()
        m.update(ssecurity)
        m.update(nonce)
        return m.digest()

    @staticmethod
    def _rc4_encrypt(key, data):
        if isinstance(data, str):
            data = data.encode("utf-8")
        s = list(range(256))
        j = 0
        for i in range(256):
            j = (j + s[i] + key[i % len(key)]) % 256
            s[i], s[j] = s[j], s[i]
        i = j = 0
        # 小米 SDK 约定：密钥流先生成 1024 字节并丢弃（drop 1024）。
        # 请求参数加密与响应解密都必须用同一约定，否则解密结果会是乱码。
        for _ in range(1024):
            i = (i + 1) % 256
            j = (j + s[i]) % 256
            s[i], s[j] = s[j], s[i]
        out = bytearray()
        for char in data:
            i = (i + 1) % 256
            j = (j + s[i]) % 256
            s[i], s[j] = s[j], s[i]
            out.append(char ^ s[(s[i] + s[j]) % 256])
        return bytes(out)

    def request(self, api_url, params):
        """带签名调用小米 API，返回解密后的 JSON。"""
        if not self.ssecurity:
            raise XiaomiError("小米未登录或 ssecurity 缺失")

        nonce = self._gen_nonce()
        signed_nonce = self._gen_signed_nonce(self.ssecurity, nonce)

        s_hash = f"POST&{api_url}&data={params}&" + base64.b64encode(signed_nonce).decode("utf-8")
        rc4_hash = base64.b64encode(
            hashlib.sha1(s_hash.encode("utf-8")).digest()
        ).decode("utf-8")

        enc_params = base64.b64encode(
            self._rc4_encrypt(signed_nonce, params)
        ).decode("utf-8")
        enc_rc4_hash = base64.b64encode(
            self._rc4_encrypt(signed_nonce, rc4_hash)
        ).decode("utf-8")

        s_sig = (
            f"POST&{api_url}&data={enc_params}&rc4_hash__={enc_rc4_hash}&"
            + base64.b64encode(signed_nonce).decode("utf-8")
        )
        signature = base64.b64encode(
            hashlib.sha1(s_sig.encode("utf-8")).digest()
        ).decode("utf-8")

        final_data = {
            "data": enc_params,
            "rc4_hash__": enc_rc4_hash,
            "signature": signature,
            "_nonce": base64.b64encode(nonce).decode("utf-8"),
        }

        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        cookie_dict = self.session.cookies.get_dict()
        if cookie_dict:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookie_dict.items())

        try:
            resp = self.session.post(
                API_BASE + api_url, data=final_data, headers=headers, timeout=30
            )
        except requests.RequestException as exc:
            raise XiaomiError(f"小米 API 请求失败: {exc}") from exc

        if resp.status_code != 200:
            raise XiaomiError(f"小米 API 请求失败: HTTP {resp.status_code}")

        try:
            resp_bytes = base64.b64decode(resp.text)
            decrypted = self._rc4_encrypt(signed_nonce, resp_bytes)
            return json.loads(decrypted)
        except (ValueError, TypeError):
            try:
                return json.loads(resp.text)
            except (ValueError, TypeError):
                # 增强诊断：区分「密文解密失败」(key 不匹配/算法变更)
                # 与「非密文明文」(登录态失效/风控页)，便于定位根因。
                try:
                    resp_bytes = base64.b64decode(resp.text)
                    decrypted = self._rc4_encrypt(signed_nonce, resp_bytes)
                    detail = (
                        f"base64密文{len(resp_bytes)}字节，"
                        f"RC4解密后: {decrypted[:80]!r}"
                    )
                except Exception:
                    detail = f"非密文响应: {resp.text[:200]!r}"
                raise XiaomiError(f"小米 API 响应解析失败: {detail}") from None

    # ---------- 体重数据 ----------

    def get_weight_data(self, start_time, end_time):
        """获取 [start_time, end_time]（秒）区间内的全部体重记录。

        返回 list[dict]: timestamp(秒)/date/weight(kg)/bmi/body_fat_rate/...
        """
        all_data = []
        next_key = None
        while True:
            params = {
                "start_time": int(start_time),
                "end_time": int(end_time),
                "key": "weight",
            }
            if next_key:
                params["next_key"] = next_key
            req_params = json.dumps(params, separators=(",", ":"))

            data = self.request("/app/v1/data/get_fitness_data_by_time", req_params)

            if isinstance(data, dict):
                if data.get("code") != 0:
                    raise XiaomiError(
                        f"小米获取体重数据失败: {data.get('message', data.get('desc', 'unknown'))}"
                    )
                result = data.get("result", {}) or {}
                data_list = result.get("data_list", []) or []
                all_data.extend(data_list)
                has_more = result.get("has_more", False)
                next_key = result.get("next_key")
                if not has_more or not next_key:
                    break
            else:
                break

        return unmarshal_fitness_data(all_data)
