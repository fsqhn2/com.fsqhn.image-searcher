"""候选 URL 里的平台账号：统一收集成「其它平台」线索。

为什么单开一层：``extract.match_account()`` 早就能把 ``tiktok.com/@xxx/video/…``
解析成 handle，但反查链路只认 X——``_hunt_repost_author`` 走 ``_repost_x_handles()``，
非 X 的号被直接跳过。结果就是候选 URL 里明明写着 TikTok ``@starrychibun``
（实测 `s-20261123-1018-838e`），用户却看不到。

这一层只做「认出来、整理成线索」，认人仍然靠指纹：能不能核图由上层按平台决定。

- B 站：检索接口能拿到投稿封面（``core.bilibili.resolve_accounts``），可以核图。
- 微博：拿到 UID 就能换昵称粉丝，有登录 cookie 才翻得了带图帖。
- TikTok / Instagram / 抖音：没有可用的公开封面（2026-11-23 实测 TikTok oembed
  回 200 但 JSON 里只有 iframe，没有 ``thumbnail_url``），只能当账号线索。
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from .extract import match_account

# ``extract.match_account()`` 的平台标识 → 插件里的中文叫法（与 ``platform_of()`` 一致）。
LABELS = {
    "x": "X (Twitter)",
    "weibo": "微博",
    "bilibili": "B站",
    "tiktok": "TikTok",
    "instagram": "Instagram",
    "douyin": "抖音",
    "pixiv": "pixiv",
}

# 能拿公开图去比指纹的平台。其余平台只列账号，不假装核过。
VERIFIABLE = ("bilibili", "weibo")

# 这一轮明确不碰的：小红书要 x-s 签名、抖音有风控。
SKIPPED = ("xiaohongshu", "douyin")


def label(kind: str) -> str:
    """平台标识换成人看的名字；认不出的原样返回。"""
    text = str(kind or "").strip()
    return LABELS.get(text.lower(), text)


def account_from_url(url: str = "") -> dict[str, Any] | None:
    """链接 → 账号（只认账号页/带账号的帖子页），认不出返回 None。"""
    found = match_account(str(url or "").strip())
    if not found:
        return None
    kind, raw, page = found
    handle = str(raw or "").strip()
    if not handle:
        return None
    return {
        "kind": kind,
        "platform": label(kind),
        "handle": handle,
        "url": page,
        # 纯数字是平台内部 ID（微博 UID / B 站 mid），展示时不能加 @。
        "uid": handle if handle.isdigit() else "",
        "verifiable": kind in VERIFIABLE,
    }


def display(account: dict[str, Any]) -> str:
    """线索里怎么写这个号：``TikTok @starrychibun`` / ``B站 UID 123456``。"""
    platform = str(account.get("platform") or label(account.get("kind") or "")).strip()
    handle = str(account.get("handle") or "").strip()
    if not handle:
        return platform
    if handle.isdigit():
        return f"{platform} UID {handle}"
    return f"{platform} @{handle.lstrip('@')}"


def clean_name(raw: str) -> str:
    """候选的 ``name`` 字段洗成一个能当账号名用的短词，洗不出来返回空串。

    那个字段是站方给的**页面标题**，不一定是账号名：TikTok 的视频页会给
    ``Im sideways #klee #kleecosplay``（视频文案），Instagram 给
    ``阡狐Huni • Instagram photos and videos``（带站点后缀）。直接摆出来既难看，
    拿去做 B 站检索关键词也搜不到人。所以剥后缀、砍 hashtag，剩下带空白或过长的
    一律当没有。
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    text = re.sub(r"\s*[•·|]\s*.*$", "", text)
    text = re.sub(r"的个人空间.*$", "", text)
    text = re.sub(r"[-－][^-－]*哔哩哔哩.*$", "", text)
    text = text.split("#")[0].strip()
    text = text.strip(" -–—|·/").strip()
    if not text or any(char.isspace() for char in text) or len(text) > 24:
        return ""
    return text


def collect(
    candidates: Iterable[Any], limit: int = 8, max_distance: int = 12
) -> list[dict[str, Any]]:
    """候选里出现的非 X 平台账号，按「平台 + 账号」去重。

    只收候选自己带的账号页／带账号的帖子页：这是页面上写明的归属，比从标题里
    猜出来的名字硬。距离越近的排越前，能核图的平台再往前放——用户最想点开的
    就是「能确认的那个」。

    **只挡「量出来很远」的候选**：引擎按题材返回、距离三十几的页面，上面的号跟
    这张图没关系。``distance=None`` 的不挡——838e 的 TikTok ``@starrychibun``、
    19ab 的 Instagram 都是 lens 的视觉候选，只是因为页面取不到主图才量不出距离
    （TikTok／IG 不给 `og:image`），挡掉就把最该给用户的线索扔了。
    """
    seen: set[tuple[str, str]] = set()
    rows: list[dict[str, Any]] = []
    for item in candidates or ():
        found = account_from_url(str(getattr(item, "url", "") or ""))
        if not found:
            continue
        if found["kind"] == "x" or found["kind"] in SKIPPED:
            continue
        distance = getattr(item, "distance", None)
        if distance is not None and int(distance) > int(max_distance):
            continue
        key = (found["kind"], found["handle"].lower())
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                **found,
                # 洗过的名字只在能当「再检索一次」的关键词时才留（B 站空间页那条
                # 只有它有用），洗不出来宁可空着，别把视频文案当账号名摆出去。
                "name": clean_name(str(getattr(item, "name", "") or "")),
                "page": str(getattr(item, "url", "") or ""),
                "title": str(getattr(item, "title", "") or "")[:80],
                "engine": str(getattr(item, "engine", "") or ""),
                "distance": int(distance) if isinstance(distance, int) else None,
            }
        )
    rows.sort(
        key=lambda row: (
            0 if row.get("verifiable") else 1,
            row["distance"] if row.get("distance") is not None else 999,
        )
    )
    return rows[: max(1, int(limit or 1))]


def from_handle(platform: str, handle: str, url: str = "") -> dict[str, Any]:
    """按平台名 + 账号拼一条线索（跨平台同名搜索的结果用这个）。"""
    text = str(handle or "").strip()
    name = str(platform or "").strip()
    kind = next((key for key, value in LABELS.items() if value == name), name.lower())
    return {
        "kind": kind,
        "platform": name or label(kind),
        "handle": text,
        "url": str(url or "").strip(),
        "uid": text if text.isdigit() else "",
        "verifiable": kind in VERIFIABLE,
    }
