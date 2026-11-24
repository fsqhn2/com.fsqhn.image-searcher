"""搜索页识别与关键词反解：纯函数，不碰网络。

Lens 常把候选落在平台的搜索页（本单 `s-20261121-1955-bae5` 落在
`search.bilibili.com/all?keyword=XxMiss Naruko/`，指纹距离 27）：这类页面是
JS 渲染的搜索结果列表，既不是某一条帖子，也解析不出发布者，
`vision.verdict_of()` 只能判 `VERDICT_NO_ACCOUNT` 降成 suspect。可它 query 里
那个关键词是有用的——拿它去平台检索接口反查，能得到几个候选账号，再拿这些号
自己的公开图比指纹，对上了才承认。

这里只放纯函数，三件事：

1. `search_page_query()`：这个 URL 是不是平台搜索页、关键词是什么。
2. `accounts_from_videos()` / `accounts_from_users()`：检索接口返回 → 账号候选
   （mid、昵称、空间页、封面图）。
3. `pick_hit()` / `hit_note()` / `miss_note()`：哪张图对上了、文案怎么写。

网络请求在 `core/bilibili.py`；判定与文案留在这里，方便单测。
"""

from __future__ import annotations

import re
from typing import Any, Iterable
from urllib.parse import parse_qs, unquote, urlparse

# 平台搜索页：域名 → 允许的路径。只认「搜索结果列表」这一种，别的页面不算。
# 加平台时在这里加一条，并且只在 `core/` 里补对应的检索层，不要动主干。
_SEARCH_PAGES: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    (
        "B站",
        ("search.bilibili.com",),
        # 空路径就是 B 站搜索站的默认页（会跳 /all），同样是搜索结果列表。
        ("", "/all", "/search", "/video"),
    ),
)

# 这些标题说明页面不是搜索结果列表（B 站个人空间页标题写「xx的个人空间」）。
# 标题只用来兜底排除，关键词一律从 URL 取：搜索结果页的标题会被截断、改写
# （「XxMiss Naruko-哔哩哔哩_bilibili」这类），拿它当关键词只会引错。
_NOT_SEARCH_TITLES = ("个人空间", "投稿视频", "的全部动态")

# 关键词两侧的引号：Lens／引擎常把关键词连引号一起塞进 query。
_QUOTES = ('"', "'", "“", "”", "‘", "’", "「", "」", "『", "』")

_SPACE_URL = "https://space.bilibili.com/{mid}"

_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")


def clean_keyword(raw: str) -> str:
    """URL 关键词归一：解码、去引号、去尾部斜杠、压空白。

    本单那条链接原文是 `?keyword=XxMiss Naruko/`——末尾那个 `/` 是 Lens 拼出来的，
    照原样送去检索一个号都搜不到（实测 `XxMiss Naruko/`、`XxMiss Naruko` 都是 0 条，
    但 `/` 留着只会让别的平台也搜空）。
    """
    text = unquote(str(raw or "")).strip()
    while text and text[-1] in "/／":
        text = text[:-1].rstrip()
    changed = True
    while changed and text:
        changed = False
        for mark in _QUOTES:
            if text.startswith(mark):
                text = text[len(mark):].lstrip()
                changed = True
            if text.endswith(mark):
                text = text[:-len(mark)].rstrip()
                changed = True
    return _SPACE_RE.sub(" ", text)


def search_page_query(url: str = "", title: str = "") -> tuple[str, str] | None:
    """这条候选是不是平台搜索页；是的话返回 `(平台, 关键词)`，不是返回 None。

    ``platform`` 用插件里的中文叫法（和 `extract.platform_of()` 一致，如「B站」），
    上层直接写进 trace。``space.bilibili.com`` 不是搜索页——那是账号主页，
    本身就能解析出 mid。
    """
    text = str(url or "").strip()
    if not text:
        return None
    if title and any(word in str(title) for word in _NOT_SEARCH_TITLES):
        return None
    parsed = urlparse(text)
    host = (parsed.netloc or "").split("@")[-1].split(":")[0].lower()
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return None
    path = (parsed.path or "/").rstrip("/").lower()
    query = parse_qs(parsed.query or "")
    for platform, hosts, paths in _SEARCH_PAGES:
        if not any(host == item or host.endswith("." + item) for item in hosts):
            continue
        if path not in paths:
            return None
        for value in query.get("keyword") or []:
            keyword = clean_keyword(value)
            if keyword:
                return platform, keyword
        # 搜索页但没带关键词：反解不出东西，按「不是搜索页」处理。
        return None
    return None


def space_url(mid: str | int) -> str:
    """B 站 mid → 空间页。"""
    return _SPACE_URL.format(mid=str(mid).strip())


def cover_url(raw: str) -> str:
    """投稿封面地址：B 站给的是协议相对地址（`//i0.hdslb.com/...`），补成 https。"""
    text = str(raw or "").strip()
    if text.startswith("//"):
        text = "https:" + text
    if not text.startswith(("http://", "https://")):
        return ""
    return text


def _rows(payload: Any) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if not isinstance(data, dict):
        return []
    rows = data.get("result")
    return [row for row in (rows or []) if isinstance(row, dict)]


def _clean_name(raw: Any) -> str:
    """B 站检索会给昵称加 `<em class="keyword">` 高亮标签，去掉再展示。"""
    return _TAG_RE.sub("", str(raw or "")).strip()


def accounts_from_videos(
    payload: Any, limit: int = 8, images_per_account: int = 4
) -> list[dict[str, Any]]:
    """视频搜索结果 → 账号候选。

    一条结果同时给出作者 mid 和这条投稿的封面：拿关键词搜一遍，就能既反解出号、
    又拿到这个号公开过的图（实锤要用的就是封面）。按出现顺序去重，一个号一条。
    """
    accounts: dict[str, dict[str, Any]] = {}
    for row in _rows(payload):
        mid = str(row.get("mid") or "").strip()
        if not mid.isdigit() or mid == "0":
            continue
        account = accounts.get(mid)
        if account is None:
            account = {
                "mid": mid,
                "name": _clean_name(row.get("author")),
                "url": space_url(mid),
                "images": [],
                "videos": [],
            }
            accounts[mid] = account
        bvid = str(row.get("bvid") or "").strip()
        if bvid:
            video = f"https://www.bilibili.com/video/{bvid}"
            if video not in account["videos"]:
                account["videos"].append(video)
        cover = cover_url(row.get("pic"))
        if cover and cover not in account["images"] and len(account["images"]) < images_per_account:
            account["images"].append(cover)
        if len(accounts) >= max(1, limit):
            break
    return list(accounts.values())[: max(1, limit)]


def accounts_from_users(payload: Any, limit: int = 6) -> list[dict[str, Any]]:
    """用户搜索结果 → 账号候选（只有 mid、昵称、空间页，没有图）。

    这一路只用来「名字对得上」，图的来源要靠再搜一次这个号自己的投稿
    （`core/bilibili.resolve_accounts()` 里做）；拿不到图就不能实锤，只能记疑似。
    """
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in _rows(payload):
        mid = str(row.get("mid") or "").strip()
        if not mid.isdigit() or mid == "0" or mid in seen:
            continue
        seen.add(mid)
        found.append(
            {
                "mid": mid,
                "name": _clean_name(row.get("uname")),
                "url": space_url(mid),
                "images": [],
                "videos": [],
            }
        )
        if len(found) >= max(1, limit):
            break
    return found


def pick_hit(
    accounts: Iterable[dict[str, Any]],
    distances: dict[str, int | None],
    threshold: int,
) -> tuple[dict[str, Any], str, int] | None:
    """哪个号的哪张图对上了：返回 `(账号, 图片地址, 距离)`，一张都没对上返回 None。

    准入只看指纹：粉丝数、播放量一概不参与，同名的号只能靠图区分。
    """
    best: tuple[dict[str, Any], str, int] | None = None
    for account in accounts or ():
        for image in account.get("images") or []:
            distance = distances.get(str(image))
            if distance is None or distance > threshold:
                continue
            if best is None or distance < best[2]:
                best = (account, str(image), int(distance))
    return best


def hit_note(keyword: str, name: str, distance: int) -> str:
    """命中时那一行说明：说清是顺着关键词找的、不是引擎直接给的。"""
    who = f"「{name}」这个号" if name else "一个同名账号"
    return f"关键词「{keyword}」反解到{who}，他/她自己的投稿封面里有这张图（指纹距离 {distance}）"


def miss_note(names: str | list[str], reason: str = "图对不上") -> str:
    """反解到号、但实锤没过时的说明（保持 suspect，只多一行解释）。

    粉丝数不能代替指纹，所以这里也不会因为号大就网开一面。
    """
    if isinstance(names, str):
        rows = [names.strip()] if names.strip() else []
    else:
        rows = [str(name or "").strip() for name in (names or []) if str(name or "").strip()]
    if not rows:
        return f"关键词反解到疑似账号，{reason}"
    if len(rows) == 1:
        return f"关键词反解到疑似账号 @{rows[0]}，{reason}"
    listed = "、".join("@" + name for name in rows[:3])
    return f"关键词反解到 {len(rows)} 个疑似账号（{listed}），{reason}"
