"""引擎结果解析的单元测试。"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from unittest import mock

from requests.exceptions import SSLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.engines import (  # noqa: E402
    Candidate,
    EngineError,
    host_image_publicly,
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

    def test_empty_result_retries_with_reencoded_jpeg(self) -> None:
        """重试完仍空时，换一份 JPEG 编码再传一次；拿到结果就不再叫 Serper。"""
        import core.engines as engines_mod
        from core.keypool import KeyPool

        engine = LensEngine(KeyPool(["serpapi-key"]), serper_pool=KeyPool(["a" * 40]), retries=0)
        seen: list[bytes] = []

        def fake_once(raw: bytes, image_url: str = "") -> list[Candidate]:
            seen.append(raw)
            if raw == b"\xff\xd8jpeg":
                return [Candidate(engine="lens", url="https://x.com/foo/status/1")]
            return []

        engine._search_once = fake_once  # type: ignore[assignment]
        original = engines_mod._reencode_for_lens
        engines_mod._reencode_for_lens = lambda raw: b"\xff\xd8jpeg"  # type: ignore[assignment]
        try:
            items = engine.search(b"img")
        finally:
            engines_mod._reencode_for_lens = original  # type: ignore[assignment]
        self.assertEqual(seen, [b"img", b"\xff\xd8jpeg"])
        self.assertEqual(len(items), 1)

    def test_reencode_for_lens_returns_jpeg(self) -> None:
        """真拿一张 PNG 转码：出来必须是 JPEG，且比原图小。"""
        import io

        from PIL import Image

        from core.engines import _reencode_for_lens

        buffer = io.BytesIO()
        Image.new("RGBA", (2400, 1600), (200, 30, 40, 255)).save(buffer, format="PNG")
        raw = buffer.getvalue()
        payload = _reencode_for_lens(raw)
        self.assertTrue(payload.startswith(b"\xff\xd8"))
        self.assertLess(len(payload), len(raw))
        self.assertEqual(_reencode_for_lens(b"not an image"), b"")

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


class LensTimeoutBudgetTest(unittest.TestCase):
    """6440：lens 一次要跑「上传图 + 检索」两步，外部时限按两步给。

    原来外层统一给 engine_timeout+5=55 秒，而内部上传和检索各能等 50 秒，
    上传那步稍慢整条 lens 就先被外层砍掉，转码重传和 Serper 补枪全来不及跑。
    """

    def test_deadline_factor_is_two(self) -> None:
        self.assertEqual(LensEngine.deadline_factor, 2)

    def test_timeout_cools_key_briefly(self) -> None:
        """超时是网络抖，不是 key 坏了：只冷却十几秒，别把并发的那单一起锁死。"""
        from core.engines import TIMEOUT_COOLDOWN
        from core.keypool import KeyPool

        class _Resp:
            status_code = 200

            def json(self):
                return {"image_id": "id1"}

        class _Session:
            def post(self, *args, **kwargs):
                return _Resp()

            def get(self, *args, **kwargs):
                raise TimeoutError("timed out")

            def close(self):
                pass

        pool = KeyPool(["k"])
        engine = LensEngine(pool, retries=0)
        with mock.patch("core.engines.make_session", return_value=_Session()):
            with self.assertRaises(EngineError):
                engine._search_once(b"img")
        left = pool._cooldown_until["k"] - time.time()
        self.assertGreater(left, TIMEOUT_COOLDOWN - 6)
        self.assertLessEqual(left, TIMEOUT_COOLDOWN + 1)


class LensUploadQuotaTest(unittest.TestCase):
    """上传接口回 429 是「key 额度用完」，不是「图没传上去」。

    s-20261116-2147-a881：3 把 SerpAPI key 死了 2 把，上传接口回的是
    HTTP 429 + {"error": "Your account has run out of searches."}。旧代码把它
    静默当成上传失败，死 key 一直留在轮询里，三分之二的单 lens 都是 0，日志上却只
    写「图片上传失败」，事后根本分不清是额度用完还是图不行。
    """

    def _resp(self, status=200, payload=None, text=""):
        resp = mock.MagicMock()
        resp.status_code = status
        resp.text = text
        resp.json.return_value = payload if payload is not None else {}
        return resp

    def _session(self, uploads, search=None):
        session = mock.MagicMock()
        session.post.side_effect = list(uploads)
        if search is not None:
            session.get.return_value = search
        return session

    def test_quota_dead_key_is_marked_exhausted(self) -> None:
        from core.keypool import KeyPool

        pool = KeyPool(["dead"])
        session = self._session([self._resp(429, text='{"error": "Your account has run out of searches."}')])
        with mock.patch("core.engines.make_session", return_value=session):
            with self.assertRaises(EngineError) as ctx:
                LensEngine(pool, retries=0)._search_once(b"img")
        self.assertIn("额度", str(ctx.exception))
        self.assertTrue(pool.is_exhausted("dead"))

    def test_search_moves_to_next_key_after_quota_dead(self) -> None:
        from core.keypool import KeyPool

        pool = KeyPool(["dead", "good"])
        search = self._resp(
            payload={"visual_matches": [{"link": "https://x.com/a/status/1", "title": "t"}]}
        )
        sessions = [
            self._session([self._resp(429, text='{"error": "Your account has run out of searches."}')]),
            self._session([self._resp(payload={"image_id": "id1"})], search),
        ]
        with mock.patch("core.engines.make_session", side_effect=sessions):
            with mock.patch("core.engines.time.sleep"):
                items = LensEngine(pool, retries=2, retry_delay=0).search(b"img")
        self.assertEqual(len(items), 1)
        self.assertTrue(pool.is_exhausted("dead"))
        self.assertEqual(pool.available_count(), 1)

    def test_auth_failure_cools_the_key_without_burning_it(self) -> None:
        from core.keypool import KeyPool

        pool = KeyPool(["bad"])
        session = self._session([self._resp(401, text="unauthorized")])
        with mock.patch("core.engines.make_session", return_value=session):
            with self.assertRaises(EngineError):
                LensEngine(pool, retries=0)._search_once(b"img")
        self.assertFalse(pool.is_exhausted("bad"))
        self.assertGreater(pool.cooldown_remaining("bad"), 0)

    def test_plain_bad_gateway_still_treated_as_upload_failure(self) -> None:
        """500/502 是图没传上去，key 还留着，回退按链接搜索。"""
        from core.keypool import KeyPool

        pool = KeyPool(["k"])
        session = self._session([self._resp(502, text="bad gateway")])
        with mock.patch("core.engines.make_session", return_value=session):
            with self.assertRaises(EngineError) as ctx:
                LensEngine(pool, retries=0)._search_once(b"img")
        self.assertIn("图片上传失败", str(ctx.exception))
        self.assertFalse(pool.is_exhausted("k"))

    def test_quota_deadline_comes_from_the_account_api(self) -> None:
        """屏蔽到哪天要问账号接口：这两把 key 的重置日是 11-22，不是月初。"""
        from core import keystate
        from core.keypool import KeyPool

        pool = KeyPool(["dead"])
        session = self._session(
            [self._resp(429, text='{"error": "Your account has run out of searches."}')]
        )
        session.get.return_value = self._resp(payload={"plan_renewal_date": "2026-11-22"})
        with mock.patch("core.engines.make_session", return_value=session):
            with self.assertRaises(EngineError):
                LensEngine(pool, retries=0)._search_once(b"img")
        until = pool.snapshot()[keystate.key_id("dead")]
        self.assertEqual(until, keystate.renewal_deadline("2026-11-22"))

    def test_quota_deadline_falls_back_to_month_end(self) -> None:
        """问不到重置日才按自然月兜底。"""
        from core import keystate
        from core.keypool import KeyPool

        pool = KeyPool(["dead"])
        session = self._session(
            [self._resp(429, text='{"error": "Your account has run out of searches."}')]
        )
        session.get.side_effect = TimeoutError("account api down")
        with mock.patch("core.engines.make_session", return_value=session):
            with self.assertRaises(EngineError):
                LensEngine(pool, retries=0)._search_once(b"img")
        until = pool.snapshot()[keystate.key_id("dead")]
        self.assertAlmostEqual(until, keystate.month_end(), delta=120)


class SauceNaoRouteTest(unittest.TestCase):
    """代理偶发 TLS 握手失败时换直连再试，别因为一次握手丢掉整条。"""

    def _engine(self):
        from core.engines import SauceNaoEngine
        from core.keypool import KeyPool

        return SauceNaoEngine(KeyPool(["k"]), proxy="http://127.0.0.1:6789")

    def test_falls_back_to_direct_when_proxy_tls_fails(self) -> None:
        calls: list[str] = []

        class _Resp:
            status_code = 200

            def json(self):
                return {"header": {"status": 0}, "results": []}

        class _Ok:
            def post(self, *args, **kwargs):
                return _Resp()

            def close(self):
                pass

        class _Bad:
            def post(self, *args, **kwargs):
                raise SSLError("bad handshake")

            def close(self):
                pass

        def fake_session(proxy=""):
            calls.append(proxy)
            return _Bad() if proxy else _Ok()

        with mock.patch("core.engines.make_session", side_effect=fake_session):
            self.assertEqual(self._engine().search(b"img"), [])
        self.assertEqual(calls, ["http://127.0.0.1:6789", ""])

    def test_both_routes_failing_reports_both(self) -> None:
        class _Bad:
            def post(self, *args, **kwargs):
                raise SSLError("bad handshake")

            def close(self):
                pass

        with mock.patch("core.engines.make_session", return_value=_Bad()):
            with self.assertRaises(EngineError) as ctx:
                self._engine().search(b"img")
        message = str(ctx.exception)
        self.assertIn("代理", message)
        self.assertIn("直连", message)


if __name__ == "__main__":
    unittest.main()


class HostImageTest(unittest.TestCase):
    """图床上传失败必须留下原因。

    以前 except 一把吞掉，日志里只剩「图床没换成」，事后分不清是超时、被拒还是
    回了空（s-20261115-2237-0365 就是这样）。而且 uguu 会整段 502，过一次往往
    就通了，所以第一次失败要再试一次。
    """

    def _session(self, status=502, text="<html>502 Bad Gateway</html>", exc=None):
        session = mock.MagicMock()
        if exc is not None:
            session.post.side_effect = exc
        else:
            resp = mock.MagicMock()
            resp.status_code = status
            resp.text = text
            session.post.return_value = resp
        return session

    def test_http_error_is_reported_and_retried_once(self) -> None:
        notes: list[str] = []
        with mock.patch("core.engines.make_session", return_value=self._session()):
            with mock.patch("core.engines.time.sleep"):
                link = host_image_publicly(b"x" * 64, note=notes.append, hosting=("uguu",))
        self.assertEqual(link, "")
        self.assertEqual(len(notes), 2, notes)
        self.assertTrue(all("HTTP 502" in item for item in notes), notes)

    def test_transport_error_names_the_exception(self) -> None:
        notes: list[str] = []
        with mock.patch(
            "core.engines.make_session",
            return_value=self._session(exc=RuntimeError("connection reset")),
        ):
            with mock.patch("core.engines.time.sleep"):
                link = host_image_publicly(b"x" * 64, note=notes.append)
        self.assertEqual(link, "")
        self.assertTrue(any("RuntimeError" in item for item in notes), notes)

    def test_second_attempt_can_succeed(self) -> None:
        bad = self._session()
        good = self._session(status=200, text="https://d.uguu.se/abc.jpg ")
        notes: list[str] = []
        with mock.patch("core.engines.make_session", side_effect=[bad, good]):
            with mock.patch("core.engines.time.sleep"):
                link = host_image_publicly(b"x" * 64, note=notes.append, hosting=("uguu",))
        self.assertEqual(link, "https://d.uguu.se/abc.jpg")
        self.assertEqual(len(notes), 1, notes)

    def test_empty_body_is_reported(self) -> None:
        notes: list[str] = []
        with mock.patch("core.engines.make_session", return_value=self._session(status=200, text="")):
            link = host_image_publicly(b"x" * 64, note=notes.append, hosting=("uguu",))
        self.assertEqual(link, "")
        self.assertTrue(any("没回直链" in item for item in notes), notes)


class HostingChainTest(unittest.TestCase):
    """图床链：uguu → ChatGLM → CliIm，前一家失败就换下一家。

    2026-11-16 实测三家的直链 Google Lens 都抓得到（各回 60 条），所以留了三个，
    哪家抽风都还有下一家顶着。
    """

    def _resp(self, status=200, payload=None, text=""):
        resp = mock.MagicMock()
        resp.status_code = status
        resp.text = text
        resp.json.return_value = payload if payload is not None else {}
        return resp

    def _session(self, *responses):
        session = mock.MagicMock()
        session.post.side_effect = list(responses)
        return session

    def test_falls_back_to_chatglm_when_uguu_fails(self) -> None:
        notes: list[str] = []
        uguu = self._session(self._resp(status=502, text="bad gateway"))
        chatglm = self._session(
            self._resp(payload={"result": {"file_url": "https://t1.chatglm.cn/file/a.jpg?sign=x"}})
        )
        with mock.patch("core.engines.make_session", side_effect=[uguu, uguu, chatglm]):
            with mock.patch("core.engines.time.sleep"):
                link = host_image_publicly(b"x" * 64, note=notes.append)
        self.assertEqual(link, "https://t1.chatglm.cn/file/a.jpg?sign=x")
        self.assertTrue(any("换下一家" in item for item in notes), notes)
        self.assertTrue(any("chatglm" in item.lower() for item in notes), notes)

    def test_falls_back_to_cliim_when_both_above_fail(self) -> None:
        uguu = self._session(self._resp(status=502, text="bad gateway"))
        chatglm = self._session(self._resp(status=500, text="boom"))
        cliim = self._session(
            self._resp(
                payload={"status": "1", "data": {"path": "http://oss-cn-hangzhou.aliyuncs.com/a.jpg"}}
            )
        )
        with mock.patch("core.engines.make_session", side_effect=[uguu, uguu, chatglm, cliim]):
            with mock.patch("core.engines.time.sleep"):
                link = host_image_publicly(b"x" * 64)
        self.assertEqual(link, "http://oss-cn-hangzhou.aliyuncs.com/a.jpg")

    def test_cliim_bare_path_gets_https_prefix(self) -> None:
        cliim = self._session(self._resp(payload={"data": {"path": "oss.example.com/a.jpg"}}))
        with mock.patch("core.engines.make_session", return_value=cliim):
            link = host_image_publicly(b"x" * 64, hosting=("cliim",))
        self.assertEqual(link, "https://oss.example.com/a.jpg")

    def test_disabled_hosts_are_not_touched(self) -> None:
        """配置里只开 cliim 时，uguu 一次都不能碰。"""
        cliim = self._session(self._resp(payload={"data": {"path": "http://oss.cn/a.jpg"}}))
        with mock.patch("core.engines.make_session", return_value=cliim) as made:
            link = host_image_publicly(b"x" * 64, hosting=("cliim",))
        self.assertEqual(link, "http://oss.cn/a.jpg")
        self.assertEqual(made.call_count, 1)

    def test_no_host_enabled_returns_empty(self) -> None:
        notes: list[str] = []
        self.assertEqual(host_image_publicly(b"x" * 64, note=notes.append, hosting=()), "")
        self.assertTrue(any("一家都没开" in item for item in notes), notes)

    def test_chatglm_without_url_is_not_used(self) -> None:
        """回了 JSON 但没有 file_url，就不能拿空串当链接继续往下走。"""
        notes: list[str] = []
        chatglm = self._session(self._resp(payload={"message": "success", "result": {}}))
        with mock.patch("core.engines.make_session", return_value=chatglm):
            link = host_image_publicly(b"x" * 64, note=notes.append, hosting=("chatglm",))
        self.assertEqual(link, "")
        self.assertTrue(any("没回直链" in item for item in notes), notes)
