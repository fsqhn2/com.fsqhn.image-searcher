"""B 站检索：搜索页关键词 → UP 主 mid + 公开图（投稿封面）。

为什么要有这一层：Lens 的候选常落在 `search.bilibili.com/all?keyword=…`，那是
JS 渲染的搜索结果列表，抓 HTML 挖不出发布者（实测空间页 HTML 只有 10KB 空壳，
`__pinia`/`vlist` 一个都没有），只能走检索接口反查。

实测（2026-11-22，本机直连与 config 里的代理都试过）：

- `x/web-interface/wbi/search/type?search_type=video` 带 wbi 签名稳定回 20 条，
  每条同时给作者 ``mid``、昵称 ``author`` 和投稿封面 ``pic``——一次请求就能
  既反解出号、又拿到这个号公开过的图。
- 不签名直接打同一个接口回 `-412 request was banned`，所以签名这一步不能省；
  但 `search_type=bili_user` 不签名也能出结果，wbi 拿不到时留作退路。
- 空间接口 `x/space/wbi/arc/search` 与动态 `x/polymer/web-dynamic/v1/feed/space`
  不论带不带 cookie、换不换 Referer，多试几次就 `-352`／`-412`，靠不住，
  所以「账号公开图」只走搜索结果里的投稿封面。
- Google 站内检索 `site:space.bilibili.com "关键词"` 实测回来的不是 B 站页面
  （Pinterest、DeviantArt 之类），这条路不可用。

拿不到就如实返回空：这里不做「同名即认」，账号对不对由上层比指纹决定。
"""

from __future__ import annotations

import hashlib
import time
import urllib.parse
from typing import Any

import requests

from . import searchpage
from .httpclient import make_session

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_SEARCH_URL = "https://api.bilibili.com/x/web-interface/wbi/search/type"
_NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
_SPI_URL = "https://api.bilibili.com/x/frontend/finger/spi"
_REFERER = "https://search.bilibili.com/"

# wbi 签名要把 img_key + sub_key 按这张表重排后取前 32 位当密钥。
_MIXIN_TABLE = (
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
)

# 风控码：换一条线路（代理 / 直连）再试可能就好了。
_RISK_CODES = frozenset({-352, -412, -799})

# wbi 密钥一小时换一次，缓存着用，免得每次检索都多打一次 nav。
_WBI_TTL = 3600
_WBI_CACHE: dict[str, Any] = {"at": 0.0, "keys": ("", "")}

# 单次检索的搜索类型：只开放实测能用的两种。
SEARCH_TYPES = ("video", "bili_user")


class BilibiliError(RuntimeError):
    """拿不到就说拿不到，别把风控当成「没有这个人」。"""

    def __init__(self, message: str, code: int = 0) -> None:
        super().__init__(message)
        self.code = int(code or 0)

    @property
    def risky(self) -> bool:
        return self.code in _RISK_CODES


def mixin_key(img_key: str, sub_key: str) -> str:
    """wbi 密钥：两个 key 拼起来按表重排、取前 32 位。"""
    raw = f"{img_key or ''}{sub_key or ''}"
    if len(raw) < 64:
        return ""
    return "".join(raw[index] for index in _MIXIN_TABLE)[:32]


def sign_query(params: dict[str, Any], img_key: str, sub_key: str, now: float | None = None) -> str:
    """按 wbi 规则签名，返回可直接拼到 URL 后面的查询串。

    ``now`` 只为单测能固定时间：``wts`` 进签名，不固定就没法断言结果。
    """
    mixin = mixin_key(img_key, sub_key)
    if not mixin:
        raise BilibiliError("wbi 密钥不完整")
    signed = {key: value for key, value in (params or {}).items() if value is not None}
    signed["wts"] = int(now if now is not None else time.time())
    # 签名算的是「除 w_rid 外全部参数」的 urlencode 结果，最后再整体拼一次。
    # 官方前端还会对这串做一次 `quote(safe="!':(),")`，但关键词里有空格／中文时
    # 那一步会把 `%` 二次转义；实测（关键词 `XxMiss Naruko` 带空格）不做也回 20 条，
    # 所以这里不跟，省掉一个「同一份参数算出两种签名」的坑。
    query = urllib.parse.urlencode(sorted(signed.items()))
    signed["w_rid"] = hashlib.md5((query + mixin).encode()).hexdigest()
    return urllib.parse.urlencode(sorted(signed.items()))


def _session(proxy: str = "") -> requests.Session:
    session = make_session(proxy)
    session.headers.update(
        {
            "User-Agent": UA,
            "Referer": _REFERER,
            "Accept": "application/json, text/plain, */*",
        }
    )
    return session


def _stem(url: Any) -> str:
    """`.../7cd084941338484aae1ad9425b84077c.png` → 文件名主干。"""
    text = str(url or "").strip()
    if not text:
        return ""
    return text.rsplit("/", 1)[-1].split(".")[0]


def _json(
    session: requests.Session,
    url: str,
    params: dict[str, Any] | None,
    timeout: float,
) -> dict[str, Any]:
    try:
        response = session.get(url, params=params, timeout=timeout)
    except requests.RequestException as error:
        raise BilibiliError(f"请求失败：{type(error).__name__}") from error
    if response.status_code != 200:
        raise BilibiliError(f"HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError as error:
        raise BilibiliError("回的不是 JSON") from error
    if not isinstance(payload, dict):
        raise BilibiliError("回的不是 JSON 对象")
    code = int(payload.get("code") or 0)
    if code != 0:
        raise BilibiliError(f"接口 code={code}：{str(payload.get('message') or '')[:40]}", code)
    return payload


def _prime_cookies(session: requests.Session, timeout: float) -> None:
    """取一次 buvid3／buvid4，没有这两个 cookie 时检索接口更容易被判风控。"""
    try:
        session.get(_SPI_URL, timeout=timeout)
    except requests.RequestException:
        pass


def _wbi_keys(session: requests.Session, timeout: float) -> tuple[str, str]:
    """wbi 的 img_key／sub_key；拿不到返回两个空串（退路是不签名）。"""
    now = time.time()
    cached = _WBI_CACHE.get("keys") or ("", "")
    if cached[0] and cached[1] and now - float(_WBI_CACHE.get("at") or 0) < _WBI_TTL:
        return str(cached[0]), str(cached[1])
    try:
        payload = _json(session, _NAV_URL, None, timeout)
    except BilibiliError:
        return "", ""
    wbi = ((payload.get("data") or {}).get("wbi_img") or {})
    img = _stem(wbi.get("img_url"))
    sub = _stem(wbi.get("sub_url"))
    if len(img) < 32 or len(sub) < 32:
        return "", ""
    _WBI_CACHE.update({"at": now, "keys": (img, sub)})
    return img, sub


def _search_once(
    keyword: str, search_type: str, proxy: str, timeout: float
) -> dict[str, Any]:
    session = _session(proxy)
    try:
        _prime_cookies(session, timeout)
        img, sub = _wbi_keys(session, timeout)
        params = {"search_type": search_type, "keyword": keyword, "page": 1}
        if img and sub:
            query = sign_query(params, img, sub)
            return _json(session, f"{_SEARCH_URL}?{query}", None, timeout)
        # wbi 没拿到：`bili_user` 这条不签名也能用，`video` 会 -412，交给上层记失败。
        return _json(session, _SEARCH_URL, params, timeout)
    finally:
        session.close()


def search(
    keyword: str, search_type: str = "video", proxy: str = "", timeout: float = 8.0
) -> dict[str, Any]:
    """跑一次 B 站检索。

    先直连，风控或连不上时换成 ``config.toml`` 里配的代理再试一次（B 站对陌生
    IP 会直接 -412，服务器那边直连未必通）。正常返回（哪怕 0 条）不重试。
    """
    text = searchpage.clean_keyword(keyword)
    if not text:
        raise BilibiliError("关键词为空")
    if search_type not in SEARCH_TYPES:
        raise BilibiliError(f"不支持的检索类型：{search_type}")
    routes = [""] if not (proxy or "").strip() else ["", str(proxy).strip()]
    last = BilibiliError("没跑成")
    for route in routes:
        try:
            return _search_once(text, search_type, route, timeout)
        except BilibiliError as error:
            last = error
            if error.risky:
                # 风控也可能是 wbi 密钥轮换后过期，下一次换线路时重新取一份。
                _WBI_CACHE.update({"at": 0.0, "keys": ("", "")})
            if not error.risky and "请求失败" not in str(error) and "HTTP" not in str(error):
                break
    raise last


def resolve_accounts(
    keyword: str,
    proxy: str = "",
    timeout: float = 8.0,
    account_limit: int = 3,
    images_per_account: int = 4,
    enrich_limit: int = 2,
) -> list[dict[str, Any]]:
    """搜索页关键词 → 账号候选（尽量带上这个号自己的投稿封面）。

    ``account_limit`` 个号封顶，其中最多 ``enrich_limit`` 个号会再按自己昵称搜一遍
    投稿来补图（只有「名字搜到、但没有封面」的号才需要，一次检索一个号）。
    任何一步失败都不影响已经拿到的部分——反解本来就是加分项。
    """
    text = searchpage.clean_keyword(keyword)
    if not text:
        return []
    accounts: list[dict[str, Any]] = []
    try:
        payload = search(text, "video", proxy, timeout)
    except BilibiliError:
        payload = {}
    accounts = searchpage.accounts_from_videos(
        payload, limit=account_limit, images_per_account=images_per_account
    )
    if len(accounts) < account_limit:
        try:
            users = searchpage.accounts_from_users(
                search(text, "bili_user", proxy, timeout), limit=account_limit
            )
        except BilibiliError:
            users = []
        known = {item["mid"] for item in accounts}
        for user in users:
            if len(accounts) >= account_limit:
                break
            if user["mid"] in known:
                continue
            known.add(user["mid"])
            accounts.append(user)
    enriched = 0
    for account in accounts:
        if account.get("images") or enriched >= enrich_limit:
            continue
        name = str(account.get("name") or "").strip()
        if not name:
            continue
        enriched += 1
        try:
            payload = search(name, "video", proxy, timeout)
        except BilibiliError:
            continue
        for row in searchpage.accounts_from_videos(
            payload, limit=50, images_per_account=images_per_account
        ):
            if row["mid"] == account["mid"]:
                account["images"] = list(row.get("images") or [])
                account["videos"] = list(row.get("videos") or [])
                break
    return accounts
