"""从候选标题里认出疑似 X 账号的名字。

搜图引擎经常把作者别名拼进标题，比如「Kuuko / kuukow002 / kuuko_w」。
这不是引擎给的账号字段，但往往是找到作者本人 X 号的唯一线索。这里只做
「抽名字 + 和标题核对」两件纯函数的事，账号是否存在、粉丝多少、要不要
采用，都由插件联网验证后决定。
"""

from __future__ import annotations

import re

_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9_]{3,14}")

# 平台名、通用词、角色/作品名、资源站常用词都不能当账号。漏掉的靠后面的
# 「账号真的存在 + 粉丝量够 + 名字在标题里出现过」三道门槛兜。
_STOP = {
    "cosplay", "cosplayer", "cosplayers", "cosplays", "genshin", "impact",
    "nahida", "rukka", "rukkhadevata", "honkai", "starrail", "zenless",
    "patreon", "instagram", "twitter", "facebook", "tiktok", "onlyfans",
    "fansly", "youtube", "deviantart", "pixiv", "kemono", "reddit", "telegram",
    "galleryepic", "xasiat", "ehimejk", "masterfap", "v2ph", "wordpress",
    "fapello", "skeb", "kemono", "coomer", "thotsbay",
    "photo", "photos", "picture", "pictures", "pic", "pics", "album", "albums",
    "gallery", "galleries", "preview", "previews", "series", "character",
    "characters", "anime", "manga", "comic", "comics", "game", "games",
    "video", "videos", "download", "downloads", "collection", "collections",
    "store", "shop", "product", "products", "profile", "user", "users", "posts",
    "post", "image", "images", "leak", "leaks", "leaked", "nude", "nudes",
    "naked", "sexy", "hot", "update", "updates", "version", "part", "vol",
    "http", "https", "www", "html", "index", "com", "net", "org", "full",
    "set", "sets", "new", "best", "top", "free", "official", "channel",
    "group", "page", "site", "blog", "view", "show", "detail", "info",
    "from", "with", "and", "the", "for", "you", "her", "his", "this", "that",
    # 资源站分类词。实测 jxporn.net 的标题写着「精选Porn」，抽出来的 @porn
    # 是个 9.8 万粉的成人号，跟被搜的 coser 毫无关系。
    "porn", "hentai", "jav", "xxx", "adult", "nsfw", "erotic", "lewd",
    # 标题里的普通英文词。实测「Nothing To Hide (Apron)」抽出 @nothing（106 万
    # 粉）、「Design」抽出 @Design，账号确实存在，但跟这张图半点关系没有。
    "nothing", "something", "anything", "everything", "someone", "anyone",
    "design", "designs", "hide", "hidden", "apron", "princess", "femboy",
    "join", "joined", "joinme", "come", "shower", "swim", "suit", "bikini",
}


def extract(texts: list[str], limit: int = 6) -> list[str]:
    """按出现顺序抽像账号的单词，去重并过滤通用词。"""
    seen: set[str] = set()
    found: list[str] = []
    for text in texts:
        for match in _TOKEN.finditer(text or ""):
            token = match.group(0)
            low = token.lower()
            if low in _STOP or low.isdigit():
                continue
            # 连续重复字符太多的串（xxxx、aaaa）不可能是正常用户名。
            if len(set(low)) <= 2:
                continue
            if low in seen:
                continue
            seen.add(low)
            found.append(token)
            if len(found) >= max(1, limit):
                return found
    return found


def stem(handle: str) -> str:
    """去掉结尾的数字和下划线：kuukow002 → kuukow，kuuko_w → kuuko。"""
    return re.sub(r"[_\d]+$", "", (handle or "").lower())


# 模型读水印时最常见的错法就是把 w 读散：w 本来就是两个 u，
# 小字／手写体上很容易变成 uw、vv、uu，末尾再拖一个 y。
_LIGATURES = (
    ("uwy", "ww"),
    ("uw", "w"),
    ("vv", "w"),
    ("uu", "w"),
    ("vw", "w"),
    ("wy", "ww"),
)

# 字形相近的替换，只收真会看错的那几组，别把候选量放飞到几十个请求。
_CONFUSIONS = {
    "y": "w",
    "w": "y",
    "v": "u",
    "u": "v",
    "n": "m",
    "m": "n",
    "i": "l",
    "l": "i",
    "o": "0",
    "0": "o",
    "s": "5",
    "5": "s",
}

_HANDLE_OK = re.compile(r"[a-z0-9_]{3,15}")


def typo_variants(handle: str, limit: int = 18) -> list[str]:
    """水印号读错时值得再试的写法，按可信度排序。

    实测 ``Moliww`` 被读成 ``Moliuwy``（w 读散成 uw、末尾再读成 y），拿这个错号
    去查自然是查无此人，答案就漏了。这里只生成「像是同一个号」的写法，能不能
    用不由这里定：调用方要拿每个候选去核它自己的带图帖，翻到这张图才能当结论。
    """
    raw = (handle or "").strip().lstrip("@").lower()
    if not _HANDLE_OK.fullmatch(raw):
        return []
    out: list[str] = []

    def add(value: str) -> None:
        if value and value != raw and _HANDLE_OK.fullmatch(value) and value not in out:
            out.append(value)

    for old, new in _LIGATURES:
        if old in raw:
            add(raw.replace(old, new))
    for index in range(len(raw)):
        add(raw[:index] + raw[index + 1 :])
    for index in range(len(raw) - 1):
        add(raw[:index] + raw[index + 1] + raw[index] + raw[index + 2 :])
    for index, char in enumerate(raw):
        replacement = _CONFUSIONS.get(char)
        if replacement:
            add(raw[:index] + replacement + raw[index + 1 :])
    return out[: max(1, limit)]


def in_url_slug(name: str, urls: list[str]) -> bool:
    """这个名字是不是出现在候选 URL 里（搬运站的路径通常就是它认定的主人名）。

    标题常把好几个别名并列（「cw / heyimbee / heyymimimi」），只按粉丝挑会选中最
    出名的那个，而它可能只是被顺手抄进来的大号。站点自己的路径更接近「这套图是谁」。
    """
    core = stem(name)
    if len(core) < 4:
        return False
    return any(core in (url or "").lower() for url in urls)


def pick_by_slug(options: list[dict], urls: list[str]) -> dict | None:
    """挑一个候选账号：名字在 URL 路径里出现过的优先，其余按粉丝数最大。

    options 里每项要有 `guess`（标题里抽出来的名字）和 `followers`。
    """
    if not options:
        return None
    slugged = [item for item in options if in_url_slug(str(item.get("guess") or ""), urls)]
    return max(slugged or options, key=lambda item: int(item.get("followers") or 0))


def match_score(handle: str, texts: list[str]) -> int:
    """这个名字是否真的出现在候选标题里。返回主干长度，0 表示没有。

    只认「原样出现」：`kuukow002` 的主干 `kuukow` 出现在 KuukoW 里算命中，
    随手拼出来的词不会命中。

    主干至少 6 个字符：4~5 字的英文几乎都是常用词（porn、ghost、bride、
    vermeil 的前缀），实测「Ghost Sweeper Mikami」这种作品名会被当成嫌疑人，
    抽中一个 5 万粉的无关账号。真账号名一般更长。
    """
    core = stem(handle)
    if len(core) < 6:
        return 0
    joined = " ".join(texts).lower()
    return len(core) if core in joined else 0


# 约稿平台和电商页的标题里出现用户名，说明的是「页面主人」而不是被搜的这张图
# 的作者。实测 skeb.jp/@murabitoufu/works/69 这条候选把 @murabitoufu（22 万粉）
# 顶成了「疑似同一个人」，而那页只是别人接稿/委托的页面，跟图毫无关系。
BLOCKED_SOURCES = (
    "skeb.jp",
    "joom.com",
    "aliexpress",
    "market.yandex",
    "amazon.",
    "ebay.",
)


def is_blocked_source(source: str) -> bool:
    """这类站点的标题不能拿来推作者。"""
    host = (source or "").strip().lower()
    return any(token in host for token in BLOCKED_SOURCES)


def shares_anchor(handle: str, texts: list[str]) -> bool:
    """这个名字跟「已经靠图像对上的那几条」是不是同一个名字。

    只看「标题里出现过这个名字」远远不够：标题里出现的是页面主人、是接稿的
    画师、是商品名，都可能。所以再卡一道——候选名必须和已命中候选里的名字
    有公共主干（`nene` ⊂ `nenechi890`、`kuukow` == `kuukow002` 的主干），
    否则宁可这条线索不给。
    """
    core = stem(handle)
    if len(core) < 4:
        return False
    for token in _anchor_tokens(texts):
        if core in token or token in core:
            return True
    return False


def _anchor_tokens(texts: list[str]) -> set[str]:
    """把一批标题拆成去重后的小写主干，供相似判断用。"""
    pool: set[str] = set()
    for text in texts:
        for match in _TOKEN.finditer(text or ""):
            low = match.group(0).lower()
            if len(low) < 4:
                continue
            pool.add(stem(low) or low)
    return pool


def supported_by_context(handle: str, texts: list[str], anchors: list[str]) -> bool:
    """这个名字所在的那条标题里，是否也出现了跟已命中名字对得上的词。

    放过的是「同一条标题里并列多个别名」的情况：标题写着
    「NENEchi890, NERUchi890 #1304」时，NERUchi890 自己跟 NENE 不像，但它和
    NENEchi890 是同一条标题里并列的别名，可以采信。反过来，标题里孤零零只有
    `@murabitoufu` 那种（页面主人），两边都对不上，就该丢。
    """
    core = stem(handle)
    if len(core) < 4:
        return False
    wanted = _anchor_tokens(anchors)
    if not wanted:
        return False
    for text in texts:
        low = (text or "").lower()
        if core not in low:
            continue
        for token in _anchor_tokens([low]):
            for anchor in wanted:
                if token in anchor or anchor in token:
                    return True
    return False
