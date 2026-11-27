"""判断图上读到的那串字够不够格当「账号」用。

模型读浅色花体小字会读出一个「看着像英文单词」的东西：s-20261126-1334-832b
把地毯上的一串白色连笔签名读成 ``Stellach``，而 X 上恰好真有一个同名号——
点进去是 2010 年注册、71 个粉丝、韩文简介的私人号，跟这张 cos 照毫无关系。
于是首屏摆出一条带主页链接的「线索」，还顺着它去搜了 Instagram 和 B 站。

所以「号存在」不等于「读得准」：图上没明写 ``@`` 或网址、读出来的词又落到一个
明显不像本图作者的号上时，只能当读歪了，不能说成线索。

全部是纯函数，网络请求留在 plugin.py。
"""

from __future__ import annotations

import re

# 粉丝少到这个数、或者是私人号时，不再把它当「图上那个人」。
# 定在 100 是因为真做过水印的作者很少低于这个量级；71 粉那种私人号基本是巧合
# 撞名（832b 那单），而不是读对了号。
MIN_FOLLOWERS = 100

# 图上明写账号的形态。命中任意一种就说明不是「猜出来的单词」，画像再怪也只降级
# 提示，不否定读法。
_EXPLICIT = re.compile(
    r"(?:@|＠|x\.com/|twitter\.com/|weibo\.com/|weibo\.cn/|instagram\.com/"
    r"|t\.me/|bilibili\.com/|douyin\.com/|微博|ins[:：])",
    re.I,
)

_YEAR = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")


def explicit_account(raw: str, handle: str = "") -> bool:
    """图上明写着 ``@abc``、``x.com/abc`` 这类标记就算明写。

    只有一个单词、前面什么都没带的，字迹清楚时也可能是账号（实测右上角印着
    ``KWATERIN``，对应 @kwaterin），所以这条不判断「读得对不对」，只决定要不要
    多核一道画像：明写的读法出错概率低，踩它的门槛要更高。
    """
    text = str(raw or "")
    target = str(handle or "").strip().lstrip("@＠")
    if not text.strip():
        return False
    if _EXPLICIT.search(text):
        return True
    # `xxx.xxx` 这种带点的短词多半是网址或域名，也算明写。
    return bool(target) and bool(re.search(r"[.\-]", text.strip()))


def joined_year(joined: str) -> str:
    """从 ``Mon Mar 01 12:30:25 +0000 2010`` 里抠出年份。"""
    match = _YEAR.search(f" {str(joined or '').strip()} ")
    return match.group(1) if match else ""


def implausible(
    *,
    followers: int = -1,
    protected: bool = False,
    joined: str = "",
) -> str:
    """不像本图作者的理由；看着正常就返回空串。

    ``followers`` 传 -1 表示没拿到（接口有时不给），这时不判粉丝多少。
    返回的是能直接嵌进句子的短语，例如「是私人号、只有 71 个粉丝（2010 年注册）」。
    """
    bits: list[str] = []
    if protected:
        bits.append("是私人号")
    if isinstance(followers, int) and 0 <= followers < MIN_FOLLOWERS:
        bits.append(f"只有 {followers} 个粉丝")
    if not bits:
        return ""
    phrase = "、".join(bits)
    year = joined_year(joined)
    if year:
        phrase += f"（{year} 年注册）"
    return phrase
