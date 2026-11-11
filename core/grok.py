"""Grok 兜底：把原图交给中转站的 Grok，让它自己去 X 上找发布者。

dHash 只能判断「是不是同一张图」，视觉拼图只能判断「是不是同一个人」，两者都
摸不到账号。Grok 带 ``x_search`` 能自己看图、在 X 里检索、给出账号或帖子链接。

提示词分成三步：先看图上的 ID，有 ID 就拿 ID 去 X 里核实发布者，没有 ID 或
核实不到才走图搜图。之所以要加这一层，是很多图的水印直接把账号印在上面，
不先读它等于白扔最硬的线索。

但读水印必须带核实：实测让它单纯读图上的水印，会把 ``@ste11ar021`` 读成
``@butter021``。所以图上读到的 ID 只记成线索，结论仍然只认它返回的帖子或
账号链接，链接还要再拉回来比指纹。

全部是纯函数：写 prompt、拼请求体、解析返回。网络请求与超时在 plugin.py。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .extract import is_social_platform

PROMPT_TEMPLATE = """有人在找这张图的出处。

请你先看清画面：人物、服装、姿势、场景。

第一步，找图上的 ID。看画面上所有的文字：水印、签名、边角小字、半透明文字、平台用户名、帖子号、网址。看到什么就按原样抄下来，别改字符、别补全、别按印象纠正；确实没有就写「无」。这一步只回答图上写了什么，不要猜。

第二步，图上有 ID 就走这条：拿这个 ID 去 X 找对应账号，确认它是不是发布者、有没有发过这张图——可以直接检索这个账号的带图帖子。核实到了就给出具体帖子地址。图上 ID 是用户名、帖子号还是别的东西，就按它在 X 里实际能查到什么来判断。核实不到（这个号不存在、号里没有这张图）要在依据里写明，然后走第三步。

第三步，图上没有 ID、或者 ID 核实不到时，用图找出发布者：按画面特征搜一次，再把这张图本身作为检索输入去 X 搜一次。

{hints}要求：
1. 目标是原帖：能定位到具体帖子就写帖子地址。有多条看着像的帖子就全部列出来，一条一行，最多 8 条——后面会逐条比对图片指纹，宁可多给，不要只给一条。
2. 只找到账号不算完成。出现「某个号像她」的念头时，必须先按第三步拿这张图本身再搜一次，确认这个号确实发过这张图、或者确实找不到原帖，才能收工。
3. 必须真的搜过再回答。不要凭水印或 ID 猜，也不要给出你没在搜索结果里见到的账号。
4. 判断依据要写清楚：图上读到什么 ID、你搜了什么、看到哪些账号、为什么认为是这个人。
5. 结尾单独一行写「依据：」，用不超过 40 个字说明关键证据；如果最后只剩账号没有帖子，要在这里写明「已用图搜过，未见原帖」。
6. 图上 ID 抄不准比不写更糟：看不清就写「无」，不要猜一个长得像的名字。

输出格式：
图上ID：@xxx（没有就写「无」）
账号：@xxx
帖子：https://x.com/xxx/status/1234567890
帖子：https://x.com/xxx/status/1234567891
依据：不超过 40 字

帖子写「无」只允许出现在「已经以图搜过、确实没有原帖」的情况下。只对上一个名字、没拿图搜过就写「无」，不算完成。完全找不到时，账号一行也写「无」，并在依据里说明搜了什么、没有结果。
"""

HINT_BLOCK = """搜索引擎这边已经返回了这些候选（账号 | 平台 | 标题），第三步里逐个和画面对比：
{hints}

请先看哪一个是这张图真正的发布者——找到就直接给它的帖子地址。
只对上名字不算对上人，按第 2 条先拿这张图本身去 X 搜索核对，别停在一个同名账号上。
"""

REPOST_BLOCK = """还有一条已知事实：候选里出现了转发列表页 {items}。那说明这张图是该号从别人那里转来的，不是本人发的，不要把它当成发布者。请直接去找最初发这个号的账号或帖子；图上如果有水印、签名，那才是原发布者留下的线索。
"""

REPLY_BLOCK = """另一条已知事实：程序已经顺着 {items} 的回复记录往上找过一遍，没有定位到原帖（可能翻不到、也可能接口限流）。请优先用 from:<账号> 在你的 X 搜索里把这个号的回复/转发再查一遍，找出这张图最初是谁发的；找不到帖子就说明查过哪些方式，不要停在「这个号像她」上。
"""


def social_first(candidates: list) -> list:
    """喂给 Grok 的候选顺序：社交平台优先。

    实测 s-20261105-1433 那单：候选里有一条 Reddit 帖「Join me ;3 : u/Smeepsi」，
    单独把这条交给 Grok，它能顺着这个用户名推出 X 上的 @smeepsx；可那一单的候选
    前几条全是画师站的插画（跟这张自拍毫无关系），把 Reddit 这条挤出了前 8 条，
    Grok 只能空手而归。社交平台候选里才可能写着发布者的账号，先给它们位置。
    """
    return sorted(
        candidates,
        key=lambda item: 0
        if is_social_platform(url=str(getattr(item, "url", "") or ""), source=str(getattr(item, "platform", "") or ""))
        else 1,
    )


def model_chain(primary: str, extras: list[str] | None = None) -> list[str]:
    """按顺序排好要尝试的模型：主模型在前，备用去重后接上。

    中转站会轮流挂 4.7 和 4.6，配置里给两个模型时，前一个超时或回 5xx 就换
    下一个，不用把整单拖到超时结束。
    """
    first = (primary or "").strip() or "grok-4.7"
    chain = [first]
    for item in extras or []:
        name = str(item).strip()
        if name and name not in chain:
            chain.append(name)
    return chain


UPSTREAM_DOWN_RE = re.compile(r"HTTP\s+(50[0-4])\b")


def is_upstream_down(message: str) -> bool:
    """失败报文是不是「中转站上游整家挂了」。

    实测 22:23 与 22:41 两单：grok-4.7 回 502，换 grok-4.6 还是 502，每次都要
    等 100 秒上下才肯回，一单白搭 3~4 分钟。两个模型走的是同一家上游，整家挂的
    时候换号没有意义；只有 5xx 才算，401/403 是 key 的问题、由调用方单独处理。
    """
    return bool(UPSTREAM_DOWN_RE.search(message or ""))


def build_hints(candidates: list, limit: int = 8) -> list[str]:
    """把候选整理成给 Grok 看的清单：账号优先，其次标题。

    图片反搜拿到的候选里，账号和标题往往就是最有价值的线索——让人去核实
    「这个账号是不是发过这张图」，比让它凭空看图猜关键词靠谱得多。
    """
    lines: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        handle = str(getattr(item, "handle", "") or "").strip()
        name = str(getattr(item, "name", "") or "").strip()
        platform = str(getattr(item, "platform", "") or "").strip()
        if not handle and not name:
            continue
        key = (handle or name).lower()
        if key in seen:
            continue
        seen.add(key)
        parts = [handle or "无账号"]
        if platform:
            parts.append(platform[:24])
        if name:
            parts.append(" ".join(name.split())[:40])
        lines.append(" | ".join(parts))
        if len(lines) >= max(1, limit):
            break
    return lines


def build_prompt(hints: list[str] | None = None, repost_note: str = "", reply_hint: str = "") -> str:
    """有候选清单就在第三步带上候选，没有就留空。

    ``repost_note`` 是候选里命中的转发页地址。带着它和不带它的差别是实测出来的：
    同一张图，不说「这是转发页」，模型会把转发者当成发布者、直接答「没有原帖」；
    说清楚之后它会转而读图上的水印，找到真正发帖的那个号。

    ``reply_hint`` 是「程序已经顺着回复翻过一遍、没找到」时补的一句。回复页上的图
    本来就是她回复的那条帖子的，翻失败说明该让模型换一条路（按 from:<号> 再查），
    不能让它以为没人试过。
    """
    blocks: list[str] = []
    if hints:
        blocks.append(
            HINT_BLOCK.format(hints="\n".join(f"{index}. {line}" for index, line in enumerate(hints, 1)))
        )
    note = " ".join((repost_note or "").split())
    if note:
        blocks.append(REPOST_BLOCK.format(items=note[:200]))
    trace = "、".join((reply_hint or "").split())
    if trace:
        blocks.append(REPLY_BLOCK.format(items=trace[:200]))
    return PROMPT_TEMPLATE.format(hints=("\n\n".join(blocks) + "\n\n") if blocks else "").strip()


# X 的保留路径，出现在 x.com/<x> 里不能当账号。
RESERVED = {
    "i",
    "home",
    "search",
    "explore",
    "hashtag",
    "intent",
    "settings",
    "messages",
    "notifications",
    "compose",
    "login",
    "signup",
    "tos",
    "privacy",
    "share",
    "download",
    "account",
    "about",
}

_STATUS = re.compile(
    r"(?:x|twitter)\.com/(?P<handle>[A-Za-z0-9_]{1,15})/status(?:es)?/(?P<sid>\d{5,})",
    re.I,
)
_PROFILE = re.compile(
    r"(?:x|twitter)\.com/(?P<handle>[A-Za-z0-9_]{1,15})(?=[/?#\s)\]}>\"']|$)",
    re.I,
)
_REASON = re.compile(r"依据\s*[:：]\s*(.+)")
# prompt 里约定「图上ID：」这一行，只作线索记录，不参与结论判定。
_ID_FIELD = re.compile(r"图上\s*ID\s*[:：]\s*(.+)")
# 「帖子：」这一行是模型对「有没有原帖」的明确表态，比 annotations 里的
# 搜索命中链接可信：那些链接只是它检索时看见的，不是它给出的结论。
_POST_FIELD = re.compile(r"帖子\s*[:：]\s*(.+)")
# prompt 里约定了「账号：@xxx」这一行。只认这个结构化字段，不去正文里捞
# 裸写的 @名字——模型可能在复述「我搜了某某但没有结果」，扫全文会误判。
_HANDLE_FIELD = re.compile(r"账号\s*[:：]\s*@?([A-Za-z0-9_]{1,15})")
_URL = re.compile(r"https?://\S+")


@dataclass
class GrokResult:
    """一次 Grok 找人的结果。"""

    ok: bool = False
    # post=给出具体帖子；account=只给出账号主页；none=没找到
    tier: str = "none"
    post_url: str = ""
    handle: str = ""
    # Grok 一次可能给出多条看着像的帖子，全部留着逐条比指纹，别只信第一条。
    posts: list[tuple[str, str]] = field(default_factory=list)
    # 图上读到的 ID 原文（水印、签名、帖号等）。只是线索：实测读水印会认错字，
    # 所以它不参与结论判定，只用来在日志里核对「它到底看到了什么」。
    watermark: str = ""
    reason: str = ""
    text: str = ""
    model: str = ""
    error: str = ""
    tools: list[str] = field(default_factory=list)


def build_payload(
    model: str,
    image_data_url: str,
    *,
    hints: list[str] | None = None,
    repost_note: str = "",
    reply_hint: str = "",
    max_output_tokens: int = 1500,
) -> dict:
    """拼 Responses 接口的请求体。

    中转站的 ``chat/completions`` 带 ``search_parameters`` 会被上游打回 410，
    只有这里这条 ``/responses`` + ``tools`` 的路能通。
    """
    return {
        "model": model,
        "input": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": build_prompt(hints, repost_note=repost_note, reply_hint=reply_hint),
                    },
                    {"type": "input_image", "image_url": image_data_url},
                ],
            }
        ],
        "tools": [{"type": "x_search"}],
        "max_output_tokens": max_output_tokens,
    }


def collect_text_and_urls(body: dict) -> tuple[str, list[str], list[str]]:
    """从 Responses 的返回里取出正文、链接、调用过的工具名。"""
    texts: list[str] = []
    urls: list[str] = []
    tools: list[str] = []
    for item in body.get("output") or []:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "custom_tool_call":
            name = str(item.get("name") or "")
            if name and name not in tools:
                tools.append(name)
            continue
        if kind != "message":
            continue
        for piece in item.get("content") or []:
            if not isinstance(piece, dict):
                continue
            if piece.get("type") in {"output_text", "text"}:
                texts.append(str(piece.get("text") or ""))
            for note in piece.get("annotations") or []:
                if not isinstance(note, dict):
                    continue
                url = str(note.get("url") or "")
                if url:
                    urls.append(url)
    for note in body.get("citations") or []:
        if isinstance(note, dict) and note.get("url"):
            urls.append(str(note["url"]))
        elif isinstance(note, str):
            urls.append(note)
    text = "\n".join(part for part in texts if part).strip()
    return text, urls, tools


def find_posts(text: str, urls: list[str]) -> list[tuple[str, str]]:
    """按出现顺序收集帖子链接，返回 [(handle, url)]，去重保序。

    ``x.com/i/status/<id>`` 是 X 自己的内部跳转链接，拿不到账号名，但 status
    id 是真的，fxtwitter 能顺着它解析出真实作者。Grok 从搜索结果里抓到的常常
    就是这种形式，整类丢掉会把最有用的一批线索扔掉，所以单独保留，handle 记空。
    """
    seen: set[str] = set()
    found: list[tuple[str, str]] = []
    for blob in [text, *urls]:
        for match in _STATUS.finditer(blob or ""):
            handle = match.group("handle")
            sid = match.group("sid")
            if sid in seen:
                continue
            if handle.lower() in RESERVED:
                if handle.lower() != "i":
                    continue
                seen.add(sid)
                found.append(("", f"https://x.com/i/status/{sid}"))
                continue
            seen.add(sid)
            found.append((handle, f"https://x.com/{handle}/status/{sid}"))
    return found


def find_accounts(text: str, urls: list[str]) -> list[str]:
    """收集只出现过主页、没有具体帖子的账号。"""
    seen: set[str] = set()
    found: list[str] = []
    for blob in [text, *urls]:
        for match in _PROFILE.finditer(blob or ""):
            handle = match.group("handle")
            if handle.lower() in RESERVED or handle in seen:
                continue
            # 帖子链接里的账号已经算进帖子档，这里不重复收录。
            tail = (blob[match.end() : match.end() + 9] or "").lower()
            if tail.startswith("/status"):
                continue
            seen.add(handle)
            found.append(handle)
    # 模型没写链接、只按格式写了「账号：@xxx」时，认这一处。
    for match in _HANDLE_FIELD.finditer(text or ""):
        handle = match.group(1)
        if handle.lower() in RESERVED or handle in seen:
            continue
        seen.add(handle)
        found.append(handle)
    return found


def pick_best_check(checks: list[dict]) -> dict | None:
    """多条候选帖子里挑一条：命中优先，其次指纹距离最近的，全都没下到图就取第一条。

    Grok 可能一次给出好几条看着像的帖子，逐条比过指纹之后，得由这里决定用哪条
    当结论。命中（距离 ≤ 阈值）是唯一的硬判据，没有命中才退回「谁最近」。
    """
    if not checks:
        return None
    for check in checks:
        if check.get("verdict") == "hit":
            return check
    usable = [check for check in checks if (check.get("distance") if check.get("distance") is not None else -1) >= 0]
    if usable:
        return min(usable, key=lambda check: check["distance"])
    return checks[0]


def watermark_of(text: str) -> str:
    """取「图上ID：」那一行，原样保留（不校验是否真的是个账号）。

    模型看到的可能是用户名、帖子号、别的平台 ID，这里不做格式约束；只把
    「没有」这类回答归一成空串。
    """
    if not text:
        return ""
    match = _ID_FIELD.search(text)
    if not match:
        return ""
    raw = " ".join(match.group(1).split()).strip()
    if raw[:1] in {"无", "沒", "没"} or raw.lower() in {"none", "n/a", "null", "-"}:
        return ""
    return raw[:60]


def field_posts(text: str) -> tuple[bool, list[tuple[str, str]]]:
    """读正文里「帖子：」这几行。

    返回 (模型有没有写这个字段, 字段里给出的帖子)。写了「帖子：无」时字段
    存在但列表为空——这时不能拿 annotations 里的链接顶替，那些只是它检索
    过程中撞见的页面，不是它认定的原帖。
    """
    seen = False
    found: list[tuple[str, str]] = []
    for line in (text or "").splitlines():
        match = _POST_FIELD.match(line.strip())
        if not match:
            continue
        seen = True
        value = match.group(1).strip()
        if not value or value[:1] in {"无", "沒", "没"} or value.lower() in {"none", "n/a", "-"}:
            continue
        found.extend(find_posts(value, []))
    return seen, found


def reason_of(text: str) -> str:
    """取「依据：」那一行；模型没按格式写就退化成去掉链接后的开头。"""
    if not text:
        return ""
    match = _REASON.search(text)
    if match:
        return match.group(1).strip()[:80]
    plain = _URL.sub("", text)
    plain = " ".join(plain.split())
    return plain[:80]


def parse_output(body: dict, *, model: str = "") -> GrokResult:
    """把 Responses 的返回整理成分档结果。

    只认真实出现在正文或 annotations 里的链接：模型可能编一个账号出来，而编
    造出来的名字不会带着可点击的帖子地址。
    """
    result = GrokResult(model=str(body.get("model") or model or ""))
    text, urls, tools = collect_text_and_urls(body)
    result.text = text
    result.tools = tools
    if not text and not urls:
        result.error = "返回空内容"
        return result

    result.watermark = watermark_of(text)
    stated, field_found = field_posts(text)
    # 模型明确写了「帖子：无」时，annotations 里的链接不算它给出的原帖；
    # 没写这个字段才退回全文扫描。
    posts = field_found if stated else find_posts(text, urls)
    result.reason = reason_of(text)
    if posts:
        result.posts = posts
        handle, url = posts[0]
        result.ok = True
        result.tier = "post"
        result.handle = handle
        if not handle:
            # x.com/i/status/<id> 拿不到作者名，正文「账号：」里写了就补上，
            # 否则客户端只剩一条没有作者的链接。
            result.handle = next(
                (
                    match.group(1)
                    for match in _HANDLE_FIELD.finditer(text)
                    if match.group(1).lower() not in RESERVED
                ),
                "",
            )
        result.post_url = url
        return result

    accounts = find_accounts(text, urls)
    if accounts:
        result.ok = True
        result.tier = "account"
        result.handle = accounts[0]
        return result

    result.ok = True
    result.tier = "none"
    return result


def dumps(result: GrokResult) -> str:
    """日志用：一行说清这一轮 Grok 干了什么。"""
    return json.dumps(
        {
            "tier": result.tier,
            "handle": result.handle,
            "post": result.post_url,
            "watermark": result.watermark,
            "reason": result.reason,
            "tools": result.tools,
            "error": result.error,
        },
        ensure_ascii=False,
    )
