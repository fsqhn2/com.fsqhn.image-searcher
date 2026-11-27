"""域名与白名单：一条规则，别每个模块各抄一份。

这个插件判「这是哪个站」的次数非常多——社交平台、图集站、搬运站、论坛、电商页、
付费图包站、停服站……历史上每加一类站点就顺手抄一遍

    host == token or host.endswith("." + token)

抄到第九份的时候，麻烦已经不是啰嗦，而是「改一处漏一处」：名单里写成 `.x.com`、
漏了先剥 `www.` 与端口、或者写成 `"x.com" in host`，行为就在各模块之间悄悄分叉。
所以匹配规则只留在这里，名单仍留在各自的业务模块里，谁的业务谁维护。

踩过的两个坑写成了用例（``tests/test_hosts.py``）：
``x.com.evil.example`` 不是 ``x.com``；``de.hentai-cosplay-xxx.com`` 也不是
——早期用子串判断，把带 xxx 的资源站当成了 X。
"""

from __future__ import annotations

from urllib.parse import urlparse
from typing import Iterable

__all__ = ["host_of", "matched_token", "is_host"]


def host_of(value: str = "") -> str:
    """从链接或裸域名里取主机名：小写、去账号密码、去端口，不剥 ``www.``。

    不剥 ``www.`` 是故意的：各模块的名单里都没有 ``www.``，而 ``is_host()`` 按
    「等于它或是它的子域」判，``www.x.com`` 本来就能命中。少做一步变换，出错
    的地方就少一处。
    """
    text = str(value or "").strip()
    if not text:
        return ""
    if "://" in text:
        host = urlparse(text).netloc
    else:
        # 没 scheme 的按「域名 / 域名+路径」处理：候选的 platform 字段常常就是一个
        # 光域名，不能把它当成解析失败。
        host = text.split("/", 1)[0]
    host = host.rsplit("@", 1)[-1].split(":")[0]
    return host.strip().lower()


def matched_token(value: str = "", tokens: Iterable[str] = ()) -> str:
    """``value`` 命中名单里的哪一项；没命中返回空串。

    ``value`` 既可以是域名，也可以是完整链接。需要拿到命中项本身时用这个
    （比如停服站要返回站名、付费图包站要把品牌名写进提示），只要是非判断就用
    ``is_host()``。
    """
    host = host_of(value)
    if not host:
        return ""
    for token in tokens:
        item = str(token or "").strip().lower()
        if not item:
            continue
        if host == item or host.endswith("." + item):
            return str(token)
    return ""


def is_host(value: str = "", tokens: Iterable[str] = ()) -> bool:
    """``value`` 是不是 ``tokens`` 里的某个域名（或它的子域）。"""
    return bool(matched_token(value, tokens))
