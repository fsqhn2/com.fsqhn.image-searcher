"""引擎结果解析的单元测试。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

from requests.exceptions import SSLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.engines import (  # noqa: E402
    Candidate,
    EngineError,
    LensEngine,
    YandexEngine,
    is_yandex_shell_page,
    reference_candidates,
    to_yandex_candidate,
    verify_candidate,
)


class YandexCandidateTest(unittest.TestCase):
    """Yandex 必须拿站点原图比对，缩略图是裁过的方框，比不出东西。"""

    def test_prefers_original_image(self) -> None:
        site = {
            "title": "foo on X",
            "url": "https://x.com/foo/status/1",
            "domain": "x.com",
            "thumb": {"url": "//avatars.mds.yandex.net/i?id=1&w=296&h=180"},
            "originalImage": {"url": "https://pbs.twimg.com/media/abc.jpg"},
        }
        candidate = to_yandex_candidate(site, "yandex")
        self.assertEqual(candidate.image_url, "https://pbs.twimg.com/media/abc.jpg")
        self.assertEqual(candidate.note, "原图比对")

    def test_protocol_relative_original(self) -> None:
        site = {
            "url": "https://weibo.com/1/2",
            "originalImage": {"url": "//wx1.sinaimg.cn/large/abc.jpg"},
        }
        candidate = to_yandex_candidate(site, "yandex")
        self.assertEqual(candidate.image_url, "https://wx1.sinaimg.cn/large/abc.jpg")

    def test_original_keeps_thumb_as_fallback(self) -> None:
        site = {
            "url": "https://x.com/foo/status/1",
            "thumb": {"url": "//avatars.mds.yandex.net/i?id=1"},
            "originalImage": {"url": "https://pbs.twimg.com/media/abc.jpg"},
        }
        candidate = to_yandex_candidate(site, "yandex")
        self.assertEqual(candidate.image_url, "https://pbs.twimg.com/media/abc.jpg")
        self.assertEqual(candidate.fallback_image, "https://avatars.mds.yandex.net/i?id=1")

    def test_falls_back_to_thumb(self) -> None:
        site = {
            "url": "https://example.com/a",
            "thumb": {"url": "//avatars.mds.yandex.net/i?id=2&w=296&h=180"},
        }
        candidate = to_yandex_candidate(site, "yandex")
        self.assertEqual(candidate.image_url, "https://avatars.mds.yandex.net/i?id=2&w=296&h=180")
        self.assertEqual(candidate.fallback_image, "")
        self.assertEqual(candidate.note, "缩略图比对")


class VerifyFallbackTest(unittest.TestCase):
    """原图下不下来时，用缩略图补一次，别直接判「未验证」。"""

    def _png(self) -> bytes:
        from io import BytesIO

        from PIL import Image

        buffer = BytesIO()
        Image.new("RGB", (32, 32), (200, 120, 60)).save(buffer, format="PNG")
        return buffer.getvalue()

    def test_retries_with_fallback_image(self) -> None:
        from unittest.mock import patch

        from core.hashing import dhash
        from core.httpclient import HttpError

        raw = self._png()
        target = dhash(raw)
        candidate = Candidate(
            engine="yandex",
            url="https://x.com/foo/status/1",
            image_url="https://cdn.donmai.us/original.jpg",
            fallback_image="https://avatars.mds.yandex.net/i?id=1",
        )

        with patch("core.engines.download_image_multi") as downloader:
            downloader.side_effect = [HttpError("403 拒绝"), raw]
            result = verify_candidate(candidate, target, 8, "", 5)

        self.assertEqual(downloader.call_count, 2)
        self.assertEqual(result.status, "match")
        self.assertEqual(result.distance, 0)

    def test_both_routes_fail_marks_unverified(self) -> None:
        from unittest.mock import patch

        from core.httpclient import HttpError

        candidate = Candidate(
            engine="yandex",
            url="https://x.com/foo/status/1",
            image_url="https://cdn.donmai.us/original.jpg",
            fallback_image="https://avatars.mds.yandex.net/i?id=1",
        )

        with patch("core.engines.download_image_multi", side_effect=HttpError("403")):
            result = verify_candidate(candidate, 12345, 8, "", 5)

        self.assertEqual(result.status, "unverified")
        self.assertIn("下载失败", result.note)


class XPageKindTest(unittest.TestCase):
    """X 的主页、回复列表页不能当原帖：图是别人发的。"""

    def _png(self) -> bytes:
        from io import BytesIO

        from PIL import Image

        buffer = BytesIO()
        Image.new("RGB", (32, 32), (200, 120, 60)).save(buffer, format="PNG")
        return buffer.getvalue()

    def _verify(
        self,
        url: str,
        image_url: str = "https://cdn.example.com/a.jpg",
        name: str = "",
        handle: str = "",
    ):
        from unittest.mock import patch

        from core.hashing import dhash

        raw = self._png()
        candidate = Candidate(
            engine="lens", url=url, image_url=image_url, name=name, handle=handle
        )
        with patch("core.engines.download_image_multi", return_value=raw):
            return verify_candidate(candidate, dhash(raw), 8, "", 5)

    def test_avatar_image_is_not_the_original_post(self) -> None:
        """s-20261106-1543 的实况：Lens 把 40×40 站点头像当原图，点开看不到图。"""
        result = self._verify(
            "https://dzmm.ai/user/following",
            image_url="https://pxlorbit.example.com/avatar/ab/12/cd.png",
            name="DZMM AI - 让幻想有回应",
            handle="@dzmm",
        )
        self.assertEqual(result.status, "suspect")
        self.assertTrue(result.avatar)
        self.assertIn("站点账号头像", result.note)
        self.assertIn("指纹距离 0", result.note)
        self.assertEqual(result.name, "")
        self.assertEqual(result.handle, "")

    def test_avatar_flag_survives_far_distance(self) -> None:
        """s-20261106-1642 的实况：头像距离 22 不算命中，但也得标成头像。"""
        from io import BytesIO
        from unittest.mock import patch

        from PIL import Image, ImageDraw

        from core.hashing import dhash

        def stripes(offset: int) -> bytes:
            image = Image.new("RGB", (64, 64), (255, 255, 255))
            draw = ImageDraw.Draw(image)
            for index in range(0, 64, 8):
                if (index // 8 + offset) % 2 == 0:
                    draw.rectangle([index, 0, index + 7, 63], fill=(0, 0, 0))
            buffer = BytesIO()
            image.save(buffer, format="PNG")
            return buffer.getvalue()

        candidate = Candidate(
            engine="lens",
            url="https://x.com/lonely82089351",
            image_url="https://pbs.twimg.com/profile_images/2062750521325391872/Gk7_NOC2_400x400.jpg",
        )
        with patch("core.engines.download_image_multi", return_value=stripes(0)):
            result = verify_candidate(candidate, dhash(stripes(1)), 8, "", 5)
        self.assertEqual(result.status, "mismatch")
        self.assertEqual(result.distance, 64)
        self.assertTrue(result.avatar)

    def test_replies_page_is_not_the_original_post(self) -> None:
        """s-20261105-1423 的实况：把 AlbertGreen69 的回复列表页当成原帖主。"""
        result = self._verify("https://x.com/AlbertGreen69/with_replies")
        self.assertEqual(result.status, "suspect")
        self.assertIn("回复列表页", result.note)
        self.assertIn("指纹一致（距离 0）", result.note)

    def test_profile_page_is_not_a_post(self) -> None:
        result = self._verify("https://x.com/NENEchi8890")
        self.assertEqual(result.status, "suspect")
        self.assertIn("账号主页", result.note)

    def test_status_page_still_matches(self) -> None:
        result = self._verify("https://x.com/NERUchi890/status/2117586176714612824")
        self.assertEqual(result.status, "match")
        self.assertEqual(result.note, "指纹一致（距离 0）")

    def test_non_x_page_unaffected(self) -> None:
        result = self._verify("https://nudogram.com/models/nenechi890/290/")
        self.assertEqual(result.status, "match")


class ReferenceCandidatesTest(unittest.TestCase):
    """没找到时的参考图要跳过头像命中：距离 0 的头像配上去只会自相矛盾。"""

    def _avatar(self) -> Candidate:
        item = Candidate(
            engine="lens",
            url="https://dzmm.ai/user/x/following",
            image_url="https://pxlorbit.com/avatar/a.png",
            distance=0,
            status="suspect",
            note="该图是站点账号头像（指纹距离 0），不是帖子原图",
            avatar=True,
        )
        return item

    def test_avatar_hit_is_skipped_even_after_vision_rewrites_note(self) -> None:
        """视觉降级会改写 note，头像标记不能因此丢掉。"""
        avatar = self._avatar()
        avatar.note = "视觉复核：疑似同一个人，但那是dzmm.ai上的页面、不是作者主页（指纹距离 0）"
        baidu = Candidate(engine="baidu", url="https://example.com/b", distance=12, status="suspect")
        pool = reference_candidates(None, [avatar, baidu])
        self.assertEqual(pool, [baidu])

    def test_avatar_hit_is_skipped(self) -> None:
        avatar = self._avatar()
        baidu = Candidate(engine="baidu", url="https://example.com/b", distance=12, status="suspect")
        pool = reference_candidates(None, [avatar, baidu])
        self.assertEqual(pool, [baidu])

    def test_only_avatars_leave_pool_empty(self) -> None:
        self.assertEqual(reference_candidates(None, [self._avatar()]), [])

    def test_vision_pick_comes_before_closer_candidate(self) -> None:
        """视觉挑中的那张比指纹更近的图更值得当参考。"""
        closer = Candidate(engine="lens", url="https://example.com/c", image_url="https://img/c.jpg", distance=20, status="mismatch")
        picked = Candidate(engine="lens", url="https://example.com/p", image_url="https://img/p.jpg", distance=24, status="suspect")
        pool = reference_candidates(None, [closer, picked], ["https://img/p.jpg"])
        self.assertEqual(pool[0], picked)
        self.assertEqual(pool[1], closer)

    def test_avatar_pick_still_skipped(self) -> None:
        avatar = self._avatar()
        other = Candidate(engine="baidu", url="https://example.com/b", image_url="https://img/b.jpg", distance=12, status="suspect")
        pool = reference_candidates(None, [avatar, other], ["https://pxlorbit.com/avatar/a.png"])
        self.assertEqual(pool, [other])

    def test_best_comes_first_and_keeps_everything(self) -> None:
        avatar = self._avatar()
        best = Candidate(engine="lens", url="https://x.com/a/status/1", distance=0, status="match")
        pool = reference_candidates(best, [avatar])
        self.assertEqual(pool[0], best)
        self.assertIn(avatar, pool)

    def test_far_candidates_dropped_when_max_distance_given(self) -> None:
        """2504：距离 27 的同款球衣不该当参考图，超出上限整组丢掉。"""
        far = Candidate(engine="baidu", url="https://example.com/f", image_url="https://img/f.jpg", distance=27, status="suspect")
        self.assertEqual(reference_candidates(None, [far], max_distance=12), [])

    def test_unknown_distance_dropped_when_max_distance_given(self) -> None:
        """没算出距离（图没下载下来）的候选同样不能当参考图。"""
        unknown = Candidate(engine="baidu", url="https://example.com/u", image_url="https://img/u.jpg", distance=None, status="suspect")
        self.assertEqual(reference_candidates(None, [unknown], max_distance=12), [])

    def test_max_distance_keeps_close_one_and_orders_by_vision_pick(self) -> None:
        near = Candidate(engine="lens", url="https://example.com/n", image_url="https://img/n.jpg", distance=11, status="suspect")
        picked = Candidate(engine="lens", url="https://example.com/p", image_url="https://img/p.jpg", distance=12, status="suspect")
        far = Candidate(engine="lens", url="https://example.com/f", image_url="https://img/f.jpg", distance=27, status="suspect")
        pool = reference_candidates(None, [far, near, picked], ["https://img/p.jpg"], max_distance=12)
        self.assertEqual(pool, [picked, near])

    def test_max_distance_ignored_when_best_found(self) -> None:
        """有结论时参考图就是结论那张，不受距离门槛影响。"""
        best = Candidate(engine="lens", url="https://x.com/a/status/1", distance=0, status="match")
        self.assertEqual(reference_candidates(best, [], max_distance=12), [best])


class SerperLensTest(unittest.TestCase):
    """Serper 的 Lens 是替补：只收公网链接，结果字段跟 SerpAPI 完全不同。"""

    def test_parses_organic(self) -> None:
        from core.engines import serper_lens_candidates

        payload = {
            "organic": [
                {
                    "title": "某某 on X",
                    "source": "X",
                    "link": "https://x.com/foo/status/1",
                    "imageUrl": "https://pbs.twimg.com/media/a.jpg",
                    "thumbnailUrl": "https://encrypted-tbn0.gstatic.com/x",
                },
                {"title": "没有链接", "link": ""},
            ]
        }
        items = serper_lens_candidates(payload)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].url, "https://x.com/foo/status/1")
        self.assertEqual(items[0].image_url, "https://pbs.twimg.com/media/a.jpg")
        self.assertEqual(items[0].source, "X")

    def test_caps_result_count(self) -> None:
        """Serper 一次回 60 条，全收下来指纹比对会跑很久。"""
        from core.engines import serper_lens_candidates

        payload = {"organic": [{"link": f"https://x.com/a/status/{i}"} for i in range(60)]}
        self.assertEqual(len(serper_lens_candidates(payload)), 30)

    def test_bad_payload(self) -> None:
        from core.engines import serper_lens_candidates

        self.assertEqual(serper_lens_candidates(None), [])
        self.assertEqual(serper_lens_candidates({}), [])

    def test_host_returns_plain_text_link(self) -> None:
        """uguu 用 output=text 直接回一行直链，不用解 JSON。"""
        from core import engines

        class FakeResponse:
            status_code = 200
            text = "https://d.uguu.se/abc.jpg\n"

            def json(self):
                raise AssertionError("不该去解 JSON")

        class FakeSession:
            def post(self, *args, **kwargs):
                return FakeResponse()

            def close(self):
                pass

        with mock.patch.object(engines, "make_session", lambda *a, **k: FakeSession()):
            self.assertEqual(engines.host_image_publicly(b"x"), "https://d.uguu.se/abc.jpg")

    def test_host_rejects_non_link_body(self) -> None:
        """图床回了一段错误文本，就不能当链接用。"""
        from core import engines

        class FakeResponse:
            status_code = 200
            text = "Upload failed"

            def json(self):
                return {}

        class FakeSession:
            def post(self, *args, **kwargs):
                return FakeResponse()

            def close(self):
                pass

        with mock.patch.object(engines, "make_session", lambda *a, **k: FakeSession()):
            self.assertEqual(engines.host_image_publicly(b"x"), "")

    def test_host_returns_empty_when_upload_fails(self) -> None:
        """图床不通就返回空串，别拿一个假链接去搜。"""
        from core import engines

        class FakeResponse:
            status_code = 500
            text = "boom"

            def json(self):
                return {}

        class FakeSession:
            def post(self, *args, **kwargs):
                return FakeResponse()

            def close(self):
                pass

        with mock.patch.object(engines, "make_session", lambda *a, **k: FakeSession()):
            self.assertEqual(engines.host_image_publicly(b"x"), "")


class LensSerperFallbackTest(unittest.TestCase):
    """Serper 是独立的一池：SerpAPI 还能用就绝不轮到它。"""

    def test_prefers_serpapi_and_leaves_serper_untouched(self) -> None:
        from core.engines import LensEngine
        from core.keypool import KeyPool

        serper = KeyPool(["a" * 40])
        engine = LensEngine(KeyPool(["serpapi-key"]), serper_pool=serper)
        key, pool = engine._pick_key()
        self.assertEqual(key, "serpapi-key")
        self.assertIs(pool, engine.pool)
        # 主池抽中时不能把 Serper 那把也消耗掉
        self.assertEqual(serper.available_count(), 1)

    def test_falls_back_only_when_serpapi_all_cooling(self) -> None:
        from core.engines import LensEngine
        from core.keypool import KeyPool

        engine = LensEngine(KeyPool(["serpapi-key"]), serper_pool=KeyPool(["a" * 40]))
        engine.pool.mark_failure("serpapi-key", 3600)
        key, pool = engine._pick_key()
        self.assertEqual(key, "a" * 40)
        self.assertIs(pool, engine.serper_pool)

    def test_no_fallback_when_not_configured(self) -> None:
        from core.engines import LensEngine
        from core.keypool import KeyPool

        engine = LensEngine(KeyPool(["serpapi-key"]), serper_pool=None)
        engine.pool.mark_failure("serpapi-key", 3600)
        key, _ = engine._pick_key()
        self.assertEqual(key, "")

    def test_empty_result_falls_back_to_serper(self) -> None:
        """s-20261112-1250-f895：SerpAPI 重试完仍空，用 Serper 的 Lens 补一枪。"""
        from core.keypool import KeyPool

        engine = LensEngine(KeyPool(["serpapi-key"]), serper_pool=KeyPool(["a" * 40]), retries=0)
        engine._search_once = lambda raw, image_url="": []  # type: ignore[assignment]
        used: list[str] = []

        def fake_serper(key: str, raw: bytes, image_url: str = "", pool=None):
            used.append(key)
            return [Candidate(engine="lens", url="https://x.com/foo/status/1")]

        engine._search_serper = fake_serper  # type: ignore[assignment]
        items = engine.search(b"img")
        self.assertEqual(used, ["a" * 40])
        self.assertEqual(len(items), 1)

    def test_serper_failure_keeps_result_empty(self) -> None:
        """Serper 自己失败时不能把「SerpAPI 返回空」改写成引擎报错。"""
        from core.engines import EngineError
        from core.keypool import KeyPool

        engine = LensEngine(KeyPool(["serpapi-key"]), serper_pool=KeyPool(["a" * 40]), retries=0)
        engine._search_once = lambda raw, image_url="": []  # type: ignore[assignment]

        def boom(key: str, raw: bytes, image_url: str = "", pool=None):
            raise EngineError("Serper Lens 返回 HTTP 500")

        engine._search_serper = boom  # type: ignore[assignment]
        self.assertEqual(engine.search(b"img"), [])

    def test_serper_fallback_leaves_a_note(self) -> None:
        """2504：替补那枪过去全静默，事后分不清「没补成」和「补了 0 条」。"""
        from core.keypool import KeyPool

        notes: list[str] = []
        engine = LensEngine(
            KeyPool(["serpapi-key"]),
            serper_pool=KeyPool(["a" * 40]),
            retries=0,
            note=notes.append,
        )
        engine._search_once = lambda raw, image_url="": []  # type: ignore[assignment]
        engine._search_serper = lambda key, raw, image_url="", pool=None: []  # type: ignore[assignment]
        self.assertEqual(engine.search(b"img"), [])
        self.assertTrue(any("补出 0 条" in line for line in notes), notes)

    def test_serper_failure_note_says_why(self) -> None:
        from core.engines import EngineError
        from core.keypool import KeyPool

        notes: list[str] = []
        engine = LensEngine(
            KeyPool(["serpapi-key"]),
            serper_pool=KeyPool(["a" * 40]),
            retries=0,
            note=notes.append,
        )
        engine._search_once = lambda raw, image_url="": []  # type: ignore[assignment]

        def boom(key: str, raw: bytes, image_url: str = "", pool=None):
            raise EngineError("Serper Lens 需要公网图片链接，图床没换成")

        engine._search_serper = boom  # type: ignore[assignment]
        engine.search(b"img")
        self.assertTrue(any("Serper 补枪失败" in line and "图床" in line for line in notes), notes)


class LensRetryTest(unittest.TestCase):
    """Lens 对同一张图会随机返回 0 条，空返回必须重试，有结果就立刻停。"""

    def _engine(self, fake, retries: int = 2):
        from core.keypool import KeyPool

        engine = LensEngine(KeyPool(["k"]), retries=retries, retry_delay=0)
        engine._search_once = fake
        return engine

    def test_retries_until_non_empty(self) -> None:
        calls: list[int] = []

        def fake(raw: bytes, image_url: str = "") -> list[Candidate]:
            calls.append(1)
            if len(calls) < 3:
                return []
            return [Candidate(engine="lens", url="https://x.com/foo/status/1")]

        items = self._engine(fake).search(b"img")
        self.assertEqual(len(items), 1)
        self.assertEqual(len(calls), 3)

    def test_stops_on_first_non_empty(self) -> None:
        calls: list[int] = []

        def fake(raw: bytes, image_url: str = "") -> list[Candidate]:
            calls.append(1)
            return [Candidate(engine="lens", url="https://x.com/foo/status/1")]

        items = self._engine(fake, retries=3).search(b"img")
        self.assertEqual(len(items), 1)
        self.assertEqual(len(calls), 1)

    def test_gives_up_after_retries(self) -> None:
        calls: list[int] = []

        def fake(raw: bytes, image_url: str = "") -> list[Candidate]:
            calls.append(1)
            return []

        self.assertEqual(self._engine(fake).search(b"img"), [])
        self.assertEqual(len(calls), 3)

    def test_retries_after_engine_error(self) -> None:
        """上传失败也要重试，别让一次抖动整轮丢掉 Lens。"""
        from core.engines import EngineError

        calls: list[int] = []

        def fake(raw: bytes, image_url: str = "") -> list[Candidate]:
            calls.append(1)
            if len(calls) == 1:
                raise EngineError("图片上传失败")
            return [Candidate(engine="lens", url="https://x.com/foo/status/1")]

        items = self._engine(fake).search(b"img")
        self.assertEqual(len(items), 1)
        self.assertEqual(len(calls), 2)

    def test_raises_when_all_attempts_fail(self) -> None:
        from core.engines import EngineError

        calls: list[int] = []

        def fake(raw: bytes, image_url: str = "") -> list[Candidate]:
            calls.append(1)
            raise EngineError("图片上传失败")

        with self.assertRaises(EngineError):
            self._engine(fake).search(b"img")
        self.assertEqual(len(calls), 3)


class _FakeResponse:
    def __init__(self, text: str, status_code: int = 200) -> None:
        self.text = text
        self.status_code = status_code


class _FakeSession:
    """fail=True 模拟代理线路 TLS 被掐断，False 返回正常页。"""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    def post(self, *args, **kwargs):
        if self.fail:
            raise SSLError("EOF occurred in violation of protocol")
        return _FakeResponse("<html></html>")

    def close(self) -> None:
        pass


class YandexEngineRouteTest(unittest.TestCase):
    """代理线路抖动时要换直连，别整条引擎失败；空壳页要如实说明。"""

    def test_routes_try_proxy_then_direct(self) -> None:
        self.assertEqual(
            YandexEngine("http://127.0.0.1:7890")._routes(),
            ["http://127.0.0.1:7890", ""],
        )
        self.assertEqual(YandexEngine("")._routes(), [""])

    def test_falls_back_to_direct_when_proxy_tls_fails(self) -> None:
        used: list[str] = []

        def factory(proxy: str = ""):
            used.append(proxy)
            return _FakeSession(fail=bool(proxy))

        site = {"url": "https://x.com/a/status/1", "title": "t"}
        with mock.patch("core.engines.make_session", side_effect=factory), mock.patch(
            "core.engines.parse_yandex_sites", return_value=[site]
        ):
            got = YandexEngine("http://127.0.0.1:7890", timeout=10).search(b"img")
        self.assertEqual(used[0], "http://127.0.0.1:7890")
        self.assertEqual(used[1], "")
        self.assertEqual(len(got), 1)

    def test_shell_page_is_reported_as_shell(self) -> None:
        with mock.patch("core.engines.make_session", return_value=_FakeSession()), mock.patch(
            "core.engines.parse_yandex_sites", return_value=None
        ), mock.patch("core.engines.is_yandex_shell_page", return_value=True):
            with self.assertRaises(EngineError) as ctx:
                YandexEngine("", timeout=10).search(b"img")
        self.assertIn("空壳页", str(ctx.exception))

    def test_shell_detection(self) -> None:
        shell = "<html><body><h1>The service is under construction.</h1></body></html>"
        self.assertTrue(is_yandex_shell_page(shell))
        self.assertFalse(is_yandex_shell_page("<html>" + "x" * 6000 + "under construction</html>"))
        self.assertFalse(is_yandex_shell_page("<html><div data-state='{}'></div></html>"))


if __name__ == "__main__":
    unittest.main()
