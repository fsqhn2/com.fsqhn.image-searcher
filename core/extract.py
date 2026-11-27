"""从结果链接里提取平台、名字与账号。

不抓页面：x.com 需要登录且靠 JS 渲染，handle 直接从 URL 或标题里取更快更稳。
"""

from __future__ import annotations

import re
from urllib.parse import unquote, urljoin, urlparse

from .hosts import host_of, is_host, matched_token

MIRROR_URL_RE = re.compile(
    r"^https?://(?:[A-Za-z0-9-]+\.)*(?:sotwe\.com|twstalker\.com|twstalker\.org)/([A-Za-z0-9_]+)",
    re.I,
)
# 社交平台的「爬虫占位图」：Google 索引里拿到的是这些地址，点开返回的是登录页
# HTML，不是图片。Instagram 可以换成 /p/<短码>/media/ 直链，Facebook、Threads
# 没有免登录的等价接口。
CRAWLER_PLACEHOLDER_HOSTS = ("lookaside.instagram.com", "lookaside.fbsbx.com")
# X 的域名只有这两个。x_page_kind / x_page_handle / platform_of 原来各抄一遍，
# 加第三个域名（比如某个镜像）时会漏掉其中一处，收到这里。
_X_HOSTS = ("x.com", "twitter.com")
IG_SHORTCODE_RE = re.compile(r"instagram\.com/(?:p|reel|tv)/([A-Za-z0-9_-]{5,20})")

X_STATUS_RE = re.compile(r"^https?://(?:www\.)?(?:x|twitter)\.com/([A-Za-z0-9_]{2,20})(?:/status/|/?)", re.I)
HANDLE_RE = re.compile(r"@([A-Za-z0-9_]{2,20})")
SOTWE_TITLE_SUFFIX_RE = re.compile(r"\s*[-–—]\s*(?:Twitter|X).*$", re.I)
# X 帖子的搜索结果标题长这样：`□카🕯 on X: "그러니까 …"`。后面那半截是正文，
# 不是名字；整条当名字发出去，用户看到的就是一长串句子。
# `on` 两边不一定有空格：中文名紧贴着写（`关先生on X: "…"`），老规则的 `\s+`
# 要求空格，匹配不上就退回整条标题。前面那个 lookbehind 是为了别把名字里的
# `on` 当成连接词（`Nikon X: …` 不该切成 `Nik`）。
X_ON_TITLE_RE = re.compile(
    r"^(?P<name>.+?)(?<![A-Za-z0-9_])on\s*(?:X|Twitter)\s*[:：]\s*(?P<text>.*)$",
    re.I | re.S,
)
# 没有正文的那种标题：`Tsuki_月隐on X / X`。这种多半是 X 的页面标题被搜索引擎
# 截了，`on X` 后面没有冒号和正文，上面那条要求冒号的正则匹配不上，名字尾巴就
# 带着 `on` 一路去各平台搜（cf7d 那单搜的是「Tsuki_月隐on」，白搜一轮）。
# lookbehind 同上：`Nikon X` 这种名字自带 `on` 的不切。
X_ON_TAIL_RE = re.compile(
    r"^(?P<name>.+?)(?<![A-Za-z0-9_])on\s*(?:X|Twitter)(?:\s*/\s*(?:X|Twitter))?\s*$",
    re.I,
)
# 正文外面那圈引号：中英文、单双引号、书名号都有，剥掉再展示。
_TITLE_QUOTE_CHARS = "\"'“”‘’「」『』"
# x.com 页面标题的尾巴：「名字 (@handle) / X」。账号先被抽走，这条再收后缀。
_X_PAGE_SUFFIX_RE = re.compile(r"\s*/\s*(?:X|Twitter)\s*$", re.I)
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


# 搬运站的图床文件名常把 pixiv 作品号拼进去，还顺手写上作者名：
# `沒有名字-by-lol-with-Arknights-Endfield-…-152342429.webp`。这是「这张图出自
# 哪个 pixiv 作品」的现成注脚，比任何相似度都硬——但必须回流到 pixiv 校验：
# 作品号要真的存在，作者名要真的对得上，否则 `photo-20261123.jpg` 这种也能凑巧
# 撞上一个 2016 年的旧作品号。
_ARTWORK_TAIL_RE = re.compile(r"[-_](\d{6,10})\.(?:webp|jpe?g|png|avif|gif)$", re.I)


def pixiv_filename_hint(url: str) -> tuple[str, str]:
    """从图片地址的文件名里读 pixiv 作品号与作者名：返回 ``(作品号, 文件名)``。

    抽不到作品号时返回 ``("", "")``。作者名不在这里判断，交给调用方跟 pixiv
    返回的 ``userName`` 比——文件名里 `by-xxx` 那一段可能是「by admin」这种
    站方署名，光看文件名分不清。
    """
    path = unquote(urlparse(url or "").path or "")
    name = path.rsplit("/", 1)[-1]
    if not name:
        return "", ""
    match = _ARTWORK_TAIL_RE.search(name)
    if not match:
        return "", ""
    return match.group(1), name


def pixiv_artwork_url(illust_id: str) -> str:
    """pixiv 作品页地址（老链接一律归一成 /artworks/<id>）。"""
    value = str(illust_id or "").strip()
    return f"https://www.pixiv.net/artworks/{value}" if value else ""


# pixiv 作品页地址里的号：新版是 `/artworks/<id>`，镜像站（kurocore 这类）用
# `/illust/<id>`，老版是 `?illust_id=<id>`。镜像站把 pixiv 作品原样重发，路径里的
# 号就是 pixiv 作品号——b08b 那单的 kurocore 就是这样。
_PIXIV_PATH_ID_RE = re.compile(r"/(?:artworks|illust|i)/(\d{5,12})(?:[/?#]|$)", re.I)
_PIXIV_QUERY_ID_RE = re.compile(r"(?:^|[?&])illust_id=(\d{5,12})", re.I)
# 只认 pixiv 自己与已知镜像站：别的站 `/illust/123` 是自家编号，拿来当 pixiv 作品
# 号会撞上不相干的作品（真正把关的仍是回 pixiv 核对 + 作者名比对）。
_PIXIV_ARTWORK_HOSTS = ("pixiv.net", "pixiv.re", "kurocore.com")


def is_pixiv_artwork_host(url: str = "") -> bool:
    return is_host(url, _PIXIV_ARTWORK_HOSTS)


def pixiv_url_hint(url: str = "") -> str:
    """作品页地址里的 pixiv 作品号；读不到返回空串。"""
    text = url or ""
    if not is_pixiv_artwork_host(text):
        return ""
    parsed = urlparse(text)
    found = _PIXIV_PATH_ID_RE.search(parsed.path or "")
    if found:
        return found.group(1)
    found = _PIXIV_QUERY_ID_RE.search("?" + (parsed.query or ""))
    return found.group(1) if found else ""


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
        if is_host(item, _SOCIAL_HOSTS):
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
    if not is_host(host, _X_HOSTS):
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
    if not is_host(host, _X_HOSTS):
        return ""
    parts = [part for part in (parsed.path or "").split("/") if part]
    if not parts:
        return ""
    head = parts[0].lstrip("@")
    if head.lower() in {"i", "search", "hashtag", "home", "explore", "notifications"}:
        return ""
    return head


# 平台 → 域名。顺序就是原来的判断顺序，别随便调：X 放最前面，因为 sotwe 这类
# 镜像页面的正文也是 X 的。
_PLATFORM_HOSTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("X (Twitter)", ("sotwe.com",) + _X_HOSTS),
    ("B站", ("bilibili.com", "b23.tv")),
    ("抖音", ("douyin.com", "iesdouyin.com")),
    ("微博", ("weibo.com", "weibo.cn")),
    ("小红书", ("xiaohongshu.com", "xhslink.com")),
    ("Instagram", ("instagram.com",)),
    ("TikTok", ("tiktok.com",)),
    ("Telegram", ("telegra.ph",)),
    ("Pixiv FANBOX", ("fanbox.cc",)),
    ("Patreon", ("patreon.com",)),
)
# Pinterest 是一堆国别域名（pinterest.de、pinterest.co.uk…），按品牌认，不逐个列。
_PINTEREST_HOST_RE = re.compile(r"(?:^|\.)pinterest\.", re.I)


def platform_of(url: str = "", source: str = "", title: str = "") -> str:
    """判断结果所在平台。"""
    host = (urlparse(url).netloc or source or "").lower()
    # 必须按域名后缀精确匹配：`"x.com" in "de.hentai-cosplay-xxx.com"` 会命中，
    # 把带 xxx 的资源站当成 X。is_social_platform() 用的是同一套规则。
    for name, tokens in _PLATFORM_HOSTS:
        if is_host(host, tokens):
            return name
    if _PINTEREST_HOST_RE.search(host):
        return "Pinterest"
    if host:
        return host.removeprefix("www.")
    if title:
        return "网页"
    return "未知来源"


# 论坛 / 社区站的页面标题是帖子标题，不是人名。把它当「名字」发出去，用户看到的
# 就是一串串韩文、日文或「xxx - 某某版」的垃圾（实测 inven.co.kr 那单）。
# 别和 gallery.FORUM_HOSTS 合并：那份是「答案常写在回复里、值得点开翻」的站
# （NGA／贴吧／reddit…），这份是「标题不能当人名」的站，两边有重叠但不是一回事。
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

# 已经停服关站的站点：落在这上面的候选不该当结论，用户点开只会看到空白页。
# bcy.net（半次元）2023-07-12 停止运营并关闭服务器，实测域名已无法连接。
DEAD_HOSTS = {
    "bcy.net": "半次元",
}


def dead_site(url: str = "", source: str = "") -> str:
    """这个落点是不是已经关站的站点；是的话返回站点名。"""
    host = (urlparse(url).netloc or source or "").strip().lower()
    host = host_of(host)
    if not host:
        return ""
    token = matched_token(host, DEAD_HOSTS)
    return DEAD_HOSTS.get(token, "") if token else ""


# 「求出处」类帖子：发帖人自己也在找这张图。页面标题就是一句问句，真正的答案
# （如果有）只在正文或回帖里。把它当出处发出去，用户点进去看到的是「有人知道吗」。
# 实测 2026-11-11 那单距离 0 命中司机社「有没有大佬知道这个远旅的动图出自哪里-
# 求出处」，被当成结论回了群，而正确答案是 pixiv 上一个画师的 GIF。
_REQUEST_POST_RE = re.compile(
    r"求出处|求原图|求这张图|求此图|求出处帖|"
    r"有没有(?:大佬|人|大神)?知道|有人知道这|这是哪[部个里]|出自哪|"
    r"who is (?:this|she)|where(?:'s| is) this from|what anime",
    re.I,
)


def is_request_post(title: str = "") -> bool:
    """这条候选是不是「求出处 / 求原图」的求助帖。

    只认标题。这类页面的标题本身就是问句，认标题比认站点可靠：同一个论坛既有人
    求图也有人发图，按域名一刀切会把正常帖一起毙掉。
    """
    text = (title or "").strip()
    return bool(text) and bool(_REQUEST_POST_RE.search(text))


def is_forum_page(url: str = "", source: str = "") -> bool:
    """这个链接是不是论坛 / 社区帖。是的话标题只能当标题，不能当名字。"""
    host = (urlparse(url).netloc or source or "").strip().lower()
    host = host.rsplit("@", 1)[-1].split(":")[0]
    if not host:
        return False
    return is_host(host, _FORUM_HOSTS)


# 站点的「列表页」：标签、分类、归档、分页、站内搜索。这类页面本身不是一篇帖子，
# 页面上的图是列表里某一条的缩略图，指纹对上只说明「这张图出现在这个列表里」。
# 实测 s-20261119-2329-c8e9：best 落在 `nudebird.biz/tag/bang-dream/`（距离 4），
# 点进去是一整页缩略图，找不到这张图对应的那篇帖子。
# `/page/N` 不算列表页信号：文章和图集自己也分页（`…/p/slug/page/2`、
# `…/en/article/27949/page/3/`），那只是同一篇的第二页。实测 c077 那单真正那篇
# 帖子因为带 `/page/2` 被降成「列表页」，接着挖层还从它挖到了站点合集页。
_LISTING_PATH_RE = re.compile(
    r"/(?:tag|tags|category|categories|label|labels|archive|archives|topic|topics"
    r"|collection|collections|series)(?:/|$)",
    re.I,
)
_LISTING_QUERY_RE = re.compile(r"(?:^|&)(?:s|tag|cat|category|label|q)=", re.I)
_LISTING_TITLE_RE = re.compile(
    r"\barchives?\b|^\s*tag\s*[:：]|\bpage\s*\d+\s*$|\bpage\s*\d+\s+of\s+\d+", re.I
)


def listing_page_kind(url: str = "", title: str = "") -> str:
    """这条候选是不是站点的列表页；是的话返回中文叫法，不是返回空串。

    只看 URL 路径段和标题，不按域名一刀切：同一个站点既有具体帖子页也有标签页
    （`xx.knit.bid/en/article/32501/` 是帖子，`…/tag/xxx/` 是列表）。
    """
    text = (url or "").strip()
    if text:
        parsed = urlparse(text)
        if _LISTING_PATH_RE.search(parsed.path or ""):
            return "站点的标签／分类列表页"
        if _LISTING_QUERY_RE.search(parsed.query or ""):
            return "站点的搜索／分类列表页"
    if title and _LISTING_TITLE_RE.search(str(title)):
        return "站点的列表页"
    return ""


_ANCHOR_HREF_RE = re.compile(r'<a\b[^>]*href="([^"]+)"', re.I)
_IMAGE_FILE_RE = re.compile(r"\.(?:jpe?g|png|webp|gif|avif|bmp)(?:$|\?)", re.I)


def listing_entry_link(html: str = "", image_url: str = "", page_url: str = "") -> str:
    """列表页里某张图所属那篇帖子的链接。

    列表页上每张缩略图都包在一个指向具体帖子的 `<a>` 里：拿图片地址（找不到就
    拿文件名主干）在 HTML 里定位，再往前找最近的那个链接，就是这一篇。

    实测 `nudebird.biz/tag/bang-dream/`：缩略图 `…/21-1792635363-592x888-c.webp`
    前一个链接是 `…/x-maru-on-the-road/`，那篇帖子的 og:image 正是原图（距离 4）。
    """
    text = str(html or "")
    if not text:
        return ""
    index = -1
    needle = str(image_url or "").strip()
    if needle:
        index = text.find(needle)
    if index < 0 and needle:
        # 懒加载会把地址改写（-592x888 这类尺寸后缀），退一步只认文件名主干。
        name = needle.rsplit("/", 1)[-1]
        stem = re.sub(r"[-_]\d+x\d+", "", name.rsplit(".", 1)[0])
        if len(stem) >= 8:
            index = text.find(stem)
    if index < 0:
        return ""
    window = text[max(0, index - 4000):index]
    links = _ANCHOR_HREF_RE.findall(window)
    for href in reversed(links):
        target = urljoin(page_url or "", str(href).strip())
        if not target.startswith(("http://", "https://")):
            continue
        if target == (page_url or "").strip():
            continue
        if _IMAGE_FILE_RE.search(urlparse(target).path or ""):
            continue
        if listing_page_kind(target, ""):
            continue
        return target
    return ""


def is_site_name(word: str = "", urls: list[str] | None = None) -> bool:
    """这个词是不是候选站点自己的名字。

    图集站会把站名压到图上（`PixiBB` → `cosplay.pixibb.com`），水印那一步把它当账号
    去核，X 上恰好有同名号就挂成了线索。实测 158d：`@pixiBB` 是个叫 brandy french
    的号，跟这张图毫无关系，顺着它还去查了一遍微博。
    """
    core = re.sub(r"[^a-z0-9]", "", str(word or "").lower())
    # 4 个字符以下太容易撞上站点名片段（`Pixi` 就是 `pixibb` 的前缀），不判。
    if len(core) < 5:
        return False
    for url in urls or []:
        host = urlparse(url).netloc.lower()
        if not host:
            continue
        flat = re.sub(r"[^a-z0-9]", "", host)
        if core in flat:
            return True
    return False


def page_main_image(html: str = "", page_url: str = "") -> str:
    """帖子页里那张“正文原图”：只认 og:image / twitter:image，别把站标当图。"""
    text = str(html or "")
    if not text:
        return ""
    for pattern in (
        r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"',
        r'<meta[^>]+content="([^"]+)"[^>]+property="og:image"',
        r'<meta[^>]+name="twitter:image"[^>]+content="([^"]+)"',
        r'<meta[^>]+content="([^"]+)"[^>]+name="twitter:image"',
    ):
        found = re.search(pattern, text, re.I)
        if found:
            return urljoin(page_url or "", found.group(1).strip())
    return ""




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
    人名，一路推出 @halloween 和 leakgallery.com/Halloween。中文名常直接把
    ``on`` 贴在后面（``关先生on X: "…"``），所以 ``on`` 前的空白不是必须的。
    """
    text = (title or "").strip()
    found = X_ON_TITLE_RE.match(text)
    if found:
        return found.group("name").strip()
    tail = X_ON_TAIL_RE.match(text)
    return tail.group("name").strip() if tail else text


def x_title_text(title: str = "") -> str:
    """X 帖子标题里 ``on X:`` 后面那段正文，去掉包裹的引号。

    名字和正文是两样东西：名字进「👤 名字」那一行，正文最多当帖子标题展示。
    拆不出来（不是 X 帖标题）时返回空串，调用方就别自己猜了。
    """
    text = (title or "").strip()
    found = X_ON_TITLE_RE.match(text)
    if not found:
        return ""
    return (found.group("text") or "").strip().strip(_TITLE_QUOTE_CHARS).strip()


_MODEL_PREFIX_RE = re.compile(r"^\s*(?:the\s+)?(?:model|cosplayer|coser|cosplay\s+model)\s*[:：]?\s+", re.I)
_EMPTY_PAREN_RE = re.compile(r"\s*[(（]\s*[)）]\s*$")
_ANY_EMPTY_PAREN_RE = re.compile(r"[(（]\s*[)）]")
_REPOST_SEP_RE = re.compile(r"\s+(?:[–—|]|-|:)\s+|\s*[：]\s*")
_IN_COSPLAY_RE = re.compile(r"\s+in\s+cosplay\s+", re.I)
_PHOTO_COUNT_RE = re.compile(r"(?:set\s+of\s+)?(\d+)\s*photos?\b", re.I)
# 后半截得像个套图说明（写着 cosplay / photos 之类），否则可能只是角色名
# （"Nahida – Genshin Impact" 前半截是角色不是模特）。
_SET_HINT_RE = re.compile(r"cosplay|photo|picture|album|image|set\b", re.I)
# 图集站标题开头残留的分类标签（开括号被站方吃掉那种）：「Cosplay] 小仓千代w …」。
_LEAD_TAG_RE = re.compile(
    r"^\s*(?:cosplay|cosplayer|photo|photos|model|coser|gallery|set|写真|套图)\s*[\]\)】〕]\s*",
    re.I,
)
_CJK_HINT = re.compile(r"[一-鿿]")


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
    # 开头的分类标签：「Cosplay] 小仓千代w 明日方舟W泳装| Xasiat」——图集站常把开
    # 括号吃掉，只剩一个「Cosplay]」。不剥掉的话下面切出来的名字带着这截标签。
    text = _LEAD_TAG_RE.sub("", text)
    # 「泳装| Xasiat」这种竖线两边不带空格的写法也要能切。
    text = re.sub(r"\s*\|\s*", " | ", text)
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
    # 中文标题常把作品名直接跟在名字后面（「小仓千代w 明日方舟W泳装」），尾巴里
    # 又没有 cosplay/photos 那几个词：名字就是开头那一截，后面是作品名。整条当名
    # 字摆出来，用户看到的是一条脏标题（5832 那单）。
    head = re.split(r"\s+", name, maxsplit=1)
    if len(head) == 2 and _CJK_HINT.search(head[0]) and 2 <= len(head[0]) <= 24:
        return head[0], head[1].strip()
    # 切不出套图说明时，只有标题自带「Model X」前缀才敢认这个名字；否则整条
    # 原样返回（"Nahida – Genshin Impact" 的 Nahida 是角色，不是模特）。
    return (name if had_prefix and name else raw), ""


def extract_identity(url: str, title: str = "") -> tuple[str, str]:
    """返回 (名字, 账号)。

    账号优先从链接里取：只在链接是账号页形态时才认，帖子页/搜索页不硬猜。
    """
    clean_title = SOTWE_TITLE_SUFFIX_RE.sub("", title or "").strip()
    # X 帖标题后面跟着正文，正文里 @ 到的是别人，不是这条的作者。只留 on X:
    # 前面那截：名字从这里取，账号也只从这截里找。
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

    name = HANDLE_RE.sub("", clean_title)
    # x.com 的页面标题是「名字 (@handle) / X」：账号抽走后会剩下「名字 () / X」，
    # 空括号和站点后缀都不该算名字的一部分（s-20261113-2147-2f31）。
    name = _ANY_EMPTY_PAREN_RE.sub(" ", name)
    name = _X_PAGE_SUFFIX_RE.sub("", name)
    name = re.sub(r"\s{2,}", " ", name).strip(" -–—|·/")
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
    if is_host(host, CRAWLER_PLACEHOLDER_HOSTS):
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
