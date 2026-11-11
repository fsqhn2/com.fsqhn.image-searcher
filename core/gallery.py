"""从图集站 / 搬运站的页面里读出「这张图归哪个社交账号」。

视觉复核挑中的图经常落在 4play、v2ph、acgtang 这类图集站上：它们把别人的
Instagram、X 内容搬下来重新打包，页面本身不是作者主页。但这些站通常会在
标题、meta 描述或图集链接里写着来源账号——实测 4play 的 meta 直接给了
``instagram.com/menruinyanko``，而插件原来只把这页判成「不是作者主页」就
丢掉了，等于把唯一还能指向本人的线索一起扔掉。

这里只做纯函数：认站点、从 HTML 里抽账号。联网由插件负责。抽出来的账号一律
只当线索——站方标错的也见过，不能替用户认人。
"""

from __future__ import annotations

import re

# 实测见过的图集 / 搬运站。只收确认过的：误标一个正经站点比漏标更糟。
GALLERY_HOSTS = (
    "4play.to",
    "v2ph.com",
    "v2ph.ru",
    "acgtang.com",
    "beauty-draw.02o.app",
    "leakgallery.com",
    "jxporn.net",
    "lanrenbl.net",
    "4khd.com",
    "yuemeinv.com",
)

# 社区、论坛、搬运性质的站点：这类页面自己也贴别人的图，图上还有没有水印、
# 水印写的是谁，往往比页面本身有用。实测 arca.live 那条「나히다 코스프레」跟
# 原图 dHash 距离 0，页面里却一个来源账号都没有，只写着杂物分类（짤）。
REPOST_HOSTS = (
    "arca.live",
    "dcinside.com",
    "reddit.com",
    "tieba.baidu.com",
    "bbs.nga.cn",
    "ngabbs.com",
    "5ch.net",
    # 图集 / 盗图站：页面自己不标来源，标题里却常把模特的号写出来
    # （cosplayrule34「Model Misa Daidai (@xidaidai)」）。它们同样不是作者主页。
    "galleryepic.com",
    "cosplayrule34.com",
    # 游戏站的「囧图」「每日一囧」这类日更合集：整篇都是网上扒来的图，页面本身
    # 指不到作者。实测 s-20261111-2327-5138 命中游民星空一篇囧图合集，指纹距离 3
    # （基本就是同一张），但反查候选里的两个 X 号都没翻到，落点仍是搬运。
    "gamersky.com",
)

# 这些路径不是账号名。
_IG_RESERVED = {
    "p",
    "reel",
    "reels",
    "tv",
    "stories",
    "explore",
    "accounts",
    "directory",
    "about",
    "developer",
    "legal",
    "web",
    "static",
    "embed",
}
_X_RESERVED = {
    "i",
    "intent",
    "share",
    "home",
    "explore",
    "search",
    "settings",
    "login",
    "signup",
    "tos",
    "privacy",
    "hashtag",
    "compose",
    "messages",
}

_IG = re.compile(r"instagram\.com/([A-Za-z0-9_.]{2,30})", re.I)
_X = re.compile(r"(?:x|twitter)\.com/([A-Za-z0-9_]{2,15})", re.I)

# 同一个账号在页面上通常被引用很多次（每张缩略图都链向她的图集）；页脚、广告、
# 友链只会出现一两次。低于这个数说明不了「这页讲的就是她」。
MIN_HITS = 3


def is_gallery_site(url: str = "", platform: str = "") -> bool:
    """这个落点是不是图集 / 搬运站。按域名后缀精确匹配。"""
    hosts: list[str] = []
    host = (url or "").split("://", 1)[-1].split("/", 1)[0].rsplit("@", 1)[-1].split(":")[0]
    if host:
        hosts.append(host.strip().lower())
    if platform:
        hosts.append(str(platform).strip().lower())
    for item in hosts:
        item = item.removeprefix("www.")
        if item and any(item == token or item.endswith("." + token) for token in GALLERY_HOSTS):
            return True
    return False


def is_repost_site(url: str = "", platform: str = "") -> bool:
    """这个落点是不是搬运 / 社区站：图集站也算，它们同样不是作者主页。"""
    if is_gallery_site(url, platform):
        return True
    hosts: list[str] = []
    host = (url or "").split("://", 1)[-1].split("/", 1)[0].rsplit("@", 1)[-1].split(":")[0]
    if host:
        hosts.append(host.strip().lower())
    if platform:
        hosts.append(str(platform).strip().lower())
    for item in hosts:
        item = item.removeprefix("www.")
        if item and any(item == token or item.endswith("." + token) for token in REPOST_HOSTS):
            return True
    return False


def _clean(raw: str, reserved: set[str]) -> str:
    value = (raw or "").strip().strip(".").strip("_").lower()
    if not value or value in reserved:
        return ""
    # 纯数字是帖子 ID 之类的路径片段，不是账号名。
    if value.isdigit():
        return ""
    return value


def parse_source_accounts(html: str = "", page_url: str = "") -> list[dict[str, object]]:
    """从页面里抽来源账号：每个平台取出现次数最多的那个。

    只统计出现次数够多的账号。返回按次数从多到少排的列表，最多两个（IG / X）。
    页面里一个都认不出来就返回空列表——别硬猜。
    """
    text = str(html or "")
    if not text:
        return []
    found: dict[str, dict[str, object]] = {}
    for platform, pattern, reserved, base in (
        ("Instagram", _IG, _IG_RESERVED, "https://instagram.com/"),
        ("X", _X, _X_RESERVED, "https://x.com/"),
    ):
        counts: dict[str, int] = {}
        for raw in pattern.findall(text):
            handle = _clean(raw, reserved)
            if not handle:
                continue
            counts[handle] = counts.get(handle, 0) + 1
        if not counts:
            continue
        handle, count = max(counts.items(), key=lambda item: item[1])
        if count < MIN_HITS:
            continue
        found[platform] = {
            "platform": platform,
            "handle": handle,
            "url": base + handle,
            "count": count,
        }
    return sorted(found.values(), key=lambda item: int(item["count"]), reverse=True)
