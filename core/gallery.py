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
from urllib.parse import urljoin, urlparse

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
    # 纯图集站：页面 JSON-LD 的 author 写的就是站名（`Hentai.yoga`），标题里的
    # 「Mai Khán Hân」是站方自己起的分类名——日文版页面把它写成「マイ・カン・ハン」，
    # 而且同一套图里混着不同造型的人。不认成图集站的话，d=0 命中会被当「找到了」
    # 发出去，用户点进去只看到一堆盗图。c51b 那单就是这么来的。
    "hentai.yoga",
    # 同一类：把 coser 的图成套搬过来，按 `/model/<名字>` 分类，页面里一个社媒
    # 账号都不挂。实测 c077：hotgirlpix 的帖子页、模型页、合集页跑一遍抽账号全是空，
    # misskon 那篇投稿页也一样。这种命中只能算「别的网站转过这张图」。
    "hotgirlpix.com",
    "misskon.com",
    # 英文图集站：把 coser 的图成套搬过来，标题只写作品和本站分类，页面里没有社媒
    # 账号。实测 5832 那单距离 0、落点是 xasiat.com，判成「找到了」发出去，用户点
    # 进去看到的是一堆转载；因为没降级，标题里明明写着「小仓千代w」也没去查微博。
    "xasiat.com",
    # 中文资源打包站（COS团）：正文写着「34P-182MB、度盘下载、解压密码」，署名是
    # 站方编辑而不是模特。649c 那单距离 0 命中它，被当成「找到了」，因为没降级，
    # 「标题里写着纸悦Etsu_ko 却不去微博查」这条腿整条没跑。
    "costuan.com",
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
    # 英文图集站：帖子页署名一律是 `By admin`，标题是搬运工自己起的（「[X] Maru on
    # the road」），页面里没有作者。实测 c8e9 那单顺着标签页挖到具体帖子后仍指不到人，
    # 不认成搬运站的话会把这篇当成「找到了」发出去。
    "nudebird.biz",
    # 中文 / 英文两个大图集站，标题末尾都缀着站名（「… - 染三ran3 - 爱妹子」），
    # 署名与「首发」都不是作者本人。158d 那单的 d=0 落点就是它们，当「找到了」
    # 发出去，用户点进去看到的是转载页。
    "xx.knit.bid",
    "pixibb.com",
    # 崩铁 / 绝区零的同人图站：原图成套扒自 pixiv，图床文件名还留着 pixiv 作品号
    # 与作者名（`沒有名字-by-lol-with-Arknights-Endfield-…-152342429.webp`），
    # 页面本身只是 `/gallery/<id>/<中文标题>` 的图集，没有作者主页。cdfa 那单
    # 距离 0 命中它，被当成「找到了」，用户点进去看到的是一堆转载。它不像
    # 付费图包站那样只卖订阅，所以进搬运站这档，别写「付费图包页」。
    "honkaihaven.com",
)

# 内容号落地页：百度动态、百家号的分享页。图可能是号主自己发的，也可能是从别处
# 搬来的，页面里没有任何能证明身份的东西——b3b4 那单落在 mbd.baidu.com，整页只有
# 一句正文加标题里的「@诗诗爱生活的动态」，没有 author 字段、没有外站链接。这类
# 命中一律只当线索：要认成「找到本人」，得靠按名字反查到带图帖对上，不能靠同名。
CONTENT_HOSTS = (
    "mbd.baidu.com",
    "baijiahao.baidu.com",
    "baijiahao.com",
    # 中文资讯／内容站：文章标题写的是别人（「穿衣v.s脫衣哪個好？貧乳細腰《Mio Kuuko》
    # 白皙纖瘦體態別有一番…」），正文是编辑攒的，页面里没有作者字段。411f 那单指纹
    # 距离 0 落在 clickme.net，原样当「找到了」发出去等于替一篇内容号文章认证。
    "clickme.net",
)

# 电商／商品页：这些页面卖的是同款，不是作者发的图。838e 那单的原图是一只
# 可莉背包，引擎回来的全是同款商品页（dokidokicos／gcosplay／miccostumes／
# genshinfans／ezcosplay，距离 24~28），参考区摆出来像淘宝搜索结果。
SHOP_HOSTS = (
    "dokidokicos.com",
    "gcosplay.com",
    "miccostumes.com",
    "genshinfans.com",
    "ezcosplay.com",
    "aliexpress.com",
    "aliexpress.us",
    "taobao.com",
    "tmall.com",
    "jd.com",
    "1688.com",
    "pinduoduo.com",
    "shopee.com",
    "amazon.com",
)
# 电商模板的商品页路径（`/products/...`、`/goods/...`）；只在域名本身也像商店时
# 才认，免得把图集站的 `/product/` 之类误挡。
_SHOP_PATH = re.compile(r"/(?:products?|goods|item)/", re.I)
_SHOP_HOSTWORD = re.compile(r"(?:shop|store|mall|buy|wholesale)", re.I)

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
    # 图床路径 `/files/`、`/media/` 这些段位也不是账号。
    "files",
    "file",
    "media",
    "photo",
    "photos",
    "status",
    "statuses",
    "notifications",
    "logout",
    "following",
    "followers",
}

# 左边必须带边界：`f.hotgirlpix.com/files/2025/03/03/21/x.jpg` 里藏着
# `x.com/files` 这一截，不设边界就会把图床路径的 `files` 当成 X 号（c077 那单
# 的 `x.com/files`、count 10 就是这么来的）。
_IG = re.compile(r"(?<![A-Za-z0-9_-])instagram\.com/([A-Za-z0-9_.]{2,30})", re.I)
_X = re.compile(r"(?<![A-Za-z0-9_-])(?:x|twitter)\.com/([A-Za-z0-9_]{2,15})", re.I)

# 同一个账号在页面上通常被引用很多次（每张缩略图都链向她的图集）；页脚、广告、
# 友链只会出现一两次。低于这个数说明不了「这页讲的就是她」。
MIN_HITS = 3


# 论坛 / 讨论帖：页面本身就是一问一答，答案常在回复里（有人贴 pixiv、X 链接）。
# 这类站点抓不到内容时，能给的最有用的一句是「去翻回复」——b62e／ecaa 两单就是
# 用户自己在 NGA 的回帖里找到 pixiv 原图的，而 NGA 对未登录访客直接返回
# 「访客不能直接访问」（read.php 403，app_api 要 access_token），插件读不了。
FORUM_HOSTS = (
    "bbs.nga.cn",
    "ngabbs.com",
    "tieba.baidu.com",
    "5ch.net",
    "reddit.com",
    "arca.live",
    "dcinside.com",
)


def is_forum_site(url: str = "", platform: str = "") -> bool:
    """这个落点是不是论坛讨论帖（答案常在回复里，页面抓不到就得点开看）。"""
    hosts: list[str] = []
    host = (url or "").split("://", 1)[-1].split("/", 1)[0].rsplit("@", 1)[-1].split(":")[0]
    if host:
        hosts.append(host.strip().lower())
    if platform:
        hosts.append(str(platform).strip().lower())
    for item in hosts:
        item = item.removeprefix("www.")
        if item and any(item == token or item.endswith("." + token) for token in FORUM_HOSTS):
            return True
    return False


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


def is_content_site(url: str = "", platform: str = "") -> bool:
    """这个落点是不是百度动态 / 百家号这类内容号落地页。"""
    hosts: list[str] = []
    host = (url or "").split("://", 1)[-1].split("/", 1)[0].rsplit("@", 1)[-1].split(":")[0]
    if host:
        hosts.append(host.strip().lower())
    if platform:
        hosts.append(str(platform).strip().lower())
    for item in hosts:
        item = item.removeprefix("www.")
        if item and any(item == token or item.endswith("." + token) for token in CONTENT_HOSTS):
            return True
    return False


def is_shop_site(url: str = "", platform: str = "") -> bool:
    """这条候选是不是电商／商品页（同款商品，不是作者发的图）。"""
    raw = str(url or "").strip()
    host = raw.split("://", 1)[-1].split("/", 1)[0].rsplit("@", 1)[-1].split(":")[0]
    hosts = [item for item in (host.strip().lower().removeprefix("www."), str(platform or "").strip().lower()) if item]
    for item in hosts:
        if any(item == token or item.endswith("." + token) for token in SHOP_HOSTS):
            return True
    # 模板路径只在域名也像商店时才认：`/products/` 是电商模板，但单看路径会误伤。
    if raw and _SHOP_PATH.search(raw) and any(_SHOP_HOSTWORD.search(item) for item in hosts):
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


# 只在「抓页面找发布者」时认的站，跟 GALLERY_HOSTS 分开：进 GALLERY_HOSTS 会把
# 结论降级成「只找到转发」，可这类页面上的图就是原图本身（指纹对得上），用户
# 要的是「这张图是谁发的」。实测 s-20261119-1605-e790：`picazor.com` 的帖子页
# 没有任何外链账号，但页面里有发布者主页 `/en/<名字>`，主页上直接挂着作者的 X。
AUTHOR_PAGE_HOSTS = (
    "picazor.com",
    "ibradome.com",
)


def is_author_page_site(url: str = "", platform: str = "") -> bool:
    """这个落点值不值得再往「页面 → 发布者主页」翻一层。"""
    for value in (_host_part(url), str(platform or "")):
        item = value.strip().lower().removeprefix("www.")
        if item and any(item == token or item.endswith("." + token) for token in AUTHOR_PAGE_HOSTS):
            return True
    return False


def _host_part(url: str) -> str:
    """从 URL 取域名，没 scheme 的字符串当域名处理。"""
    value = str(url or "").strip()
    if "://" in value:
        host = value.split("://", 1)[-1].split("/", 1)[0]
    else:
        host = value.split("/", 1)[0]
    return host.rsplit("@", 1)[-1].split(":")[0]


def author_page_url(html: str = "", page_url: str = "") -> str:
    """从帖子页里找出发布者主页：同站、且正好是当前路径去掉最后一段。

    不猜站点结构，只认页面里真实存在的链接——``picazor.com/en/maonu164196-1/48``
    的页面里写着 ``/en/maonu164196-1``，那就是发布者主页。找不到就返回空串。
    """
    text = str(html or "")
    page = str(page_url or "").strip()
    if not text or "://" not in page:
        return ""
    parsed = urlparse(page)
    path = parsed.path.rstrip("/")
    if "/" not in path.lstrip("/"):
        return ""
    parent = path.rsplit("/", 1)[0]
    if not parent:
        return ""
    for href in re.findall(r'href="([^"]+)"', text):
        target = urljoin(page, href)
        got = urlparse(target)
        if got.netloc.lower() != parsed.netloc.lower():
            continue
        if got.path.rstrip("/") == parent:
            return f"{got.scheme}://{got.netloc}{got.path.rstrip('/')}"
    return ""


def author_from_author_page(html: str = "", page_url: str = "") -> list[dict[str, object]]:
    """发布者主页上挂的社交账号：出现一次就认。

    跟帖子页的判据不同——帖子页满屏是可点的缩略图和推荐位，同一个账号出现
    好几次才说明「这页讲的就是她」；发布者主页只有一个作者信息区块，X 链接
    自然只出现一次（实测 picazor 的 ``/en/maonu164196-1`` 页里就一条
    ``x.com/maonu164196``）。多个账号时取出现次数最多的那个。

    返回仍只当线索：``from_author_page=True`` 表示这是站方给的「主页属于谁」，
    但站方也会标错，文案里必须让用户点开核对。
    """
    text = str(html or "")
    if not text:
        return []
    found: list[dict[str, object]] = []
    for platform, pattern, reserved, base in (
        ("X", _X, _X_RESERVED, "https://x.com/"),
        ("Instagram", _IG, _IG_RESERVED, "https://instagram.com/"),
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
        found.append(
            {
                "platform": platform,
                "handle": handle,
                "url": base + handle,
                "count": count,
                "from_author_page": True,
            }
        )
    return found

