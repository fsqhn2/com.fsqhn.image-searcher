"""搬运站图床文件名里的 pixiv 作品号：读出来、回 pixiv 核对、顶到结论上面。

起因是 `s-20261124-0954-cdfa`：用户把 pixiv 上的图存下来又发进群，图片地址是
``cdn.honkaihaven.com/art/…/沒有名字-by-lol-…-152342429.webp``——文件名里就写着
pixiv 作品号 152342429 和作者名 ``lol``，可搜索引擎一条 pixiv 都没回来，结论
落在 honkaihaven 这个转载站上。这类站点按 pixiv 作品扒图，图床文件名是它自己拼
的 SEO 串，等于把出处抄在文件名上。

守三件事：① 作品号要真存在、作者名要真对得上（否则 `photo-20261123.jpg` 这种
流水号会撞上旧作品）；② 这条只能说明「出处是哪个作品」，pixiv 未登录给不了图，
比不了指纹，文案必须写明；③ 读到了就把「未找到」换成「原图出自 pixiv」。
"""

from __future__ import annotations

import asyncio
import logging
import types
import unittest
from unittest import mock

from test_plugin_import import import_plugin

FILENAME = (
    "沒有名字-by-lol-with-Arknights-Endfield-Arknights-Endfield-Arknights-152342429.webp"
)
IMAGE_URL = "https://cdn.honkaihaven.com/art/2026/11/22/" + FILENAME


def _plugin():
    module = import_plugin()
    plugin = object.__new__(module.ImageSearcherPlugin)
    plugin._ctx = types.SimpleNamespace(logger=logging.getLogger("test"))
    config = mock.Mock()
    config.plugin.verbose_log = False
    config.plugin.hash_threshold = 8
    plugin._plugin_config_instance = config
    try:
        plugin.config = config
    except AttributeError:
        pass
    return module, plugin


class PixivFilenameHintTest(unittest.TestCase):
    """文件名里的作品号：只认尾部的长数字，别的流水号不许当作品号。"""

    def setUp(self) -> None:
        self.module = import_plugin()

    def test_reads_id_and_filename(self) -> None:
        illust_id, filename = self.module.pixiv_filename_hint(IMAGE_URL)
        self.assertEqual(illust_id, "152342429")
        self.assertEqual(filename, FILENAME)

    def test_ignores_date_like_name(self) -> None:
        """`photo-20261123.jpg` 没有 6~10 位的尾部编号？有，但它会过——所以这
        条只保证「不是文件名里的随便一段数字」，真正的把关在 pixiv 核对那一步。"""
        illust_id, _ = self.module.pixiv_filename_hint("https://a.example/photo-20261123.jpg")
        self.assertEqual(illust_id, "20261123")

    def test_needs_a_long_digit_tail(self) -> None:
        self.assertEqual(self.module.pixiv_filename_hint("https://a.example/img-1234.jpg"), ("", ""))
        self.assertEqual(self.module.pixiv_filename_hint("https://a.example/123456.jpg"), ("", ""))
        self.assertEqual(self.module.pixiv_filename_hint(""), ("", ""))

    def test_url_encoded_name_still_works(self) -> None:
        url = "https://a.example/%E6%B2%92%E6%9C%89%E5%90%8D%E5%AD%97-by-lol-152342429.webp"
        illust_id, filename = self.module.pixiv_filename_hint(url)
        self.assertEqual(illust_id, "152342429")
        self.assertIn("沒有名字", filename)


class TrackPixivSourceTest(unittest.TestCase):
    """`_track_pixiv_source`：作品号回 pixiv 核对，作者名对不上就不采信。"""

    def _run(self, illust_body, urls=None, upload=None):
        module, plugin = _plugin()
        trace: list[dict] = []

        async def illust(self, illust_id):
            illust.calls.append(illust_id)
            return illust_body

        async def mirror(self, illust_id, data, info):
            mirror.calls.append(illust_id)
            return None

        illust.calls = []
        mirror.calls = []

        candidates = []
        for url in urls if urls is not None else [IMAGE_URL]:
            candidates.append(module.Candidate(engine="lens", url="https://honkaihaven.com/gallery/1129766/", image_url=url))
        with mock.patch.object(module.ImageSearcherPlugin, "_vlog", lambda *a, **k: None), mock.patch.object(
            module.ImageSearcherPlugin, "_pixiv_illust", illust
        ), mock.patch.object(module.ImageSearcherPlugin, "_pixiv_mirror_match", mirror):
            info, pick = asyncio.run(plugin._track_pixiv_source(candidates, upload, trace))
        return info, pick, trace, illust.calls

    def test_author_matched_source_is_reported(self) -> None:
        body = {
            "illustId": "152342429",
            "illustTitle": "永恒的新生(1)",
            "userId": "22251303",
            "userName": "lol",
            "xRestrict": 1,
            "createDate": "2026-11-21T21:22:00+00:00",
            "pageCount": 9,
        }
        info, pick, trace, calls = self._run(body)
        self.assertEqual(calls, ["152342429"])
        self.assertIsNotNone(info)
        self.assertEqual(info["title"], "永恒的新生(1)")
        self.assertEqual(info["user_name"], "lol")
        self.assertEqual(info["url"], "https://www.pixiv.net/artworks/152342429")
        self.assertEqual(info["page_count"], 9)
        self.assertEqual(info["x_restrict"], 1)
        self.assertIn("152342429", info["read_from"])
        self.assertEqual(trace[-1]["state"], "hit")
        # 没给原图就没有可比对的，不能凭空造候选
        self.assertIsNone(pick)

    def test_author_name_missing_from_filename_is_rejected(self) -> None:
        """pixiv 上那个作品号确实是别人的作品（流水号撞上旧作品）→ 不采信。"""
        body = {"illustId": "20261123", "illustTitle": "无关旧作", "userName": "someone", "userId": "1"}
        info, _pick, trace, _calls = self._run(
            body, urls=["https://a.example/photo-20261123.jpg"]
        )
        self.assertIsNone(info)
        self.assertEqual(trace[-1]["state"], "none")

    def test_no_id_in_urls_is_skipped(self) -> None:
        info, _pick, trace, calls = self._run(
            {}, urls=["https://img.nga.cn/attachments/mon_202611/23/niQ75-c9v2ZdT3cSsg-pc.jpg"]
        )
        self.assertIsNone(info)
        self.assertEqual(calls, [])
        self.assertEqual(trace[-1]["state"], "skip")

    def test_at_most_three_ids_are_probed(self) -> None:
        urls = ["https://a.example/a-%d.webp" % number for number in (152342401, 152342402, 152342403, 152342404)]
        _info, _pick, _trace, calls = self._run({}, urls=urls)
        self.assertEqual(len(calls), 3)


class PixivMirrorMatchTest(unittest.TestCase):
    """pixiv.re 取回原图比指纹：对上了才造 pixiv 候选。"""

    def _image(self, seed: int, size: int = 64) -> bytes:
        import io

        from PIL import Image

        img = Image.new("L", (size, size))
        pixels = img.load()
        for y in range(size):
            for x in range(size):
                pixels[x, y] = (x * (seed + 1) + y * seed * 3) % 256
        buffer = io.BytesIO()
        img.save(buffer, format="PNG")
        return buffer.getvalue()

    def _run(self, remote: bytes | None, upload: bytes | None, threshold: int = 8):
        module, plugin = _plugin()
        plugin._plugin_config_instance.plugin.hash_threshold = threshold

        async def download(self, url):
            download.calls.append(url)
            return remote

        download.calls = []
        info = {
            "illust_id": "152342429",
            "title": "永恒的新生(1)",
            "user_name": "lol",
            "url": "https://www.pixiv.net/artworks/152342429",
        }
        with mock.patch.object(module.ImageSearcherPlugin, "_vlog", lambda *a, **k: None), mock.patch.object(
            module.ImageSearcherPlugin, "_download", download
        ):
            pick = asyncio.run(plugin._pixiv_mirror_match("152342429", upload, info))
        return pick, info, download.calls

    def test_matching_image_becomes_a_candidate(self) -> None:
        image = self._image(3)
        pick, info, calls = self._run(image, image)
        self.assertEqual(calls[0], "https://pixiv.re/152342429.jpg")
        self.assertEqual(info["distance"], 0)
        self.assertTrue(info["verified"])
        self.assertIsNotNone(pick)
        self.assertEqual(pick.status, "match")
        self.assertEqual(pick.distance, 0)
        self.assertEqual(pick.url, "https://www.pixiv.net/artworks/152342429")

    def test_different_image_is_not_a_candidate(self) -> None:
        pick, info, _calls = self._run(self._image(1), self._image(9))
        self.assertIsNone(pick)
        self.assertGreater(info["distance"], 8)
        self.assertFalse(info["verified"])

    def test_no_upload_skips_the_download(self) -> None:
        pick, info, calls = self._run(self._image(3), None)
        self.assertIsNone(pick)
        self.assertEqual(calls, [])
        self.assertNotIn("distance", info)

    def test_download_failure_keeps_only_the_source(self) -> None:
        pick, info, _calls = self._run(None, self._image(3))
        self.assertIsNone(pick)
        self.assertNotIn("distance", info)


class PixivSourceLinesTest(unittest.TestCase):
    """首屏：读到了就把「未找到」换掉，并写明没能比指纹。"""

    def _make_outcome(self, module, **kwargs):
        return module.SearchOutcome(status="ok", **kwargs)

    def test_lines_name_the_artwork(self) -> None:
        module = import_plugin()
        outcome = self._make_outcome(
            module,
            pixiv_source={
                "illust_id": "152342429",
                "title": "永恒的新生(1)",
                "user_name": "lol",
                "user_id": "22251303",
                "x_restrict": 1,
                "page_count": 9,
                "create_date": "2026-11-21T21:22:00+00:00",
                "url": "https://www.pixiv.net/artworks/152342429",
                "read_from": FILENAME,
            },
        )
        text = "\n".join(module.formatting._pixiv_source_lines(outcome))
        self.assertIn("原图出自 pixiv", text)
        self.assertIn("永恒的新生(1)", text)
        self.assertIn("by lol", text)
        self.assertIn("R-18", text)
        self.assertIn("9 页", text)
        self.assertIn("2026-11-21", text)
        self.assertIn("https://www.pixiv.net/artworks/152342429", text)
        self.assertIn("没能比指纹", text)

    def test_verified_note_carries_distance(self) -> None:
        """取回 pixiv 原图比过的，就写距离，不再说「没能比指纹」。"""
        module = import_plugin()
        outcome = self._make_outcome(
            module,
            pixiv_source={
                "illust_id": "152342429",
                "title": "永恒的新生(1)",
                "user_name": "lol",
                "url": "https://www.pixiv.net/artworks/152342429",
                "distance": 0,
                "verified": True,
            },
        )
        text = "\n".join(module.formatting._pixiv_source_lines(outcome))
        self.assertIn("距离 0", text)
        self.assertNotIn("没能比指纹", text)

    def test_compact_keeps_two_lines(self) -> None:
        module = import_plugin()
        outcome = self._make_outcome(
            module,
            pixiv_source={"illust_id": "1", "title": "t", "url": "https://www.pixiv.net/artworks/1"},
        )
        lines = module.formatting._pixiv_source_lines(outcome, compact=True)
        self.assertEqual(len(lines), 2)

    def test_leading_line_replaces_not_found(self) -> None:
        """降级的转载站 + 读到的 pixiv 作品号：首屏第一行是出处，不是「未找到」。"""
        module = import_plugin()
        best = module.Candidate(
            engine="lens",
            url="https://honkaihaven.com/gallery/1129766/",
            title="漫画预告 | HonkaiHaven",
            status="suspect",
            downgrade="repost",
            distance=0,
            platform="honkaihaven.com",
        )
        outcome = self._make_outcome(
            module,
            best=best,
            candidates=[best],
            pixiv_source={
                "illust_id": "152342429",
                "title": "永恒的新生(1)",
                "user_name": "lol",
                "url": "https://www.pixiv.net/artworks/152342429",
            },
        )
        task = module.SearchTask(task_id="t", stream_id="s", user_id="u", outcome=outcome)
        text, _images = module.formatting.build_first_reply(task, command="盆地找鸡")
        lines = text.splitlines()
        self.assertIn("盆地找鸡结果", lines[0])
        self.assertIn("原图出自 pixiv", lines[1])
        self.assertNotIn("未找到可靠出处", text)

    def test_forum_thread_hint_when_only_nga(self) -> None:
        """b62e／ecaa 只搜到 NGA：插件读不了 NGA 正文，得提示去翻回复。"""
        module = import_plugin()
        nga = module.Candidate(
            engine="lens",
            url="https://bbs.nga.cn/read.php?tid=47962202",
            title="破事氵] 全塔卫二最舒嫩浅草的角色出现了NGA玩家社区",
            status="suspect",
            downgrade="repost",
            distance=23,
            platform="bbs.nga.cn",
        )
        outcome = self._make_outcome(module, best=None, candidates=[nga])
        task = module.SearchTask(task_id="t", stream_id="s", user_id="u", outcome=outcome)
        text, _images = module.formatting.build_first_reply(task, command="盆地找鸡")
        self.assertIn("论坛讨论帖", text)
        self.assertIn("bbs.nga.cn", text)

    def test_no_forum_hint_without_forum_candidates(self) -> None:
        module = import_plugin()
        outcome = self._make_outcome(module, best=None, candidates=[])
        task = module.SearchTask(task_id="t", stream_id="s", user_id="u", outcome=outcome)
        text, _images = module.formatting.build_first_reply(task, command="盆地找鸡")
        self.assertNotIn("论坛讨论帖", text)


if __name__ == "__main__":
    unittest.main()
