"""结果文案拼装：用户侧走合并转发，模型侧只给纯文字。"""

from __future__ import annotations

import re

from .engines import Candidate, is_avatar_hit
from .extract import is_avatar_image, is_forum_page
from .tasks import SearchOutcome, SearchTask

AI_FLAG_THRESHOLD = 0.75

# B 站视频号：BV 开头加一串大小写字母数字，例如 BV16m4y1E7Tf。
_BV_RE = re.compile(r"^BV[0-9A-Za-z]{8,}$")


def _ai_line(ai: dict | None) -> str:
    if not ai:
        return "🤖 AI 检测：未执行"
    if not ai.get("ok"):
        return f"🤖 AI 检测失败：{ai.get('error') or '未知原因'}"
    score = ai.get("score")
    try:
        value = float(score)
    except (TypeError, ValueError):
        return "🤖 AI 检测：未返回有效分值"
    percent = value * 100
    if value >= AI_FLAG_THRESHOLD:
        return f"🤖 AI 生成概率：{percent:.0f}%（疑似 AI 生成）"
    return f"🤖 AI 生成概率：{percent:.1f}%（大概率真人拍摄）"


def _route_line(outcome: SearchOutcome) -> str:
    if outcome.route == "drawing":
        reason = f"，{outcome.route_reason}" if outcome.route_reason else ""
        return f"🖼 匹配路线：插画{reason}"
    return "🖼 匹配路线：照片"


def _identity_lines(candidate: Candidate) -> list[str]:
    lines = [f"🌐 平台：{candidate.platform}"]
    if candidate.note:
        lines.append(f"🔍 匹配：{candidate.note}")
    if candidate.name:
        # 论坛帖的「名字」其实是帖子标题，写清楚，免得当成作者名。
        label = "📄 标题" if is_forum_page(candidate.url) else "👤 名字"
        lines.append(f"{label}：{candidate.name}")
    if candidate.handle:
        # B 站的 BV 号是「视频号」，不是账号。写成「账号：BV1xx…」会让人以为
        # 有这么一个用户，去搜还搜不到——它跟 UID 一样只是定位用的编号。
        label = "🎬 视频号" if _BV_RE.match(candidate.handle.strip()) else "🆔 账号"
        lines.append(f"{label}：{candidate.handle}")
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
    handle = str(grok.get("handle") or "").strip().lstrip("@").lower()
    seen = raw.lstrip("@").strip().lower()
    if handle and handle in seen:
        return f"🏷 图上印的 ID 就是这个号（{raw}），已按它核对。"
    return f"🏷 图上读到 ID：{raw}（与上面的结论不同，已按核实结果为准）。"


def _grok_lines(grok: dict) -> list[str]:
    """Grok 兜底的两档文案：post 是找到，account 只是线索。"""
    tier = grok.get("tier")
    reason = str(grok.get("reason") or "").strip()
    mark = _watermark_line(grok)
    if tier == "post":
        lines = ["✅ 找到 X 上的原帖"]
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
    lines.append("只是同一个人可能用的号，不是这张图的原帖。")
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
        if item.get("tied"):
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
    pick = min(picks, key=lambda item: item.distance if item.distance is not None else 999)
    where = pick.platform or pick.source or pick.engine
    gap = f"，指纹距离 {pick.distance}" if pick.distance is not None else ""
    return f"👉 最接近的一条（{where}{gap}）\n{pick.url}"


def _best_block(outcome: SearchOutcome, *, skip_grok: bool = False) -> str:
    text = _best_block_core(outcome, skip_grok=skip_grok)
    extra: list[str] = []
    if not skip_grok:
        miss = _grok_miss_line(outcome)
        if miss:
            extra.append(miss)
    extra.extend(_same_person_lines(outcome))
    extra.extend(_x_guess_lines(outcome))
    extra.extend(_paid_lines(outcome))
    if extra:
        text = "\n".join([text, *extra])
    return text


def _best_block_core(outcome: SearchOutcome, *, skip_grok: bool = False) -> str:
    best = outcome.best
    grok = outcome.grok or {}
    grok_lines = _grok_lines(grok)
    # 帖子档高于视觉档：帖子能点开对照，视觉只是模型判断，所以先给帖子。
    if grok.get("tier") == "post" and not skip_grok:
        if best is not None and best.status == "vision":
            grok_lines.append("视觉复核也认为是同一个人（模型判断，仅供参考），那张候选见参考区。")
        return "\n".join(grok_lines)
    if best is None and grok_lines and not skip_grok:
        # 只拿到账号：比「完全没找到」有用，但没有帖子可对照，仍然算线索。
        vision = outcome.vision or {}
        if vision.get("ok") and vision.get("checked"):
            if vision.get("downgraded"):
                grok_lines.append(
                    f"视觉复核挑出 {vision['downgraded']} 条，但都不能当原帖：落点在图库站、商品页这类页面，或账号最近没发过这张图；其中最近的一条可能就包含这张图。"
                )
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
        return "\n".join(grok_lines)
    if best is None:
        compared = sum(1 for item in outcome.candidates if item.distance is not None)
        highest = max((item.distance or 0 for item in outcome.candidates), default=0)
        # 视觉挑中又降级的条目上面已经单独说明并给了链接，不再算进「同角色或
        # 同款服装」——同一批图不能既说「像同一个人」又说「不是这张照片」。
        suspects = [
            item
            for item in outcome.candidates
            if item.status == "suspect"
            and not is_avatar_hit(item)
            and not (item.note or "").startswith("视觉复核")
        ]
        avatars = [
            item
            for item in outcome.candidates
            if item.status == "suspect" and is_avatar_hit(item)
        ]
        lines = ["⚠️ 未找到可靠出处"]
        if compared:
            lines.append(f"已比对 {compared} 条候选，最高指纹距离 {highest}，均未通过阈值。")
        else:
            lines.append("候选图都没能取到或解码，无法验证。")
        vision = outcome.vision or {}
        if vision.get("ok") and vision.get("checked"):
            if vision.get("downgraded"):
                lines.append(
                    f"视觉复核挑出 {vision['downgraded']} 条，但都不能当原帖：落点在图库站、商品页这类页面，或账号最近没发过这张图；其中最近的一条可能就包含这张图。"
                )
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
                lines.append("Grok 在 X 上搜过，没有找到对应的账号或帖子。")
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
        return "\n".join(lines)
    lines = _identity_lines(best)
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
}


def _engine_name(engine: str) -> str:
    return ENGINE_LABELS.get(engine, engine or "未知引擎")


def _candidate_identity(item: Candidate) -> str:
    """没下载比对过、或指纹明确不符的候选，账号并不是原作者，不能当结论发出去。"""
    if item.status in {"unverified", "mismatch"}:
        return "（账号未确认）"
    return item.handle or item.name or "（未识别）"


def _candidate_text(item: Candidate, index: int) -> str:
    tag = f"[{item.platform}]"
    identity = _candidate_identity(item)
    status = item.note or "未验证"
    lines = [f"{index}. {tag} {identity} · {status}"]
    if item.url:
        lines.append(f"   {item.url}")
    return "\n".join(lines)


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
        result.append((engine, ordered[: max(1, limit)]))
    return result


def candidate_image(outcome: SearchOutcome, item: Candidate) -> str:
    """取候选的展示图。

    参考区里每个引擎都要有「自己最接近的那张」的配图，所以即使这张已经当过
    顶部的参考图，这里也照样返回；否则那个引擎的列表看起来就像没配上图。
    """
    if not item.image_url:
        return ""
    return (outcome.images or {}).get(item.image_url, "")


def _header_text(outcome: SearchOutcome, command: str, replay: bool) -> str:
    header = f"🔍 {command}结果"
    if outcome.status == "error":
        header = f"🔍 {command} · 搜索失败"
    if replay:
        header = f"🔍 {command}结果 · 全部候选"
    if outcome.ai and outcome.ai.get("ok"):
        try:
            if float(outcome.ai.get("score") or 0) >= AI_FLAG_THRESHOLD:
                header = f"🔍 {command} · 疑似 AI 生成"
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


def _footer_lines(outcome: SearchOutcome, command: str, ttl_minutes: int, replay: bool, task_id: str = "") -> list[str]:
    lines = [_route_line(outcome), _ai_line(outcome.ai)]
    if outcome.cached:
        # 说清这是复用，不是刚跑出来的：同一张图结果一样，但引擎这次一条都没调。
        lines.append("💾 这张图之前搜过，直接复用了当时的结果（引擎没有重新跑）")
    if outcome.image_b64 and outcome.image_source == "upload":
        lines.append("（没有可用的候选图，转发里的图是你发的原图）")
    elif outcome.image_b64 and outcome.best is None:
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


def build_forward_nodes(
    task: SearchTask,
    *,
    command: str,
    show_reference: bool = True,
    limit: int = 8,
    nickname: str = "麦麦寻人",
    ttl_minutes: int = 15,
    replay: bool = False,
    user_id: str = "",
) -> list[dict]:
    """生成合并转发节点：标题、最佳匹配、按引擎分组的参考区、页脚。

    limit 是每个引擎最多列几条。图片由调用方预先塞进 outcome.images
    （键为候选图地址），首次回复只放每个引擎最接近的那张，回「更多」时放全部。
    user_id 会写进转发节点的 uin。QQ 端要求 uin 非空，留空会出现
    「消息异常」或只剩一个 [聊天记录] 占位，所以调用方要把机器人账号传进来。
    """
    outcome = task.outcome or SearchOutcome(status="error")
    nodes: list[dict] = []

    def node(content: list[dict]) -> dict:
        item = {"nickname": nickname, "content": content}
        if user_id:
            item["user_id"] = user_id
        return item

    nodes.append(node([{"type": "text", "content": _header_text(outcome, command, replay)}]))

    best_content: list[dict] = [{"type": "text", "content": _best_block(outcome)}]
    if outcome.image_b64 and outcome.best is not None:
        best_content.append({"type": "image", "content": outcome.image_b64})
    nodes.append(node(best_content))

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
            [
                {
                    "type": "text",
                    "content": "\n".join(_footer_lines(outcome, command, ttl_minutes, replay, task.task_id)),
                }
            ]
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
            [
                {
                    "type": "text",
                    "content": "\n".join(_footer_lines(outcome, command, ttl_minutes, replay, task.task_id)),
                }
            ],
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


def build_result_forwards(
    task: SearchTask,
    *,
    command: str,
    show_reference: bool = True,
    limit: int = 8,
    nickname: str = "麦麦寻人",
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
    header = f"{_header_text(outcome, command, True)} · {label}"
    nodes.append(node([{"type": "text", "content": header}]))

    if with_best:
        best_content: list[dict] = [{"type": "text", "content": _best_block(outcome)}]
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
    lines.append(_route_line(outcome).replace("🖼 ", ""))
    lines.append(_ai_line(outcome.ai).replace("🤖 ", ""))
    lines.append(f"耗时 {outcome.elapsed_ms / 1000:.1f} 秒，共 {outcome.total_candidates} 条候选。")
    return "\n".join(lines)
