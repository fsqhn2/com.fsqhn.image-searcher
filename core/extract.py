"""从结果链接里提取平台、名字与账号。

不抓页面：x.com 需要登录且靠 JS 渲染，handle 直接从 URL 或标题里取更快更稳。
"""

from __future__ import annotations

import re
from urllib.parse import unquote, urlparse

MIRROR_URL_RE = re.compile(
    r"^https?://(?:[A-Za-z0-9-]+\.)*(?:sotwe\.com|twstalker\.com|twstalker\.org)/([A-Za-z0-9_]+)",
    re.I,
)
# 社交平台的「爬虫占位图」：Google 索引里拿到的是这些地址，点开返回的是登录页
# HTML，不是图片。Instagram 可以换成 /p/<短码>/media/ 直链，Facebook、Threads
# 没有免登录的等价接口。
CRAWLER_PLACEHOLDER_HOSTS = ("lookaside.instagram.com", "lookaside.fbsbx.com")
IG_SHORTCODE_RE = re.compile(r"instagram\.com/(?:p|reel|tv)/([A-Za-z0-9_-]{5,20})")

X_STATUS_RE = re.compile(r"^https?://(?:www\.)?(?:x|twitter)\.com/([A-Za-z0-9_]{2,20})(?:/status/|/?)", re.I)
HANDLE_RE = re.compile(r"@([A-Za-z0-9_]{2,20})")
SOTWE_TITLE_SUFFIX_RE = re.compile(r"\s*[-–—]\s*(?:Twitter|X).*$", re.I)
# X 帖子的搜索结果标题长这样：`□카🕯 on X: "그러니까 …"`。后面那半截是正文，
# 不是名字；整条当名字发出去，用户看到的就是一长串句子。
X_ON_TITLE_RE = re.compile(r"^(?P<name>.+?)\s+on\s+(?:X|Twitter):\s*", re.I)
BILIBILI_SPACE_RE = re.compile(r"space\.bilibili\.com/(\d+)")
BILIBILI_VIDEO_RE = re.compile(r"bilibili\.com/video/(BV[A-Za-z0-9]+)")
DOUYIN_USER_RE = re.compile(r"douyin\.com/user/([A-Za-z0-9_\-]+)")
WEIBO_UID_RE = re.compile(r"weibo\.(?:com|cn)/(?:u/)?(\d{5,})")

# ── 账号页识别 ────────────────────────────────────────────────────────────
#
# 引擎（尤其 Lens / 百度）给回来的链接大量是帖子页、图集页、搜索页，
# 甚至有 x.com/i/… 这种占位路径。只按「域名 + 第一段」抓 handle 会把它们当成账号，
# 用户点开不是 404 就是首页。这里显式列白名单形态 + 每平台的路径黑名单。

INSTAGRAM_ACCOUNT_RE = re.compile(r"^https?://(?:www\.)?instagram\.com/([A-Za-z0-9_.]{2,30})/?", re.I)
PIXIV_USER_RE = re.compile(
    r"^https?://(?:www\.)?pixiv\.net/(?:en/)?(?:users/(\d+)|member\.php\?id=(\d+))",
    re.I,
)
WEIBO_NICK_RE = re.compile(r"^https?://(?:www\.|m\.)?weibo\.(?:com|cn)/n/([^/?#]+)", re.I)
TIKTOK_ACCOUNT_RE = re.compile(r"^https?://(?:www\.)?tiktok\.com/@([A-Za-z0-9_.]{2,30})", re.I)

# 这些第一段不是账号，是功能页。命中就直接判定「不是账号页」。
ACCOUNT_PATH_DENY: dict[str, frozenset[str]] = {
    "x": frozenset({
        "i", "intent", "share", "home", "search", "hashtag", "explore", "notifications",
        "messages", "settings", "login", "signup", "tos", "privacy", "account", "compose",
    }),
    "weibo": frozenset({
        "ttarticle", "p", "show", "status", "detail", "photo", "video", "article", "fav",
        "like", "comment", "search", "hot", "topic", "tv", "page", "s", "login", "signup", "reg",
    }),
    "instagram": frozenset({
        "p", "reel", "reels", "explore", "accounts", "stories", "tv", "about", "legal",
        "directory", "developer", "web",
    }),
    "pixiv": frozenset({"artworks", "i", "en", "tags", "search", "ranking", "novel", "ajax", "bookmark"}),
}

HIGH_VALUE_HOSTS = (
    "sotwe.com",
    "twstalker.com",
    "twitter.com",
    "x.com",
    "instagram.com",
    "weibo.com",
    "weibo.cn",
    "douyin.com",
    "bilibili.com",
    "tiktok.com",
    "telegra.ph",
    "v2ph",
    "ocosplay",
    "fanbox",
    "patreon.com",
)
LOW_VALUE_HOSTS = ("pinterest.", "google.", "bing.", "gstatic.")


def promote_mirror(url: str, title: str = "") -> tuple[str, str]:
    """把 sotwe / TwStalker 这类 X 镜像链接还原成 x.com 主页，并校正标题。

    返回 (url, title)。标题里的 @handle 比镜像路径可靠，路径有时会多带下划线。
    Google 抓不到 x.com，Lens 命中 X 上的图时给回来的往往是这些镜像页，
    还原成主页用户才有得点。
    """
    match = MIRROR_URL_RE.match(url or "")
    if not match:
        return url, title
    cleaned = SOTWE_TITLE_SUFFIX_RE.sub("", title or "").strip()
    found = HANDLE_RE.search(cleaned)
    handle = found.group(1) if found else match.group(1).rstrip("_")
    return f"https://x.com/{handle}", cleaned


def is_x_platform(platform: str) -> bool:
    """是否为 X / Twitter 结果。

    用于「指纹距离相同优先用 X」的排序规则，只认平台名，不猜链接。
    """
    text = (platform or "").strip().lower()
    return "twitter" in text or text in {"x", "x.com"}


def is_pixiv_platform(platform: str, url: str = "") -> bool:
    """是否为 pixiv 主站结果（含 i.pximg.net 图床）。

    插画路线用它把 pixiv 的原始出处顶到前面；FANBOX 属于赞助页，不算主站。
    """
    text = f"{platform or ''} {(urlparse(url).netloc or '')}".lower()
    return "pixiv.net" in text or "pximg.net" in text


_SOCIAL_HOSTS = (
    "x.com",
    "twitter.com",
    "twimg.com",
    "sotwe.com",
    "twstalker.com",
    "weibo.com",
    "weibo.cn",
    "xiaohongshu.com",
    "xhslink.com",
    "instagram.com",
    "facebook.com",
    "threads.net",
    "tiktok.com",
    "douyin.com",
    "bilibili.com",
    "b23.tv",
    "pixiv.net",
    "pximg.net",
    "reddit.com",
    "pinterest.com",
    "pinterest.jp",
    "youtube.com",
    "youtu.be",
    "vk.com",
    "t.me",
)


def is_social_platform(url: str = "", source: str = "") -> bool:
    """是否为社交平台结果。

    照片路线用它优先核对「原帖最可能在哪」：这些站点上的候选即使超出
    常规验证配额，也要补进去比一次。按域名后缀精确匹配，避免 netflix.com
    这类域名被 x.com 误判。
    """
    host = (urlparse(url).netloc or "").strip().lower()
    hosts = [host] if host else []
    if source:
        hosts.append(str(source).strip().lower())
    for item in hosts:
        item = item.rsplit("@", 1)[-1].split(":")[0]
        if item and any(item == token or item.endswith("." + token) for token in _SOCIAL_HOSTS):
            return True
    return False


# X 上这三个页面是「她转的别人的帖子」，不是她自己发的。
_REPOST_PATHS = {"reposts", "likes", "with_replies"}


def is_repost_page(url: str = "") -> bool:
    """是否为 X 的转发 / 点赞 / 回复页。

    这类页面上出现的图不一定是这个人发的：coser 之间互相转发很常见，
    拿转发页当作者会直接把图判到别人头上。
    """
    return x_page_kind(url) in _REPOST_PATHS


# 非帖子页面在结果里要写清是哪种页面，别让它冒充原帖。
X_PAGE_LABELS = {
    "reposts": "转发列表页",
    "likes": "点赞列表页",
    "with_replies": "回复列表页",
    "profile": "账号主页",
    "media": "带图帖子列表页",
    "search": "搜索页",
    "hashtag": "话题页",
    "other": "页面",
}


def x_page_kind(url: str = "") -> str:
    """X 链接是哪一种页面。

    只有 ``/<号>/status/<id>`` 这种帖子页才算「某条帖子」。主页、转发/点赞/
    回复列表页上出现的图，发布者是别人——实测 s-20261105-1423 那单把
    ``x.com/AlbertGreen69/with_replies`` 当成原帖主，就是踩了这个坑。
    """
    parsed = urlparse(url)
    host = (parsed.netloc or "").strip().lower()
    if host == "sotwe.com" or host.endswith(".sotwe.com"):
        return "profile"
    if not any(host == token or host.endswith("." + token) for token in ("x.com", "twitter.com")):
        return ""
    parts = [part.lower() for part in (parsed.path or "").split("/") if part]
    if not parts:
        return "profile"
    if parts[0] in {"search", "hashtag"}:
        return parts[0]
    if len(parts) > 1 and parts[1] in _REPOST_PATHS:
        return parts[1]
    if "status" in parts[:3]:
        return "post"
    if len(parts) == 1:
        return "profile"
    if parts[1] in {"media", "photo"}:
        return "media"
    return "other"


def is_x_post_page(url: str = "") -> bool:
    """是不是一条具体的 X 帖子（只有这种页面能当「原帖」）。"""
    return x_page_kind(url) == "post"


def x_page_handle(url: str = "") -> str:
    """从 X 链接里取第一段路径当账号名：``x.com/<号>/with_replies`` → ``<号>``。

    只认 x.com / twitter.com；``i``、``search``、``hashtag`` 这些功能段不算账号。
    """
    parsed = urlparse(url)
    host = (parsed.netloc or "").strip().lower()
    if not any(host == token or host.endswith("." + token) for token in ("x.com", "twitter.com")):
        return ""
    parts = [part for part in (parsed.path or "").split("/") if part]
    if not parts:
        return ""
    head = parts[0].lstrip("@")
    if head.lower() in {"i", "search", "hashtag", "home", "explore", "notifications"}:
        return ""
    return head


def platform_of(url: str = "", source: str = "", title: str = "") -> str:
    """判断结果所在平台。"""
    host = (urlparse(url).netloc or source or "").lower()
    # 必须按域名后缀精确匹配：`"x.com" in "de.hentai-cosplay-xxx.com"` 会命中，
    # 把带 xxx 的资源站当成 X。is_social_platform() 用的是同一套规则。
    if any(host == token or host.endswith("." + token) for token in ("sotwe.com", "twitter.com", "x.com")):
        return "X (Twitter)"
    if "bilibili.com" in host or "b23.tv" in host:
        return "B站"
    if "douyin.com" in host or "iesdouyin.com" in host:
        return "抖音"
    if "weibo.com" in host or "weibo.cn" in host:
        return "微博"
    if "instagram.com" in host:
        return "Instagram"
    if "tiktok.com" in host:
        return "TikTok"
    if "pinterest." in host:
        return "Pinterest"
    if "telegra.ph" in host:
        return "Telegram"
    if "fanbox.cc" in host:
        return "Pixiv FANBOX"
    if "patreon.com" in host:
        return "Patreon"
    if host:
        return host.removeprefix("www.")
    if title:
        return "网页"
    return "未知来源"


# 论坛 / 社区站的页面标题是帖子标题，不是人名。把它当「名字」发出去，用户看到的
# 就是一串串韩文、日文或「xxx - 某某版」的垃圾（实测 inven.co.kr 那单）。
_FORUM_HOSTS = (
    "inven.co.kr",
    "dcinside.com",
    "arca.live",
    "theqoo.net",
    "bcy.net",
    "tieba.baidu.com",
    "5ch.net",
    "2ch.sc",
    "ptt.cc",
    "reddit.com",
    "namu.wiki",
)


def is_forum_page(url: str = "", source: str = "") -> bool:
    """这个链接是不是论坛 / 社区帖。是的话标题只能当标题，不能当名字。"""
    host = (urlparse(url).netloc or source or "").strip().lower()
    host = host.rsplit("@", 1)[-1].split(":")[0]
    if not host:
        return False
    return any(host == token or host.endswith("." + token) for token in _FORUM_HOSTS)


def match_account(url: str = "") -> tuple[str, str, str] | None:
    """把链接解析成 (平台标识, 账号, 规范化的账号页地址)。

    只认账号页形态：帖子页、图集页、搜索页一律返回 None，交给标题兜底。
    pixiv 老站 `member.php?id=` 会归一成 `/users/<id>`，微博 `/n/昵称` 会解码。
    """
    text = unquote((url or "").strip())
    if not text:
        return None

    x = X_STATUS_RE.match(text)
    if x and not (x.group(1) or "").lower() in ACCOUNT_PATH_DENY["x"]:
        handle = x.group(1)
        return "x", handle, f"https://x.com/{handle}"

    weibo = re.match(r"^https?://(?:www\.|m\.)?weibo\.(?:com|cn)/(?:u/)?(\d{5,})", text, re.I)
    if weibo:
        uid = weibo.group(1)
        return "weibo", uid, f"https://weibo.com/u/{uid}"

    nick = WEIBO_NICK_RE.match(text)
    if nick:
        name = nick.group(1)
        return "weibo", name, f"https://weibo.com/n/{name}"

    bili = BILIBILI_SPACE_RE.search(text)
    if bili:
        return "bilibili", bili.group(1), f"https://space.bilibili.com/{bili.group(1)}"

    douyin = DOUYIN_USER_RE.search(text)
    if douyin:
        return "douyin", douyin.group(1), f"https://www.douyin.com/user/{douyin.group(1)}"

    insta = INSTAGRAM_ACCOUNT_RE.match(text)
    if insta and (insta.group(1) or "").lower() not in ACCOUNT_PATH_DENY["instagram"]:
        handle = insta.group(1)
        return "instagram", handle, f"https://www.instagram.com/{handle}/"

    pixiv = PIXIV_USER_RE.match(text)
    if pixiv:
        uid = pixiv.group(1) or pixiv.group(2) or ""
        if uid:
            return "pixiv", uid, f"https://www.pixiv.net/users/{uid}"

    tiktok = TIKTOK_ACCOUNT_RE.match(text)
    if tiktok:
        handle = tiktok.group(1)
        return "tiktok", handle, f"https://www.tiktok.com/@{handle}"

    return None


def canonical_account_url(url: str = "") -> str:
    """账号页归一化：认得的平台给规范地址，不认得原样返回。"""
    account = match_account(url)
    return account[2] if account else (url or "")


def x_title_name(title: str = "") -> str:
    """X 帖子标题里只有 ``on X:`` 前面那截是名字，后面跟的是正文。

    ``kiko on X: "Happy Halloween!"`` 整条拿去抽名字，就会把 Halloween 当成
    人名，一路推出 @halloween 和 leakgallery.com/Halloween。
    """
    text = (title or "").strip()
    found = X_ON_TITLE_RE.match(text)
    return found.group("name").strip() if found else text


_MODEL_PREFIX_RE = re.compile(r"^\s*(?:the\s+)?(?:model|cosplayer|coser|cosplay\s+model)\s*[:：]?\s+", re.I)
_EMPTY_PAREN_RE = re.compile(r"\s*[(（]\s*[)）]\s*$")
_ANY_EMPTY_PAREN_RE = re.compile(r"[(（]\s*[)）]")
_REPOST_SEP_RE = re.compile(r"\s+(?:[–—|]|-|:)\s+|\s*[：]\s*")
_IN_COSPLAY_RE = re.compile(r"\s+in\s+cosplay\s+", re.I)
_PHOTO_COUNT_RE = re.compile(r"(?:set\s+of\s+)?(\d+)\s*photos?\b", re.I)
# 后半截得像个套图说明（写着 cosplay / photos 之类），否则可能只是角色名
# （"Nahida – Genshin Impact" 前半截是角色不是模特）。
_SET_HINT_RE = re.compile(r"cosplay|photo|picture|album|image|set\b", re.I)


def repost_model_name(title: str = "") -> str:
    """图集站标题里的模特名。

    cosplayrule34 的标题是 ``Model Misa Daidai (@xidaidai)``，账号被抽走后
    只剩 ``Model Misa Daidai ()``：直接当名字摆出来又长又带一对空括号。
    """
    raw = (title or "").strip()
    text = _EMPTY_PAREN_RE.sub("", raw)
    text = _MODEL_PREFIX_RE.sub("", text)
    text = re.sub(r"\s{2,}", " ", text).strip()
    return text or raw


def split_repost_title(title: str = "") -> tuple[str, str]:
    """图集站标题切成（模特名, 套图说明）。

    galleryepic 的标题是 ``Xidaidai – Aemeath 18+ Cosplay (Wuthering Waves), 18
    Photos``：整条当名字摆出来，用户看到的是一串英文。名字就在分隔符前那截。

    切不出来、或者切出来的尾巴太碎（``Aemeath from ...``）就当没有：宁可不
    说，也别把半截标题当成套图信息。
    """
    raw = (title or "").strip()
    text = _EMPTY_PAREN_RE.sub("", raw)
    had_prefix = bool(_MODEL_PREFIX_RE.match(text))
    text = _MODEL_PREFIX_RE.sub("", text)
    text = _ANY_EMPTY_PAREN_RE.sub(" ", text)
    text = re.sub(r"\s{2,}", " ", text).strip()
    name, extra = text, ""
    for pattern in (_IN_COSPLAY_RE, _REPOST_SEP_RE):
        parts = pattern.split(text, maxsplit=1)
        if len(parts) == 2:
            name, extra = parts[0].strip(), parts[1].strip()
            break
    else:
        # 没有分隔符时退一步看逗号，但前半截得像个人名（词少、不带句读）。
        if ", " in text:
            head, tail = text.split(", ", 1)
            if head and len(head.split()) <= 4:
                name, extra = head.strip(), tail.strip()
    if not extra or not _SET_HINT_RE.search(extra) or extra.endswith(("...", "…")):
        extra = ""
    if extra:
        return name, _PHOTO_COUNT_RE.sub(r"\1 张", extra)
    # 切不出套图说明时，只有标题自带「Model X」前缀才敢认这个名字；否则整条
    # 原样返回（"Nahida – Genshin Impact" 的 Nahida 是角色，不是模特）。
    return (name if had_prefix and name else raw), ""


def extract_identity(url: str, title: str = "") -> tuple[str, str]:
    """返回 (名字, 账号)。

    账号优先从链接里取：只在链接是账号页形态时才认，帖子页/搜索页不硬猜。
    """
    clean_title = SOTWE_TITLE_SUFFIX_RE.sub("", title or "").strip()
    clean_title = x_title_name(clean_title)
    handle = ""

    account = match_account(url)
    if account:
        raw = account[1]
        # 纯数字（微博 uid / B站 mid / pixiv 会员号）看不出是谁，标成 UID。
        handle = f"UID {raw}" if raw.isdigit() else "@" + raw
    elif BILIBILI_VIDEO_RE.search(url or ""):
        handle = BILIBILI_VIDEO_RE.search(url or "").group(1)

    # 标题里通常长这样：「荣小兔头 @usagi_rr - Twitter」
    if not handle:
        found = HANDLE_RE.search(clean_title)
        if found:
            handle = "@" + found.group(1)

    name = HANDLE_RE.sub("", clean_title).strip(" -–—|·")
    return name[:60], handle


def account_key(platform: str, handle: str) -> str:
    """账号去重用的键。没有账号的结果返回空串，不参与去重。"""
    text = (handle or "").strip()
    if text.startswith("UID "):
        text = text[4:]
    text = text.lstrip("@").strip().lower()
    if not text:
        return ""
    return f"{(platform or '').strip()}|{text}"


_STATUS_QUALITY = {"match": 0, "vision": 1, "suspect": 2, "unverified": 3, "mismatch": 4, "error": 5}


def _quality(item: object) -> tuple[int, int]:
    """同一条账号被多个引擎撞见时，比较谁更可信：先看验证状态，再看指纹距离。"""
    status = str(getattr(item, "status", "") or "unverified")
    distance = getattr(item, "distance", None)
    return (_STATUS_QUALITY.get(status, 3), distance if isinstance(distance, int) else 999)


def dedupe_accounts(items: list) -> list:
    """同一账号被多个引擎撞见时只留一条，位置按第一次出现，内容取更可信的那条。

    没有账号的候选原样保留——参考区里它们仍然有用。
    """
    index_of: dict[str, int] = {}
    kept: list = []
    for item in items or []:
        key = account_key(getattr(item, "platform", ""), getattr(item, "handle", ""))
        if not key:
            kept.append(item)
            continue
        position = index_of.get(key)
        if position is None:
            index_of[key] = len(kept)
            kept.append(item)
            continue
        if _quality(item) < _quality(kept[position]):
            kept[position] = item
    return kept


def relevance_rank(url: str = "", source: str = "") -> int:
    """来源价值：账号页/原创站 0，普通网页 1，相册转载站 2。"""
    host = (urlparse(url).netloc or source or "").lower()
    if any(token in host for token in HIGH_VALUE_HOSTS):
        return 0
    if any(token in host for token in LOW_VALUE_HOSTS):
        return 2
    return 1


def clean_url(url: str) -> str:
    """去掉常见追踪参数，并解码一次。"""
    if not url:
        return ""
    return unquote(url.strip())


def is_crawler_placeholder(url: str) -> bool:
    """判断是不是社交平台的「爬虫占位图」地址。

    这些地址是 Google 那边索引到的入口，取回来是一张 500KB 左右的登录页 HTML，
    既解不成图也没有任何信号。当成普通图片去下，只会白占一次下载并在结果里留
    一条「无法解码」。
    """
    raw = (url or "").strip()
    if not raw:
        return False
    host = urlparse(raw).netloc.lower()
    for placeholder in CRAWLER_PLACEHOLDER_HOSTS:
        if host == placeholder or host.endswith("." + placeholder):
            return True
    return "/seo/google_widget/crawler" in raw or "/lookaside/crawler" in raw


# 头像命中的 note 前缀。参考图挑选、结论统计都靠它认出来，改文案时只改这里。
AVATAR_NOTE = "该图是站点账号头像"


_AVATAR_HINTS = (
    "avatar",
    "logo",
    "favicon",
    # X / Twitter 的头像图：pbs.twimg.com/profile_images/<id>/xxx_400x400.jpg
    "profile_images",
    "profile_image",
    "profile_pic",
)


def is_avatar_image(url: str = "") -> bool:
    """图片地址看着像站点头像/标志，而不是帖子里的原图。

    Lens 会把页面里的用户头像、站点图标当结果图返回。头像跟原图指纹一致说明
    「这张图被这个账号拿去当头像了」，但点进页面往往看不到它，不能当原帖出处。
    """
    path = urlparse(url or "").path.lower()
    if not path:
        return False
    return any(hint in path for hint in _AVATAR_HINTS)


def rescue_image_url(page_url: str, image_url: str) -> str:
    """把占位图地址换成能直接下的真图地址，换不了返回空串。

    Instagram 的 ``/p/<短码>/media/?size=l`` 不需要登录就返回 JPEG（实测 5/5）；
    Facebook、Threads 没有免登录的等价接口，只能放弃。
    """
    if not is_crawler_placeholder(image_url):
        return ""
    page = page_url or ""
    if "instagram.com" not in page:
        return ""
    found = IG_SHORTCODE_RE.search(page)
    if not found:
        return ""
    return f"https://www.instagram.com/p/{found.group(1)}/media/?size=l"
