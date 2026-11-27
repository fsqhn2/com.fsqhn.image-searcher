"""结果文案拼装：用户侧走合并转发，模型侧只给纯文字。"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from .engines import Candidate, downgrade_kind, is_avatar_hit
from .extract import dead_site, is_avatar_image, is_forum_page, split_repost_title
from .tasks import SearchOutcome, SearchTask
from . import gallery, handles, paywall
from .hosts import matched_token

AI_FLAG_THRESHOLD = 0.75

# X 账号只可能是这些字符，长度 1~15。搬运站反查的 scanned 里混进过图集页编号
# （"UID 1856233540"），写进文案会变成看不懂的东西。
_REPOST_HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{2,15}$")

# B 站视频号：BV 开头加一串大小写字母数字，例如 BV16m4y1E7Tf。
_BV_RE = re.compile(r"^BV[0-9A-Za-z]{8,}$")


def _is_x_platform(platform: str) -> bool:
    """这条候选是不是落在 X 上。注意别用 ``"x" in platform``：pixiv 也有 x。"""
    text = str(platform or "").strip().lower()
    return text in {"x", "twitter", "x (twitter)"} or text.startswith("x (")


def _ai_probability_line(ai: dict | None) -> str:
    """只给「AI 生成概率」那一行。

    没搜到的卡片用它：结论后面紧跟着说清这张图大概是不是 AI 生成的，别让人再回
    「更多」翻。没跑或跑失败时返回空串——那种情况下「未执行」写在结论里只是占地。
    """
    if not ai or not ai.get("ok"):
        return ""
    try:
        value = float(ai.get("score"))
    except (TypeError, ValueError):
        return ""
    percent = value * 100
    if value >= AI_FLAG_THRESHOLD:
        return f"🤖 AI 生成概率：{percent:.0f}%（疑似 AI 生成）"
    return f"🤖 AI 生成概率：{percent:.1f}%（大概率真人拍摄）"


# 付费图包与打包图站：fantia / Patreon / FANBOX 把图锁在订阅后面，4play、秀人
# 这类整站就是把写真打包卖。落在这些站上的图，公开索引里不会有原帖——用户
# 2026-11-19 要求没搜到时把这件事点明，免得以为换个引擎还能搜出来。
PAID_PACK_HOSTS = (
    "fantia.jp",
    "patreon.com",
    "fanbox.cc",
    "dlsite.com",
    "booth.pm",
    "xiurenol.com",
    "coser.love",
    "cosppi.net",
)


def _paid_pack_host(url: str, platform: str = "") -> str:
    """落在付费图包 / 打包图站时返回站名，否则空串。"""
    host = _host_of(url) or str(platform or "")
    host = host.strip().lower().removeprefix("www.")
    if not host:
        return ""
    token = matched_token(host, PAID_PACK_HOSTS)
    if token:
        return token
    return host if gallery.is_gallery_site(url, platform) else ""


def _forum_thread_line(outcome: SearchOutcome) -> str:
    """命中的是论坛讨论帖：答案常写在回复里，插件读不到，只能提示去翻。

    b62e／ecaa 两单都只搜到 NGA，用户自己在回帖里找到了 pixiv 原图。NGA 对
    未登录访客一律 403（read.php 回「访客不能直接访问」，app_api 要 access_token），
    插件拿不到正文，能给的就是这一句——比让用户以为「这页没用」好。
    """
    threads = [
        item
        for item in outcome.candidates
        if gallery.is_forum_site(item.url or "", item.platform or "")
    ]
    if not threads:
        return ""
    hosts = list(dict.fromkeys((_host_of(item.url) or item.platform or "").strip() for item in threads))
    shown = "、".join([host for host in hosts if host][:2])
    return f"💬 有候选落在 {shown} 这类论坛讨论帖上：答案常写在回复里，点开翻一眼比换引擎快。"


def _help_post_lines(outcome: SearchOutcome) -> list[str]:
    """候选落在求图 / 问答帖上时，把帖子里写的出处说出来。

    826c 那单只有 whos.tv 的求助帖（距离 0、视觉同图），最佳答案已经写着
    「原图在这儿 https://x.com/Ahab_Channel/status/…」，插件却把它降级成
    「不是作者主页」就收工。现在会去读正文：核上了就点名，没核上就写清还得
    自己点开核对。
    """
    info = outcome.help_post or {}
    status = str(info.get("status") or "")
    host = str(info.get("host") or "").strip()
    page = str(info.get("page") or "").strip()
    handle = str(info.get("handle") or "").strip().lstrip("@")
    post = str(info.get("post") or "").strip()
    distance = info.get("distance")
    if status == "hit" and handle:
        where = f"（{host}）" if host else ""
        return [f"💬 出处来自求助帖的回复：X @{handle} 那条帖子，指纹一致{where}。"]
    if status == "clue" and handle:
        if isinstance(distance, int):
            gap = (
                f"指纹距离 {distance}，跟原图不是同一张，可能是同一套的另一张"
            )
        else:
            gap = "没取到配图，没比指纹"
        who = str(info.get("name") or "").strip()
        followers = info.get("followers")
        if isinstance(followers, int) and followers > 0:
            who = f"{who}，{_fans_text(followers)}" if who else _fans_text(followers)
        head = f"X @{handle}（{who}）" if who else f"X @{handle}"
        where = f"{host} 的求助帖里" if host else "一个求助帖里"
        lines = [f"💬 {where}，回复给的出处是 {head}"]
        lines.append(f"   {post}（{gap}，点开核对{('；页：' + page) if page else ''}）")
        return lines
    if status == "unreadable":
        where = f"{host} 的求助帖" if host else "一个求助帖"
        return [f"💬 有候选落在{where}上，这页抓不到正文，只能点开翻回复。"]
    return []


def _paid_pack_line(outcome: SearchOutcome) -> str:
    """没搜到时补一句：候选落在付费图包页，公开索引里不会有原帖。

    已经拿到她售卖页时不写——`_paywall_lines()` 已经把平台和链接摆出来了，
    再说一遍是废话。
    """
    if outcome.paywall:
        return ""
    names: list[str] = []
    for item in outcome.candidates:
        host = _paid_pack_host(item.url or "", item.platform or "")
        if host and host not in names:
            names.append(host)
    if not names:
        return ""
    shown = "、".join(names[:2])
    rest = " 等" if len(names) > 2 else ""
    return (
        f"💸 有候选落在 {shown}{rest} 这种付费图包页，"
        "这类图通常只在订阅里，公开索引没有原帖，换引擎也一样。"
    )


def _ai_line(ai: dict | None) -> str:
    if not ai:
        return "🤖 AI 检测：未执行"

    if not ai.get("ok"):
        return f"🤖 AI 检测失败：{ai.get('error') or '未知原因'}"
    score = ai.get("score")
    try:
        float(score)
    except (TypeError, ValueError):
        return "🤖 AI 检测：未返回有效分值"
    return _ai_probability_line(ai)


def _route_line(outcome: SearchOutcome) -> str:
    reason = f"，{outcome.route_reason}" if outcome.route_reason else ""
    if outcome.route == "drawing":
        return f"🖼 匹配路线：插画{reason}"
    # 照片路线也写原因：常见疑问是「明明是插画，怎么没走 pixiv」，而 pixiv 只有
    # SauceNAO 能带出来，得把「它没认出来」这件事说清楚。
    return f"🖼 匹配路线：照片{reason}"


def _identity_lines(candidate: Candidate, *, detail: bool = True) -> list[str]:
    label = candidate.platform
    dead = dead_site(candidate.url, candidate.platform)
    if dead:
        # 站点已经关站，链接点开只会是空的（半次元 2023-07 停服）。
        label = f"{label}（{dead}已关站，这条打不开）"
    if not dead and not (candidate.handle or "").strip() and gallery.is_repost_site(
        candidate.url, candidate.platform
    ):
        # 图集站/搬运页不是作者主页，光写「平台：galleryepic.com」会让人以为图就发在那儿
        # （实测 s-20261111-1028-1063 丘浩南直接问「不是原作者的页面吧」）。
        label = f"{label}（搬运/图集站，不是作者主页）"
    lines = [f"🌐 平台：{label}"]
    if candidate.note:
        note = candidate.note
        if not detail and candidate.status == "match":
            # 首次回复只给结论：命中说明里那句「这是谁的回复页、顺着回复找到原帖」
            # 属于过程，留在「更多」里看。距离是可信度的凭据，保留。
            if candidate.engine in {"watermark", "identity", "repost_author"}:
                note = "本人发过这张图"
            else:
                note = note.split("：", 1)[0]
        lines.append(f"🔍 匹配：{note}")
    if candidate.name:
        name, extra = candidate.name, ""
        if gallery.is_repost_site(candidate.url, candidate.platform):
            # 图集站标题常写成「Xidaidai – Aemeath 18+ Cosplay, 18 Photos」：
            # 整条当名字摆出来，用户看到的是一串英文。切成名字 + 套图两行。
            name, extra = split_repost_title(candidate.name)
        if extra:
            # 名字是站方标注的模特名，不是作者本人的账号名，得说明白。
            lines.append(f"👤 名字：{name}（图集站标注的模特名）")
            lines.append(f"📦 套图：{extra}")
        elif name != candidate.name.strip() and not (candidate.handle or "").strip():
            lines.append(f"👤 名字：{name}（图集站标注的模特名）")
        else:
            # 论坛帖的「名字」其实是帖子标题，写清楚，免得当成作者名。
            label = "📄 标题" if is_forum_page(candidate.url) else "👤 名字"
            lines.append(f"{label}：{name}")
    if candidate.handle:
        # B 站的 BV 号是「视频号」，不是账号。写成「账号：BV1xx…」会让人以为
        # 有这么一个用户，去搜还搜不到——它跟 UID 一样只是定位用的编号。
        label = "🎬 视频号" if _BV_RE.match(candidate.handle.strip()) else "🆔 账号"
        lines.append(f"{label}：{candidate.handle}")
        if label == "🆔 账号" and _is_x_platform(candidate.platform):
            # X 上给的是帖子链接，账号本身还得点进主页看：直接把主页地址写出来。
            profile = f"https://x.com/{candidate.handle.strip().lstrip('@')}"
            if _bare_url(profile) != _bare_url(candidate.url):
                lines.append(f"🐦 X 主页：{profile}")
    if candidate.account_note:
        lines.append(candidate.account_note)
    if candidate.url:
        lines.append(f"🔗 {candidate.url}")
    return lines


def _watermark_line(grok: dict) -> str:
    """图上 ID 与结论账号的关系。

    实测读水印会把 ``@ste11ar021`` 认成 ``@butter021``，所以读到的 ID 不能
    静默丢掉：跟结论一致就说明这一单是顺着水印核出来的，不一致就明写出来，
    免得用户以为是图搜的结果。
    """
    raw = str(grok.get("watermark") or "").strip()
    if not raw:
        return ""
    handle = str(grok.get("handle") or "").strip().lstrip("@")
    seen = raw.lstrip("@").strip().lower()
    if handle and handle.lower() in seen:
        return f"🏷 图上印的 ID 就是这个号（{raw}），已按它核对。"
    if handle and _handle_key(handle) == _handle_key(raw):
        # 号名只差个下划线：这就是同一个号没读全，不能说成「与结论不同」。
        # 15ea 那单图上 @Banbanko、真号 @Banbanko_，还顺手把另一个同名的
        # 日本议员号摆了出来，看着就像认错人。
        return (
            f"🏷 图上印的 ID 就是这个号（{raw}，号名里的符号没读全，"
            f"实际是 @{handle}），已按它核对。"
        )
    return f"🏷 图上读到 ID：{raw}（与上面的结论不同，已按核实结果为准）。"


def _grok_lines(grok: dict, *, skip_mark: bool = False) -> list[str]:
    """Grok 兜底的两档文案：post 是找到，account 只是线索。

    ``skip_mark`` 用于「水印那行已经讲清楚了」的场合：78c8 那单水印
    @Asagiriairi001 与结论 @asagiriairi002 是同一个人，一处说「是她自己的另一个
    号」、另一处又说「与结论不同」，读起来像插件自己没弄明白。
    """
    tier = grok.get("tier")
    reason = str(grok.get("reason") or "").strip()
    mark = "" if skip_mark else _watermark_line(grok)
    if tier == "post":
        lines = ["✅ 找到 X 上的原帖"]
        name = str(grok.get("name") or "").strip()
        handle = str(grok.get("handle") or "").strip().lstrip("@")
        if name:
            lines.append(f"👤 名字：{name}")
        if handle:
            lines.append(f"🆔 账号：@{handle}")
            lines.append(f"🐦 X 主页：https://x.com/{handle}")
        if grok.get("post_url"):
            lines.append(f"🔗 {grok['post_url']}")
        if mark:
            lines.append(mark)
        if reason:
            lines.append(f"🔍 依据：{reason}")
        return lines
    if tier == "account":
        lines = ["🔎 X 搜索指向这个账号，但没核到原帖"]
        if grok.get("handle"):
            lines.append(f"🔗 https://x.com/{grok['handle']}")
        if grok.get("post_url"):
            # 帖子配图指纹对不上：账号多半是对的，帖子可能是同一套的另一张。
            # 只给链接，不说成原帖——同景同角色换个人拍，指纹也会这样。
            lines.append(f"🔗 存疑参考（配图相似但非同图，可能是同一套的另一张，请自行核对）：{grok['post_url']}")
        if mark:
            lines.append(mark)
        if reason:
            lines.append(f"🔍 依据：{reason}")
        return lines
    return []


def _fans_text(count: int) -> str:
    if count >= 10000:
        return f"{count / 10000:.1f} 万"
    return str(count)


def _repost_handles(info: dict, key: str) -> list[str]:
    """搬运站反查里那些号：挡掉「UID 1856233540」这类脏值。

    实测 s-20261111-1207-2c38 的落盘里扫过 "UID 1856233540"——那是图集页里的
    编号，不是账号；写进文案会变成「翻过 @UID 1856233540」这种看不懂的东西。
    """
    out: list[str] = []
    for item in info.get(key) or []:
        text = str(item).strip().lstrip("@")
        if text and text.isdigit():
            continue
        if not _REPOST_HANDLE_RE.match(text):
            continue
        out.append(text)
    return out


def _repost_author_lines(outcome: SearchOutcome, *, detail: bool = False) -> list[str]:
    """命中搬运站之后有没有接着翻候选里的 X 号，翻到了什么。

    只写结论会让人以为「找到搬运站就收工」——jh361 那单命中距离 2，标题里还写着
    「推特」，看着就像作者在 X 上发过。所以翻了哪几个号、翻到没有，都要留一句。
    这类「怎么找的」只进「更多」，首次回复还是只给结论。
    """
    info = outcome.repost_author or {}
    status = str(info.get("status") or "")
    scanned = _repost_handles(info, "scanned")
    if status == "hit":
        handle = str(info.get("handle") or "").strip().lstrip("@")
        if not handle:
            return []
        return [f"🔎 结论是翻出来的：搬运站那条只是转贴，@{handle} 自己的带图帖里有这张"]
    if status != "none" or not scanned:
        return []
    if not detail:
        # 首次回复只给结论。「翻过哪几个号」属于怎么找的，只在「更多」里出现
        # （2026-11-11 丘浩南确认维持原设计）。
        return []
    names = "、".join(f"@{item}" for item in scanned[:3])
    failed = _repost_handles(info, "failed")
    tail = f"（{'、'.join('@' + item for item in failed[:2])} 那次没翻成）" if failed else ""
    return [
        f"🔎 也翻过候选里 {names} 自己的带图帖和搜索，没找到这张{tail}；"
        "这几个号只看得到最近发的那批"
    ]


def _author_split_text(info: dict) -> str:
    """把「名字 + 账号」拆开讲：中文名和英文 id 常写在一起，混着看像一个人名。

    拆的是**标题里那串原样名字**，不是最后对上的 X 号：标题「染三ran3」拆成名字
    「染三」+ 账号「ran3」，而真正命中的号可能是 ``ran3ran3ran3``，拿它来当「拆出来
    的账号」既跟标题对不上，下面 @ 那一句还要再重复一遍。
    """
    author = str(info.get("author") or "").strip()
    if author:
        name, handle = handles.split_author_name(author)
        if name and handle and name != handle:
            return f"标题写的是「{author}」，拆开是名字「{name}」+ 账号「{handle}」"
        if name:
            return f"标题里的名字是「{name}」"
        if handle:
            return f"标题里的账号是「{handle}」"
        return f"标题里的名字是「{author}」"
    name = str(info.get("name") or "").strip()
    handle = str(info.get("handle") or "").strip().lstrip("@")
    if name and handle and name != handle:
        return f"拆开是名字「{name}」+ 账号「{handle}」"
    if name:
        return f"标题里的名字是「{name}」"
    if handle:
        return f"标题里的账号是「{handle}」"
    return "标题里没有可用的名字"


def _title_author_lines(outcome: SearchOutcome, *, detail: bool = False) -> list[str]:
    """按转载站标题里的作者名找到的 X 号。

    证据强度介于两头之间：不是「她帖子里有这张图」（那会换掉结论），而是「名字
    对得上这个人」。所以单独写一行说明来历，并提醒用户自己核对。

    名字和账号分两截说：很多人把中文名和英文 id 写在一起（「染三ran3」），
    混成一句会被当成一个人名。走模型那条路时再补一句说明名字是哪来的，
    免得以为还是规则切的。
    """
    info = outcome.title_author or {}
    status = str(info.get("status") or "")
    if status == "hit":
        # 结论已经换成那条帖子了，账号在结论行里，这里不再重复。
        return []
    via_llm = str(info.get("via") or "") == "llm"
    handle = str(info.get("handle") or "").strip().lstrip("@")
    if status == "named" and handle:
        display = str(info.get("x_name") or "").strip()
        who = f"（显示名「{display}」）" if display else ""
        lines = [
            f"🔗 {_author_split_text(info)}；在 X 上对得上 @{handle}{who}：https://x.com/{handle}",
            "她最近的带图帖里没翻到这张，可能发得更早或被删——点开自己核一下。",
        ]
        if via_llm:
            lines.append("（名字是模型从页面标题／正文里读出来的，不是按分隔符硬切出来的）")
        return lines
    if status == "seen" and handle:
        # 页面标题自己写着的号（``Model Kokura Chiyo (@yummychiyo)``）：账号可信，但
        # 这张图没在她最近的帖子里翻到，所以只摆出来、不当结论。
        display = str(info.get("x_name") or "").strip()
        who = f"（显示名「{display}」）" if display else ""
        return [
            f"🔗 页面标题里直接写着 @{handle}{who}：https://x.com/{handle}",
            "她最近的带图帖里没翻到这张，可能发得更早或被删——点开自己核一下。",
        ]
    if status == "none" and detail:
        variants = [str(item) for item in (info.get("variants") or []) if item]
        if variants:
            lines = [
                f"🔎 {_author_split_text(info)}，试过 {'、'.join(variants[:4])}，"
                "都对不上 X 上的显示名"
            ]
            if via_llm:
                lines.append("（名字是模型从页面标题／正文里读出来的）")
            return lines
    return []


def _x_identity_lines(outcome: SearchOutcome) -> list[str]:
    """按微博昵称换到 X 号的那一步，怎么换的、换到没换到，都要写清。

    这类单子上「人对、平台错」最容易让人误会：微博那个 UID 确实是她本人，但这张
    图她没发在微博。所以换成功要说明换的路径（昵称 → X 号 → 她自己的帖子），
    没换成功也要把昵称写出来，别让用户只看到一串 UID。
    """
    info = outcome.x_identity or {}
    if not info:
        return []
    status = str(info.get("status") or "")
    weibo = info.get("weibo") or {}
    weibo_name = str(weibo.get("name") or info.get("name") or "").strip()
    weibo_fans = int(weibo.get("followers") or 0)
    weibo_part = weibo_name
    if weibo_part and weibo_fans:
        weibo_part = f"{weibo_part}（{_fans_text(weibo_fans)}粉）"
    if status == "hit":
        handle = str(info.get("handle") or "").strip().lstrip("@")
        if not handle:
            return []
        return [f"🔎 这个 X 号是按名字找到的：微博 {weibo_part or '同名账号'} → X @{handle}"]
    if status == "seen":
        handle = str(info.get("handle") or "").strip().lstrip("@")
        if not handle:
            return []
        x_name = str(info.get("x_name") or "").strip()
        fans = int(info.get("followers") or 0)
        who = f"@{handle}"
        if x_name:
            who += f"（{x_name}"
            who += f"，{_fans_text(fans)}粉）" if fans else "）"
        reason = str(info.get("reason") or "").strip() or "最近十几条带图帖里没这张"
        return [
            f"🔎 微博 {weibo_part or '同名账号'} 对应的 X 是 {who}，但{reason}："
            "这人应该没错，原帖大概只发在别处"
        ]
    if not weibo_part:
        return []
    return [f"🔎 微博 {weibo_part} 这个号是存在的；这次没搜到对应的 X 号，没能往下核"]


def _x_guess_lines(outcome: SearchOutcome) -> list[str]:
    """疑似 X 账号：必须写清「从哪条候选推出来的」，不然会被当成原帖。"""
    guess = outcome.x_guess or {}
    handle = str(guess.get("handle") or "").strip()
    if not handle:
        return []
    # Grok 已经核到同一个号，就不用再说一遍名字推断的过程。
    if str((outcome.grok or {}).get("handle") or "").strip().lower() == handle.lower():
        return []
    fans = int(guess.get("followers") or 0)
    platform = str(guess.get("source_platform") or "").strip() or "候选"
    title = str(guess.get("source_title") or "").strip()
    lines = [f"🐦 疑似同一个人的 X：@{handle} https://x.com/{handle}（粉丝 {_fans_text(fans)}）"]
    if title:
        lines.append(f"从候选 {platform} 的标题「{title}」里认出这个名字，X 上确认账号存在。")
    else:
        lines.append(f"从候选 {platform} 的标题里认出这个名字，X 上确认账号存在。")
    if guess.get("checked_posts"):
        lines.append("她自己最近的带图帖里翻过，没有这张——只能算同一个人可能用的号，不是这张图的原帖。")
    else:
        lines.append("只是同一个人可能用的号，不是这张图的原帖。")
    return lines


def _role_hint_lines(outcome: SearchOutcome) -> list[str]:
    """图搜图没结论时按角色找到的同角色账号：只给名字，不当结论。

    s-20261118-1414-e965 就是这么漏的：图搜引擎一条都没收（小号、没水印），
    但角色认得出，按「角色名 + コスプレ」一搜，人就在里面。所以这条要摆出来，
    同时说明白它只是线索——角色对得上不等于人是同一个。
    """
    info = outcome.role_hint or {}
    roles = [str(item).strip() for item in (info.get("roles") or []) if str(item).strip()]
    accounts = info.get("accounts") or []
    if not roles and not accounts:
        return []
    lines: list[str] = []
    if roles:
        lines.append(f"🎭 角色：{'、'.join(roles[:2])}")
    people: list[str] = []
    for item in accounts[:5]:
        if not isinstance(item, dict):
            continue
        handle = str(item.get("handle") or "").strip().lstrip("@")
        if not handle:
            continue
        fans = int(item.get("fans") or 0)
        people.append(f"@{handle}（粉丝 {_fans_text(fans)}）" if fans else f"@{handle}")
    if people:
        lines.append("👥 同角色账号（只是线索，没核过图）：" + " ".join(people))
    return lines


def _weibo_lines(outcome: SearchOutcome) -> list[str]:
    """微博站内反查的结果：搜到了谁，或者搜过但只有路人号。

    微博昵称就是账号名（没有独立 handle），重名的路人号特别多——实测搜「青生w」
    出来 15 个「阮青生w*」，粉丝 1~102。所以这条永远带着粉丝数，让用户一眼看出
    是不是正主；没搜到像样的也照实写一句，别让人以为「没查」。
    """
    info = outcome.weibo or {}
    status = str(info.get("status") or "")
    if status == "hit":
        name = str(info.get("name") or "").strip() or str(info.get("query") or "")
        fans = int(info.get("followers") or 0)
        url = str(info.get("url") or "").strip()
        source = str(info.get("source") or "").strip()
        line = f"📱 微博：{name}（粉丝 {_fans_text(fans)}）{url}".rstrip()
        how = f"从{source}里认出的" if source else "按这个名字搜到的"
        return [line, f"{how}同名号，微博昵称就是账号名，点开自己对一下是不是她。"]
    query = str(info.get("query") or "").strip()
    if status == "error":
        # 填了登录 cookie 却失效时，不写这句用户只会觉得「微博反查没反应」。
        return [
            "📱 微博：登录态失效了，这次没搜成——重新复制一次 `[weibo] cookie` 就能恢复"
            "（不填 cookie 也能用，走免登录）"
        ]
    if not query:
        return []
    if status != "none":
        return []
    top = info.get("max_fans")
    if top is None:
        return [f"📱 微博：按「{query}」搜过，没有昵称对得上的号"]
    return [
        f"📱 微博：按「{query}」搜过，同名的都是路人号（粉丝最多 {_fans_text(int(top))}），"
        "不像正主，没往结果里塞"
    ]


def _name_hunt_lines(outcome: SearchOutcome) -> list[str]:
    """按标题里的名字找号：核到那张图就写明是这么找到的，只找到主页就列出来。

    c077 那单的图只落在图集站上，页面上一个账号都不挂；标题里那个名字是唯一
    还能指到人的东西。这条线永远只给「名字 → 主页」，谁都不能凭名字顶掉指纹
    命中，所以文案里要写清「只是线索」。
    """
    info = outcome.name_hunt or {}
    status = str(info.get("status") or "")
    name = str(info.get("name") or "").strip()
    source = str(info.get("source") or "").strip()
    if status == "hit":
        distance = int(info.get("distance") or 0)
        if str(info.get("platform") or "").strip() == "微博":
            who = f"微博 {info.get('account') or info.get('handle') or ''}".strip()
        else:
            who = f"@{str(info.get('handle') or '').strip().lstrip('@')}"
        lines = [
            f"🔎 按标题里的名字「{name}」找到 {who}，"
            f"她自己的帖子里有这张图（指纹距离 {distance}）"
        ]
        if source:
            lines.append(f"（{source}）")
        return lines
    if status != "seen":
        return []
    profiles = [item for item in (info.get("profiles") or []) if isinstance(item, dict)]
    if not profiles:
        return []
    parts: list[str] = []
    verified_handle = str(info.get("x_handle") or "").strip().lstrip("@").lower()
    verified_fans = int(info.get("x_followers") or 0)
    verified_name = str(info.get("x_name") or "").strip()
    for item in profiles[:4]:
        platform = str(item.get("platform") or "").strip()
        handle = str(item.get("handle") or "").strip()
        account = str(item.get("name") or "").strip()
        fans = int(item.get("followers") or 0)
        url = str(item.get("url") or "").strip()
        if platform == "微博":
            label = account or handle
            extra = []
            if fans:
                extra.append(f"{_fans_text(fans)}粉")
            if url:
                extra.append(url)
            parts.append(f"微博 {label}（{' · '.join(extra)}）" if extra else f"微博 {label}")
        elif platform == "X":
            name_only = handle.lstrip("@")
            label = f"X @{name_only}"
            verified = bool(verified_handle) and name_only.lower() == verified_handle
            if verified:
                # 显示名对得上的那个号才有粉丝数：只是列表里的同名号没核过。
                detail = []
                if verified_fans:
                    detail.append(f"{_fans_text(verified_fans)}粉")
                if verified_name:
                    detail.append(f"显示名「{verified_name}」对得上")
                if detail:
                    label += f"（{'，'.join(detail)}）"
                # 核实过的号才给主页链接。只从检索结果里捞到的号可能根本不存在
                # （b08b 的 @yuukiyuki1334 打开就是 404），摆一个假主页比不摆更糟。
                label += f" https://x.com/{name_only}"
            else:
                label += "（没核实这个号是否存在）"
            parts.append(label)
        else:
            parts.append(f"{platform} {handle}".strip())
    lines = [
        f"🔎 按名字找出这些主页（名字「{name}」是从标题里读出来的）：" + " / ".join(parts),
    ]
    scanned = int(info.get("x_scanned_posts") or 0)
    if verified_handle and scanned:
        # 「核过但没对上」和「没核」看起来一样，用户会当成没查（8bc9 那单就是）。
        # 把翻了几条写出来，同时交代预算截断——不能把「没翻完」暗示成「都翻过」。
        images = int(info.get("x_scanned_images") or 0)
        where = f"她的 X 最近 {scanned} 条带图帖" + (f"（{images} 张图）" if images else "")
        tail = "；更早的还没翻完" if info.get("x_scan_truncated") else ""
        lines.append(f"翻了{where}，没有这张{tail}。")
    lines.append("这些只是线索：名字对得上不等于人就是同一个，没核到这张图之前不改结论。")
    if source:
        lines.append(f"（{source}）")
    return lines


def _other_accounts_lines(outcome: SearchOutcome) -> list[str]:
    """候选 URL 里写明的其它平台账号：核到图的一条，核不上的列号。

    X 之外能核图的只有 B 站（投稿封面）和有登录 cookie 的微博，TikTok / IG 没有
    可用的公开封面（oembed 只回 iframe）。所以这些号大多数时候是「账号对得上」
    而不是「图对上了」，文案必须写清这一点，否则用户会以为已经确认过了。
    """
    info = outcome.other_accounts or {}
    hit = info.get("hit") or {}
    hit_platform = str(hit.get("platform") or "").strip()
    hit_handle = str(hit.get("handle") or "").strip().lower()
    lines: list[str] = []
    if hit_platform:
        name = str(hit.get("name") or "").strip()
        url = str(hit.get("url") or "").strip()
        distance = int(hit.get("distance") or 0)
        who = f"{hit_platform} {name}".strip()
        line = f"🌐 她本人在 {who} 也发过这张（指纹距离 {distance}）"
        if url:
            line += f"：{url}"
        lines.append(line)
    rows = [
        item
        for item in (info.get("accounts") or [])
        if isinstance(item, dict)
        and str(item.get("handle") or "").strip()
        and not (
            str(item.get("platform") or "").strip() == hit_platform
            and str(item.get("handle") or "").strip().lower() == hit_handle
        )
    ]
    if not rows:
        return lines
    parts: list[str] = []
    for item in rows[:5]:
        platform = str(item.get("platform") or "").strip()
        handle = str(item.get("handle") or "").strip()
        name = str(item.get("name") or "").strip()
        followers = int(item.get("followers") or 0)
        detail: list[str] = []
        if name:
            detail.append(name)
        elif handle.isdigit():
            detail.append(f"UID {handle}")
        else:
            detail.append(f"@{handle.lstrip('@')}")
        if followers:
            detail.append(f"{_fans_text(followers)}粉")
        if item.get("verified"):
            detail.append("图对上了")
        elif item.get("checked"):
            detail.append("没核到这张图")
        text = f"{platform} {' · '.join(detail)}".strip()
        url = str(item.get("url") or "").strip()
        parts.append(f"{text}（{url}）" if url else text)
    lines.append("🌐 候选里写着这些平台账号：" + " / ".join(parts))
    lines.append(
        "这些只是账号线索：TikTok / Instagram 没有公开的图可核，B 站和微博没核到图之前不改结论。"
    )
    return lines


def _paywall_lines(outcome: SearchOutcome) -> list[str]:
    """确认作者之后找到的售卖页：卖在哪、多少钱、有没有免费预览。

    图包里的图公开渠道永远搜不到，能给到底的就是这一块。所以它属于结论，
    放首次回复；「怎么查到的」才留给「更多」。付费帖封面给非订阅者打糊，
    这里不假装能看到内容，只写清楚要不要订阅。
    """
    info = outcome.paywall or {}
    platform = str(info.get("platform") or "").strip().lower()
    if not platform:
        return []
    label = {"patreon": "Patreon", "fanbox": "FANBOX"}.get(platform, platform)
    shop = str(info.get("shop") or "").strip()
    post = info.get("post") or {}
    lines: list[str] = []
    if post.get("url"):
        head = f"🛒 售卖页（{label}）：{post.get('title')}"
        if post.get("published"):
            head += f"，{post['published']} 发布"
        lines.append(head)
        lines.append(f"🔗 {post['url']}")
    elif shop:
        lines.append(f"🛒 她的 {label}：{shop}（最近二十期里没对上这一期）")
    x_url = str(info.get("x_url") or "").strip()
    if x_url:
        lines.append(f"🐦 她的 X：{x_url}")
    tiers = info.get("tiers") or []
    # 只有 Free 档时「档位：Free（免费）」等于没说，不如省一行。
    if any(not tier.get("free") for tier in tiers):
        text = paywall.tier_text(tiers)
        if text:
            lines.append(f"💰 档位：{text}")
    preview = info.get("preview") or {}
    if preview.get("url"):
        title = str(preview.get("title") or "").strip()
        lines.append(f"🎁 免费预览：{title} {preview['url']}".replace("  ", " "))
    else:
        teaser = info.get("teaser") or {}
        teaser_url = str(teaser.get("url") or "").strip()
        # 兜一道保险：预告跟结论是同一条帖子时，写了就是自说自话。
        posted = {
            _bare_url(value)
            for value in (
                (outcome.best.url or "") if outcome.best is not None else "",
                str((outcome.grok or {}).get("post_url") or ""),
            )
            if value
        }
        if teaser_url and _bare_url(teaser_url) in posted:
            teaser_url = ""
        if teaser_url:
            when = str(teaser.get("when") or "").strip()
            if teaser.get("same_period"):
                lines.append(f"🎁 免费预告：{teaser_url}（这一期她自己在 X 上发的，不一定是同一张）")
            elif when:
                lines.append(f"🔎 顺带看到：{teaser_url}（她 X 上 {when} 提到过这个角色，不是这一期）")
            else:
                lines.append(f"🔎 顺带看到：{teaser_url}（她 X 上提到过这个角色，不一定是这一期）")
        elif post.get("url"):
            lines.append(f"🔒 {paywall.gate_text(post, tiers)}")
    return lines


def _paid_lines(outcome: SearchOutcome) -> list[str]:
    """付费内容线索：写清是「这个人做 OnlyFans」而不是「这张图的原帖」。"""
    paid = outcome.paid or {}
    handle = str(paid.get("handle") or "").strip()
    if not handle:
        return []
    name = str(paid.get("name") or "").strip() or handle
    links = [f"https://onlyfans.com/{handle}"]
    x_handle = str(paid.get("x_handle") or "").strip()
    if x_handle:
        fans = int(paid.get("x_followers") or 0)
        tail = f"（粉丝 {_fans_text(fans)}）" if fans else ""
        links.append(f"https://x.com/{x_handle}{tail}")
    instagram = str(paid.get("instagram") or "").strip()
    if instagram:
        links.append(instagram)
    lines = [f"💳 疑似同一个人的 OnlyFans：{name}"]
    lines.append("主页 " + "，".join(links))
    platform = str(paid.get("source_platform") or "").strip() or "候选"
    title = str(paid.get("source_title") or "").strip()
    page_url = str(paid.get("page_url") or "").strip()
    if title:
        head = f"从候选 {platform} 的标题「{title}」里认出这个名字"
    else:
        head = f"从候选 {platform} 的标题里认出这个名字"
    lines.append(f"{head}，泄露站收录过该名字的付费内容{'：' + page_url if page_url else '。'}")
    lines.append("只是同一个人可能在卖的号，不是这张图的原帖。")
    return lines


def _trace_lines(outcome: SearchOutcome, *, detail: bool = False) -> list[str]:
    """「这次是怎么找的」：水印读了哪一次、反查跑到哪停的。

    只在「更多」里给——首次回复按用户的要求只留结论和图上水印那几段线索，
    过程说明塞进去就成了刷屏。老任务与按图缓存里没有这份记录时什么都不写。

    用户 2026-11-20 的原话：「把这次的水印读了哪次、反查停在哪写出来」。
    以前「没触发」和「触发了没读到」在群里长得一模一样，分不出是漏了还是白跑。
    """
    if not detail:
        return []
    trace = [item for item in (outcome.trace or []) if isinstance(item, dict)]
    if not trace:
        return []
    marks = {"hit": "✅", "ran": "·", "none": "×", "skip": "○"}
    lines = ["—", "🧭 这次是怎么找的"]
    for item in trace:
        step = str(item.get("step") or "").strip()
        if not step:
            continue
        state = str(item.get("state") or "")
        note = str(item.get("note") or "").strip()
        if not note:
            note = "这一步没跑" if state == "skip" else "跑完了，没有结果"
        lines.append(f"{marks.get(state, '·')} {step}：{note}")
    return lines


def _same_person_lines(outcome: SearchOutcome) -> list[str]:
    """几个 X 号挂着同一条外链：写明依据，别让人以为是猜出来的。"""
    info = outcome.same_person or {}
    members = [item for item in (info.get("members") or []) if isinstance(item, dict)]
    link = str(info.get("link") or "").strip()
    if len(members) < 2:
        return []
    parts: list[str] = []
    for item in members:
        tag = _fans_text(int(item.get("followers") or 0)) if item.get("followers") else ""
        # 只有指纹真对上这张图的号才标「对上的那条」；视觉判的同一个人是另一张图。
        if item.get("exact"):
            tag = f"{tag}，跟这张图对上的那条" if tag else "跟这张图对上的那条"
        parts.append(f"@{item.get('handle')}（{tag}）" if tag else f"@{item.get('handle')}")
    who = "、".join(parts)
    lines = [f"👥 这{len(members)}个号是同一个人：{who}"]
    if link:
        lines.append(f"依据：几个号的资料里挂着同一条 {link}，不是猜名字。")
    else:
        lines.append("依据：几个号挂着同一条外链，不是猜名字。")
    lines.append("这条只说明这几个号是同一个人，不表示他们就是这张图的作者。")
    return lines


def _vision_reason(vision: dict | None) -> str:
    """把视觉模型给的理由原样亮出来。

    它常按「黑蕾丝、白短袜、室内」这类服装与场景判成同一个人，理由里甚至会把
    发色写错。不写出来，用户只看「疑似同一个人」无从判断；写出来一眼就知道这
    是照服装猜的，不是认脸。
    """
    reason = str((vision or {}).get("reason") or "").strip()
    if not reason:
        return ""
    return f"🧠 它给的理由：{reason[:80]}"


def _vision_pick_line(outcome: SearchOutcome) -> str:
    """视觉挑中又降级时，把最近那条的链接直接放到结论里。

    降级说的是「这页不能当作者主页」，不是「没搜到」：这条往往就是这张图所在的
    作品页、图库页。只写一句「只当参考」时，群里得点开参考区逐条找，看着就像
    什么都没给。
    """
    picks = [
        item
        for item in outcome.candidates
        if item.status == "suspect"
        and (item.note or "").startswith("视觉复核")
        and not is_avatar_hit(item)
        and not is_avatar_image(item.image_url)
        and item.url
    ]
    if not picks:
        return ""
    # 视觉挑中的那条 best 被降级时，用户想看的就是它——按距离另挑一条（比如
    # YouTube 的 shorts 列表页）反而离题。
    best = outcome.best
    pick = None
    label = "最接近的一条"
    if best is not None and best.url:
        pick = next((item for item in picks if item.url == best.url), None)
        if pick is not None:
            label = "视觉挑中的那条"
    if pick is None:
        # 其次优先「页面自己标了来源账号」的那条：它至少还指向一个人，比一条
        # 距离更近、但只是盗图页的链接有用。
        sourced = {
            _bare_url(str(item.get("page") or ""))
            for item in (outcome.vision or {}).get("sources") or []
            if isinstance(item, dict)
        }
        pick = next((item for item in picks if _bare_url(item.url) in sourced), None)
        if pick is not None:
            label = "页面标了来源的那条"
    if pick is None:
        pick = min(picks, key=lambda item: item.distance if item.distance is not None else 999)
    if label == "最接近的一条":
        nearest = min(
            (item.distance for item in outcome.candidates if item.distance is not None),
            default=None,
        )
        if nearest is not None and pick.distance is not None and pick.distance > nearest:
            # 上一句刚写「最近的一条指纹距离 25」，这里再摆一条 30 的说是「最接近
            # 的一条」，两行并排就自相矛盾（实测 s-20261111-0847-24c4）。
            label = "视觉挑中的那条"
    where = pick.platform or pick.source or pick.engine
    gap = f"，指纹距离 {pick.distance}" if pick.distance is not None else ""
    return f"👉 {label}（{where}{gap}）\n{pick.url}"


def _downgraded_pick_line(outcome: SearchOutcome) -> str:
    """搬运页、求图帖这类降级线索：把原因和链接直接摆到结论里。

    结论写的是「没找到原作者」，但「这张图还出现在哪」本身有用：只写一句没找到，
    群里还得自己翻参考区（s-20261111-2327-5138 命中游民星空那篇囧图合集，就是
    这么被当成出处发出去的）。视觉降级那条另有 _vision_pick_line，这里不重复。
    """
    best = outcome.best
    pool = [
        item
        for item in outcome.candidates
        if downgrade_kind(item) in {"repost", "request", "aggregator"} and (item.url or "").strip()
    ]
    if (
        best is not None
        and downgrade_kind(best) in {"repost", "request", "aggregator"}
        and (best.url or "").strip()
    ):
        pick = best
    elif pool:
        pick = min(pool, key=lambda item: item.distance if item.distance is not None else 999)
    else:
        return ""
    where = pick.platform or pick.source or pick.engine
    gap = f"，指纹距离 {pick.distance}" if pick.distance is not None else ""
    reason = (pick.note or "").strip() or "这条不能当原帖"
    return f"👉 {reason}（{where}{gap}）\n{pick.url}"


def _vision_downgrade_line(outcome: SearchOutcome, vision: dict | None) -> str:
    """视觉挑中又被降级时的那句说明。

    降级原因不止「图库站、商品页」一种：X 的转发页、只解析出平台页面都算。写死
    成商品页时，实际落点是转发页就会让人看不明白，所以直接引用被降级那条自己的
    说明，认不出来才退回旧说法。
    """
    vision = vision or {}
    count = int(vision.get("downgraded") or 0)
    if not count:
        return ""
    notes: list[str] = []
    for item in outcome.candidates:
        note = (item.note or "").strip()
        if item.status != "suspect" or not note.startswith("视觉复核"):
            continue
        text = note[len("视觉复核：") :] if note.startswith("视觉复核：") else note
        text = text.split("（指纹")[0].split("（未做")[0].strip("，, 　")
        if text.endswith("，仅供参考"):
            text = text[: -len("，仅供参考")]
        if text and text not in notes:
            notes.append(text)
    if notes:
        tail = "；".join(notes[:2])
        if count > min(len(notes), 2):
            # 写「挑出 3 条」却只列 2 条，读起来像漏了一条。
            tail += "等"
    else:
        tail = "落点在图库站、商品页这类页面，或账号最近没发过这张图；其中最近的一条可能就包含这张图"
    return f"视觉复核挑出 {count} 条，但都不能当原帖：{tail}。"


def _best_block(outcome: SearchOutcome, *, skip_grok: bool = False, detail: bool = False) -> str:
    text = _best_block_core(outcome, skip_grok=skip_grok, detail=detail)
    extra: list[str] = []
    watermark = _vision_watermark_line(outcome)
    if watermark:
        extra.append(watermark)
    extra.extend(_x_identity_lines(outcome))
    extra.extend(_repost_author_lines(outcome, detail=detail))
    extra.extend(_title_author_lines(outcome, detail=detail))
    extra.extend(_paywall_lines(outcome))
    extra.extend(_gallery_source_lines(outcome))
    if (
        outcome.best is not None
        and not downgrade_kind(outcome.best)
        and (outcome.best.url or "") != str((outcome.pixiv_source or {}).get("url") or "")
    ):
        extra.extend(_pixiv_source_lines(outcome, compact=True))
    if not skip_grok and GROK_QUIET_NOTE not in text:
        miss = _grok_miss_line(outcome)
        if miss:
            extra.append(miss)
    extra.extend(_same_person_lines(outcome))
    extra.extend(_x_guess_lines(outcome))
    extra.extend(_weibo_lines(outcome))
    extra.extend(_name_hunt_lines(outcome))
    extra.extend(_other_accounts_lines(outcome))
    extra.extend(_help_post_lines(outcome))
    extra.extend(_role_hint_lines(outcome))
    extra.extend(_paid_lines(outcome))
    extra.extend(_trace_lines(outcome, detail=detail))
    if extra:
        text = "\n".join([text, *extra])
    return text


def _vision_watermark_line(outcome: SearchOutcome) -> str:
    """视觉模型顺手抄下来的图上水印，只能当线索，不能当结论。

    实测模型读小号白字水印会出错（@ste11ar021 读成 @butter021），所以文案里
    必须写明「模型识别，可能读错」；能确认到账号时附主页，让用户自己点开核对。

    视觉和 Grok 读得不一样时两个都写出来：s-20261109-2341-5b5b 那单一个读成
    ``X@Miharu``（一个无关老号）、一个读成 ``X@Moliuwu``，只亮一个反而会把人
    引到错的主页去。
    """
    vision = outcome.vision or {}
    grok = outcome.grok or {}
    readings = [
        item for item in (vision.get("watermark_readings") or []) if isinstance(item, dict)
    ]
    # 号名读偏、靠近似拼写才找回来的那种，必须讲清楚：用户看到的字跟结论里的号
    # 对不上时，不说原因就像插件认错人了（实测 Moliww 被读成 Miharu／Moliuwy）。
    hit = next((item for item in readings if str(item.get("post") or "").strip()), None)
    if hit is not None and str(hit.get("fixed") or "").strip():
        raws: list[str] = []
        for item in readings:
            if str(item.get("post") or "").strip() and not str(item.get("fixed") or "").strip():
                # 命中且没经过近似拼写的那条，写法本身没错，不用列。
                continue
            text = str(item.get("raw") or "").strip()
            if text and text not in raws:
                raws.append(text)
        where = "、".join(raws[:2]) or f"X@{hit.get('fixed')}"
        if len(raws) > 2:
            where += " 等"
        distance = hit.get("distance")
        gap = f"，指纹距离 {distance}" if isinstance(distance, int) else ""
        post = str(hit.get("post") or "").strip()
        best_url = (outcome.best.url or "").strip() if outcome.best is not None else ""
        tail = "" if best_url and _bare_url(best_url) == _bare_url(post) else f"：{post}"
        return (
            f"🔎 图上水印读偏了：{where} 都不是；按近似拼写核到 @{hit.get('handle')}，"
            f"这张图就在它自己的帖子里{gap}{tail}"
        )
    text = str(vision.get("watermark") or "").strip()
    conclusion = _conclusion_handle(outcome)
    if text and conclusion and _handle_key(text) == _handle_key(conclusion):
        # 水印读法跟已核实的结论是同一个号（最多差个下划线）：别再来一句
        # 「这个号存在、但最近没有这张图」——那句话是说给另一个同名账号听的，
        # 摆在结论下面只会像认错了人。
        return ""
    if not text:
        handle = str(vision.get("watermark_handle") or "").strip()
        if handle:
            text = f"X@{handle.lstrip('@')}"
    other = str(grok.get("watermark") or "").strip()
    disagree = bool(other) and _squash_handle(other) != _squash_handle(text)
    lines: list[str] = []
    post = str(vision.get("watermark_post") or "").strip()
    if post and outcome.best is not None and (outcome.best.url or "").strip() == post:
        # 结论本身就是这条帖子，再写一遍同样的链接只是重复。
        return ""
    state = str(vision.get("watermark_state") or "").strip()
    user_id = str(vision.get("watermark_user_id") or "").strip()
    weak = str(vision.get("watermark_weak") or "").strip()
    mark_name = _squash_name(vision.get("watermark_name"))
    post_name = _squash_name(outcome.best.name if outcome.best is not None else grok.get("name"))
    same_person = bool(mark_name) and mark_name == post_name
    if text:
        url = str(vision.get("watermark_url") or "").strip()
        # 老任务里可能存着「t.me/xxx」被点号拆出来的垃圾地址（`x.com/me/xxx`）：
        # 主页路径只有一段才算数，多一段就宁可不给链接，别让用户点开是个 404。
        if url:
            path = urlparse(url).path.strip("/")
            if not re.fullmatch(r"[A-Za-z0-9_]{2,20}", path):
                url = ""
        note = str(vision.get("watermark_note") or "").strip()
        if post:
            # 水印号自己的带图帖里翻到了这张：这是核对过的结论，直接把帖子给出来。
            lines.append(f"🔎 图上水印核对上了：{text}（{note}）：{post}")
        elif weak:
            # 号是读歪撞上的：只写读到的字，不给主页。832b 那单把地毯上的连笔签名
            # 读成 Stellach，X 上那个号是 71 粉的私人韩文号，点开只会浪费时间。
            lines.append(
                f"🔎 图上水印读不准：读出「{text}」，但 X 上的同名号{weak}，"
                "不像这张图的人（花体、浅色小字容易读成别的词）"
            )
        elif same_person and state == "ok":
            # 78c8 那单：水印 @Asagiriairi001、结论 @asagiriairi002，两边都叫
            # 「朝霧愛理」——这是她自己的另一个号，不是「模型读错了」，也不是
            # 「与结论不同」。分两句写只会让人以为插件认错了人。
            lines.append(
                f"🔎 图上水印 {text} 是她自己的另一个号（两边都叫「{vision.get('watermark_name')}」），"
                f"这张图发在 @{conclusion} 上"
            )
        elif state == "ok" and url:
            # 号确实存在就把主页和数字 ID 都摆出来：能不能认人是用户的事，
            # 但「有没有这个号」必须先核过，别让人自己去试。存在 ≠ 这图是她发的，
            # 所以这一行永远标「线索，不是结论」——s-20261110-0836-ac88 那单
            # 读出来的 @Miharu 就是个 2010 年的无关老号。
            detail = "已核实这个号存在" + (f"（用户 ID {user_id}）" if user_id else "")
            if note:
                detail += f"，但{note}"
            # 「可能读错」只说一次：note 自己讲了就不再在括号里叠一遍。
            caveat = "" if "读错" in note else "（视觉模型识别，可能读错）"
            lines.append(
                f"🔎 图上水印（线索，不是结论）：{text}{caveat}：{url} —— {detail}"
            )
        elif disagree:
            # 两个模型读得不一样，连是哪个号都没定：这时候再附主页，用户点进去
            # 只会看到一个无关账号，白白浪费时间。
            lines.append(f"🔎 图上水印：{text}（视觉模型识别，可能读错，先别去认这个号）")
        elif _is_board_name(text):
            # 贴吧水印：那块板不是人，别附主页（点开往往是同名的老号）。
            lines.append(
                f"🔎 图上水印：{text}（视觉模型识别，可能读错；这是贴吧的板块名，不是账号）"
            )
        elif state == "suspended":
            # 同名号已被封禁：附主页只会让人以为「人找到了」，点开却是空白页。
            lines.append(
                f"🔎 图上水印：{text}（视觉模型识别，可能读错；同名号已被 X 封禁，别当线索）"
            )
        elif url:
            lines.append(f"🔎 图上水印：{text}（视觉模型识别，可能读错，可点开核对）：{url}")
        elif note:
            caveat = "" if "读错" in note else "视觉模型识别，可能读错；"
            lines.append(f"🔎 图上水印：{text}（{caveat}{note}）")
        else:
            lines.append(f"🔎 图上水印：{text}（视觉模型识别，可能读错）")
    if disagree:
        if post:
            # 已经用指纹核对到帖子了，另一个读法就是读错，不必再让人两头猜。
            lines.append(f"🔎 Grok 读成 {other}，跟核对结果不一致，那是读错了")
        else:
            other_info = vision.get("watermark_other")
            other_state = str((other_info or {}).get("state") or "") if isinstance(other_info, dict) else ""
            state_note = _account_state_note(other_state)
            tail = f"（{state_note}）" if state_note else ""
            if state == "ok":
                lines.append(
                    f"🔎 Grok 读成 {other}{tail}，跟上面那个号不是同一个；两个都只当线索"
                )
            else:
                lines.append(f"🔎 Grok 读到的水印不一样：{other}{tail}，两个都别当结论")
    # 水印里写着「id=某某」时，那个名字在各平台捞到的同名主页。名字是从图上读来的，
    # 同名的号不一定是本人，所以这一行永远标「只是线索」，也不超过三个：一串链接
    # 摆得像结论，反而把人引到错的地方去。
    profiles = [
        item for item in (vision.get("watermark_profiles") or []) if isinstance(item, dict)
    ]
    id_name = str(vision.get("watermark_id_name") or "").strip()
    if profiles and id_name and not str(vision.get("watermark_id_hit") or "").strip():
        labels: list[str] = []
        for item in profiles[:3]:
            platform = str(item.get("platform") or "网页").strip()
            handle = str(item.get("handle") or "").strip()
            labels.append(f"{platform} {handle}".strip())
        more = f" 等 {len(profiles)} 个" if len(profiles) > 3 else ""
        lines.append(
            f"🔎 图上写着 id={id_name}：搜到同名主页 {'、'.join(labels)}{more}"
            "（只是线索，没核到这张图）"
        )
    return "\n".join(lines)


def _account_state_note(state: str) -> str:
    """把账号核实结果翻成一句人话。"""
    if state == "ok":
        return "这个号存在，但没核到这张图"
    if state == "missing":
        return "查无此号"
    if state == "suspended":
        return "读不到这个号，可能受限或已封"
    return ""


def _bare_url(url: str) -> str:
    """比两条链接是不是同一页时用：去掉查询串与照片序号。

    X 的同一条帖子有 ``/status/<id>``、``/status/<id>/photo/1``、``/video/1``
    几种写法，不归一就看不出是同一页（实测 s-20261110-2122-4035：结论给的是
    ``/photo/1``，免费预告又把同一条帖子写了一遍）。
    """
    text = str(url or "").split("?", 1)[0].rstrip("/")
    return re.sub(r"/(photo|video)/\d+$", "", text).rstrip("/").lower()


def _squash_handle(text: str) -> str:
    """比水印是否同一个号时用：忽略 ``X@`` / ``IG@`` 前缀、@、空格和大小写。"""
    value = re.sub(r"^[a-z]{1,3}@", "", str(text or "").strip().lower())
    return re.sub(r"[^0-9a-z_]+", "", value)


def _squash_name(text: Any) -> str:
    """比两个号是不是同一个人时用：只去掉空白、不看大小写。

    别用 ``_squash_handle``：它会把中文整个抹掉，「朝霧愛理」和「纸悦」都会变成
    空串，一比就相等了。
    """
    return re.sub(r"\s+", "", str(text or "").strip().lower())


def _handle_key(text: str) -> str:
    """比「是不是同一个号」时用的归一化写法：连分隔符号一起忽略。

    ``@Banbanko`` 与 ``@Banbanko_`` 只差一个下划线，多数时候是号名没读全；
    但下划线后面也可能真挂着另一个账号（15ea 那单的 ``@banbanko`` 是个日本
    议员），所以这个比较只用来解释水印读法，不拿来改结论。
    """
    return re.sub(r"[^0-9a-z]+", "", str(text or "").strip().lstrip("@").lower())


def _is_board_name(text: str) -> bool:
    """``@cosplay吧`` 这种是百度贴吧的板块名，不是账号。"""
    return handles.is_board_name(text)


def _conclusion_handle(outcome: SearchOutcome) -> str:
    """这单的结论挂在哪个号上：有候选落点就用它，没有才认 Grok 给的号。"""
    best = outcome.best
    if best is not None and str(best.handle or "").strip():
        return str(best.handle).strip().lstrip("@")
    grok = outcome.grok or {}
    if str(grok.get("tier") or "") == "post":
        return str(grok.get("handle") or "").strip().lstrip("@")
    return ""


def _pixiv_source_lines(outcome: SearchOutcome, *, compact: bool = False) -> list[str]:
    """从搬运站文件名里读到的 pixiv 作品：出处是哪个作品、作者是谁。

    这条与其它线索不同——它不是「可能同一个人」，而是「这张图出自哪个作品」：
    搬运站按 pixiv 作品扒图时把作品号抄进了图床文件名，回 pixiv 核对标题与
    作者名都能对上。但 pixiv 未登录（连非 R-18 也一样）给不了图片地址，所以
    比不了指纹，文案必须写明这一点，别让人以为核过图。
    """
    info = outcome.pixiv_source or {}
    if not info.get("illust_id"):
        return []
    title = str(info.get("title") or "").strip()
    user_name = str(info.get("user_name") or "").strip()
    user_id = str(info.get("user_id") or "").strip()
    url = str(info.get("url") or "").strip()
    head = f"🎯 原图出自 pixiv：{'《' + title + '》' if title else '作品'}"
    if user_name:
        head += ("" if head.endswith("》") else " ") + f"by {user_name}"
    detail: list[str] = []
    if user_id:
        detail.append(f"users/{user_id}")
    if int(info.get("x_restrict") or 0):
        detail.append("R-18")
    pages = int(info.get("page_count") or 0)
    if pages > 1:
        detail.append(f"{pages} 页")
    created = str(info.get("create_date") or "")[:10]
    if created:
        detail.append(created)
    if detail:
        head += f"（{' · '.join(detail)}）"
    lines = [head]
    if url:
        lines.append(url)
    if compact:
        return lines
    distance = info.get("distance")
    read_from = str(info.get("read_from") or "").strip()
    if len(read_from) > 48:
        read_from = "…" + read_from[-46:]
    if distance is not None:
        note = "作品号是从转载站的图床文件名里读到的"
        if read_from:
            note += f"（{read_from}）"
        note += f"，取回 pixiv 原图比过指纹，距离 {distance}。"
    else:
        note = "作品号是从转载站的图床文件名里读到的"
        if read_from:
            note += f"（{read_from}）"
        note += "，pixiv 未登录看不到这张图，没能比指纹。"
    lines.append(note)
    return lines


def _gallery_source_lines(outcome: SearchOutcome) -> list[str]:
    """图集 / 泄露站页面上标的来源账号。

    站点把别人的 IG / X 内容搬下来打包，页面不是作者主页，但站方通常写着来源
    账号。这是「视觉判定同一人、落点却被降级」时唯一还能指向本人的线索：实测
    4play.to 的 meta 直接给了 ``instagram.com/menruinyanko``。站方也会标错，
    所以只当线索，必须让用户自己点开核对。

    来源有两种：帖子页自己写的（``vision["sources"]``），以及从帖子页翻到发布者
    主页再抽到的（``outcome.gallery_sources``，带 ``author_page``）。后者硬一些
    ——那是站方给「这个主页属于谁」的链接，不是从标题里抄的。
    """
    raw: list = []
    vision = outcome.vision or {}
    if isinstance(vision.get("sources"), list):
        raw.extend(vision["sources"])
    if isinstance(outcome.gallery_sources, list):
        raw.extend(outcome.gallery_sources)
    lines: list[str] = []
    seen: set[tuple[str, str]] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        handle = str(item.get("handle") or "").strip()
        url = str(item.get("url") or "").strip()
        if not handle or not url:
            continue
        platform = str(item.get("platform") or "账号")
        if (platform, handle) in seen:
            continue
        seen.add((platform, handle))
        site = str(item.get("site") or "").strip()
        author_page = str(item.get("author_page") or "").strip()
        if author_page:
            where = site or "这个页面"
            lines.append(
                f"🔗 {where} 的发布者主页写着 {platform} @{handle}：{url}"
                "（页面自己标的，点开核对一下）"
            )
            continue
        where = f"来自 {site} 页面" if site else "图集站页面"
        lines.append(f"🔎 {where}标的来源：{platform} @{handle}（站方标注，未必是作者本人）：{url}")
    if lines:
        lines.append("⚠️ 这几条是站方标的来源，点开核对一下再认人。")
    return lines


# 盗图/付费资源站：页面上标的账号多半是从标题或水印里抄来的，不是作者主页。
# 只放实测见过的站，遇到新的再加——误标一个正经站点比漏标更糟。
LEAK_SITES = (
    "jxporn",
    "lanrenbl",
    "4khd",
    "yuemeinv",
)


def leak_site_of(url: str = "", platform: str = "", name: str = "") -> str:
    """命中盗图/资源站时返回展示用的站点名，没命中返回空串。"""
    value = str(url or "")
    host = urlparse(value).netloc.lower() if "://" in value else ""
    blob = f"{host} {platform} {name}".lower()
    for token in LEAK_SITES:
        if token in blob:
            return host or str(platform or token)
    return ""


def _leak_line(best) -> str:
    """落在盗图站时点明，免得用户以为页面上的账号就是发布者。"""
    site = leak_site_of(best.url, best.platform, best.name)
    if not site:
        return ""
    return f"⚠️ 这条落在 {site} 上，那是盗图/资源站：页面上的账号是从标题里抄的，不是作者主页。"


def _search_page_line(outcome: SearchOutcome) -> str:
    """搜索页反解的结果摆到结论里。

    反解到号但图对不上时，这句话以前只挂在搜索页那条候选的 account_note 上；
    可那条永远是 suspect、不会被选成结论，用户等于完全看不到这一步跑过。
    """
    info = outcome.search_page or {}
    note = str(info.get("note") or "").strip()
    if not note:
        return ""
    return f"🔎 搜索页反解：{note}"


def _best_block_core(outcome: SearchOutcome, *, skip_grok: bool = False, detail: bool = False) -> str:
    best = outcome.best
    grok = outcome.grok or {}
    vision_watermark = str((outcome.vision or {}).get("watermark") or "").strip()
    grok_watermark = str(grok.get("watermark") or "").strip()
    conclusion = _conclusion_handle(outcome)
    # 水印号就是结论号（最多差个下划线）时，那句「图上印的 ID 就是这个号」还得留着：
    # 视觉那行在同号时会自己早退，两边一起去掉就什么都不说了。
    same_as_conclusion = bool(vision_watermark) and bool(conclusion) and (
        _handle_key(vision_watermark) == _handle_key(conclusion)
    )
    skip_mark = (
        bool(vision_watermark)
        and _squash_handle(vision_watermark) == _squash_handle(grok_watermark)
        and not same_as_conclusion
    )
    grok_lines = _grok_lines(grok, skip_mark=skip_mark)
    # 被降级的那条不能当结论：视觉挑中的落点不是原帖、搬运页上确实有这张图但那
    # 是转载、命中的是别人在求图的帖子，三种都算「没找到」。降级原因和链接由下面
    # 的未找到分支统一给出。
    downgraded = best is not None and bool(downgrade_kind(best))
    solid = best is not None and not downgraded
    # 帖子档高于视觉档：帖子能点开对照，视觉只是模型判断，所以先给帖子。
    if grok.get("tier") == "post" and not skip_grok:
        if best is not None and best.status == "vision":
            grok_lines.append("视觉复核也认为是同一个人（模型判断，仅供参考），那张候选见参考区。")
        return "\n".join(grok_lines)
    if not solid and grok_lines and not skip_grok:
        # 只拿到账号：比「完全没找到」有用，但没有帖子可对照，仍然算线索。
        vision = outcome.vision or {}
        if vision.get("ok") and vision.get("checked"):
            if vision.get("downgraded"):
                grok_lines.append(_vision_downgrade_line(outcome, vision))
                reason_line = _vision_reason(vision)
                if reason_line:
                    grok_lines.append(reason_line)
                pick_line = _vision_pick_line(outcome)
                if pick_line:
                    grok_lines.append(pick_line)
            else:
                grok_lines.append(f"视觉复核了 {vision['checked']} 张候选，没有发现同一个人。")
        elif vision.get("error"):
            grok_lines.append(f"视觉复核没做完：{vision['error']}")
        clue = _downgraded_pick_line(outcome)
        if clue:
            grok_lines.append(clue)
        return "\n".join(grok_lines)
    if not solid:
        compared = sum(1 for item in outcome.candidates if item.distance is not None)
        nearest = min(
            (item for item in outcome.candidates if item.distance is not None),
            key=lambda item: item.distance or 0,
            default=None,
        )
        # 降级的条目上面已经单独说明并给了链接，不再算进「同角色或同款服装」——
        # 同一批图不能既说「像同一个人」又说「不是这张照片」。
        suspects = [
            item
            for item in outcome.candidates
            if item.status == "suspect"
            and not is_avatar_hit(item)
            and not downgrade_kind(item)
        ]
        avatars = [
            item
            for item in outcome.candidates
            if item.status == "suspect" and is_avatar_hit(item)
        ]
        # 落点是别的网站转载时，得把「只能找到转发」这件事说明白：单写一句「未找到
        # 可靠出处」，群里会以为连链接都没有（s-20261112-1244-7c22）。两句说的是同一件
        # 事，合成一行，别在群里摆两行重复的话。
        reposted = any(downgrade_kind(item) == "repost" for item in outcome.candidates)
        aggregated = any(downgrade_kind(item) == "aggregator" for item in outcome.candidates)
        lines = [
            "⚠️ 未找到可靠出处：只找到内容号发的同款，证明不了是本人。"
            if aggregated
            else (
                "⚠️ 未找到可靠出处：只找到别的网站的转发，没能找到原始出处。"
                if reposted
                else "⚠️ 未找到可靠出处"
            )
        ]
        # 读到 pixiv 作品号时，出处其实已经有了：转载站那一条只是「这张图也被搬过」。
        # 这句话要顶到最前面，别让「未找到」把真正的出处盖住。
        pixiv_lines = _pixiv_source_lines(outcome)
        if pixiv_lines:
            lines = pixiv_lines
        if compared and nearest is not None:
            if nearest.status == "suspect":
                # 距离够近但被降级（转发页、头像这类）：不能说「均未通过阈值」，
                # 那会跟下面的「指纹距离 0」自相矛盾。
                lines.append(
                    f"已比对 {compared} 条候选，最近的一条指纹距离 {nearest.distance}，"
                    f"但那条不能当原帖（原因见下）。"
                )
            else:
                lines.append(
                    f"已比对 {compared} 条候选，最近的一条指纹距离 {nearest.distance}，都没到阈值。"
                )
        elif compared:
            lines.append(f"已比对 {compared} 条候选，都没到阈值。")
        else:
            lines.append("候选图都没能取到或解码，无法验证。")
        search_page_line = _search_page_line(outcome)
        if search_page_line:
            lines.append(search_page_line)
        # 上一句说「原因见下」，这里就得真给出来：搬运页/求图帖那条连同链接一起摆。
        clue = _downgraded_pick_line(outcome)
        if clue:
            lines.append(clue)
        forum_line = _forum_thread_line(outcome)
        if forum_line:
            lines.append(forum_line)
        vision = outcome.vision or {}
        if vision.get("ok") and vision.get("checked"):
            if vision.get("downgraded"):
                lines.append(_vision_downgrade_line(outcome, vision))
                reason_line = _vision_reason(vision)
                if reason_line:
                    lines.append(reason_line)
                pick_line = _vision_pick_line(outcome)
                if pick_line:
                    lines.append(pick_line)
            else:
                lines.append(f"视觉复核了 {vision['checked']} 张候选，没有发现同一个人。")
        elif vision.get("error"):
            lines.append(f"视觉复核没做完：{vision['error']}")
        if grok and not skip_grok:
            if grok.get("error") and grok.get("tier") != "none":
                lines.append(f"Grok 找人没做完：{grok['error']}")
            elif grok.get("error"):
                lines.append(f"Grok 找人没做成：{grok['error']}")
            else:
                lines.append(GROK_QUIET_NOTE)
        if avatars:
            sites = "、".join(dict.fromkeys(item.platform for item in avatars if item.platform))
            where = f"（{sites}）" if sites else ""
            lines.append(
                f"另有 {len(avatars)} 条是站点账号头像{where}：指纹对上了，但那是头像不是原帖，点开页面通常看不到这张图。"
            )
        if suspects:
            # 「疑似同图」会把 20 多距离的照骗说成同一张：实际多是同角色、同款服装
            # 的另一个人，写清楚免得以为快找到了。
            low = min((item.distance for item in suspects if item.distance is not None), default=None)
            high = max((item.distance for item in suspects if item.distance is not None), default=None)
            gap = f"（指纹距离 {low}～{high}）" if low is not None and high is not None and low != high else ""
            if gap:
                lines.append(
                    f"另有 {len(suspects)} 条是同角色或同款服装的图{gap}，不是这张照片本身，见参考区。"
                )
            else:
                lines.append(f"另有 {len(suspects)} 条是同角色或同款服装的图，不是这张照片本身，见参考区。")
        if outcome.total_candidates:
            lines.append(f"共收到 {outcome.total_candidates} 条结果，见参考区。")
        # 候选落在付费图包页时点一句：这类图公开索引里本来就没有原帖。
        paid_pack = _paid_pack_line(outcome)
        if paid_pack:
            lines.append(paid_pack)
        return "\n".join(lines)
    lines = _identity_lines(best, detail=detail)
    leak_line = _leak_line(best)
    if leak_line:
        lines.append(leak_line)
    if best.status == "vision":
        # 视觉判的是「同一个人」，不是同一张图，别让用户以为这就是那张原图。
        lines.insert(0, "🧠 没找到同一张图，但视觉复核认为是同一个人")
        reason_line = _vision_reason(outcome.vision)
        if reason_line:
            lines.append(reason_line)
        lines.append("⚠️ 这条是视觉模型挑的，不是指纹比对，可能认错，请自行核对。")
    if grok_lines:
        lines.append("—")
        lines.extend(grok_lines)
    return "\n".join(lines)


ENGINE_LABELS = {
    "lens": "Google Lens",
    "yandex": "Yandex",
    "baidu": "百度识图",
    "saucenao": "SauceNAO",
    "watermark": "图上水印核对",
    "identity": "按名字换到 X 核对",
    "repost_author": "搬运站转贴反查",
    "author": "图上水印 + 标题署名",
}


def _engine_name(engine: str) -> str:
    return ENGINE_LABELS.get(engine, engine or "未知引擎")


def _host_of(url: str) -> str:
    """从链接里取域名，候选没带 platform 时用它顶一下。"""
    value = (url or "").strip()
    return urlparse(value).netloc.lower() if "://" in value else ""


def _candidate_identity(item: Candidate) -> str:
    """没下载比对过、或指纹明确不符的候选，账号并不是原作者，不能当结论发出去。"""
    if item.status in {"unverified", "mismatch"}:
        return "（账号未确认）"
    return item.handle or item.name or "（未识别）"


def _candidate_text(item: Candidate, index: int, short: bool = False) -> str:
    tag = f"[{item.platform}]"
    identity = _candidate_identity(item)
    status = _short_status(item) if short else (item.note or "未验证")
    lines = [f"{index}. {tag} {identity} · {status}"]
    if item.url:
        lines.append(f"   {item.url}")
    return "\n".join(lines)


def _short_status(item: Candidate) -> str:
    """一条候选只用一句话交代：距离多少、是不是同一张，别把长备注抄进来。"""
    gap = f"（距离 {item.distance}）" if item.distance is not None else ""
    if item.status == "match":
        return f"指纹一致{gap}"
    if item.status == "mismatch":
        return f"不是同一张{gap}"
    if item.status == "suspect":
        if item.distance is None and item.similarity is not None:
            return f"相似度 {item.similarity}%"
        return f"同款或同角色的另一张{gap}"
    if item.status == "unverified":
        return "图没下到，没验证"
    return item.note or "未验证"


def engine_groups(
    outcome: SearchOutcome,
    best: Candidate | None,
    limit: int,
) -> list[tuple[str, list[Candidate]]]:
    """按引擎把候选分组，组内按指纹距离从近到远排；limit 是每个引擎最多列几条。

    组内排序按「距离优先、其次相似度」，这样每组第一条就是该引擎最接近的那张，
    配图时取它，用户看到的也就是最接近的那条。
    """
    groups: dict[str, list[Candidate]] = {}
    for item in outcome.candidates:
        # 视觉挑中的那条也要留在参考区，结论里「那张候选见参考区」才对得上号；
        # 指纹命中时 best 已经在结论里给过，不必重复列。
        if item is best and (best is None or best.status != "vision"):
            continue
        # 电商同款不进参考区：838e 那单是只可莉背包，参考区被五条卖包的页面占满，
        # 看着像淘宝搜索结果。挡了多少条记在 trace 的「商品页」那一步。
        if gallery.is_shop_site(item.url or "", item.platform or ""):
            continue
        groups.setdefault(item.engine, []).append(item)
    result: list[tuple[str, list[Candidate]]] = []
    for engine, items in groups.items():
        ordered = sorted(
            items,
            key=lambda item: (
                item.distance if item.distance is not None else 999,
                -(item.similarity or 0),
            ),
        )
        # 同一个页面上的多张图会各算一条候选（Lens 常这么给），参考区里连着摆
        # 两行一模一样的链接，看着像搜重了。同一 URL 只留最接近的那条。
        unique: list[Candidate] = []
        seen: set[str] = set()
        for item in ordered:
            key = _bare_url(item.url or "") or (item.image_url or "")
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            unique.append(item)
        result.append((engine, unique[: max(1, limit)]))
    return result


def candidate_image(outcome: SearchOutcome, item: Candidate) -> str:
    """取候选的展示图。

    参考区里每个引擎都要有「自己最接近的那张」的配图，所以即使这张已经当过
    顶部的参考图，这里也照样返回；否则那个引擎的列表看起来就像没配上图。
    """
    if not item.image_url:
        return ""
    return (outcome.images or {}).get(item.image_url, "")


def _header_text(outcome: SearchOutcome, command: str, replay: bool, header_name: str = "") -> str:
    # 页眉上那个名字用「转发昵称」配置项，不是触发指令：生产里指令是「盆地找鸡」，
    # 昵称才是「盆地寻人」。页脚里的「回复「xx 更多」」仍旧必须用真指令，照抄才能触发。
    label = (header_name or "").strip() or command
    header = f"🔍 {label}结果"
    if outcome.status == "error":
        header = f"🔍 {label} · 搜索失败"
    if replay:
        header = f"🔍 {label}结果 · 全部候选"
    if outcome.ai and outcome.ai.get("ok"):
        try:
            if float(outcome.ai.get("score") or 0) >= AI_FLAG_THRESHOLD:
                header = f"🔍 {label} · 疑似 AI 生成"
        except (TypeError, ValueError):
            pass
    if outcome.cached:
        header += " · 缓存"
    return header


def ttl_text(minutes: int) -> str:
    """把保留时长写成人话：1440 分钟读起来太费劲。"""
    if minutes >= 1440 and minutes % 1440 == 0:
        return f"{minutes // 1440} 天"
    if minutes >= 60:
        hours = minutes / 60
        return f"{hours:.0f} 小时" if hours == int(hours) else f"{hours:.1f} 小时"
    return f"{minutes} 分钟"


# 搜图耗时提示。最近 14 单实测：指纹直接命中的中位数 31 秒，要跑视觉/Grok 兜底的
# 中位数 86 秒、最慢 373 秒，所以文案先说 1 分钟，再交代难找时的上限。
ETA_FAST = "约 1 分钟"
ETA_SLOW_NOTE = "如果这图比较难找，会需要 2 到 6 分钟"
# 超过这个时长还没出结果，补一条进度提示，别让人对着空气等六分钟。
SLOW_PING_SECONDS = 180
SLOW_PING_TEXT = "这图有点难找，我需要细细对比，再稍等下哦。"
# 回复页反查（顺着 X 回复去摸原帖）跑起来时补一句，免得这几十秒看着像卡住。
# 预算 60 秒，最坏两轮 2 分钟，按这个写。
TRACE_PING_TEXT = "已经找到蛛丝马迹了，正在顺藤摸瓜找原帖，大概还要 1 分钟（最多 2 分钟）。"
# Grok 跑过但空手的交代。只该出现一次：未找到分支里写过之后，_best_block 末尾
# 不能再补一句同义的话（实测 s-20261111-0847-24c4 两行连着说同一件事）。
GROK_QUIET_NOTE = "Grok 在 X 上搜过，没有找到对应的账号或帖子。"


def eta_full() -> str:
    """聊天里的一句话耗时交代。"""
    return f"{ETA_FAST}，{ETA_SLOW_NOTE}"



def _reference_distance(outcome: SearchOutcome, source: str) -> int | None:
    """参考图跟原图的指纹距离。

    先按图片地址在候选里找；Grok 参考图不在候选里时，用它核对时记下的最近
    距离。取不到就返回 None，文案里就不写距离。
    """
    if not source:
        return None
    for item in outcome.candidates:
        if item.image_url and item.image_url == source and item.distance is not None:
            return item.distance
    grok = outcome.grok or {}
    if str(grok.get("reference_url") or "") == source:
        distance = grok.get("distance")
        if isinstance(distance, int) and distance >= 0:
            return distance
    return None


def reference_kind(outcome: SearchOutcome, source: str) -> str:
    """参考图是哪来的：vision（视觉方案挑的）/ grok（Grok 帖子配图）/ candidate。

    群里看到一张图总得知道它是谁给的，否则容易把「视觉挑的」当成「引擎搜到的」。
    """
    if not source:
        return "candidate"
    if source == "upload":
        return "upload"
    grok_ref = str((outcome.grok or {}).get("reference_url") or "")
    if grok_ref and source == grok_ref:
        return "grok"
    picked = (outcome.vision or {}).get("picked") or []
    if source in picked:
        return "vision"
    # 视觉挑中、但落点被降级成疑似的那些也记在 picked_images 里；参考图取自它们时
    # 同样要标「视觉方案挑中的那张」，别让人以为是引擎按距离挑的。
    if source in ((outcome.vision or {}).get("picked_images") or []):
        return "vision"
    return "candidate"


def _distance_phrase(distance: int | None) -> str:
    """把距离写成一句话：0 就是同一张，不能说成「不是同一张」。"""
    if distance is None:
        return "未经指纹确认"
    if distance == 0:
        return "跟原图是同一张（指纹距离 0）"
    return f"指纹距离 {distance}，不是同一张"


def _is_avatar_source(outcome: SearchOutcome, source: str) -> bool:
    """参考图是不是某条头像命中——这种图跟原帖无关，说法要单独写。"""
    if is_avatar_image(source):
        return True
    return any(
        item.image_url and item.image_url == source and is_avatar_hit(item)
        for item in outcome.candidates
    )


def _reference_hint(outcome: SearchOutcome, source: str) -> str:
    """参考图的标题：点明来源，并标出跟原图的指纹距离。"""
    distance = _reference_distance(outcome, source)
    gap = _distance_phrase(distance)
    kind = reference_kind(outcome, source)
    if kind == "upload":
        return "📋 参考图（没有可用的候选图，这张是你发的原图，没得对照）"
    if _is_avatar_source(outcome, source):
        return f"📋 参考图（这张是站点账号头像，{gap}，页面里看不到它，也不是原帖）"
    if kind == "grok":
        return f"📋 参考图（Grok 帖子配图里跟原图最接近的一张，{gap}）"
    if kind == "vision":
        return f"📋 参考图（视觉方案挑中的那张，{gap}）"
    return (
        f"📋 参考图（引擎候选里跟原图最接近的一张，{gap}；"
        "不是视觉方案挑的，也不是 Grok 给的）"
    )


def _footer_lines(outcome: SearchOutcome, command: str, ttl_minutes: int, replay: bool, task_id: str = "", reference_note: bool = True) -> list[str]:
    # 「怎么找的」只在「更多」里写：首次回复给了结论就走，路线与 AI 检测属于详情。
    lines: list[str] = [_route_line(outcome), _ai_line(outcome.ai)] if replay else []
    if outcome.cached:
        # 说清这是复用，不是刚跑出来的：同一张图结果一样，但引擎这次一条都没调。
        lines.append("💾 这张图之前搜过，直接复用了当时的结果（引擎没有重新跑）")
    if reference_note and outcome.image_b64 and outcome.image_source == "upload":
        lines.append("（没有可用的候选图，转发里的图是你发的原图）")
    elif reference_note and outcome.image_b64 and outcome.best is None:
        phrase = _distance_phrase(_reference_distance(outcome, outcome.image_source))
        kind = reference_kind(outcome, outcome.image_source)
        if _is_avatar_source(outcome, outcome.image_source):
            lines.append(f"（参考图是站点账号头像，{phrase}，页面里看不到它）")
        elif kind == "grok":
            lines.append(f"（参考图是 Grok 给的帖子里跟原图最接近的一张，{phrase}）")
        elif kind == "vision":
            lines.append(f"（参考图是视觉方案挑中的那张，{phrase}）")
        else:
            lines.append(f"（参考区那张是候选里跟原图最接近的一张，{phrase}）")
    if outcome.errors:
        failed = "、".join(f"{name}（{message[:40]}）" for name, message in outcome.errors.items())
        lines.append(f"⚠️ 未返回结果的引擎：{failed}")
    lines.append("")
    if replay:
        lines.append("以上为全部参考结果")
    else:
        if task_id:
            # 编号是「更多」的凭据：群里同时有好几次搜索时，光说「更多」会取错。
            lines.append(f"任务编号：{task_id}（保留 {ttl_text(ttl_minutes)}）")
            lines.append(f"回复「{command} 更多 {task_id}」查看全部结果")
        else:
            lines.append(f"{ttl_text(ttl_minutes)}内回复「{command} 更多」可查看全部结果")
        # 缓存命中时结果会秒回，给个明确的重新搜入口，免得以为只能等缓存过期。
        lines.append(f"重新搜一遍（忽略缓存）：发图时带上「{command} 重搜」")
    return lines


def _footer_needs_upload(outcome: SearchOutcome, replay: bool) -> bool:
    """页脚要不要补那张原图。"""
    return bool(not replay and outcome.upload_b64 and outcome.image_source != "upload")


def _footer_content(outcome: SearchOutcome, lines: list[str], replay: bool) -> list[dict]:
    """页脚节点的内容：文字之后补一张本次搜索的原图。

    群里一次连着好几单时，只看结论分不清这堆结果对应哪张图，所以把原图贴在
    合并转发的最末尾。三种情况不补：
    - 回「更多」（replay）：原图在首次回复里已经出现过，再发一遍只会更乱；
    - 旧任务或按图缓存：原图不落盘，字段是空的；
    - 没有可用候选图时（image_source == upload）：参考区那张本来就是原图。
    """
    content: list[dict] = [{"type": "text", "content": "\n".join(lines)}]
    if not _footer_needs_upload(outcome, replay):
        return content
    content.append({"type": "text", "content": "🖼 本次搜索的原图（用来对照上面的结果）"})
    content.append({"type": "image", "content": outcome.upload_b64})
    return content


def build_forward_nodes(
    task: SearchTask,
    *,
    command: str,
    show_reference: bool = True,
    limit: int = 8,
    nickname: str = "麦麦寻人",
    header_name: str = "",
    ttl_minutes: int = 15,
    replay: bool = False,
    user_id: str = "",
) -> list[dict]:
    """生成合并转发节点：标题、最佳匹配、按引擎分组的参考区、页脚。

    limit 是每个引擎最多列几条。图片由调用方预先塞进 outcome.images
    （键为候选图地址），首次回复只放每个引擎最接近的那张，回「更多」时放全部。
    user_id 会写进转发节点的 uin。QQ 端要求 uin 非空，留空会出现
    「消息异常」或只剩一个 [聊天记录] 占位，所以调用方要把机器人账号传进来。
    header_name 是页眉上显示的名字（传转发昵称）；留空时退回 command。
    """
    outcome = task.outcome or SearchOutcome(status="error")
    nodes: list[dict] = []

    def node(content: list[dict]) -> dict:
        item = {"nickname": nickname, "content": content}
        if user_id:
            item["user_id"] = user_id
        return item

    nodes.append(
        node(
            [
                {
                    "type": "text",
                    "content": f"{_header_text(outcome, command, replay, header_name)}\n{_best_block(outcome, detail=replay)}",
                }
            ]
        )
    )
    if outcome.image_b64 and outcome.best is not None:
        nodes[-1]["content"].append({"type": "image", "content": outcome.image_b64})

    # 视觉和 Grok 都挑出了东西时，两张图并排给：结论里一句「Grok 那边也有个号」
    # 不如图直接摆出来好判断。
    if outcome.grok_image_b64:
        grok_gap = (outcome.grok or {}).get("distance")
        gap_text = f"，指纹距离 {grok_gap}" if isinstance(grok_gap, int) and grok_gap >= 0 else ""
        nodes.append(
            node(
                [
                    {"type": "text", "content": f"📋 Grok 帖子配图里跟原图最接近的一张{gap_text}（配图非同图，仅供参考）"},
                    {"type": "image", "content": outcome.grok_image_b64},
                ]
            )
        )

    # 没找到可靠出处时，先单独给一张参考图，方便用户自己核对是不是同一张。
    if outcome.best is None and outcome.image_b64:
        hint = _reference_hint(outcome, outcome.image_source)
        nodes.append(
            node(
                [
                    {"type": "text", "content": hint},
                    {"type": "image", "content": outcome.image_b64},
                ]
            )
        )

    # 参考区按引擎分组，每个引擎一个节点，组内候选顺序沿用排序结果。
    # 命中时（指纹、视觉、Grok 任一）首次回复只给结论，参考区留给「更多」：
    # 一次搜到结果还要刷四五条转发，群里看着比没找到更烦。
    if show_reference and outcome.status != "ok":
        for engine, items in engine_groups(outcome, outcome.best, limit):
            content: list[dict] = [
                {"type": "text", "content": f"▍{_engine_name(engine)}（{len(items)} 条）"}
            ]
            for index, item in enumerate(items, start=1):
                content.append({"type": "text", "content": _candidate_text(item, index)})
                image = candidate_image(outcome, item)
                if image:
                    content.append({"type": "image", "content": image})
            nodes.append(node(content))

    nodes.append(
        node(
            _footer_content(
                outcome,
                _footer_lines(outcome, command, ttl_minutes, replay, task.task_id),
                replay,
            )
        )
    )

    return nodes


def _node(content: list[dict], nickname: str, user_id: str) -> dict:
    item: dict = {"nickname": nickname, "content": content}
    if user_id:
        item["user_id"] = user_id
    return item


def _grok_miss_line(outcome: SearchOutcome) -> str:
    """Grok 跑了但没找到时给一句交代。

    没有这句时，「没触发」和「触发了没结果」在群里看起来一模一样。
    """
    grok = outcome.grok or {}
    if grok.get("tier") != "none" or not grok.get("ok"):
        return ""
    return "🔎 Grok 也拿这张图去 X 搜过，没撞见原帖。"


def _vision_block(outcome: SearchOutcome) -> list[dict] | None:
    """视觉复核命中时单独一块：结论 + 它挑中的那张图。"""
    best = outcome.best
    if best is None or best.status != "vision":
        return None
    lines = ["🧠 没找到同一张图，但视觉复核认为是同一个人", *_identity_lines(best)]
    lines.append("⚠️ 这条是视觉模型挑的，不是指纹比对，可能认错，请自行核对。")
    miss = _grok_miss_line(outcome)
    if miss:
        lines.append(miss)
    lines.extend(_same_person_lines(outcome))
    lines.extend(_x_identity_lines(outcome))
    content: list[dict] = [{"type": "text", "content": "\n".join(lines)}]
    if outcome.image_b64:
        content.append({"type": "image", "content": outcome.image_b64})
    return content


def _grok_block(outcome: SearchOutcome) -> list[dict] | None:
    """Grok 有结论时单独一块：账号/帖子 + 帖子配图里最接近的一张。"""
    grok = outcome.grok or {}
    if grok.get("tier") not in {"post", "account"}:
        return None
    lines = ["🔎 Grok 核对（X）", *_grok_lines(grok)]
    best = outcome.best
    if best is not None and best.status == "vision":
        lines.append("👀 视觉复核也挑了一条，见上一条。")
    content: list[dict] = [{"type": "text", "content": "\n".join(lines)}]

    grok_ref = str(grok.get("reference_url") or "")
    image = ""
    hint = ""
    distance: int | None = None
    if outcome.grok_image_b64:
        image = outcome.grok_image_b64
        hint = "📋 Grok 帖子配图里跟原图最接近的一张"
        gap = grok.get("distance")
        if isinstance(gap, int) and gap >= 0:
            distance = gap
    elif outcome.image_b64 and grok_ref and outcome.image_source == grok_ref:
        image = outcome.image_b64
        hint = "📋 参考图（Grok 帖子配图里跟原图最接近的一张）"
        distance = _reference_distance(outcome, grok_ref)

    if image:
        gap_text = f"指纹距离 {distance}，" if distance is not None else ""
        content.append({"type": "text", "content": f"{hint}（{gap_text}不是同一张）"})
        content.append({"type": "image", "content": image})
    return content


def _tail_nodes(
    task: SearchTask,
    outcome: SearchOutcome,
    *,
    command: str,
    show_reference: bool,
    limit: int,
    nickname: str,
    ttl_minutes: int,
    replay: bool,
    user_id: str,
    grok_taken: bool,
) -> list[dict]:
    """最后一条：没找到时给结论、参考图和参考区，然后统一收尾。"""
    nodes: list[dict] = []
    if outcome.best is None:
        grok = outcome.grok or {}
        if grok_taken and grok.get("tier") == "post":
            # 原帖已经在 Grok 那块给出来了，这里再写「未找到可靠出处」等于自相矛盾。
            nodes.append(
                _node(
                    [{"type": "text", "content": "🔗 找到的原帖和依据见上一条，下面是参考图与候选。"}],
                    nickname,
                    user_id,
                )
            )
        else:
            # Grok 已经单独占一条时，这里不要再重复它的结论。
            nodes.append(
                _node(
                    [{"type": "text", "content": _best_block(outcome, skip_grok=grok_taken)}],
                    nickname,
                    user_id,
                )
            )
        grok_ref = str((outcome.grok or {}).get("reference_url") or "")
        grok_gave_image = grok_taken and outcome.image_source == grok_ref
        if outcome.image_b64 and not grok_gave_image:
            hint = _reference_hint(outcome, outcome.image_source)
            nodes.append(
                _node(
                    [
                        {"type": "text", "content": hint},
                        {"type": "image", "content": outcome.image_b64},
                    ],
                    nickname,
                    user_id,
                )
            )
        if show_reference:
            for engine, items in engine_groups(outcome, outcome.best, limit):
                content: list[dict] = [
                    {"type": "text", "content": f"▍{_engine_name(engine)}（{len(items)} 条）"}
                ]
                for index, item in enumerate(items, start=1):
                    content.append({"type": "text", "content": _candidate_text(item, index)})
                    image = candidate_image(outcome, item)
                    if image:
                        content.append({"type": "image", "content": image})
                nodes.append(_node(content, nickname, user_id))

    nodes.append(
        _node(
            _footer_content(
                outcome,
                _footer_lines(outcome, command, ttl_minutes, replay, task.task_id),
                replay,
            ),
            nickname,
            user_id,
        )
    )
    return nodes


def count_forward_images(nodes: list[dict]) -> int:
    """数一条合并转发里有几张图。

    NapCat 单条转发实测 6 张能过、13 张失败，发送层按这个数决定是合并成
    一条还是拆开发。
    """
    return sum(
        1
        for item in nodes
        for part in item.get("content") or []
        if isinstance(part, dict) and part.get("type") == "image"
    )


# Grok 调过哪些工具，说给人听时用中文：原始工具名是 x_user_search 这种。
_GROK_TOOL_NAMES = {
    "x_user_search": "用户搜索",
    "x_semantic_search": "语义搜索",
    "x_keyword_search": "关键词搜索",
    "x_post_search": "帖子搜索",
}


def _grok_tool_text(tools: list) -> str:
    names: list[str] = []
    for tool in tools:
        name = _GROK_TOOL_NAMES.get(str(tool), str(tool))
        if name and name not in names:
            names.append(name)
    return "、".join(names)


def _pick_line(item: Candidate) -> str:
    """「最像的那条」：一行说明 + 一行链接，别再抄一遍候选里那句长备注。"""
    where = item.platform or _host_of(item.url)
    identity = _candidate_identity(item)
    gap = f"（指纹距离 {item.distance}）" if item.distance is not None else ""
    line = f"👉 {identity} · {where}{gap}" if where else f"👉 {identity}{gap}"
    if item.url:
        line += f"\n   {item.url}"
    return line


def _miss_headline(outcome: SearchOutcome) -> str:
    """没找到时的第一句：先说结论，再说比了多少条、最近的那条是什么。"""
    lines = ["⚠️ 没搜到：公开索引里没有这张图的原帖"]
    counts = outcome.engine_counts or {}
    parts = [f"{_engine_name(name)} {value}" for name, value in counts.items() if isinstance(value, int) and value]
    raw = sum(value for value in counts.values() if isinstance(value, int))
    total = outcome.total_candidates or len(outcome.candidates)
    detail: list[str] = []
    if parts:
        detail.append(" / ".join(parts))
    if raw and total and raw != total:
        detail.append(f"共 {raw} 条原始结果、去重后 {total} 条")
    elif total:
        detail.append(f"共 {total} 条结果")
    compared = [item for item in outcome.candidates if item.distance is not None]
    nearest = min(compared, key=lambda item: item.distance or 0, default=None)
    if nearest is not None:
        where = nearest.platform or _host_of(nearest.url)
        gap = f"距离 {nearest.distance}"
        bits = f"最近的一条是 {where}（{gap}）" if where else f"最近的一条（{gap}）"
        detail.append(bits + "，同款或同角色的另一张，不是这张图")
    if detail:
        lines.append("；".join(detail) + "。")
    return "\n".join(lines)


def _vision_miss_entry(outcome: SearchOutcome) -> tuple[str, Candidate | None]:
    """没找到时视觉那一条：挑了哪张、给的理由、单图。"""
    vision = outcome.vision or {}
    checked = int(vision.get("checked") or 0)
    if not vision.get("ok"):
        error = str(vision.get("error") or "").strip()
        return (f"🧠 视觉复核没做完：{error}" if error else ""), None
    if not checked:
        return "", None
    picked = [str(url) for url in (vision.get("picked_images") or []) if url]
    by_image = {item.image_url: item for item in outcome.candidates if item.image_url}
    pick = next((by_image[url] for url in picked if url in by_image), None)
    if pick is None:
        pick = next((item for item in outcome.candidates if downgrade_kind(item) == "vision"), None)
    if picked:
        head = f"🧠 视觉复核：把 {checked} 张候选拼成一张对照图逐格看，挑了 {len(picked)} 条"
    else:
        head = f"🧠 视觉复核：把 {checked} 张候选拼成一张对照图逐格看，没有一条对得上"
    lines = [head]
    reason = str(vision.get("reason") or "").strip()
    if reason:
        lines.append(f"它给的理由：{reason}（照着服装、发型判的，不一定准）")
    return "\n".join(lines), pick


def _grok_miss_entry(outcome: SearchOutcome) -> str:
    """没找到时 Grok 那一条：它换了哪些搜法、核了几条帖子、最近差多少。"""
    grok = outcome.grok or {}
    if not grok.get("ok") or grok.get("tier") == "none":
        error = str(grok.get("error") or "").strip()
        return f"🔎 Grok 找人没做完：{error}" if error else ""
    hints = int(grok.get("hints") or 0)
    tools = _grok_tool_text(list(grok.get("tools") or []))
    checked = int(grok.get("checked_posts") or 0)
    distance = grok.get("distance")
    bits: list[str] = []
    if hints and tools:
        bits.append(f"在 X 上换了 {hints} 组关键词搜（{tools}）")
    elif hints:
        bits.append(f"在 X 上换了 {hints} 组关键词搜")
    else:
        bits.append("在 X 上搜过")
    if checked:
        bits.append(f"逐条核了 {checked} 条帖子")
    if isinstance(distance, int) and distance >= 0:
        bits.append(f"最近的一条配图距离 {distance}")
    lines = ["🔎 Grok：" + "，".join(bits) + "，都不是这张"]
    handle = str(grok.get("handle") or "").strip()
    post = str(grok.get("post_url") or "").strip()
    if handle and post:
        lines.append(f"👉 @{handle}：{post}")
    elif handle:
        lines.append(f"👉 @{handle}：https://x.com/{handle}")
    return "\n".join(lines)


def is_miss_layout(outcome: SearchOutcome) -> bool:
    """没搜到、而且也没有可靠结论时走「一条条摆出来」的结构。

    Grok 已经给出原帖（tier=post）时不走这里：那种情况有真结论，
    再写「没搜到」就是自相矛盾。
    """
    if outcome.status == "error" or outcome.best is not None:
        return False
    grok = outcome.grok or {}
    return grok.get("tier") != "post"


def build_miss_nodes(
    task: SearchTask,
    *,
    command: str,
    nickname: str,
    ttl_minutes: int,
    replay: bool,
    user_id: str,
) -> list[dict]:
    """没找到时的转发：结论打头，接着每个引擎一条、视觉一条、Grok 一条。

    用户 2026-11-14 的要求：什么都没搜到的时候，先直说没搜到，别把线索摆前头；
    每个引擎只给最近的那一条并配图，视觉和 Grok 有结果也各占一条，还要说清
    它们各自做了什么。
    """
    outcome = task.outcome or SearchOutcome(status="error")
    # 同一张图重搜时两次的首行可能一字不差，QQ 会按首行判成重复内容：消息体留在
    # 服务端，客户端却整条不渲染（s-20261115-2237-0365 的 22:20 与 22:43 两次，
    # 电脑端和手机端都看不见）。前面挂上任务编号，保证每张卡片的开头都不一样。
    headline = f"[{task.task_id}] {_miss_headline(outcome)}"
    # 用户要求：没搜到的单子也把「AI 生成概率」直接摆在结论里，别让人回「更多」才看得到。
    # 只多这一行——匹配路线、耗时这些仍然只留在「更多」。检测没跑或失败时不写。
    first: list[dict] = [{"type": "text", "content": headline}]
    # 求助帖里读到的出处要摆在最前面：这是「再搜一次也搜不到」时最有用的东西。
    for line in _help_post_lines(outcome):
        first.append({"type": "text", "content": line})
    ai_probability = _ai_probability_line(outcome.ai)
    if ai_probability:
        first.append({"type": "text", "content": ai_probability})
    # 候选全落在付费图包 / 打包图站时，得说清这类图本来就没有公开原帖——
    # 否则看着像「再搜一次就能搜到」。
    paid_pack = _paid_pack_line(outcome)
    if paid_pack:
        first.append({"type": "text", "content": paid_pack})
    # 没搜到的时候，候选里那些账号线索更该给出来——人可能就在那儿，只是图没核上。
    for line in _other_accounts_lines(outcome):
        first.append({"type": "text", "content": line})
    nodes: list[dict] = [_node(first, nickname, user_id)]
    shown: set[str] = set()
    shown_urls: dict[str, str] = {}

    order = {name: index for index, name in enumerate(ENGINE_LABELS)}
    groups = sorted(
        engine_groups(outcome, outcome.best, 1),
        key=lambda pair: order.get(pair[0], len(order)),
    )
    for engine, items in groups:
        total = (outcome.engine_counts or {}).get(engine)
        head = f"▍{_engine_name(engine)}：{total} 条里最近的一条" if total else f"▍{_engine_name(engine)}"
        content: list[dict] = [{"type": "text", "content": head}]
        for index, item in enumerate(items, start=1):
            content.append({"type": "text", "content": _candidate_text(item, index, short=True)})
            image = candidate_image(outcome, item)
            if image:
                content.append({"type": "image", "content": image})
                shown.add(item.image_url)
            if item.url:
                shown_urls.setdefault(_bare_url(item.url), _engine_name(engine))
        nodes.append(_node(content, nickname, user_id))

    vision_text, pick = _vision_miss_entry(outcome)
    if vision_text:
        if pick is not None:
            owner = shown_urls.get(_bare_url(pick.url or "")) if pick.url else ""
            gap = f"（距离 {pick.distance}）" if pick.distance is not None else ""
            if owner:
                vision_text += f"\n👉 最像的就是上面 ▍{owner} 那条{gap}"
            else:
                vision_text += "\n" + _pick_line(pick)
        content = [{"type": "text", "content": vision_text}]
        image = candidate_image(outcome, pick) if pick is not None else ""
        if image and pick.image_url not in shown:
            content.append({"type": "image", "content": image})
            shown.add(pick.image_url)
        nodes.append(_node(content, nickname, user_id))

    grok_text = _grok_miss_entry(outcome)
    if grok_text:
        content = [{"type": "text", "content": grok_text}]
        # Grok 只给到账号时不会单独存 grok_image_b64，但它核对过的那张配图已经
        # 存在 image_b64 里（image_source 指向它就是这张）。不补这一张，Grok 那条
        # 就只剩文字。
        grok_image = outcome.grok_image_b64
        grok_ref = str((outcome.grok or {}).get("reference_url") or "")
        if (
            not grok_image
            and outcome.image_b64
            and grok_ref
            and outcome.image_source == grok_ref
            and grok_ref not in shown
        ):
            grok_image = outcome.image_b64
        if grok_image:
            content.append({"type": "image", "content": grok_image})
        nodes.append(_node(content, nickname, user_id))

    nodes.append(
        _node(
            _footer_content(
                outcome,
                _footer_lines(
                    outcome,
                    command,
                    ttl_minutes,
                    replay,
                    task.task_id,
                    reference_note=False,
                ),
                replay,
            ),
            nickname,
            user_id,
        )
    )
    return nodes


def build_result_forwards(
    task: SearchTask,
    *,
    command: str,
    show_reference: bool = True,
    limit: int = 8,
    nickname: str = "麦麦寻人",
    header_name: str = "",
    ttl_minutes: int = 15,
    replay: bool = False,
    user_id: str = "",
) -> list[list[dict]]:
    """把结果拆成几条消息：视觉一条、Grok 一条、页脚一条。

    视觉的结果和图、Grok 的结果和图各占一条，页脚（任务编号、更多、重搜）
    单独一条。发送层把这几条放进同一个合并转发节点列表，用户在群里看到的
    是一条转发，点开是三条。两边都没内容时返回原来的单条结构。
    """
    outcome = task.outcome or SearchOutcome(status="error")

    if is_miss_layout(outcome):
        # 什么都没搜到：结论打头，然后每个引擎一条、视觉一条、Grok 一条。
        nodes = build_miss_nodes(
            task,
            command=command,
            nickname=nickname,
            ttl_minutes=ttl_minutes,
            replay=replay,
            user_id=user_id,
        )
        # 页脚带原图时单独成块：这套排版本来就凑得满 6 张图（4 引擎 + 视觉 +
        # Grok），再加一张原图就是 7 张，NapCat 会整条拒收、群里什么都看不到。
        # 拆成两块后，发送层在图少时会自动合回一条，图多时页脚（连着原图）
        # 单独一条跟上，原图始终跟页脚在一起。
        if nodes and _footer_needs_upload(outcome, replay):
            return [nodes[:-1], [nodes[-1]]]
        return [nodes]

    blocks: list[list[dict]] = []
    vision = _vision_block(outcome)
    if vision:
        blocks.append([_node(vision, nickname, user_id)])
    grok = _grok_block(outcome)
    if grok:
        blocks.append([_node(grok, nickname, user_id)])

    if not blocks:
        return [
            build_forward_nodes(
                task,
                command=command,
                show_reference=show_reference,
                limit=limit,
                nickname=nickname,
                header_name=header_name,
                ttl_minutes=ttl_minutes,
                replay=replay,
                user_id=user_id,
            )
        ]

    blocks.append(
        _tail_nodes(
            task,
            outcome,
            command=command,
            show_reference=show_reference,
            limit=limit,
            nickname=nickname,
            ttl_minutes=ttl_minutes,
            replay=replay,
            user_id=user_id,
            grok_taken=bool(grok),
        )
    )
    return blocks


def build_engine_forward_nodes(
    task: SearchTask,
    engine: str,
    items: list[Candidate],
    *,
    command: str,
    nickname: str = "麦麦寻人",
    header_name: str = "",
    ttl_minutes: int = 15,
    user_id: str = "",
    part: tuple[int, int] | None = None,
    with_best: bool = False,
    include_footer: bool = False,
) -> list[dict]:
    """把一个引擎的一批候选单独打成一条合并转发。

    QQ 对单条合并转发的图片总量有限制，图一多（十几张）组装就会失败，
    所以「更多」按引擎拆成几条发，每条只有该引擎的候选、图片随候选走。
    """
    outcome = task.outcome or SearchOutcome(status="error")
    nodes: list[dict] = []

    def node(content: list[dict]) -> dict:
        entry = {"nickname": nickname, "content": content}
        if user_id:
            entry["user_id"] = user_id
        return entry

    label = f"▍{_engine_name(engine)}（{len(items)} 条）"
    if part:
        label += f" · {part[0]}/{part[1]}"
    header = f"{_header_text(outcome, command, True, header_name)} · {label}"
    nodes.append(node([{"type": "text", "content": header}]))

    if with_best:
        # 这里是「更多」按引擎拆条的分支：详情全给出来。
        best_content: list[dict] = [{"type": "text", "content": _best_block(outcome, detail=True)}]
        if outcome.image_b64 and outcome.best is not None:
            best_content.append({"type": "image", "content": outcome.image_b64})
        nodes.append(node(best_content))

    for index, item in enumerate(items, start=1):
        content: list[dict] = [{"type": "text", "content": _candidate_text(item, index)}]
        image = candidate_image(outcome, item)
        if image:
            content.append({"type": "image", "content": image})
        nodes.append(node(content))

    if include_footer:
        nodes.append(
            node([{"type": "text", "content": "\n".join(_footer_lines(outcome, command, ttl_minutes, True))}])
        )
    return nodes


def stale_image_hint(stale_seconds: int) -> str:
    """图找到了、但早于取图窗口时给模型的提示：引导引用，而不是说没有图。"""
    minutes = max(1, int(stale_seconds) // 60)
    return (
        f"当前会话最近一张图是约 {minutes} 分钟前发的，超出 3 分钟的自动取图窗口，所以没取到。"
        "这不是图片服务故障，不要反复重试。那条消息在上下文里有 msg_id，"
        "把它作为 image_search 的 msg_id 参数再调一次即可；也可以请用户引用那张图。"
    )


def build_text_summary(
    task: SearchTask,
    *,
    command: str,
    show_reference: bool = True,
    limit: int = 5,
    eta_text: str = "约 60 秒",
) -> str:
    """给模型看的纯文字摘要，不带图片。"""
    outcome = task.outcome
    if outcome is None:
        if task.status == "running":
            return f"搜索还在进行中（task_id={task.task_id}），{eta_text}后可用 search_status 取结果。"
        return f"任务失败：{task.error or '未知错误'}"

    lines = [f"任务 {task.task_id}：{_best_block(outcome)}"]
    if outcome.status != "ok":
        lines.append(f"结论：{outcome.status}")
    if show_reference and outcome.candidates and outcome.status != "ok":
        # 命中时不列参考候选：模型只需要知道结论，列多了它会忍不住在群里复述一遍。
        groups = engine_groups(outcome, outcome.best, limit)
        if groups:
            lines.append("其他候选（按引擎分组）：")
            for engine, items in groups:
                lines.append(f"[{_engine_name(engine)}]")
                for index, item in enumerate(items, start=1):
                    identity = _candidate_identity(item)
                    detail = f"{index}. [{item.platform}] {identity} · {item.note or '未验证'}"
                    if item.url:
                        detail += f" · {item.url}"
                    lines.append(detail)
    # 匹配路线、AI 检测、耗时这些属于「怎么找的」，统一放进「更多」；
    # 首次回复只给最终结果，群里看着干净。
    lines.append(f"（搜索路线、耗时与全部 {outcome.total_candidates} 条候选见「{command} 更多」）")
    return "\n".join(lines)


# 首次回复最多带几张图：原图一张、最像的候选一张就够了，再多就是刷屏，
# 全部候选统一留给「更多」。
FIRST_REPLY_IMAGES = 3


def _first_reply_images(outcome: SearchOutcome, limit: int = FIRST_REPLY_IMAGES) -> list[str]:
    """挑首次回复要带的关键图，最多 limit 张。

    顺序按说服力排：原图（确认搜的是哪张）→ 最佳匹配的配图（确认对不对）
    → Grok 给的那张。没命中时再补候选图，方便自己肉眼核对。
    """
    picked: list[str] = []

    def add(value: str) -> None:
        if value and value not in picked and len(picked) < limit:
            picked.append(value)

    add(outcome.image_b64)
    if outcome.best is not None:
        add(candidate_image(outcome, outcome.best))
    add(outcome.grok_image_b64)
    if len(picked) < limit and outcome.status != "ok":
        for _engine, items in engine_groups(outcome, outcome.best, limit):
            for item in items:
                add(candidate_image(outcome, item))
                if len(picked) >= limit:
                    break
            if len(picked) >= limit:
                break
    return picked


def build_help(command: str, *, ttl_minutes: int = 15) -> str:
    """帮助文案：纯文字，照着抄就能用。

    例子里一律写真指令（`command`），不换成昵称——跟页脚同一套口径：用户要拿它去
    触发，昵称只是显示名。每条只讲干什么，不解释实现。
    """
    keep = ttl_text(ttl_minutes)
    name = command or "寻人"
    return "\n".join(
        [
            f"🔍 {name} 怎么用",
            "",
            "· 发图直接搜：发一张图，约 1 分钟出结果（搜索结果以合并转发发出）",
            f"· 强制重搜：发图时带上「{name} 重搜」，跳过缓存重来一遍",
            f"· 看全部候选：{name} 更多",
            f"· 搜索结果没显示出来：{name} 直发，同样内容改发一条普通消息",
            f"· 指定某一次：上面三个指令后面加任务编号，如 {name} 更多 s-20261127-1450-66af",
            f"· 核对推文：{name} xid 推文链接（要同时带图）",
            "",
            f"结果默认保留 {keep}：「{name} 更多」和「{name} 直发」在这段时间内都能用。",
        ]
    )


def build_first_reply(
    task: SearchTask,
    *,
    command: str,
    header_name: str = "",
    ttl_minutes: int = 15,
    max_images: int = FIRST_REPLY_IMAGES,
) -> tuple[str, list[str]]:
    """首次回复的内容：一段文字结论 + 最多 max_images 张关键图。

    不再走合并转发：卡片偶发拿不到新的 real_seq，客户端会整条不画（服务端仍存着），
    而插件既预知不了也补救不了（重发拿到的还是同一个 seq）。改成普通图文消息后一定
    看得见，代价是群里多占一行。匹配路线、AI 检测、全部候选仍然留在「更多」里。
    """
    outcome = task.outcome or SearchOutcome(status="error")
    lines = [f"{_header_text(outcome, command, False, header_name)}\n{_best_block(outcome)}"]
    lines.append("")
    lines.extend(_footer_lines(outcome, command, ttl_minutes, False, task.task_id))
    return "\n".join(lines), _first_reply_images(outcome, max_images)