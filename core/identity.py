"""把落点在微博这类「认不到图」的站上的同一个人，换到她的 X 号去核。

两段都很窄，各自只做一件事：

1. 微博数字 UID → 昵称：``weibo.com/ajax/profile/info`` 免登录可读，但要先拿一次
   访客 cookie（``passport.weibo.com/visitor`` 那套）。候选里的微博链接只有 UID，
   没有昵称，而 UID 本身搜不出人。
2. 昵称 → X 号：复用 Lens 那套 SerpAPI key 跑一次 Google 网页检索，从结果标题和
   链接里挑 ``x.com/<handle>``。实测搜「纸悦Etsu_ko twitter」时 9 条结果全是同一个
   号，噪声很小；但同名同姓的人也存在，所以这里只出候选，能不能当结论砍在上层的
   指纹比对上——只有那个号自己的帖子里真有这张图才算数。
"""

from __future__ import annotations

import re
from typing import Any

import requests

# 检索结果里常被当成 handle 抓到的路径词，不是账号。
NOISE_HANDLES = frozenset(
    {
        "i", "intent", "share", "home", "search", "hashtag", "explore",
        "settings", "notifications", "messages", "tos", "privacy", "login",
        "signup", "about", "download", "account", "compose", "signin",
        "help", "en", "zh", "ja", "status", "statuses", "followers", "following",
    }
)

_HANDLE_RE = re.compile(r"(?:x|twitter)\.com/([A-Za-z0-9_]{2,20})", re.I)
_CLEAN_RE = re.compile(r"[\s_\-·.]+")

# Serper（google.serper.dev）跟 SerpAPI 是两家：前者 key 是 40 位十六进制、按次扣
# 预付额度（不按月重置），后者是 64 位、按月算。插件两处网页检索原来只认 SerpAPI，
# 用户买的是 Serper 时会被当成无效 key。
SERPER_SEARCH = "https://google.serper.dev/search"
SERPER_LENS = "https://google.serper.dev/lens"
SERPER_ACCOUNT = "https://google.serper.dev/account"


def is_serper_key(api_key: str) -> bool:
    """40 位十六进制 = Serper；64 位 = SerpAPI。"""
    key = str(api_key or "").strip()
    if len(key) != 40:
        return False
    return all(char in "0123456789abcdefABCDEF" for char in key)


def name_query(name: str, suffix: str, api_key: str) -> str:
    """按名字检索的查询串。

    Serper 免费档会拒掉「引号 + 其它词」和 ``site:`` 这类写法（实测
    ``"kuru_xox" cosplay`` 直接 HTTP 400 ``Query pattern not allowed``），所以给它
    的查询串一律用裸词；SerpAPI 那边保留引号，能压掉不少同名噪声。
    """
    name = (name or "").strip()
    suffix = (suffix or "").strip()
    if is_serper_key(api_key):
        return f"{name} {suffix}".strip()
    return f'"{name}" {suffix}'.strip()


def web_search(
    query: str,
    api_key: str,
    proxy: str = "",
    num: int = 10,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """跑一次网页检索，把两家的返回统一成 ``{"organic_results": [...]}``。

    上层（``handles_from_search`` / ``profiles_from_search``）只认这一个形状，
    所以接谁都不用改解析。
    """
    query = (query or "").strip()
    if not query or not api_key:
        return {}
    session = requests.Session()
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
    try:
        if is_serper_key(api_key):
            response = session.post(
                SERPER_SEARCH,
                headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
                json={"q": query, "num": max(10, int(num))},
                timeout=timeout,
            )
            if response.status_code != 200:
                detail = (response.text or "").strip()[:80]
                raise IdentityError(f"网页检索 HTTP {response.status_code}：{detail}")
            payload = response.json()
            organic = payload.get("organic") if isinstance(payload, dict) else None
            rows = [item for item in (organic or []) if isinstance(item, dict)]
            return {"organic_results": rows}
        response = session.get(
            "https://serpapi.com/search.json",
            params={"engine": "google", "q": query, "num": str(num), "api_key": api_key},
            timeout=timeout,
        )
        if response.status_code != 200:
            raise IdentityError(f"网页检索 HTTP {response.status_code}")
        payload = response.json()
        if isinstance(payload, dict) and payload.get("error"):
            raise IdentityError(str(payload["error"])[:80])
        return payload if isinstance(payload, dict) else {}
    except ValueError as error:
        raise IdentityError("网页检索回的不是 JSON") from error
    finally:
        session.close()



class IdentityError(RuntimeError):
    """拿不到就说拿不到，不要静默当成「没有这个人」。"""


def _clean(text: str) -> str:
    return _CLEAN_RE.sub("", (text or "").lower())


def name_matches(left: str, right: str) -> bool:
    """两个昵称是不是同一个人。

    比的是去掉空格、下划线、连字符之后的小写串，任一方包含另一方即算——微博叫
    「纸悦Etsu_ko」、X 上写「纸悦Etsu_ko」，也可能一边多挂个后缀。纯数字和长度
    不到 3 的串不比，避免「81」「ko」这种碎片撞上任何人。
    """
    left_clean = _clean(left)
    right_clean = _clean(right)
    if len(left_clean) < 3 or len(right_clean) < 3:
        return False
    if left_clean.isdigit() or right_clean.isdigit():
        return False
    return left_clean in right_clean or right_clean in left_clean


def parse_weibo_profile(payload: Any) -> dict[str, Any] | None:
    """从 ``ajax/profile/info`` 的返回里取出昵称、粉丝数、认证与简介。

    接口查不到人时 ``data.user`` 是空的（实测连 ``data`` 都没有），这时返回 None，
    让上层写「查不到这个号」，不要编一个昵称出来。
    """
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    user = (data or {}).get("user") if isinstance(data, dict) else None
    if not isinstance(user, dict):
        return None
    name = str(user.get("screen_name") or "").strip()
    uid = str(user.get("idstr") or user.get("id") or "").strip()
    if not name or not uid:
        return None
    return {
        "name": name,
        "uid": uid,
        "url": f"https://weibo.com/u/{uid}",
        "followers": int(user.get("followers_count") or 0),
        "verified": str(user.get("verified_reason") or "").strip(),
        "description": str(user.get("description") or "").strip(),
    }


def handles_from_search(payload: Any, limit: int = 3) -> list[str]:
    """从 SerpAPI 网页检索结果里挑 X 账号，按出现次数排序。

    只认标题、链接、摘要里出现的 ``x.com/<handle>``：Google 索引的 X 帖子标题
    本身就带账号名（「某人 on X」），同一页里重复出现就是在说同一个号。出现次数
    一样时保持原始顺序，让排在前面的结果优先。
    """
    if not isinstance(payload, dict):
        return []
    blocks: list[str] = []
    for item in payload.get("organic_results") or []:
        if not isinstance(item, dict):
            continue
        blocks.append(
            " ".join(
                str(item.get(key) or "")
                for key in ("title", "link", "snippet", "displayed_link")
            )
        )
    counts: dict[str, int] = {}
    order: list[str] = []
    for block in blocks:
        for handle in _HANDLE_RE.findall(block):
            if handle.lower() in NOISE_HANDLES:
                continue
            if handle not in counts:
                counts[handle] = 0
                order.append(handle)
            counts[handle] += 1
    order.sort(key=lambda item: -counts[item])
    return order[: max(1, limit)]


def _visitor_session(proxy: str, timeout: float) -> requests.Session:
    """拿一次微博访客 cookie（SUB/SUBP）。

    不带这一步，``ajax/profile/info`` 只回 ``{"error":"Forbidden"}``；带上之后
    同一个接口就能读到昵称。cookie 只在这个 session 里用，成功与否都不往外存。
    """
    session = requests.Session()
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
        ),
        "Referer": "https://passport.weibo.com/visitor/visitor",
    }
    response = session.post(
        "https://passport.weibo.com/visitor/genvisitor",
        data={"cb": "gen_callback", "fp": "{}"},
        headers=headers,
        timeout=timeout,
    )
    match = re.search(r'"tid":"([^"]+)"', response.text or "")
    if not match:
        raise IdentityError("微博访客 cookie 没拿到（genvisitor 没有 tid）")
    session.get(
        "https://passport.weibo.com/visitor/visitor",
        params={
            "a": "incarnate",
            "t": match.group(1),
            "w": "2",
            "c": "095",
            "gc": "",
            "cb": "cross_domain",
            "from": "weibo",
        },
        headers={"User-Agent": headers["User-Agent"], "Referer": "https://weibo.com/"},
        timeout=timeout,
    )
    if "SUB" not in session.cookies.keys():
        raise IdentityError("微博访客 cookie 没拿到（没有 SUB）")
    return session


def fetch_weibo_profile(uid: str, proxy: str = "", timeout: float = 15.0) -> dict[str, Any] | None:
    """微博数字 UID → 昵称/粉丝/认证/简介。查不到人返回 None，取不到数据抛 IdentityError。"""
    uid = (uid or "").strip()
    if not uid.isdigit():
        return None
    session = _visitor_session(proxy, timeout)
    try:
        response = session.get(
            f"https://weibo.com/ajax/profile/info?uid={uid}",
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
                ),
                "Referer": f"https://weibo.com/u/{uid}",
            },
            timeout=timeout,
        )
    finally:
        session.close()
    if response.status_code != 200:
        raise IdentityError(f"微博资料接口 HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError as error:
        raise IdentityError("微博资料接口回的不是 JSON") from error
    return parse_weibo_profile(payload)


def search_x_handles(
    name: str,
    api_key: str,
    proxy: str = "",
    timeout: float = 30.0,
    limit: int = 3,
) -> list[str]:
    """昵称 → 疑似 X 账号（按检索结果里的出现次数排序）。

    用 Google 网页检索而不是 X 站内搜索：站内搜索只认帖文内容，昵称对不上号；
    网页检索的 X 帖子标题自带账号名，实测同一个号能在 9 条结果里反复出现。
    """
    name = (name or "").strip()
    if not name or not api_key:
        return []
    payload = web_search(name_query(name, "twitter", api_key), api_key, proxy, num=10, timeout=timeout)
    return handles_from_search(payload, limit=limit)


# 一条检索结果里能落地的平台主页。顺序就是展示优先级：X 排头，因为只有 X 能
# 顺着主页翻带图帖核对原图，其他平台这里只当线索。
_PROFILE_PATTERNS: tuple[tuple[str, re.Pattern[str], frozenset[str]], ...] = (
    ("X", re.compile(r"(?:x|twitter)\.com/([A-Za-z0-9_]{2,20})", re.I), NOISE_HANDLES),
    (
        "Instagram",
        re.compile(r"instagram\.com/([A-Za-z0-9_.]{2,30})", re.I),
        frozenset({"p", "reel", "reels", "explore", "accounts", "tv", "stories", "share"}),
    ),
    ("TikTok", re.compile(r"tiktok\.com/@([A-Za-z0-9_.]{2,30})", re.I), frozenset()),
    (
        "微博",
        re.compile(r"weibo\.(?:com|cn)/(?:u/|profile/)?(\d{4,20})", re.I),
        frozenset(),
    ),
    (
        "Patreon",
        re.compile(r"patreon\.com/(?:user\?u=)?([A-Za-z0-9_-]{2,30})", re.I),
        frozenset({"join", "posts", "login", "home"}),
    ),
    ("Reddit", re.compile(r"reddit\.com/(?:u|user)/([A-Za-z0-9_-]{3,20})", re.I), frozenset()),
    ("Bilibili", re.compile(r"space\.bilibili\.com/(\d{4,20})", re.I), frozenset()),
    (
        "Facebook",
        re.compile(r"facebook\.com/([A-Za-z0-9_.]{3,30})", re.I),
        frozenset({"profile.php", "pages", "groups", "watch", "reel", "share", "login"}),
    ),
    ("YouTube", re.compile(r"youtube\.com/@([A-Za-z0-9_.-]{2,30})", re.I), frozenset()),
    ("小红书", re.compile(r"xiaohongshu\.com/user/profile/([0-9a-f]{10,40})", re.I), frozenset()),
)


def profiles_from_search(payload: Any, limit: int = 6) -> list[dict[str, str]]:
    """从检索结果里挑各平台主页，同一平台同一号只留一条。

    只认链接里长得像主页的那一截：``instagram.com/p/xxx`` 是单张图、
    ``facebook.com/profile.php`` 是跳转页，都不算主页。返回项固定三个字段
    （platform / handle / url），交给上层决定要不要去核图。
    """
    if not isinstance(payload, dict):
        return []
    found: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in payload.get("organic_results") or []:
        if not isinstance(item, dict):
            continue
        link = str(item.get("link") or "")
        text = " ".join(
            str(item.get(key) or "")
            for key in ("title", "link", "snippet", "displayed_link")
        )
        for platform, pattern, noise in _PROFILE_PATTERNS:
            for handle in pattern.findall(text):
                key = (platform, handle.lower())
                if handle.lower() in noise or key in seen:
                    continue
                seen.add(key)
                found.append(
                    {
                        "platform": platform,
                        "handle": handle,
                        "url": link or f"https://{platform.lower()}.com/{handle}",
                    }
                )
                if len(found) >= max(1, limit):
                    return found
    return found


def search_profiles(
    name: str,
    api_key: str,
    proxy: str = "",
    timeout: float = 30.0,
    limit: int = 6,
    fallback: bool = True,
) -> list[dict[str, str]]:
    """图上写着 ``id=某某`` → 各平台同名主页。只出线索，不替人认人。

    这类写法多是中文平台的 ID（微博、小红书），X 上往往查无此号，所以一次检索
    把各平台主页都捞出来：先按「名字 + cosplay」搜，一条主页都没捞到再退回裸名字
    搜一次（同名的人会变多，但总比空手强）。真正的结论仍要回到翻号核图那一步。
    """
    name = (name or "").strip()
    if not name or not api_key:
        return []
    queries = [name_query(name, "cosplay", api_key)]
    if fallback:
        queries.append(name_query(name, "", api_key))
    for index, query in enumerate(queries):
        payload = web_search(query, api_key, proxy, num=20, timeout=timeout)
        found = profiles_from_search(payload, limit=limit)
        if found or index == len(queries) - 1:
            return found
    return []
