"""远程图片地址过滤：群成员构造的图片段不能把请求导向本机或内网。"""

from __future__ import annotations

import io
import unittest
from unittest import mock

import requests
from PIL import Image
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import ReadTimeout

from core import httpclient
from core.hashing import dhash, prepare_upload
from core.httpclient import (
    DEFAULT_FAKE_IP_RANGES,
    HttpError,
    download_image_multi,
    download_routes,
    fake_ip_networks,
    is_public_url,
    is_transient_error,
)


class DecodeBodyTest(unittest.TestCase):
    """网页字符集：响应头没声明时别把 UTF-8 页面按 latin-1 解成乱码。"""

    def test_header_charset_wins(self) -> None:
        raw = "中文".encode("gb18030")
        self.assertEqual(httpclient.decode_body(raw, "text/html; charset=gb18030"), "中文")

    def test_meta_charset_when_header_is_bare(self) -> None:
        raw = b'<meta charset="gbk"><p>Cos\xd5\xfd\xc6\xac</p>'
        self.assertIn("Cos正片", httpclient.decode_body(raw, "text/html"))

    def test_utf8_page_without_any_declaration(self) -> None:
        """新浪这类页面只在正文里写中文、不带 charset，按 utf-8 解才不是 Cosæ­£ç。"""
        raw = "<title>Cos正片——战舰少女R：埃塞克斯</title>".encode("utf-8")
        self.assertIn("战舰少女R：埃塞克斯", httpclient.decode_body(raw, "text/html"))

    def test_empty(self) -> None:
        self.assertEqual(httpclient.decode_body(b""), "")


class PublicUrlTest(unittest.TestCase):
    def test_loopback_and_private_are_rejected(self) -> None:
        for url in (
            "http://127.0.0.1/a.jpg",
            "http://127.1.2.3:8000/a.jpg",
            "http://localhost/a.jpg",
            "http://foo.localhost/a.jpg",
            "http://10.0.0.8/a.jpg",
            "http://192.168.1.1/a.jpg",
            "http://172.16.3.4/a.jpg",
            "http://169.254.169.254/latest/meta-data/",
            "http://[::1]/a.jpg",
            "http://[fe80::1]/a.jpg",
            "http://metadata.google.internal/computeMetadata/v1/",
        ):
            with self.subTest(url=url):
                self.assertFalse(is_public_url(url))

    def test_non_http_scheme_is_rejected(self) -> None:
        for url in ("file:///C:/Windows/win.ini", "ftp://example.com/a.jpg", "gopher://1.1.1.1/"):
            with self.subTest(url=url):
                self.assertFalse(is_public_url(url))

    def test_public_ip_is_allowed(self) -> None:
        self.assertTrue(is_public_url("https://1.1.1.1/a.jpg"))
        self.assertTrue(is_public_url("http://8.8.8.8/a.jpg"))

    def test_fake_ip_rejected_by_default(self) -> None:
        for url in ("http://198.18.0.1/a.jpg", "http://198.19.255.254/a.jpg"):
            with self.subTest(url=url):
                self.assertFalse(is_public_url(url))

    def test_fake_ip_allowed_when_enabled(self) -> None:
        nets = fake_ip_networks(True)
        for url in ("http://198.18.0.1/a.jpg", "http://198.19.255.254/a.jpg"):
            with self.subTest(url=url):
                self.assertTrue(is_public_url(url, allow_nets=nets))

    def test_custom_fake_ip_range_is_honoured(self) -> None:
        nets = fake_ip_networks(True, ["100.64.0.0/10"])
        self.assertTrue(is_public_url("http://100.64.1.2/a.jpg", allow_nets=nets))
        self.assertFalse(is_public_url("http://198.18.0.1/a.jpg", allow_nets=nets))

    def test_invalid_range_is_skipped(self) -> None:
        nets = fake_ip_networks(True, ["不是网段", "", "198.18.0.0/15"])
        self.assertEqual([str(net) for net in nets], ["198.18.0.0/15"])
        self.assertTrue(is_public_url("http://198.18.0.1/a.jpg", allow_nets=nets))
        self.assertFalse(is_public_url("http://10.0.0.8/a.jpg", allow_nets=nets))

    def test_switch_off_ignores_ranges(self) -> None:
        self.assertEqual(fake_ip_networks(False, DEFAULT_FAKE_IP_RANGES), ())

    def test_fake_ip_option_keeps_private_networks_blocked(self) -> None:
        nets = fake_ip_networks(True)
        for url in ("http://10.0.0.8/a.jpg", "http://192.168.1.1/a.jpg", "http://127.0.0.1/a.jpg"):
            with self.subTest(url=url):
                self.assertFalse(is_public_url(url, allow_nets=nets))

    def test_empty_and_garbage_are_rejected(self) -> None:
        for url in ("", "not a url", "https://"):
            with self.subTest(url=url):
                self.assertFalse(is_public_url(url))


def _png(width: int, height: int) -> bytes:
    buffer = io.BytesIO()
    Image.new("L", (width, height), 128).save(buffer, format="PNG")
    return buffer.getvalue()


class PixelGuardTest(unittest.TestCase):
    def test_hash_skips_oversized_image(self) -> None:
        raw = _png(300, 300)
        with mock.patch("core.hashing.MAX_PIXELS", 1000):
            self.assertIsNone(dhash(raw))

    def test_upload_rejects_oversized_image(self) -> None:
        raw = _png(300, 300)
        with mock.patch("core.hashing.MAX_PIXELS", 1000):
            with self.assertRaises(ValueError):
                prepare_upload(raw)


class DownloadRouteTest(unittest.TestCase):
    """Yandex 图床直连更快，代理反而超时，所以线路顺序要按主机分。"""

    def test_yandex_cdn_prefers_direct(self) -> None:
        proxy = "http://127.0.0.1:6789"
        self.assertEqual(
            download_routes("https://avatars.mds.yandex.net/i?id=1", proxy),
            ["", proxy],
        )
        self.assertEqual(
            download_routes("https://avatars.mds.yandex.ru/i?id=1", proxy),
            ["", proxy],
        )

    def test_other_hosts_prefer_proxy(self) -> None:
        proxy = "http://127.0.0.1:6789"
        self.assertEqual(
            download_routes("https://cdn.donmai.us/a.jpg", proxy),
            [proxy, ""],
        )

    def test_without_proxy_only_direct(self) -> None:
        self.assertEqual(download_routes("https://cdn.donmai.us/a.jpg", ""), [""])
        self.assertEqual(download_routes("https://avatars.mds.yandex.net/x", ""), [""])


class DownloadRetryTest(unittest.TestCase):
    """Yandex 图床的 TLS 连接常被中途掐断，单次失败要换线路重试。"""

    def test_transient_error_is_retried_on_other_route(self) -> None:
        proxy = "http://127.0.0.1:6789"
        calls = []

        def fake_download(url, route="", timeout=15.0, **_kwargs):
            calls.append(route)
            if len(calls) == 1:
                raise HttpError("SSLError: SSLEOFError(8, 'EOF occurred in violation of protocol')")
            return b"image"

        with mock.patch("core.httpclient.download_image", side_effect=fake_download):
            data = download_image_multi("https://avatars.mds.yandex.net/i?id=1", proxy, timeout=1)

        self.assertEqual(data, b"image")
        self.assertEqual(calls, ["", proxy])

    def test_403_retries_once_with_referer(self) -> None:
        """403 是图床要来源页，不是线路问题：带 Referer 再试一次。"""
        calls = []

        def fake_download(url, route="", timeout=15.0, referer="", **_kwargs):
            calls.append(referer)
            if not referer:
                raise HttpError("HTTP 403")
            return b"image"

        with mock.patch("core.httpclient.download_image", side_effect=fake_download):
            data = download_image_multi("https://fapello.com/content/a.jpg", "http://127.0.0.1:6789", timeout=1)

        self.assertEqual(data, b"image")
        self.assertEqual(calls, ["", "https://fapello.com/"])

    def test_403_is_retried_only_once(self) -> None:
        """带 Referer 还是 403 就放弃，不要无休止地打对端。"""
        calls = []

        def fake_download(url, route="", timeout=15.0, referer="", **_kwargs):
            calls.append(referer)
            raise HttpError("HTTP 403")

        with mock.patch("core.httpclient.download_image", side_effect=fake_download):
            with self.assertRaises(HttpError):
                download_image_multi("https://cdn.donmai.us/a.jpg", "http://127.0.0.1:6789", timeout=1)

        self.assertEqual(len(calls), 2)

    def test_404_is_not_retried(self) -> None:
        calls = []

        def fake_download(url, route="", timeout=15.0, referer="", **_kwargs):
            calls.append(referer)
            raise HttpError("HTTP 404")

        with mock.patch("core.httpclient.download_image", side_effect=fake_download):
            with self.assertRaises(HttpError):
                download_image_multi("https://cdn.donmai.us/a.jpg", "http://127.0.0.1:6789", timeout=1)

        self.assertEqual(len(calls), 1)

    def test_transient_classification(self) -> None:
        self.assertTrue(is_transient_error(HttpError("ReadTimeout")))
        self.assertTrue(is_transient_error(HttpError("HTTP 503")))
        self.assertFalse(is_transient_error(HttpError("图片地址不是公网 http(s)，已拒绝")))
        self.assertFalse(is_transient_error(HttpError("图片超过大小上限")))


if __name__ == "__main__":
    unittest.main()


class RefererTest(unittest.TestCase):
    """403 的图床要靠来源页放行：v2ph、fapello 裸下 403、带 Referer 200。"""

    def test_origin_from_image_url(self) -> None:
        self.assertEqual(
            httpclient.referer_of("https://fapello.com/content/n/o/x/1000/x_0223_300px.jpg"),
            "https://fapello.com/",
        )
        self.assertEqual(httpclient.referer_of("http://cdn.v2ph.com/photos/a.jpg"), "http://cdn.v2ph.com/")

    def test_invalid(self) -> None:
        self.assertEqual(httpclient.referer_of(""), "")
        self.assertEqual(httpclient.referer_of("not-a-url"), "")


class ProxyFailoverTest(unittest.TestCase):
    """主备代理：主代理连接层挂了，同一个请求自动换次代理重放。

    `[plugin] proxy` 是主、`proxy_alt` 是备。11-23 实测里主代理（香港出口）三站只成
    一站、8 秒读超时，次代理（美国出口）9/9 全过——不互备的话一个代理抽风就整单搜不动。
    """

    def test_proxy_routes_normalizes(self) -> None:
        self.assertEqual(httpclient.proxy_routes("http://a:1"), ["http://a:1"])
        self.assertEqual(httpclient.proxy_routes(""), [""])
        self.assertEqual(httpclient.proxy_routes(None), [""])
        self.assertEqual(
            httpclient.proxy_routes([" http://a:1 ", "", "http://a:1", "http://b:2"]),
            ["http://a:1", "", "http://b:2"],
        )

    def test_single_proxy_keeps_plain_session(self) -> None:
        session = httpclient.make_session("http://a:1")
        self.assertNotIsInstance(session, httpclient.FailoverSession)
        self.assertEqual(session.proxies["https"], "http://a:1")

    def test_two_proxies_switch_on_connection_error(self) -> None:
        used: list[str] = []
        response = object()

        def fake_request(self, method, url, **kwargs):
            used.append(kwargs["proxies"].get("https", ""))
            if len(used) == 1:
                raise RequestsConnectionError("主代理挂了")
            return response

        session = httpclient.make_session(["http://a:1", "http://b:2"])
        with mock.patch.object(requests.Session, "request", fake_request):
            got = session.request("GET", "https://example.com")
        self.assertIs(got, response)
        self.assertEqual(used, ["http://a:1", "http://b:2"], "主代理失败后换次代理")

    def test_every_route_failing_raises_last_error(self) -> None:
        def fake_request(self, method, url, **kwargs):
            raise ReadTimeout("都超时了")

        session = httpclient.make_session(["http://a:1", "http://b:2"])
        with mock.patch.object(requests.Session, "request", fake_request):
            with self.assertRaises(ReadTimeout):
                session.request("GET", "https://example.com")

    def test_non_network_error_does_not_switch(self) -> None:
        """不是连接类错误就别换线——换线只会把同一个程序错误再犯一次。"""
        used: list[str] = []

        def fake_request(self, method, url, **kwargs):
            used.append(kwargs["proxies"].get("https", ""))
            raise ValueError("参数写错了")

        session = httpclient.make_session(["http://a:1", "http://b:2"])
        with mock.patch.object(requests.Session, "request", fake_request):
            with self.assertRaises(ValueError):
                session.request("GET", "https://example.com")
        self.assertEqual(used, ["http://a:1"])

    def test_download_routes_keeps_direct_last_for_normal_hosts(self) -> None:
        self.assertEqual(
            download_routes("https://example.com/a.jpg", ["http://a:1", "http://b:2"]),
            ["http://a:1", "http://b:2", ""],
        )
        # Yandex 图床直连比代理快，直连要排在最前
        self.assertEqual(
            download_routes("https://avatars.mds.yandex.net/i?id=1", ["http://a:1", "http://b:2"]),
            ["", "http://a:1", "http://b:2"],
        )

