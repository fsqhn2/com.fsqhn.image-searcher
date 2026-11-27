"""从候选标题里认出疑似 X 账号的名字。

搜图引擎经常把作者别名拼进标题，比如「Kuuko / kuukow002 / kuuko_w」。
这不是引擎给的账号字段，但往往是找到作者本人 X 号的唯一线索。这里只做
「抽名字 + 和标题核对」两件纯函数的事，账号是否存在、粉丝多少、要不要
采用，都由插件联网验证后决定。
"""

from __future__ import annotations

import json
import re
import unicodedata
from urllib.parse import unquote

_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9_]{3,14}")

# 转载站标题常写成「作者 - 作品名」，作者名就在第一个分隔符前面。
# 方括号也算分隔：漏了开括号的标题（抓下来只剩「Cosplay] ran3」）要能从这里切开。
_TITLE_SPLIT = re.compile(r"\s*[-–—|｜/／\]】]\s*")
# 标题开头的分类标签：「[Cosplay] 染三ran3 - …」里的方括号是分类，不是作者。
# 资源站常把开括号吃掉，只剩「Cosplay] 」，这种残留单独处理（见 _strip_title_tag）。
_TITLE_TAG = re.compile(r"^\s*[\[\(【〔（][^\[\]\(\)【】〔〕（）]{0,24}[\]\)】〕）]\s*")
_TITLE_TAG_TAIL = re.compile(r"^\s*([^\[\]\(\)【】〔〕（）\s]{1,24})[\]\)】〕）]\s*")
_CJK = re.compile(r"[一-鿿]")

# 平台名、通用词、角色/作品名、资源站常用词都不能当账号。漏掉的靠后面的
# 「账号真的存在 + 粉丝量够 + 名字在标题里出现过」三道门槛兜。
_STOP = {
    "cosplay", "cosplayer", "cosplayers", "cosplays", "genshin", "impact",
    "nahida", "rukka", "rukkhadevata", "honkai", "starrail", "zenless",
    "patreon", "instagram", "twitter", "facebook", "tiktok", "onlyfans",
    "fansly", "youtube", "deviantart", "pixiv", "kemono", "reddit", "telegram",
    # 站名：telegra.ph 的标题里写着「Telegraph」，X 上真有一个 @Telegraph，
    # 于是被当成作者号收进来（2c76 那单）。xasiat 同上。
    "telegraph", "telegra",
    # 付费平台的品牌名。泄漏站的标题常把平台列成一串（「…läckta från Onlyfans
    # Patreon Fansly FriendsOnly Fanvue iFans…」），2c76 那单就从里面挑出了
    # @fanvue（7 万粉）。这不是人名，也不可能是作者的号。
    "fanvue", "fansone", "fancentro", "friendsonly", "ifans", "manyvids",
    "loyalfans", "justforfans", "fanbox", "fantia", "subscribestar",
    "linktree", "carrd", "beacons",
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
    # 标题里的颜色和修饰词。实测 s-20261119-1722-fe9b 的标题「Cosplay] ran3 -
    # Cure Nyammy black stockings purple staff」把 purple 当成了 X 号（@Purple，
    # 4 万粉），跟这张图毫无关系。
    "purple", "black", "white", "red", "blue", "green", "pink", "yellow",
    "brown", "grey", "gray", "silver", "gold", "blonde", "dark", "light",
    "long", "short", "small", "large", "cute", "beautiful", "pretty",
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
# 「drawn by」是画站的画师署名（danbooru 的标题一律写成
# 「nozomi and hikari (blue archive) drawn by nemo_(leafnight ..」），跟 coser 的
# 社交账号是两回事。411f 那单就是从这里抽出 nemo，去 X 撞上 1.6 万粉的
# `@NemoNevermore_`，一路摆到首屏。画师的笔名不是本人账号，负向断言挡掉。
_SIG_GENERIC_BY = re.compile(
    r"(?<!\bdrawn )(?<!\billust )(?<!\billustrated )(?<!\bpainted )"
    r"\bby\s+([A-Za-z0-9_.\-]{3,24})",
    re.I,
)
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


def _strip_title_tag(value: str) -> str:
    """剥掉标题开头的分类标签：``[Cosplay] ran3 - …`` → ``ran3 - …``。

    完整成对的方括号一律当标签；只剩右括号的残留（``Cosplay] ran3``，开括号被
    站点吃掉了）只在里面确实是分类词或纯数字时才剥，免得把真名字剥掉。
    """
    for _ in range(3):
        stripped = _TITLE_TAG.sub("", value, count=1)
        if stripped == value:
            match = _TITLE_TAG_TAIL.match(value)
            if match and (match.group(1).lower() in _STOP or match.group(1).isdigit()):
                stripped = value[match.end() :]
        if stripped == value:
            break
        value = stripped
    return value.strip()


def title_author(text: str) -> str:
    """转载站标题里的作者名：剥掉分类标签后取第一个分隔符之前那段。

    ``「染三ran3 - 露露卡[108P7V-1.23GB]」`` → ``「染三ran3」``；
    ``「Cosplay] ran3 - Cure Nyammy black stockings…」`` → ``「ran3」``。这类资源站
    习惯把作者写在最前面，后面跟作品名和体积；没有分隔符时只在标题够短的时候才当
    名字，否则整句话里什么都可能是名字。剥出来的是分类词（``[Photo] ran3`` 剥成
    ``Photo``）时不算，那只是标签没剥干净。
    """
    value = _strip_title_tag(str(text or "").strip())
    if not value:
        return ""
    if not _TITLE_SPLIT.search(value):
        # 没有分隔符就不猜：整句话里什么都可能是名字，交给署名抽取那条线去认。
        return ""
    head = _TITLE_SPLIT.split(value, 1)[0].strip()
    if head.lower() in _STOP:
        return ""
    return head[:40]


# 内容平台的分享页把账号写在标题里：「夜色温柔，风也轻…@诗诗爱生活的动态」。
# 这种标题的正文是号主自己写的一句话，账号只在 @ 后面跟着平台后缀出现；正文里
# 随便一个 @ 不能当账号，所以只认带「的+平台名」的写法。
_AT_AUTHOR = re.compile(
    r"[@＠]\s*([^\s@＠,，。、;；:：!！?？\"'“”‘’()（）\[\]【】<>《》/\\|]{2,30}?)"
    r"\s*的\s*(?:动态|主页|微博|微头条|百家号|B站|b站|哔哩哔哩|小红书|抖音|快手|视频|帖子|文章)"
)


def title_at_author(text: str) -> str:
    """内容平台标题里的 ``@账号``：``「…@诗诗爱生活的动态」`` → ``「诗诗爱生活」``。

    百度动态、百家号这类分享页的候选标题是正文首句，账号只出现在 ``<title>`` 的
    ``@xxx的动态`` 里（b3b4 那单就是这样，页面上没有任何 author 字段）。只认带
    ``的+平台名`` 的写法，避免把正文里的 ``@`` 当成账号。
    """
    match = _AT_AUTHOR.search(str(text or "").strip())
    if not match:
        return ""
    value = match.group(1).strip()
    if len(value) < 2 or value.lower() in _STOP or value.isdigit():
        return ""
    return value[:40]


# 页面标题里直接写出来的 X 账号：「Model Kokura Chiyo (@yummychiyo) in cosplay W」。
# cosplayrule34、galleryepic 这类图集站习惯这么标人，括号里的号一般就是原作者。
# 比从名字展开出来的变体硬得多——变体是猜的，这个是页面自己写的。
_TITLE_X_HANDLE = re.compile(r"[\(\（\[\【]\s*[@＠]\s*([A-Za-z0-9_]{3,15})\s*[\)\）\]\】]")
# 没有括号的裸 @写法。前面必须是空白或标点，免得把邮箱 ``a@b.com`` 当账号。
_TITLE_AT_HANDLE = re.compile(
    r"(?:^|[\s,，、;；:：|｜/])\s*[@＠]([A-Za-z0-9_]{3,15})(?![A-Za-z0-9_])"
)


def is_stop_word(word: str = "") -> bool:
    """这个词是不是平台名／通用词／站名（``_STOP`` 里那些）。

    这类词不能当账号，也不能当人名：标题里写着 cosplay、照片里印着 pixibb，
    都不代表账号或作者。
    """
    return str(word or "").strip().lower() in _STOP


# 百度贴吧的板块名：`@cosplay吧`、`@崩坏星穹铁道吧`。图上打的是板块，不是账号。
_BOARD_NAME = re.compile(r"[@＠]?[A-Za-z0-9_\u4e00-\u9fff·]{1,20}吧")


def is_board_name(text: str = "") -> bool:
    """这是不是贴吧的板块名（``@cosplay吧``），不是账号。

    贴吧水印是把板块名当署名打的。拆出来的「cosplay」拿去查 X 往往真有个同名号，
    一附主页就把人引过去（b55a 那条 @cosplay 还是个被封的号）。
    """
    return bool(_BOARD_NAME.fullmatch(str(text or "").strip()))


def title_handle(text: str) -> tuple[str, str]:
    """标题里明写的 X 账号：返回 ``(账号, 账号前面的名字)``，没写返回 ``("", "")``。

    只认 ``(@handle)`` 和前面带空白的 ``@handle`` 两种写法。中文名、``@诗诗爱生活
    的动态`` 那类不带 ASCII 账号的写法在这里取不出来（那是 ``title_at_author``
    的活），这里的返回值要直接拿去查 X。
    """
    value = str(text or "").strip()
    if not value:
        return "", ""
    match = _TITLE_X_HANDLE.search(value)
    if match is None:
        match = _TITLE_AT_HANDLE.search(value)
    if match is None:
        return "", ""
    handle = match.group(1)
    if handle.isdigit() or handle.lower() in _STOP:
        return "", ""
    before = value[: match.start()].strip()
    if before:
        before = _TITLE_SPLIT.split(before)[-1].strip()
    before = before.strip(" ([（【-–—|｜/").strip()
    # 括号前面那一大串整句说明（「[X] Maru on the road」之类）不是名字，宁可不给：
    # 名字只用来核显示名，给错了反而把真号挡在外面。
    if len([word for word in re.split(r"\s+", before) if word]) > 4:
        before = ""
    return handle, before[:40]


def split_author_name(value: str) -> tuple[str, str]:
    """把「中文名 + 英文 id」拆成两截：``染三ran3`` → ``(染三, ran3)``。

    很多人把显示名和账号写在一起（「染三ran3」「渔蛋Sakana」），抽出来一个整串的话，
    展示时看着像个人名，其实里面一半是账号。纯中文返回 ``(名字, "")``；纯英文返回
    ``("", 名字)``——单看一个 ASCII 名字分不清是显示名还是账号，交给后面「显示名
    对得上」那一步判断。
    """
    text = str(value or "").strip()
    if not text:
        return "", ""
    ascii_part = re.sub(r"[^A-Za-z0-9_]+", "", text)
    other = re.sub(r"[A-Za-z0-9_]+", "", text).strip()
    if other and ascii_part and 2 <= len(ascii_part) <= 15:
        return other, ascii_part
    if other and not ascii_part:
        return text, ""
    return "", text if 2 <= len(text) <= 15 else ""


def llm_author_reply(text: str) -> tuple[str, str, str]:
    """解析模型回的那一句：作者名、其中的账号、其中的名字。

    要求模型回 JSON：
    ``{"author": "染三ran3", "name": "染三", "handle": "ran3"}``——
    ``author`` 是页面里原样的名字，``name`` / ``handle`` 是把「中文名 + 英文 id」
    拆开的结果（很多人把两截写在一起：「染三ran3」→ 染三 / ran3）。模型只给
    ``author`` 时这里也会按 ASCII 段自己拆，别指望它每次都听话。

    返回值 ``(author, handle, name)``；看不出来时三个都是空串。
    """
    raw = str(text or "").strip()
    if not raw:
        return "", "", ""
    body = re.sub(r"^```[a-zA-Z]*|```$", "", raw).strip()
    author = ""
    handle = ""
    name = ""
    match = re.search(r"\{.*\}", body, re.S)
    if match:
        try:
            data = json.loads(match.group(0))
        except (ValueError, TypeError):
            data = None
        if isinstance(data, dict):
            author = str(data.get("author") or data.get("full") or data.get("name") or "")
            handle = str(data.get("handle") or data.get("account") or "")
            name = str(data.get("name") or data.get("display") or data.get("cn") or "")
            link = str(data.get("x") or data.get("x_handle") or data.get("link") or "")
            # JSON 已经答了（哪怕都空）就以它为准，别再拿整段文字当名字。
            return _finish_author(author, handle, name, link)
    # 没给 JSON：按「字段：值」逐行认；没有字段名时只接受短得像名字的那一行。
    for line in body.splitlines():
        item = line.strip().lstrip("-*• ").strip()
        if not item:
            continue
        field = re.match(
            r"^(作者名?|author|名字|昵称|中文名|账号|handle|account|x|name)\s*[:：]\s*(.+)$", item, re.I
        )
        if field:
            key = field.group(1).lower()
            value = field.group(2)
            if key in {"账号", "handle", "account", "x"}:
                handle = handle or value
            elif key in {"名字", "昵称", "中文名", "name"}:
                name = name or value
            else:
                author = author or value
            continue
        if not author and _looks_like_name(item):
            author = item
    return _finish_author(author, handle, name)


def llm_author_list(text: str) -> list[dict[str, str]] | None:
    """解析模型针对多个页面回的那串作者。

    要求回 JSON 数组 ``[{"site", "author", "name", "handle", "x"}]``，只回一个对象、
    或包一层 ``{"authors": [...]}`` 也接。返回 ``None`` 表示模型这轮什么都没说（空串、
    只肯解释、JSON 坏了），调用方据此退回规则；返回空列表表示它明确答了「这些页面
    都没写作者」——这两种情况的处理不一样，不能混。
    """
    raw = str(text or "").strip()
    if not raw:
        return None
    body = re.sub(r"^```[a-zA-Z]*|```$", "", raw).strip()
    data = _loads_first(body)
    if data is None:
        return None
    items: list[object]
    if isinstance(data, list):
        items = list(data)
    elif isinstance(data.get("authors"), list):  # type: ignore[union-attr]
        items = list(data["authors"])  # type: ignore[index]
    else:
        items = [data]
    found: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        author, handle, name = _finish_author(
            str(item.get("author") or ""),
            str(item.get("handle") or ""),
            str(item.get("name") or ""),
            str(item.get("x") or item.get("link") or ""),
        )
        if not (author or handle or name):
            continue
        found.append(
            {
                "author": author or name or handle,
                "name": name,
                "handle": handle,
                "site": str(item.get("site") or item.get("url") or ""),
            }
        )
    return found


def _loads_first(text: str) -> object | None:
    """从一段可能夹着解释的文字里，取出第一个能解出来的 JSON 数组或对象。"""
    text = str(text or "")
    for index, char in enumerate(text):
        if char not in "[{":
            continue
        try:
            value, _ = json.JSONDecoder().raw_decode(text[index:])
        except (ValueError, TypeError):
            continue
        if isinstance(value, (list, dict)):
            return value
    return None


def _finish_author(author: str, handle: str, name: str, link: str = "") -> tuple[str, str, str]:
    """清理三个字段，并在模型没拆时用原串自己拆一遍。

    ``link`` 是页面里直接写到的 X 账号，比从名字里拆出来的可靠，有就用它。
    """
    author = _clean_author(author)
    handle = _clean_handle(handle)
    name = _clean_author(name)
    split_name, split_handle = split_author_name(author or name)
    if not name:
        name = split_name
    if not handle:
        handle = split_handle
    linked = _clean_handle(_link_handle(link))
    if linked:
        handle = linked
    return author, handle, name


def _link_handle(value: str) -> str:
    """从「x.com/xxx」「@xxx」「https://twitter.com/xxx」里取出裸账号。"""
    text = str(value or "").strip()
    if not text:
        return ""
    found = re.search(r"(?:x|twitter)\.com/([A-Za-z0-9_]{2,15})", text, re.I)
    if found:
        return found.group(1)
    return text


# 模型爱解释。整段文字里出现这些词，就不是名字，是它在说「看不出来」。
_SENTENCE_HINTS = re.compile(
    r"(没有|无法|抱歉|不确定|不知道|看不出|标题里|无法判断|请|我猜|应该是|可能是|looks|sorry|cannot|unable)",
    re.I,
)


def _looks_like_name(value: str) -> bool:
    """没有 JSON、也没有「作者：」前缀时，判断这一行像不像名字。

    ``染三ran3`` 像；``这个标题里没有写作者`` 不像——那是模型在解释。
    """
    text = str(value or "").strip()
    if not (2 <= len(text) <= 24):
        return False
    if _SENTENCE_HINTS.search(text):
        return False
    if re.search(r"[\s。，、；：！？!?;:，,]", text):
        return False
    return True


_EMPTY_REPLY = {"", "none", "null", "n/a", "na", "unknown", "-", "无", "未知", "空", "没有", "看不出来", "不确定"}


def _clean_author(value: str) -> str:
    text = str(value or "").strip().strip("\"'“”「」『』` ")
    text = text.lstrip("@").strip()
    text = re.sub(r"^(?:作者|author)\s*[:：]\s*", "", text, flags=re.I)
    text = text.strip("\"'“”「」『』` 。，,；;")
    if text.lower() in _EMPTY_REPLY:
        return ""
    # 模型爱补一句解释（「染三ran3（X 上叫 ran3ran3ran3）」），只留括号前那截。
    text = re.split(r"[（(]|，|,|（?即|也就是", text)[0].strip()
    if len(text) < 2 or len(text) > 40:
        return ""
    return text


def _clean_handle(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9_]+", "", str(value or ""))
    if not (3 <= len(text) <= 15):
        return ""
    if text.lower() in _STOP:
        return ""
    return text


def x_variants(name: str, limit: int = 6) -> list[str]:
    """把名字展开成可能的 X 账号：重复拼接是最常见的取名法。

    实测 s-20261119-1703-0c5b 的标题是「染三ran3」，正主账号就是
    ``ran3ran3ran3``（显示名「染三 ran3」）；直接拿 ``ran3`` 去查只查到一个
    1 粉的空号。所以同一个名字要试单写、二连、三连，再加上下划线拼法。

    只留 ASCII 那截：中文部分在 X 的显示名里，不可能是 handle。
    """
    ascii_part = re.sub(r"[^A-Za-z0-9_]+", "", str(name or ""))
    found: list[str] = []
    if len(ascii_part) >= 3 and ascii_part.lower() not in _STOP:
        for times in (1, 2, 3):
            candidate = ascii_part * times
            if 3 <= len(candidate) <= 15 and candidate.lower() not in {item.lower() for item in found}:
                found.append(candidate)
        for joined in (f"{ascii_part}_{ascii_part}", f"{ascii_part}{ascii_part}_"):
            if len(joined) <= 15 and joined.lower() not in {item.lower() for item in found}:
                found.append(joined)
    return found[:limit]


def name_matches(author: str, display: str) -> bool:
    """标题里的名字和 X 显示名对不对得上。

    ``「染三ran3」`` 对 ``「染三 ran3」`` 算对上；对 ``「あんらん」`` 不算。
    去空格后互相包含即可——中文名和英文名分开写时，两边都要出现。
    """
    left = re.sub(r"\s+", "", fold(author)).lower()
    right = re.sub(r"\s+", "", fold(display)).lower()
    if len(left) < 3 or not right:
        return False
    if left in right or right in left:
        return True
    cjk = _CJK.sub("", left)
    latin = re.sub(r"[^a-z0-9_]", "", left)
    return bool(cjk and latin and cjk in right and latin in right)


def fold(text: str) -> str:
    """把花体、全角字符折回普通写法。

    X 的显示名爱用数学粗斜体（「𝑴𝒊𝒐 𝑲𝒖𝒖𝒌𝒐🍀」），全角字母也常见。不折回来，
    标题里的「Mio Kuuko」跟她就是对不上——411f 那单的真号 `@Kuukow002` 就在
    检索结果里摆着，显示名一比却是 False，于是被当成不认识的号丢掉。
    NFKC 顺带把全角数字、连字之类的写法统一掉，对纯英文名没有副作用。
    """
    return unicodedata.normalize("NFKC", str(text or ""))


def stem(handle: str) -> str:
    """去掉结尾的数字和下划线：kuukow002 → kuukow，kuuko_w → kuuko。"""
    return re.sub(r"[_\d]+$", "", fold(handle).lower())


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


def cjk_part(text: str) -> str:
    """取一段文字里最长的那段中日文（至少两个字）。"""
    runs = re.findall(r"[\u4e00-\u9fff\u3040-\u30ff]{2,}", str(text or ""))
    return max(runs, key=len) if runs else ""


def watermark_name_conflict(raw: str, x_name: str) -> bool:
    """水印里带中文名、而 X 上那个号的显示名里没有这段中文 → 不是同一个人。

    s-20261117-0741-4631：水印读作 ``@Natsuko_夏夏子``，handle 只留下拉丁那段
    ``natsuko_``；X 上确实有个 @natsuko_，但那是日本画师「おおらい なつこ」，跟这个
    中文名毫无关系。只比拉丁那段，等于把完全不同的人认成作者。中文名才是这个人的
    真标识，所以带着中文时必须以中文名对得上为准。
    """
    cjk = cjk_part(raw)
    if not cjk:
        return False
    return cjk not in str(x_name or "")


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
    joined = " ".join(fold(text) for text in texts).lower()
    return len(core) if core in joined else 0


# 约稿平台和电商页的标题里出现用户名，说明的是「页面主人」而不是被搜的这张图
# 的作者。实测 skeb.jp/@murabitoufu/works/69 这条候选把 @murabitoufu（22 万粉）
# 顶成了「疑似同一个人」，而那页只是别人接稿/委托的页面，跟图毫无关系。
OWNER_SOURCES = (
    "skeb.jp",
    "joom.com",
    "aliexpress",
    "market.yandex",
    "amazon.",
    "ebay.",
)

# reddit 的标题是「求图/约炮」这类整句话（「Control my account choose my pfp」），
# 自由抽词抽出来的都是 choose、looking 这种普通词。实测 s-20261118-1838-b495
# 抽出了 @Choose（6683 粉）和 onlyfans.com/looking + leakgallery.com/looking，
# 三条跟这张图半点关系没有。这类帖只当搬运页看，不拿来推作者。
BLOCKED_SOURCES = (*OWNER_SOURCES, "reddit.com", "redd.it")

# reddit 这种「别人在说话」的站点可以走署名这条路：正经转载常把原作者原样抄在
# 标题里（「Vivian cosplay by KURU」「[source] @xxx」），那是整条标题里唯一指向
# 本人的部分，一刀切屏蔽会把它一起丢掉。署名本身仍要过指纹核对才算数。
SIGNATURE_SOURCES = ("reddit.com", "redd.it")


def is_blocked_source(source: str) -> bool:
    """这类站点的标题不能拿来自由抽词推作者。"""
    host = (source or "").strip().lower()
    return any(token in host for token in BLOCKED_SOURCES)


def is_signature_blocked(source: str) -> bool:
    """这类站点的标题连「署名」都不能认。

    只有页面主人型的站点如此：skeb 的接稿页、电商页标题里的 @号是店主或接稿人。
    reddit 反过来——它只是别人在转发，署名反而是唯一能通向本人的线索。
    """
    host = (source or "").strip().lower()
    if any(token in host for token in SIGNATURE_SOURCES):
        return False
    return any(token in host for token in OWNER_SOURCES)


def is_signature_only(source: str) -> bool:
    """这个站点的署名只能当「找号的入口」：翻不到这张图就什么都不提。

    reddit 的署名能指对人，但那页毕竟是别人转的，只看名字分不清是原作者还是
    帖子里提到的别人。翻她自己帖子里有没有这张图，有才是结论。
    """
    host = (source or "").strip().lower()
    return any(token in host for token in SIGNATURE_SOURCES)


# 标题里直接贴原帖链接的情况（「[Source] https://x.com/kuru_xox/status/123」）：
# 这比「cosplay by KURU」更明确，链接本身就指着那个号。搬运帖常这么写，而正文
# 我们抓不到（reddit 页面直接返回 403），所以标题里出现的链接得认出来。
_LINK_X = re.compile(
    r"(?:https?://)?(?:www\.|mobile\.)?(?:x\.com|twitter\.com)/([A-Za-z0-9_]{2,20})",
    re.I,
)
# 这些不是账号，是链接里的功能段。
_LINK_X_SKIP = {
    "i", "intent", "share", "home", "search", "hashtag", "explore", "settings",
    "login", "signup", "tos", "privacy", "about", "download",
}


def link_handles(texts: list[str], limit: int = 3) -> list[str]:
    """标题里贴着的 X／推特链接指向的账号，按出现顺序去重。"""
    seen: set[str] = set()
    found: list[str] = []
    for text in texts:
        for match in _LINK_X.finditer(text or ""):
            handle = match.group(1)
            low = handle.lower()
            if low in _LINK_X_SKIP or low in seen:
                continue
            seen.add(low)
            found.append(handle)
            if len(found) >= max(1, limit):
                return found
    return found


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
    """把一批标题拆成去重后的小写主干，供相似判断用。

    **停用词不算主干**：通用词人人都写着，靠它「对上」等于没对。实测 5832 那单的
    标题里写着 cosplay，另一个 coser 的标题「Hiyuki cosplay by Carol」就靠共享
    ``cosplay`` 把 ``@carol`` 送进了线索；2c76 的 ``@Telegraph`` 同理（telegra.ph
    的站名写在标题里）。
    """
    pool: set[str] = set()
    for text in texts:
        for match in _TOKEN.finditer(fold(text)):
            low = match.group(0).lower()
            if len(low) < 4:
                continue
            if low in _STOP:
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
