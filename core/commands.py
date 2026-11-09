"""指令正则构建。

MaiBot 传给指令匹配的是消息纯文本，带图的消息会带前置占位符，
例如「[image] 麦麦寻人」。如果正则用 ^ 严格锚定指令本身就会漏匹配，
所以开头的方括号占位符要允许出现零到多次。

引用消息时占位符里还套着方括号：「[回复了某人的消息: [image]] 麦麦寻人」。
只按「不含 ]」截断的写法会在内层 [image] 的右括号处提前收尾，
后面的指令就被当成多余内容，整条消息匹配失败，所以占位符要允许嵌套。

另外，图片段在纯文本里可能是空的，只剩一个前导空格（例如「图 + 麦麦寻人」这条
消息的纯文本是「 麦麦寻人」，连 [image] 都没有）。因此开头还要允许任意空白。

群里先 @机器人 再写指令时，纯文本还会多一截「@昵称」；这段 @ 不是方括号占位符，
而 MaiBot 只在文本以 @ 开头时才帮忙剥掉它，前面压着引用占位符就剥不掉，
所以前置段里也允许出现 @昵称。
"""

from __future__ import annotations

import re


def _bracket_placeholder(depth: int = 2) -> str:
    """方括号占位符，内部允许再套 depth - 1 层方括号。"""
    item = r"[^\[\]]"
    placeholder = r"\[[^\[\]]*\]"
    for _ in range(max(depth, 1) - 1):
        placeholder = r"\[(?:" + item + r"|" + placeholder + r")*\]"
    return placeholder


# 群里 @机器人 再写指令时，纯文本前面会多出一截「@昵称」，例如
# 「[回复了某人的消息: [image]] @盆地  盆地找鸡」。这段 @ 是普通文本不是方括号占位符，
# 老写法把它当成多余内容，整条消息就匹配失败；而 MaiBot 只在文本以 @ 开头时才替我们
# 剥掉指向本机器人的 @，前面压着引用占位符时剥不掉。所以前置段里也允许 @昵称。
_MENTION_PREFIX = r"@[^\s@]+"

PLACEHOLDER_PREFIX = r"(?:(?:" + _bracket_placeholder() + r"|" + _MENTION_PREFIX + r")\s*)*"

# 指令参数里写在最前面的「重搜」：无视按图缓存，强制重新跑一遍引擎。
FORCE_KEYWORDS = frozenset({"重搜", "重新搜", "刷新", "强制", "force", "fresh"})


def split_force(arg: str) -> tuple[bool, str]:
    """把参数开头的「重搜」剥出来，返回 (是否强制重搜, 剩余参数)。

    支持「重搜」「重搜 更多」这种写法；没写就是 (False, 原样)。
    """
    text = (arg or "").strip()
    parts = text.split(None, 1)
    if parts and parts[0] in FORCE_KEYWORDS:
        return True, parts[1].strip() if len(parts) > 1 else ""
    return False, text



def build_command_pattern(names: list[str]) -> str:
    body = "|".join(re.escape(item) for item in names if item)
    return r"^\s*" + PLACEHOLDER_PREFIX + r"(?:" + body + r")(?:\s+(?P<arg>[\s\S]*))?$"


def build_more_pattern(keywords: list[str]) -> str:
    body = "|".join(re.escape(item) for item in keywords if item)
    # 允许在「更多」后面跟任务编号：「更多 s-20261103-0832-d7d9」。
    # 不编号时取当前会话最近一次，保持原有用法不变。
    return r"^\s*" + PLACEHOLDER_PREFIX + r"(?:" + body + r")(?:\s+(?P<task>[A-Za-z0-9][A-Za-z0-9\-_]{3,}))?\s*$"
