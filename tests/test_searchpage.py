"""搜索页反解：URL 识别、关键词归一、检索结果解析、指纹实锤取舍。

起因是本单 `s-20261121-1955-bae5`：Lens 把候选落在
`search.bilibili.com/all?keyword=XxMiss Naruko/`（指纹距离 27），那是 JS 渲染的
搜索结果列表、解析不出发布者，视觉判「疑似同一个人」也只能降成 suspect，
整单 `not_found`。这一层做的就是拿页面上的关键词去反查账号，再用指纹实锤。

这里只测纯逻辑与主干预埋点；网络取号（`core/bilibili.py` 打 B 站接口）不写
联调用例——接口有风控，靠 `.pai/temp/` 下的实测脚本验。
"""

from __future__ import annotations

import asyncio
import logging
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import bilibili, searchpage  # noqa: E402
from core.engines import Candidate  # noqa: E402
from tests.test_plugin_import import import_plugin  # noqa: E402


class SearchPageQueryTest(unittest.TestCase):
    """哪一页算搜索页、关键词是什么。"""

    def test_the_case_that_started_it(self) -> None:
        """本单那条链接：关键词带空格、末尾还多一个 `/`。"""
        found = searchpage.search_page_query(
            "https://search.bilibili.com/all?keyword=XxMiss Naruko/"
        )
        self.assertEqual(found, ("B站", "XxMiss Naruko"))

    def test_url_encoded_keyword(self) -> None:
        """URL 编码过的关键词要解码；`%2F` 是编码后的尾斜杠，同样要去掉。"""
        self.assertEqual(
            searchpage.search_page_query(
                "https://search.bilibili.com/all?keyword=%E5%88%9D%E9%9F%B3%20Miku"
            ),
            ("B站", "初音 Miku"),
        )
        self.assertEqual(
            searchpage.search_page_query(
                "https://search.bilibili.com/all?keyword=Naruko%2F"
            ),
            ("B站", "Naruko"),
        )

    def test_quotes_are_stripped(self) -> None:
        """引擎会把关键词连引号一起塞进 query。"""
        for url in (
            'https://search.bilibili.com/all?keyword="XxMiss Naruko"',
            "https://search.bilibili.com/all?keyword=%22XxMiss%20Naruko%22",
            "https://search.bilibili.com/all?keyword=「XxMiss Naruko」",
        ):
            self.assertEqual(
                searchpage.search_page_query(url), ("B站", "XxMiss Naruko"), url
            )

    def test_search_paths(self) -> None:
        """`/all`、`/search`、`/video` 和搜索站根路径都是搜索结果列表。"""
        for path in ("/all", "/search", "/video", "/", ""):
            url = f"https://search.bilibili.com{path}?keyword=Naruko"
            self.assertEqual(searchpage.search_page_query(url), ("B站", "Naruko"), url)

    def test_space_page_is_not_a_search_page(self) -> None:
        """空间页本身就能解析出 mid，不是搜索页。"""
        self.assertIsNone(searchpage.search_page_query("https://space.bilibili.com/123456"))

    def test_ordinary_pages_return_none(self) -> None:
        """普通页面一律不认：视频页、帖子页、别的站、非搜索路径。"""
        for url in (
            "https://www.bilibili.com/video/BV1xx411c7mD",
            "https://search.bilibili.com/upuser?keyword=Naruko",
            "https://x.com/ran3ran3ran3/status/1",
            "https://example.com/?q=Naruko",
            "https://search.bilibili.com/all",
            "https://search.bilibili.com/all?keyword=",
            "https://search.bilibili.com/all?keyword=%20%20",
            "",
            "not a url",
        ):
            self.assertIsNone(searchpage.search_page_query(url), url)

    def test_title_guard(self) -> None:
        """标题写着「个人空间」的页面不当搜索页（关键词只从 URL 取）。"""
        self.assertIsNone(
            searchpage.search_page_query(
                "https://search.bilibili.com/all?keyword=Naruko", "某某的个人空间"
            )
        )


class KeywordCleanTest(unittest.TestCase):
    """关键词归一：去引号、去尾斜杠、压空白。"""

    def test_trailing_slash_and_blank(self) -> None:
        self.assertEqual(searchpage.clean_keyword(" XxMiss Naruko/ "), "XxMiss Naruko")
        self.assertEqual(searchpage.clean_keyword("XxMiss  Naruko //"), "XxMiss Naruko")
        self.assertEqual(searchpage.clean_keyword("XxMiss Naruko／"), "XxMiss Naruko")

    def test_quotes(self) -> None:
        self.assertEqual(searchpage.clean_keyword('"Naruko"'), "Naruko")
        self.assertEqual(searchpage.clean_keyword("“Naruko”"), "Naruko")
        self.assertEqual(searchpage.clean_keyword("「Naruko」"), "Naruko")

    def test_decode(self) -> None:
        self.assertEqual(searchpage.clean_keyword("%E5%88%9D%E9%9F%B3"), "初音")

    def test_empty(self) -> None:
        self.assertEqual(searchpage.clean_keyword(""), "")
        self.assertEqual(searchpage.clean_keyword(None), "")


class AccountsFromSearchTest(unittest.TestCase):
    """检索接口返回 → 账号候选。"""

    def test_videos_group_by_mid(self) -> None:
        """同一个号的多条投稿合成一条，封面去重、协议相对地址补 https。"""
        payload = {
            "code": 0,
            "data": {
                "result": [
                    {
                        "mid": 1001,
                        "author": '<em class="keyword">Naruko</em>酱',
                        "bvid": "BV1aa",
                        "pic": "//i0.hdslb.com/bfs/archive/aa.jpg",
                    },
                    {
                        "mid": 1001,
                        "author": "Naruko酱",
                        "bvid": "BV1bb",
                        "pic": "//i0.hdslb.com/bfs/archive/bb.jpg",
                    },
                    {"mid": 0, "author": "没有号", "pic": "//i0.hdslb.com/x.jpg"},
                    {"mid": "abc", "author": "不是号"},
                ]
            },
        }
        found = searchpage.accounts_from_videos(payload)
        self.assertEqual(len(found), 1)
        account = found[0]
        self.assertEqual(account["mid"], "1001")
        self.assertEqual(account["name"], "Naruko酱")
        self.assertEqual(account["url"], "https://space.bilibili.com/1001")
        self.assertEqual(
            account["images"],
            [
                "https://i0.hdslb.com/bfs/archive/aa.jpg",
                "https://i0.hdslb.com/bfs/archive/bb.jpg",
            ],
        )
        self.assertEqual(
            account["videos"],
            [
                "https://www.bilibili.com/video/BV1aa",
                "https://www.bilibili.com/video/BV1bb",
            ],
        )

    def test_videos_limit_and_image_cap(self) -> None:
        payload = {
            "data": {
                "result": [
                    {"mid": 1, "author": "a", "pic": f"//i0.hdslb.com/{n}.jpg"}
                    for n in range(6)
                ]
                + [{"mid": 2, "author": "b", "pic": "//i0.hdslb.com/b.jpg"}]
            }
        }
        found = searchpage.accounts_from_videos(payload, limit=2, images_per_account=2)
        self.assertEqual([item["mid"] for item in found], ["1", "2"])
        self.assertEqual(len(found[0]["images"]), 2)

    def test_users(self) -> None:
        payload = {
            "data": {
                "result": [
                    {"mid": 2002, "uname": '<em class="keyword">xxmiss</em>'},
                    {"mid": 2002, "uname": "重复的"},
                    {"mid": 0, "uname": "空号"},
                ]
            }
        }
        found = searchpage.accounts_from_users(payload)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["mid"], "2002")
        self.assertEqual(found[0]["name"], "xxmiss")
        self.assertEqual(found[0]["images"], [])

    def test_garbage_payload(self) -> None:
        for payload in (None, {}, {"data": None}, {"data": {"result": "no"}}, []):
            self.assertEqual(searchpage.accounts_from_videos(payload), [])
            self.assertEqual(searchpage.accounts_from_users(payload), [])


class PickHitTest(unittest.TestCase):
    """实锤判定：只有指纹能放行。"""

    def _accounts(self) -> list[dict]:
        return [
            {
                "mid": "1",
                "name": "大号",
                "url": "https://space.bilibili.com/1",
                "images": ["https://i0.hdslb.com/a.jpg", "https://i0.hdslb.com/b.jpg"],
            },
            {
                "mid": "2",
                "name": "小号",
                "url": "https://space.bilibili.com/2",
                "images": ["https://i0.hdslb.com/c.jpg"],
            },
        ]

    def test_closest_within_threshold_wins(self) -> None:
        distances = {
            "https://i0.hdslb.com/a.jpg": 20,
            "https://i0.hdslb.com/b.jpg": 6,
            "https://i0.hdslb.com/c.jpg": 3,
        }
        hit = searchpage.pick_hit(self._accounts(), distances, threshold=8)
        self.assertIsNotNone(hit)
        account, image, distance = hit
        self.assertEqual(account["mid"], "2")
        self.assertEqual(image, "https://i0.hdslb.com/c.jpg")
        self.assertEqual(distance, 3)

    def test_threshold_is_the_only_gate(self) -> None:
        """距离都在阈值外时谁都不算命中——粉丝再多也不开绿灯。"""
        distances = {
            "https://i0.hdslb.com/a.jpg": 9,
            "https://i0.hdslb.com/b.jpg": 27,
            "https://i0.hdslb.com/c.jpg": None,
        }
        self.assertIsNone(searchpage.pick_hit(self._accounts(), distances, threshold=8))

    def test_missing_distance_is_not_a_hit(self) -> None:
        """图没下载下来（距离 None）不能当成对上。"""
        self.assertIsNone(
            searchpage.pick_hit(
                self._accounts(), {"https://i0.hdslb.com/a.jpg": None}, threshold=8
            )
        )

    def test_no_images(self) -> None:
        accounts = [{"mid": "3", "name": "没图", "url": "u", "images": []}]
        self.assertIsNone(searchpage.pick_hit(accounts, {}, threshold=8))


class NoteTest(unittest.TestCase):
    """两种结果的文案：命中和「反解到号但图对不上」。"""

    def test_hit_note(self) -> None:
        text = searchpage.hit_note("XxMiss Naruko", "Naruko酱", 4)
        self.assertIn("XxMiss Naruko", text)
        self.assertIn("Naruko酱", text)
        self.assertIn("指纹距离 4", text)

    def test_miss_note_single(self) -> None:
        self.assertEqual(
            searchpage.miss_note("Naruko", "公开图里没有这张"),
            "关键词反解到疑似账号 @Naruko，公开图里没有这张",
        )

    def test_miss_note_many(self) -> None:
        text = searchpage.miss_note(["a", "b", "c"], "图对不上")
        self.assertIn("3 个疑似账号", text)
        self.assertIn("@a", text)
        # 粉丝数、播放量一概不写进说明：准入只有指纹。
        self.assertNotIn("粉丝", text)

    def test_miss_note_empty(self) -> None:
        self.assertIn("关键词反解到疑似账号", searchpage.miss_note([]))


class BilibiliSignTest(unittest.TestCase):
    """wbi 签名与解析的小函数（不联调）。"""

    def test_mixin_key(self) -> None:
        img = "7cd084941338484aae1ad9425b84077c"
        sub = "4932caff0ff746eab6f01bf08b70ac45"
        mixin = bilibili.mixin_key(img, sub)
        self.assertEqual(len(mixin), 32)
        self.assertTrue(set(mixin) <= set(img + sub))
        self.assertEqual(bilibili.mixin_key("", ""), "")

    def test_sign_query_is_stable(self) -> None:
        """同一批参数 + 同一个时间戳 → 同一个签名（`wts` 进签名，不固定没法断言）。"""
        params = {"search_type": "video", "keyword": "XxMiss Naruko", "page": 1}
        keys = ("7cd084941338484aae1ad9425b84077c", "4932caff0ff746eab6f01bf08b70ac45")
        first = bilibili.sign_query(params, *keys, now=1700000000)
        second = bilibili.sign_query(params, *keys, now=1700000000)
        self.assertEqual(first, second)
        self.assertIn("w_rid=", first)
        self.assertIn("wts=1700000000", first)
        self.assertIn("search_type=video", first)

    def test_sign_query_without_keys(self) -> None:
        with self.assertRaises(bilibili.BilibiliError):
            bilibili.sign_query({"page": 1}, "", "")


class ResolveSearchPagesTest(unittest.TestCase):
    """主干上的接线：开关、无搜索页、命中、命中不了，四种情况。"""

    def _plugin(self, *, enabled: bool = True):
        module = import_plugin()
        plugin = object.__new__(module.ImageSearcherPlugin)
        plugin._ctx = types.SimpleNamespace(logger=logging.getLogger("test"))
        config = mock.Mock()
        config.plugin.hash_threshold = 8
        config.plugin.download_timeout = 12
        config.plugin.proxy = ""
        config.plugin.verbose_log = False
        config.plugin.allow_fake_ip = False
        config.plugin.fake_ip_ranges = []
        config.plugin.search_page_resolve = enabled
        # 宿主的 `config` 是只读 property（内部读 `_plugin_config_instance`），本地桩里
        # 是可写类属性。直接 `plugin.config = …` 在服务器上会 AttributeError，
        # 所以两边都写一遍：桩吃赋值，宿主吃那个下划线字段。
        plugin._plugin_config_instance = config
        try:
            plugin.config = config
        except AttributeError:
            pass
        plugin._verify_pool = None
        return module, plugin

    def _page(self) -> Candidate:
        return Candidate(
            engine="lens",
            url="https://search.bilibili.com/all?keyword=XxMiss Naruko/",
            title="XxMiss Naruko-哔哩哔哩_bilibili",
            platform="B站",
            distance=27,
            status="suspect",
        )

    def test_switch_off(self) -> None:
        _module, plugin = self._plugin(enabled=False)
        found, state, note = asyncio.run(plugin._resolve_search_pages([self._page()], 123))
        self.assertEqual(found, [])
        self.assertEqual(state, "skip")
        self.assertIn("关掉", note)

    def test_no_search_page(self) -> None:
        _module, plugin = self._plugin()
        page = Candidate(engine="lens", url="https://x.com/a/status/1", distance=3)
        found, state, note = asyncio.run(plugin._resolve_search_pages([page], 123))
        self.assertEqual(found, [])
        self.assertEqual(state, "skip")
        self.assertIn("搜索页", note)

    def test_no_hash(self) -> None:
        _module, plugin = self._plugin()
        found, state, _note = asyncio.run(plugin._resolve_search_pages([self._page()], None))
        self.assertEqual((found, state), ([], "skip"))

    def test_miss_keeps_suspect_and_explains(self) -> None:
        """反解到号、图对不上：结论不动，只给那条搜索页候选挂一行说明。"""
        module, plugin = self._plugin()
        page = self._page()
        accounts = [
            {"mid": "1", "name": "同名号", "url": "https://space.bilibili.com/1", "images": ["https://i0.hdslb.com/a.jpg"]}
        ]
        with mock.patch.object(
            plugin, "_bilibili_accounts", mock.AsyncMock(return_value=accounts)
        ), mock.patch.object(
            plugin, "_search_page_distances", mock.AsyncMock(return_value={"https://i0.hdslb.com/a.jpg": 27})
        ):
            found, state, _note = asyncio.run(plugin._resolve_search_pages([page], 123))
        self.assertEqual(found, [])
        self.assertEqual(state, "none")
        self.assertEqual(page.status, "suspect")
        self.assertIn("关键词反解到疑似账号", page.account_note)

    def test_hit_creates_account_candidate(self) -> None:
        """反解到号且封面指纹对上：新建账号页候选，走既有复核后当结论。"""
        module, plugin = self._plugin()
        page = self._page()
        accounts = [
            {"mid": "9", "name": "真号", "url": "https://space.bilibili.com/9", "images": ["https://i0.hdslb.com/9.jpg"]}
        ]

        def fake_verify(item, target_hash, threshold, proxy="", timeout=12.0, allow_nets=()):
            item.status = "match"
            item.distance = 2
            return item

        with mock.patch.object(
            plugin, "_bilibili_accounts", mock.AsyncMock(return_value=accounts)
        ), mock.patch.object(
            plugin, "_search_page_distances", mock.AsyncMock(return_value={"https://i0.hdslb.com/9.jpg": 2})
        ), mock.patch.object(module, "verify_candidate", side_effect=fake_verify):
            found, state, note = asyncio.run(plugin._resolve_search_pages([page], 123))
        self.assertEqual(state, "hit")
        self.assertEqual(len(found), 1)
        candidate = found[0]
        self.assertEqual(candidate.url, "https://space.bilibili.com/9")
        self.assertEqual(candidate.status, "match")
        self.assertEqual(candidate.distance, 2)
        self.assertEqual(candidate.platform, "B站")
        # 空间页只解析得出 mid，`extract_identity` 会写成「UID 9」这种编号形式。
        self.assertIn("9", candidate.handle)
        self.assertIn("XxMiss Naruko", candidate.note)
        self.assertIn("9", note)
        # 结论落在 UP 主主页上，不是那一篇投稿：得写明白，也别让它冒充帖子。
        self.assertTrue(candidate.account_page)
        self.assertIn("主页", candidate.account_note)

    def test_miss_says_when_covers_could_not_be_fetched(self) -> None:
        """封面一张都没下下来，不能写成「图对不上」——那会把没跑完说成跑过了。"""
        _module, plugin = self._plugin()
        page = self._page()
        accounts = [
            {"mid": "1", "name": "同名号", "url": "https://space.bilibili.com/1", "images": ["https://i0.hdslb.com/a.jpg"]}
        ]
        with mock.patch.object(
            plugin, "_bilibili_accounts", mock.AsyncMock(return_value=accounts)
        ), mock.patch.object(
            plugin, "_search_page_distances", mock.AsyncMock(return_value={"https://i0.hdslb.com/a.jpg": None})
        ):
            found, state, note = asyncio.run(plugin._resolve_search_pages([page], 123))
        self.assertEqual((found, state), ([], "none"))
        self.assertIn("没下下来", note)
        self.assertIn("没下下来", page.account_note)

    def test_distances_keep_partial_results_on_timeout(self) -> None:
        """一张慢图不该把已经算完的那几张一起丢掉。"""
        module, plugin = self._plugin()
        accounts = [{"images": ["fast", "slow"]}]

        async def fake_download(url: str) -> bytes:
            if url == "slow":
                await asyncio.sleep(5)
            return b"img"

        with mock.patch.object(plugin, "_download", fake_download), mock.patch.object(
            module, "dhash", lambda raw: 1
        ), mock.patch.object(module, "hamming", lambda left, right: 4):
            got = asyncio.run(plugin._search_page_distances(accounts, 0, 0.2))

        self.assertEqual(got.get("fast"), 4)
        self.assertNotIn("slow", got)

    def test_account_page_does_not_top_a_close_post(self) -> None:
        """距离 2 的空间页不能顶掉距离 3 的那篇投稿。"""
        module, _plugin = self._plugin()
        space = Candidate(
            engine="lens", url="https://space.bilibili.com/9", status="match", distance=2
        )
        space.account_page = True
        post = Candidate(
            engine="lens", url="https://www.bilibili.com/video/BV1xx411c7mD", status="match", distance=3
        )
        self.assertIs(module.ImageSearcherPlugin._pick_best([space, post]), post)

    def test_account_page_still_wins_when_posts_are_far(self) -> None:
        """真实帖子差太多（超出阈值一半）就别硬顶，还是用空间页。"""
        module, _plugin = self._plugin()
        space = Candidate(
            engine="lens", url="https://space.bilibili.com/9", status="match", distance=2
        )
        space.account_page = True
        post = Candidate(
            engine="lens", url="https://www.bilibili.com/video/BV1xx411c7mD", status="match", distance=7
        )
        self.assertIs(module.ImageSearcherPlugin._pick_best([space, post]), space)

    def test_space_url_is_not_a_listing_page(self) -> None:
        """UP 主主页会被列表页判定误伤的话，命中也升不成结论。"""
        from core.extract import listing_page_kind

        self.assertEqual(listing_page_kind("https://space.bilibili.com/9", "某某 的B站空间"), "")


if __name__ == "__main__":
    unittest.main()
