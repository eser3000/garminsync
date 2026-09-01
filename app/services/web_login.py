"""网页端 SSO 登录（替代 garth 的移动端登录，规避 429 限流）。

背景
----
garth 新版(>=0.4.5x)的 ``Client.login`` 走移动端接口::

    https://sso.garmin.com/mobile/api/login?clientId=GCM_ANDROID_DARK&...

该接口对非官方客户端/数据中心网络频繁调用时会被 429 限流，导致国际区
(connectus.garmin.cn / sso.garmin.com)登录失败，而浏览器网页登录正常。

方案
----
本模块实现 garth 0.4.x 的**网页 embed 登录流程**（与浏览器网页登录同源，
走 sso.garmin.com 的普通 HTML 表单，不受移动端接口限流影响）::

    1. GET  https://sso.<domain>/sso/embed                 种 cookie
    2. GET  https://sso.<domain>/sso/signin                提取 _csrf
    3. POST https://sso.<domain>/sso/signin                提交账号密码
    4. (可选) POST .../sso/verifyMFA/loginEnterMfaCode     提交 MFA 验证码
    5. 提取 ticket -> OAuth1 token -> OAuth2 token

实现不依赖 garth 版本的登录逻辑与内部 token 类：token 使用本模块自定义的
dataclass（字段与 garmin.com 标准 OAuth 响应一致），兼容 garth 的
``asdict()`` 序列化与 ``oauth2_token.expired`` / ``__str__`` 请求机制。
"""
import dataclasses
import re
import time
from dataclasses import dataclass, fields
from datetime import datetime
from typing import Callable, Dict, Optional, Tuple
from urllib.parse import parse_qs

import requests
from requests_oauthlib import OAuth1Session

CSRF_RE = re.compile(r'name="_csrf"\s+value="(.+?)"')
TITLE_RE = re.compile(r"<title>(.+?)</title>")
OAUTH_CONSUMER_URL = "https://thegarth.s3.amazonaws.com/oauth_consumer.json"
OAUTH_CONSUMER: Dict[str, str] = {}

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


@dataclass
class OAuth1Token:
    """OAuth1 token（字段与 garmin preauthorized 响应一致）。"""

    oauth_token: str
    oauth_token_secret: str
    mfa_token: Optional[str] = None
    mfa_expiration_timestamp: Optional[str] = None
    domain: Optional[str] = None


@dataclass
class OAuth2Token:
    """OAuth2 token（字段与 garmin exchange 响应一致）。"""

    scope: str
    jti: str
    token_type: str
    access_token: str
    refresh_token: str
    expires_in: int
    expires_at: int
    refresh_token_expires_in: int
    refresh_token_expires_at: int

    @property
    def expired(self) -> bool:
        return self.expires_at < time.time()

    @property
    def refresh_expired(self) -> bool:
        return self.refresh_token_expires_at < time.time()

    def refresh(self, client):
        """garth 内部刷新的兜底实现（兼容各版本 garth 在请求时触发的刷新）。

        部分 garth 版本在请求发现 OAuth2 过期时会调用 ``oauth2_token.refresh``，
        且新版依赖 token 对象持有 OAuth1 引用，缺失时直接抛
        "OAuth1 token is required for OAuth2 refresh"。

        这里用 client 上绑定的 OAuth1 token 走标准 exchange 端点换取全新
        OAuth2，并**原地更新自身字段**后返回 self，兼容 garth 的两种调用方式：
        ``oauth2_token = oauth2_token.refresh(client)`` 与
        ``oauth2_token.refresh(client)`` 后继续使用原对象。
        """
        new = _exchange(client.oauth1_token, client.domain)
        for f in fields(OAuth2Token):
            setattr(self, f.name, getattr(new, f.name))
        return self

    def __str__(self) -> str:
        return f"{self.token_type.title()} {self.access_token}"


def _consumer() -> Dict[str, str]:
    global OAUTH_CONSUMER
    if not OAUTH_CONSUMER:
        resp = requests.get(OAUTH_CONSUMER_URL, timeout=15)
        resp.raise_for_status()
        OAUTH_CONSUMER = resp.json()
    return OAUTH_CONSUMER


def _get_csrf_token(html: str) -> str:
    m = CSRF_RE.search(html)
    if not m:
        raise RuntimeError("网页登录失败：响应中未找到 _csrf")
    return m.group(1)


def _get_title(html: str) -> str:
    m = TITLE_RE.search(html)
    if not m:
        raise RuntimeError("网页登录失败：响应中未找到页面标题")
    return m.group(1)


def _clean_mfa_ts(token: Dict[str, str]) -> None:
    """把 Garmin 返回的毫秒时间戳 mfa_expiration_timestamp 规整为 ISO 字符串。"""
    raw = token.get("mfa_expiration_timestamp")
    if not raw:
        return
    try:
        token["mfa_expiration_timestamp"] = datetime.fromtimestamp(
            int(raw) / 1000
        ).isoformat()
    except (ValueError, TypeError):
        token.pop("mfa_expiration_timestamp", None)


def _sreq(
    sess: requests.Session,
    method: str,
    domain: str,
    path: str,
    referer: Optional[str] = None,
    **kwargs,
) -> requests.Response:
    """向 sso.<domain> 发网页请求（带浏览器 UA / Referer）。"""
    url = f"https://sso.{domain}{path}"
    headers = dict(kwargs.pop("headers", {}))
    headers["User-Agent"] = USER_AGENT
    if referer:
        headers["Referer"] = referer
    resp = sess.request(method, url, headers=headers, timeout=30, **kwargs)
    if resp.status_code >= 400:
        raise RuntimeError(
            f"网页登录请求失败 {resp.status_code}: {url} {resp.text[:300]}"
        )
    return resp


def _get_oauth1_token(ticket: str, domain: str) -> OAuth1Token:
    consumer = _consumer()
    sess = OAuth1Session(consumer["consumer_key"], consumer["consumer_secret"])
    url = (
        f"https://connectapi.{domain}/oauth-service/oauth/preauthorized"
        f"?ticket={ticket}&login-url=https://sso.{domain}/sso/embed"
        f"&accepts-mfa-tokens=true"
    )
    resp = sess.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()
    parsed = parse_qs(resp.text)
    token = {k: v[0] for k, v in parsed.items()}
    _clean_mfa_ts(token)
    token["domain"] = domain
    names = {f.name for f in fields(OAuth1Token)}
    return OAuth1Token(**{k: v for k, v in token.items() if k in names})


def _exchange(oauth1: OAuth1Token, domain: str) -> OAuth2Token:
    consumer = _consumer()
    sess = OAuth1Session(
        consumer["consumer_key"],
        consumer["consumer_secret"],
        resource_owner_key=oauth1.oauth_token,
        resource_owner_secret=oauth1.oauth_token_secret,
    )
    data = {"mfa_token": oauth1.mfa_token} if oauth1.mfa_token else {}
    resp = sess.post(
        f"https://connectapi.{domain}/oauth-service/oauth/exchange/user/2.0",
        headers={
            "User-Agent": USER_AGENT,
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data=data,
        timeout=30,
    )
    resp.raise_for_status()
    token = resp.json()
    token["expires_at"] = int(time.time() + int(token["expires_in"]))
    token["refresh_token_expires_at"] = int(
        time.time() + int(token["refresh_token_expires_in"])
    )
    names = {f.name for f in fields(OAuth2Token)}
    return OAuth2Token(**{k: v for k, v in token.items() if k in names})





def web_sso_login(
    client,
    email: str,
    password: str,
    prompt_mfa: Callable[[], str],
) -> Tuple[OAuth1Token, OAuth2Token]:
    """网页 embed 流程登录，返回 (oauth1, oauth2)。

    client 仅需具备 ``domain`` 与 ``sess``（requests.Session）属性，
    例如 garth 的 ``Client(domain=...)``。
    """
    domain = client.domain
    sso_root = f"https://sso.{domain}/sso"
    embed = f"{sso_root}/embed"

    embed_params = dict(id="gauth-widget", embedWidget="true", gauthHost=sso_root)
    signin_params = {
        **embed_params,
        **dict(
            gauthHost=embed,
            service=embed,
            source=embed,
            redirectAfterAccountLoginUrl=embed,
            redirectAfterAccountCreationUrl=embed,
        ),
    }

    sess = client.sess
    try:
        sess.headers.pop("User-Agent", None)  # 不覆盖浏览器 UA（按请求单独设置）
    except Exception:
        pass

    # 1) 种 cookie
    r1 = _sreq(sess, "GET", domain, "/sso/embed", params=embed_params)
    # 2) 取 _csrf
    r2 = _sreq(
        sess, "GET", domain, "/sso/signin",
        params=signin_params, referer=r1.url,
    )
    csrf = _get_csrf_token(r2.text)

    # 3) 提交账号密码
    r3 = _sreq(
        sess, "POST", domain, "/sso/signin",
        params=signin_params, referer=r2.url,
        data=dict(username=email, password=password, embed="true", _csrf=csrf),
    )
    title = _get_title(r3.text)
    last = r3

    # 4) 处理 MFA
    if "MFA" in title:
        csrf = _get_csrf_token(r3.text)
        mfa_code = prompt_mfa()
        last = _sreq(
            sess, "POST", domain, "/sso/verifyMFA/loginEnterMfaCode",
            params=signin_params, referer=r3.url,
            data={
                "mfa-code": mfa_code,
                "embed": "true",
                "_csrf": csrf,
                "fromPage": "setupEnterMfaCode",
            },
        )
        title = _get_title(last.text)

    if title != "Success":
        raise RuntimeError(f"网页登录失败：页面标题为 {title!r}（期望 Success）")

    # 5) 提取 ticket
    m = re.search(r'embed\?ticket=([^"]+)"', last.text)
    if not m:
        raise RuntimeError("网页登录失败：响应中未找到 OAuth ticket")
    ticket = m.group(1)

    # 6) ticket -> OAuth1 -> OAuth2
    oauth1 = _get_oauth1_token(ticket, domain)
    oauth2 = _exchange(oauth1, domain)
    return oauth1, oauth2
