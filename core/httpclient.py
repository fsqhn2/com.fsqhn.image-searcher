"""HTTP 会话与图片下载。

SerpAPI 和 twimg 在国内直连不通，需要走本地代理；引擎与图片下载共用
``[plugin] proxy``（主）与 ``[plugin] proxy_alt``（备）这一对代理设置，
留空即直连。配了备代理时，主代理连接层失败会自动换它重放同一个请求。
"""

from __future__ import annotations

import ipaddress
import re
import socket
import time
from collections.abc import Sequence
from typing import Any
from urllib.parse import urlparse

import requests
from requests.exceptions import ConnectionError, ReadTimeout, SSLError

ProxySpec = str | Sequence[str]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
)

MAX_IMAGE_BYTES = 12 * 1024 * 1024

# 这些图床在国内直连比走代理更快也更稳：实测 avatars.mds.yandex.net 直连约 1 秒，
# 走代理时常 15 秒以上甚至 SSL 报错，验证阶段会因为超时把结果全标成「未验证」。
_DIRECT_FIRST_SUFFIXES = ("yandex.net", "yandex.ru", "yandex.com", "yandex.com.tr")

_BLOCKED_HOSTS = {"localhost", "localhost.localdomain", "metadata", "metadata.google.internal"}

# fake-ip 模式把域名解析成一段假地址：Clash / mihomo 默认 fake-ip-range 198.18.0.1/16
# （实际覆盖 198.18~198.19），sing-box 默认 198.18.0.0/15。这段本身不是公网，但请求
# 经代理能正常到达目标站，所以留给用户显式放行。内核或自定义配置可以把这段改成别处，
# 所以范围做成可配的，下面只是默认值。
DEFAULT_FAKE_IP_RANGES = ("198.18.0.0/15",)


def fake_ip_networks(enabled: bool, ranges: object = None) -> tuple[ipaddress._BaseNetwork, ...]:
    """把开关与网段列表解析成放行清单；关着则返回空元组。

    单个条目写错只跳过它自己，不会退回「全部放行」——配置写错的结果是少放行，
    不是把 SSRF 防护关掉。
    """
    if not enabled:
        return ()
    raw = ranges if ranges else DEFAULT_FAKE_IP_RANGES
    if isinstance(raw, str):
        raw = [raw]
    networks: list[ipaddress._BaseNetwork] = []
    for item in raw:
        text = str(item).strip()
        if not text:
            continue
        try:
            networks.append(ipaddress.ip_network(text, strict=False))
        except ValueError:
            continue
    return tuple(networks)



class HttpError(RuntimeError):
    """网络请求失败。"""


def is_public_url(url: str, *, allow_nets: tuple[ipaddress._BaseNetwork, ...] = ()) -> bool:
    """只放行指向公网地址的 http(s) 链接。

    图片 URL 有两个来源：聊天消息里的图片段和搜索引擎返回的候选图。前者
    可以由群成员构造，所以不能直接拿去请求——否则一条伪造的图片段就能让
    进程带着用户配的代理去访问本机、内网或云厂商的元数据接口。

    ``allow_nets`` 是额外放行的网段（由 ``fake_ip_networks()`` 从配置解析），
    只用于 fake-ip 这类假地址；未列出的内网地址照旧拒绝。
    """
    parsed = urlparse(url or "")
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    host = parsed.hostname.strip().lower().rstrip(".")
    if host in _BLOCKED_HOSTS or host.endswith(".localhost"):
        return False

    addresses: list[ipaddress._BaseAddress] = []
    try:
        addresses.append(ipaddress.ip_address(host))
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, None)
        except (socket.gaierror, UnicodeError):
            return False
        for info in infos:
            try:
                addresses.append(ipaddress.ip_address(info[4][0]))
            except ValueError:
                continue
    if not addresses:
        return False
    return all(
        address.is_global
        or any(
            address.version == network.version and address in network
            for network in allow_nets
        )
        for address in addresses
    )



def proxy_routes(value: ProxySpec = "") -> list[str]:
    """把「一个代理」或「一串代理」整理成线路列表（"" 表示直连），去重保序。

    11-23 起配置支持主代理 + 次代理互备：主代理连不上（超时、TLS 被掐断、连接被重置）
    就换下一条，不会因为一个代理抽风就让整单搜不动。
    """
    if isinstance(value, str) or value is None:
        items: list[Any] = [value or ""]
    else:
        items = list(value)
    routes: list[str] = []
    for item in items:
        text = str(item or "").strip()
        if text not in routes:
            routes.append(text)
    return routes


class FailoverSession(requests.Session):
    """主代理挂了自动换下一个：只在连接类错误上换线，对端拒绝（4xx）不换。

    「换线」是在同一个请求上重放，所以调用方不用关心配了几个代理：拿到 session 直接
    用，主代理连不上时它会自己走次代理，两次都倒才把原始异常抛出去。
    """

    def __init__(self, routes: Sequence[str]) -> None:
        super().__init__()
        self._routes = [str(route or "").strip() for route in routes]

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        last: Exception | None = None
        for route in self._routes:
            kwargs["proxies"] = {"http": route, "https": route} if route else {}
            try:
                return super().request(method, url, **kwargs)
            except (ConnectionError, ReadTimeout, SSLError) as exc:
                last = exc
                continue
        raise last or ConnectionError("配置的代理线路都连不上")


def make_session(
    proxy: ProxySpec = "", *, headers: dict[str, str] | None = None
) -> requests.Session:
    routes = proxy_routes(proxy)
    session: requests.Session
    if len(routes) > 1:
        session = FailoverSession(routes)
    else:
        session = requests.Session()
        route = routes[0] if routes else ""
        if route:
            session.proxies.update({"http": route, "https": route})
    session.headers.update({"User-Agent": USER_AGENT})
    if headers:
        session.headers.update(headers)
    return session


def download_routes(url: str, proxy: ProxySpec = "") -> list[str]:
    """按主机决定先走哪条线路，返回要去重尝试的顺序（"" 表示直连）。"""
    host = (urlparse(url).hostname or "").lower()
    direct_first = any(host == suffix or host.endswith("." + suffix) for suffix in _DIRECT_FIRST_SUFFIXES)
    configured = proxy_routes(proxy)
    routes = ([""] + configured) if direct_first else (configured + [""])
    ordered: list[str] = []
    for route in routes:
        if route not in ordered:
            ordered.append(route)
    return ordered


# 这些错误是瞬时的（TLS 被中途掐断、连接重置、超时），换线路或重试一次通常就能过。
_RETRY_HINTS = (
    "sslerror",
    "ssleoferror",
    "eof",
    "timeout",
    "timed out",
    "connection",
    "reset",
    "max retries",
    "http 5",
)


def is_transient_error(error: BaseException) -> bool:
    """判断下载失败是否值得重试：对端明确拒绝（4xx）或内容不合规就不重试。"""
    text = str(error).lower()
    return any(hint in text for hint in _RETRY_HINTS)


def referer_of(url: str) -> str:
    """用图片地址自己的站点根作 Referer。

    v2ph、fapello 这类图床会拦「没有来源页」的请求：裸下 403，带上自己域名的
    Referer 就 200（实测 3 条全救回）。TikTok、Patreon 的图带什么都不行，那是
    带签名的地址，属于另一回事。
    """
    parts = urlparse((url or "").strip())
    if not parts.scheme or not parts.netloc:
        return ""
    return f"{parts.scheme}://{parts.netloc}/"


def download_image_multi(
    url: str,
    proxy: ProxySpec = "",
    timeout: float = 15.0,
    attempts: int = 3,
    *,
    allow_nets: tuple[ipaddress._BaseNetwork, ...] = (),
) -> bytes:
    """下载图片，失败时换线路并重试。

    Yandex 图床直连优先（代理反而更慢），其他站点先走代理。TLS 被中途掐断这类
    瞬时错误很常见，单次尝试经常白给，所以按线路轮换重试几次；对端明确拒绝的
    （403/404、地址不合规）不浪费次数。``allow_nets`` 透传给地址校验，用于 fake-ip
    这类把域名解析成假地址、但请求经代理能到达的场景。
    """
    routes = download_routes(url, proxy)
    last_error: HttpError | None = None
    total = max(1, attempts)
    for index in range(total):
        route = routes[index % len(routes)]
        try:
            return download_image(url, route, timeout, allow_nets=allow_nets)
        except HttpError as exc:
            last_error = exc
            if not is_transient_error(exc):
                break
            if index + 1 < total:
                time.sleep(0.3)
    # 403 说明不是线路问题，而是对端要来源页；4xx 上面已经不再重试，
    # 这里换带 Referer 的姿势再试一次，能救回 v2ph、fapello 这类图床。
    if last_error is not None and "403" in str(last_error):
        origin = referer_of(url)
        if origin:
            try:
                return download_image(
                    url, routes[0], timeout, referer=origin, allow_nets=allow_nets
                )
            except HttpError as exc:
                last_error = exc
    raise last_error or HttpError("图片下载失败")


def download_image(
    url: str,
    proxy: ProxySpec = "",
    timeout: float = 15.0,
    referer: str = "",
    *,
    allow_nets: tuple[ipaddress._BaseNetwork, ...] = (),
) -> bytes:
    """下载候选图，返回原始字节；超过大小上限直接放弃。"""
    if not url:
        raise HttpError("图片地址为空")
    if not is_public_url(url, allow_nets=allow_nets):
        raise HttpError("图片地址不是公网 http(s)，已拒绝")
    session = make_session(proxy, headers={"Referer": referer})
    try:
        with session.get(url, timeout=timeout, stream=True) as resp:
            if resp.status_code != 200:
                raise HttpError(f"HTTP {resp.status_code}")
            buffer = bytearray()
            for chunk in resp.iter_content(64 * 1024):
                if not chunk:
                    continue
                buffer.extend(chunk)
                if len(buffer) > MAX_IMAGE_BYTES:
                    raise HttpError("图片超过大小上限")
            if not buffer:
                raise HttpError("图片内容为空")
            return bytes(buffer)
    except HttpError:
        raise
    except Exception as exc:  # noqa: BLE001 - 网络异常统一转成可读错误
        raise HttpError(f"{type(exc).__name__}: {exc}") from exc
    finally:
        session.close()


def json_or_raise(resp: requests.Response, engine: str) -> dict[str, Any]:
    """统一的 JSON 解析与错误提示。"""
    if resp.status_code >= 400:
        raise HttpError(f"{engine} 返回 HTTP {resp.status_code}")
    try:
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        raise HttpError(f"{engine} 返回非 JSON 内容") from exc
    if not isinstance(data, dict):
        raise HttpError(f"{engine} 返回格式异常")
    return data


def is_auth_failure(status_code: int) -> bool:
    """这些状态码说明 key 有问题或额度用完，应当冷却换 key。"""
    return status_code in (401, 402, 403, 429)


_META_CHARSET = re.compile(rb"<meta[^>]+charset=[\"']?([\w-]+)", re.I)
_HEADER_CHARSET = re.compile(r"charset=([\w-]+)", re.I)


def decode_body(raw: bytes, content_type: str = "") -> str:
    """把网页字节解成文字：先认响应头，再认页面里写的 charset。

    ``requests`` 在响应头没声明字符集时按 RFC 把 ``text/html`` 当 latin-1，
    新浪这类只靠 ``<meta charset>`` 的页面就会被解成 ``Cosæ­£ç`` 这种乱码，
    拿这串去搜等于白搜（s-20261118-1647-b0a3 的新浪转载页就是这么废的）。
    所以这里自己定顺序：响应头 → 页面 meta → utf-8 → gb18030。
    """
    if not raw:
        return ""
    header = _HEADER_CHARSET.search(content_type or "")
    meta = _META_CHARSET.search(raw[:4096])
    candidates = [
        header.group(1) if header else "",
        meta.group(1).decode("ascii", "ignore") if meta else "",
        "utf-8",
        "gb18030",
    ]
    for encoding in candidates:
        if not encoding:
            continue
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")
