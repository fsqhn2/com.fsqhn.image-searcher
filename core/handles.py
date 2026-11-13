"""从候选标题里认出疑似 X 账号的名字。

搜图引擎经常把作者别名拼进标题，比如「Kuuko / kuukow002 / kuuko_w」。
这不是引擎给的账号字段，但往往是找到作者本人 X 号的唯一线索。这里只做
「抽名字 + 和标题核对」两件纯函数的事，账号是否存在、粉丝多少、要不要
采用，都由插件联网验证后决定。
"""

from __future__ import annotations

import re
from urllib.parse import unquote

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


# 标题里的「署名」比随手抽单词可靠得多：搬运帖常把原作者名连动词一起抄下来
# （「Vivian cosplay by KURU」），那是整条标题里唯一指向本人的部分。这里只负责
# 认出这类明确写法，能不能用仍由插件联网核图决定。
_SIG_AT = re.compile(r"(?<![A-Za-z0-9_])@([A-Za-z0-9_]{2,20})")
_SIG_BY = re.compile(
    r"\b(?:cosplay(?:ed)?|cos|costume|photo|photography|shot|pic(?:ture)?|art|edit)"
    r"\s+by\s+([A-Za-z0-9_.\-]{3,24})",
    re.I,
)
_SIG_GENERIC_BY = re.compile(r"\bby\s+([A-Za-z0-9_.\-]{3,24})", re.I)
_SIG_LABEL = re.compile(
    r"\b(?:coser|cosplayer|cn|model|credit|credit\s+to|photo|photography|photographer)"
    r"\s*[:：]\s*@?([A-Za-z0-9_.\-]{2,24})",
    re.I,
)
_SIG_JP = re.compile(
    r"(?:コスプレイヤー|レイヤー|撮影|モデル|被写体)\s*[:：]?\s*@?([A-Za-z0-9_.\-]{2,24})"
)
# 泄漏站／搬运站的合集页常把「这套图是谁的」直接写在标题里：
# 「推特lazycataw（仅有足）」「Твиттер: lazycataw (только в Instagram)」。
# 这跟「by XXX」一样是明确的署名，只是换了中日俄三种写法。
# 实测 s-20261113-0106-ed9a：lens 四条候选里有三条标题写着推特lazycataw，
# 插件一个名字都没抽出来，于是一路落到「没找到」。
_SIG_TWITTER = re.compile(
    r"(?:推特|twitter|твиттер)(?!\s*[./])\s*[:：]?\s*@?([A-Za-z0-9_][A-Za-z0-9_.\-]{2,23})",
    re.I,
)

# 这些词长得像名字，其实是平台名、泛称或者句子里顺手带上的一般词。
_SIG_STOP = _STOP | {
    "unknown", "anonymous", "unnamed", "nobody", "someone", "somebody",
    "staff", "admin", "mod", "moderator", "editor", "owner", "original",
    "artist", "artists", "photographer", "photographers", "credit", "credits",
    "self", "myself", "yourself", "himself", "herself", "their", "them",
    "that", "this", "these", "those", "there", "where", "when", "what",
    "very", "just", "also", "then", "than", "only", "over", "under", "into",
    "reddit", "twitter", "pixiv", "weibo", "bilibili", "tiktok", "insta",
    "mods", "bots", "bot", "ai", "op", "nsfw", "sfw", "uncensored",
}


def _clean_signature(value: str) -> str:
    return (value or "").strip().strip(".,;:!?)]}\"'“”‘’").strip("-_").strip()


def signature_names(texts: list[str], limit: int = 3) -> list[str]:
    """从标题里认出「这是谁作的」那种明确署名。

    按可信度排序：``@handle`` 最硬，其次是 ``coser:``、``credit to`` 这类带标签
    的写法，最后才是 ``by XXX``。抽出来的只是名字，能不能当账号用要联网去核——
    核不到就退回，绝不因为名字像就改结论。

    实测 s-20261112-0752-4fdf：reddit 那页的作者是搬运号，标题写着
    「Vivian cosplay by KURU」，插件当时只从候选 URL 里抽 handle（抽到两个都不
    是），标题里的 KURU 完全没用上，于是停在搬运页。
    """
    buckets: list[list[str]] = [[], [], [], []]
    for text in texts:
        raw = text or ""
        if not raw:
            continue
        for match in _SIG_AT.finditer(raw):
            buckets[0].append(match.group(1))
        for pattern in (_SIG_LABEL, _SIG_JP, _SIG_TWITTER):
            for match in pattern.finditer(raw):
                buckets[1].append(match.group(1))
        for match in _SIG_BY.finditer(raw):
            buckets[2].append(match.group(1))
        for match in _SIG_GENERIC_BY.finditer(raw):
            buckets[3].append(match.group(1))

    seen: set[str] = set()
    found: list[str] = []
    for bucket in buckets:
        for item in bucket:
            name = _clean_signature(item)
            low = name.lower()
            if len(name) < 2 or low in _SIG_STOP or low.isdigit():
                continue
            if len(set(low)) <= 2:
                continue
            if low in seen:
                continue
            seen.add(low)
            found.append(name)
            if len(found) >= max(1, limit):
                return found
    return found


# 微博的「账号」就是昵称本身，没有单独的英文 handle，所以这里两件事都要认：
# 明写的微博链接（weibo.com/u/7012365432）和「微博@某某」这种读数。
# 实测 s-20261113-1108-73e0：Grok 把水印读成「微博@萌生w」，插件的抽取函数只认
# ASCII 账号，这个中文昵称一个字符都抽不出来，线索整条丢掉。
_WEIBO_URL = re.compile(
    r"weibo\.(?:com|cn)/(?:u/|n/|profile/)?([A-Za-z0-9_\u4e00-\u9fff.\-%]{2,40})",
    re.I,
)
# 分隔符必须显式出现（: ： @ 或空格），否则「weibo.com/x」会被当成昵称 com。
_WEIBO_AT = re.compile(
    r"(?:新浪微博|微博|weibo)\s*(?:[:：]|@|\s)\s*@?\s*"
    r"([A-Za-z0-9_\u4e00-\u9fff][A-Za-z0-9_\u4e00-\u9fff.\-]{1,29})",
    re.I,
)
# 中文昵称后面常紧跟句子成分（「微博@萌生w的水印」），在第一个虚词/名词处截断。
_WEIBO_CUT = re.compile(r"(?:的|是|在|上|里|发|写|水印|主页)")


# 水印上光秃秃的 ``@某某``：X 的 handle 只能是 ASCII，所以含中文的那部分不可能
# 是 X 号，只可能是微博这类中文平台的昵称。实测 73e0 的水印写着「轻颜 / @青生w」，
# 老抽取函数一个字符都不认，这条线索直接消失。
_AT_NAME = re.compile(
    r"@\s*([A-Za-z0-9_\u4e00-\u9fff][A-Za-z0-9_\u4e00-\u9fff.\-]{1,29})"
)
_CJK = re.compile(r"[\u4e00-\u9fff]")


def has_cjk(text: str) -> bool:
    """里面有没有汉字。"""
    return bool(_CJK.search(text or ""))


def at_names(texts: list[str], limit: int = 4) -> list[str]:
    """``@某某`` 后面的名字，中文也认。

    含不含中文由调用方判断：含中文的不可能是 X 号，要去微博这类站查。
    """
    found: list[str] = []
    seen: set[str] = set()
    for text in texts:
        for match in _AT_NAME.finditer(text or ""):
            value = _WEIBO_CUT.split(match.group(1))[0].strip(".-")
            low = value.lower()
            if len(value) < 2 or low in _SIG_STOP or low.isdigit():
                continue
            if len(set(low)) <= 2 or low in seen:
                continue
            seen.add(low)
            found.append(value)
            if len(found) >= max(1, limit):
                return found
    return found


def weibo_targets(texts: list[str], limit: int = 3) -> list[tuple[str, str]]:
    """从文本里认出「指向微博」的线索：``("uid", "7012365432")`` 或 ``("name", "青生w")``。

    只认明确写着「微博 / weibo」的地方，或者直接给了 weibo.com 的链接。水印上光秃秃
    一个 ``@青生w``（没有平台名）不在这里处理——那种要等 X 上查无此号才会拐去微博试，
    由插件那一侧决定，免得每个读到的号都去微博搜一遍。
    """
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for text in texts:
        raw = text or ""
        if not raw:
            continue
        for match in _WEIBO_URL.finditer(raw):
            value = unquote(match.group(1)).strip(".")
            if value.isdigit():
                key = ("uid", value)
            else:
                key = ("name", value)
            if key[1].lower() in {"u", "n", "profile", "com", "cn", "www"}:
                continue
            if key in seen:
                continue
            seen.add(key)
            found.append(key)
        for match in _WEIBO_AT.finditer(raw):
            value = _WEIBO_CUT.split(match.group(1))[0].strip(".-")
            low = value.lower()
            if len(value) < 2 or low in _SIG_STOP or low.isdigit():
                continue
            if len(set(low)) <= 2:
                continue
            key = ("name", value)
            if key in seen:
                continue
            seen.add(key)
            found.append(key)
        if len(found) >= max(1, limit):
            break
    return found[: max(1, limit)]


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


def signature_fix(handle: str, names: list[str], limit: int = 4) -> list[str]:
    """署名对得上、handle 只差一个字母时，把 handle 里那一段按署名改写。

    实测 s-20261112-1511-7496：水印读成 ``kufu_xox``（r 被读成 f），X 上查无此号；
    而同一条链上标题已经写着「Vivian cosplay by KURU」。拿 KURU 去套 ``kufu_xox``，
    只有 f→r 这一处不同，改写回来就是 ``kuru_xox``——真号，距离 3 的那张图就是她的。

    只认「同样长度、只差一个字母」这一种，改完还要由调用方去核它自己的带图帖，
    核不到不会当成结论。
    """
    raw = (handle or "").strip().lstrip("@").lower()
    if not _HANDLE_OK.fullmatch(raw):
        return []
    out: list[str] = []
    for name in names:
        core = re.sub(r"[^0-9a-z]+", "", str(name or "").lower())
        if len(core) < 3 or len(core) > len(raw):
            continue
        for start in range(len(raw) - len(core) + 1):
            window = raw[start : start + len(core)]
            if window == core:
                continue
            # 等长、只差一个字母：Hamming 距离为 1。
            if sum(1 for left, right in zip(window, core) if left != right) != 1:
                continue
            fixed = raw[:start] + core + raw[start + len(core) :]
            if _HANDLE_OK.fullmatch(fixed) and fixed != raw and fixed not in out:
                out.append(fixed)
    return out[: max(1, limit)]


def name_matches_handle(name: str, handle: str) -> bool:
    """标题里的署名，跟图上读出来那个号是不是同一个名字。

    7496/1710 那种情形：图上水印 `@kuru_xox`，标题写着「Seia cosplay by KURU」——
    署名正好是这个 handle 的开头一段，两个独立来源指向同一个人。这条只用来做
    「交叉验证」，不是拿名字去猜号，所以门槛写得死：名字至少四个字母，而且必须是
    handle 的整段开头（后面跟 `_`、`.`、数字或到此为止）。
    """
    raw = (handle or "").strip().lstrip("@").lower()
    core = re.sub(r"[^0-9a-z]+", "", str(name or "").lower())
    if len(core) < 4 or not raw:
        return False
    if re.match(re.escape(core) + r"(?:[_.]|$)", raw):
        return True
    # 水印读错一个字母的情形（kufu_xox / KURU）也算对上：改回来是同一个号。
    return bool(signature_fix(raw, [name]))


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
