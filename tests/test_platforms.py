"""其它平台账号反查：候选 URL 里的 TikTok / IG / B站 / 微博号要落到线索里。

起因是 `s-20261123-1018-838e`：候选里明明有一条
`tiktok.com/@starrychibun/video/…`，`extract.match_account()` 也早就解析出了
handle，但反查链路只认 X（`_hunt_repost_author` 走 `_repost_x_handles()`），
这个号从头到尾没人读过，用户自然也看不到。

这一组守住三件事：

1. 候选里的非 X 账号要被收集成线索，X 和小红书/抖音不掺进来；
2. 能核图的（B 站投稿封面、微博带图帖）核到就换结论，核不上只标「没核到」；
3. 结论落在 X 号上时，拿 handle 去微博 / B 站搜同名只当线索，不许改结论。
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

from core import platforms  # noqa: E402
from core.engines import Candidate  # noqa: E402
from tests.test_plugin_import import import_plugin  # noqa: E402


def _candidate(module, url: str, **kwargs):
    item = module.Candidate(engine=kwargs.pop("engine", "lens"), url=url, **kwargs)
    return item.finalize()


class AccountFromUrlTest(unittest.TestCase):
    """URL → 账号：带账号的帖子页也算，纯帖子页/搜索页不算。"""

    def test_tiktok_video_url_keeps_the_handle(self) -> None:
        """838e 那条：链接是视频页，但 @handle 就写在路径里。"""
        found = platforms.account_from_url("https://www.tiktok.com/@starrychibun/video/7301")
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual(found["kind"], "tiktok")
        self.assertEqual(found["handle"], "starrychibun")
        self.assertEqual(found["platform"], "TikTok")
        self.assertFalse(found["verifiable"])

    def test_bilibili_space_page(self) -> None:
        found = platforms.account_from_url("https://space.bilibili.com/123456")
        assert found is not None
        self.assertEqual(found["kind"], "bilibili")
        self.assertEqual(found["uid"], "123456")
        self.assertTrue(found["verifiable"])

    def test_weibo_uid_page(self) -> None:
        found = platforms.account_from_url("https://weibo.com/u/6880826706")
        assert found is not None
        self.assertEqual(found["kind"], "weibo")
        self.assertEqual(found["handle"], "6880826706")

    def test_plain_post_page_is_not_an_account(self) -> None:
        """微博帖子页只有帖号，抽不出人：不能当账号。"""
        self.assertIsNone(platforms.account_from_url("https://weibo.com/status/1234567890"))

    def test_display_uses_uid_for_digits(self) -> None:
        self.assertEqual(
            platforms.display({"platform": "B站", "handle": "123456"}), "B站 UID 123456"
        )
        self.assertEqual(
            platforms.display({"platform": "TikTok", "handle": "starrychibun"}),
            "TikTok @starrychibun",
        )


class CollectTest(unittest.TestCase):
    """候选池里哪些号收、哪些不收。"""

    def test_skips_x_and_keeps_other_platforms(self) -> None:
        module = import_plugin()
        rows = platforms.collect(
            [
                _candidate(module, "https://x.com/usagi_rr/status/1"),
                _candidate(module, "https://www.tiktok.com/@starrychibun/video/7301", distance=0),
                _candidate(module, "https://space.bilibili.com/9", name="真号"),
                # 小红书 / 抖音这轮明确不做，不能混进线索
                _candidate(module, "https://www.douyin.com/user/MS4wLjABAAAA"),
            ]
        )
        kinds = [row["kind"] for row in rows]
        self.assertEqual(sorted(kinds), ["bilibili", "tiktok"])

    def test_verifiable_first_then_distance(self) -> None:
        module = import_plugin()
        rows = platforms.collect(
            [
                _candidate(module, "https://www.tiktok.com/@a_long_handle", distance=0),
                _candidate(module, "https://space.bilibili.com/2", distance=6),
                _candidate(module, "https://space.bilibili.com/1", distance=2),
            ]
        )
        # 能核图的排前面，同一平台里距离近的再往前
        self.assertEqual([row["handle"] for row in rows], ["1", "2", "a_long_handle"])

    def test_same_account_twice_only_once(self) -> None:
        module = import_plugin()
        rows = platforms.collect(
            [
                _candidate(module, "https://www.tiktok.com/@starrychibun/video/1", distance=3),
                _candidate(module, "https://www.tiktok.com/@starrychibun/video/2", distance=5),
            ]
        )
        self.assertEqual(len(rows), 1)

    def test_far_candidates_do_not_contribute_accounts(self) -> None:
        """量出来距离三十几的候选只是「同题材」，上面的号不能当她的号摆出去。

        但 `distance=None` 的要留着——838e 的 TikTok、19ab 的 Instagram 都是
        lens 的视觉候选，页面不给主图才量不出距离，挡掉就把最该给的线索扔了。
        """
        module = import_plugin()
        rows = platforms.collect(
            [
                _candidate(module, "https://www.instagram.com/sen_kitsune/", distance=None),
                _candidate(module, "https://www.tiktok.com/@far_away", distance=31),
                _candidate(module, "https://www.tiktok.com/@close_one", distance=6),
            ]
        )
        self.assertEqual([row["handle"] for row in rows], ["close_one", "sen_kitsune"])

    def test_page_title_is_not_used_as_account_name(self) -> None:
        """TikTok 给的是视频文案、IG 给的是带站点后缀的页面标题，都不能当账号名。"""
        self.assertEqual(platforms.clean_name("Im sideways #klee #kleecosplay"), "")
        self.assertEqual(
            platforms.clean_name("阡狐Huni • Instagram photos and videos"), "阡狐Huni"
        )
        self.assertEqual(platforms.clean_name("某人 的个人空间-哔哩哔哩视频"), "某人")
        self.assertEqual(platforms.clean_name("小仓千代w"), "小仓千代w")


class OtherAccountsHuntTest(unittest.TestCase):
    """`_hunt_other_accounts`：收集、核图、跨平台同名。"""

    def _plugin(self, **overrides):
        module = import_plugin()
        plugin = object.__new__(module.ImageSearcherPlugin)
        plugin._ctx = types.SimpleNamespace(logger=logging.getLogger("test"))
        config = mock.Mock()
        config.plugin.hash_threshold = 8
        config.plugin.verbose_log = False
        config.plugin.other_platforms = True
        config.weibo.enabled = True
        config.weibo.min_fans = 200
        for key, value in overrides.items():
            setattr(config.plugin, key, value)
        plugin._plugin_config_instance = config
        try:
            plugin.config = config
        except AttributeError:
            pass
        plugin._pools = {}
        plugin._verify_pool = None
        return module, plugin

    def test_tiktok_account_is_a_clue_only(self) -> None:
        """TikTok 没有公开封面可核（oembed 只回 iframe），只能列账号。"""
        module, plugin = self._plugin()
        item = _candidate(
            module, "https://www.tiktok.com/@starrychibun/video/7301", distance=0, status="match"
        )
        with mock.patch.object(module.ImageSearcherPlugin, "_vlog", lambda *a, **k: None):
            info = asyncio.run(plugin._hunt_other_accounts([item], None, 123))
        assert info is not None
        self.assertEqual(info["status"], "seen")
        self.assertIsNone(info["hit"])
        self.assertEqual([row["handle"] for row in info["accounts"]], ["starrychibun"])

    def test_bilibili_cover_hit_becomes_the_conclusion(self) -> None:
        """候选里写着 B 站号 → 拿它的名字再检索，投稿封面指纹对上了就换结论。"""
        module, plugin = self._plugin()
        item = _candidate(module, "https://space.bilibili.com/9", name="真号", distance=None)
        accounts = [
            {
                "mid": "9",
                "name": "真号",
                "url": "https://space.bilibili.com/9",
                "images": ["https://i0.hdslb.com/9.jpg"],
            }
        ]
        with (
            mock.patch.object(module.ImageSearcherPlugin, "_vlog", lambda *a, **k: None),
            mock.patch.object(plugin, "_bilibili_accounts", mock.AsyncMock(return_value=accounts)),
            mock.patch.object(
                plugin,
                "_search_page_distances",
                mock.AsyncMock(return_value={"https://i0.hdslb.com/9.jpg": 3}),
            ),
        ):
            info = asyncio.run(plugin._hunt_other_accounts([item], None, 4242))
            assert info is not None
            self.assertEqual(info["status"], "hit")
            self.assertEqual(info["hit"]["platform"], "B站")
            self.assertEqual(info["hit"]["distance"], 3)
            promoted = plugin._promote_other_account(None, info)
        assert promoted is not None
        self.assertEqual(promoted.url, "https://space.bilibili.com/9")
        self.assertEqual(promoted.status, "match")
        # 结论是 UP 主主页，不是那一篇投稿：必须照搜索页反解那样写明
        self.assertTrue(promoted.account_page)
        self.assertIn("主页", promoted.account_note)

    def test_bilibili_cover_miss_marks_checked(self) -> None:
        """封面都比过、没对上：账号留着，但标「没核到这张图」。"""
        module, plugin = self._plugin()
        item = _candidate(module, "https://space.bilibili.com/9", name="真号", distance=None)
        accounts = [
            {
                "mid": "9",
                "name": "真号",
                "url": "https://space.bilibili.com/9",
                "images": ["https://i0.hdslb.com/9.jpg"],
            }
        ]
        with (
            mock.patch.object(module.ImageSearcherPlugin, "_vlog", lambda *a, **k: None),
            mock.patch.object(plugin, "_bilibili_accounts", mock.AsyncMock(return_value=accounts)),
            mock.patch.object(
                plugin,
                "_search_page_distances",
                mock.AsyncMock(return_value={"https://i0.hdslb.com/9.jpg": 21}),
            ),
        ):
            info = asyncio.run(plugin._hunt_other_accounts([item], None, 4242))
        assert info is not None
        self.assertEqual(info["status"], "seen")
        self.assertTrue(info["accounts"][0]["checked"])

    def test_x_handle_is_searched_on_weibo_as_a_clue(self) -> None:
        """结论是 X 号时，拿 handle 去微博搜同名——只当线索，不改结论。"""
        module, plugin = self._plugin()
        best = _candidate(
            module,
            "https://x.com/usagi_rr/status/1",
            distance=1,
            status="match",
            handle="@usagi_rr",
            platform="X (Twitter)",
        )
        rows = [
            {
                "uid": "6880826706",
                "name": "usagi_rr",
                "followers": 52000,
                "url": "https://weibo.com/u/6880826706",
            }
        ]

        async def search_weibo(self, name):
            return rows

        with (
            mock.patch.object(module.ImageSearcherPlugin, "_vlog", lambda *a, **k: None),
            mock.patch.object(module.ImageSearcherPlugin, "_search_weibo_users", search_weibo),
            mock.patch.object(plugin, "_bilibili_accounts", mock.AsyncMock(return_value=[])),
        ):
            info = asyncio.run(plugin._hunt_other_accounts([best], best, 4242))
        assert info is not None
        self.assertEqual(info["status"], "seen")
        self.assertIsNone(info["hit"])
        weibo = [row for row in info["accounts"] if row["platform"] == "微博"]
        self.assertEqual(len(weibo), 1)
        self.assertEqual(weibo[0]["followers"], 52000)
        self.assertIn("按 X @usagi_rr", weibo[0]["source"])

    def test_switch_off_returns_none(self) -> None:
        module, plugin = self._plugin(other_platforms=False)
        item = _candidate(module, "https://www.tiktok.com/@starrychibun/video/7301")
        self.assertIsNone(asyncio.run(plugin._hunt_other_accounts([item], None, 123)))


class BilibiliNameHuntTest(unittest.TestCase):
    """标题里的名字 → B 站检索：命中给号，没命中要带回搜过哪些号。"""

    def _plugin(self):
        module = import_plugin()
        plugin = object.__new__(module.ImageSearcherPlugin)
        plugin._ctx = types.SimpleNamespace(logger=logging.getLogger("test"))
        config = mock.Mock()
        config.plugin.hash_threshold = 8
        config.plugin.verbose_log = False
        config.plugin.other_platforms = True
        plugin._plugin_config_instance = config
        try:
            plugin.config = config
        except AttributeError:
            pass
        plugin._pools = {}
        plugin._verify_pool = None
        return module, plugin

    def test_hit_returns_the_account(self) -> None:
        module, plugin = self._plugin()
        accounts = [
            {
                "mid": "7",
                "name": "小仓千代w",
                "url": "https://space.bilibili.com/7",
                "images": ["https://i0.hdslb.com/7.jpg"],
            }
        ]
        with (
            mock.patch.object(module.ImageSearcherPlugin, "_vlog", lambda *a, **k: None),
            mock.patch.object(plugin, "_bilibili_accounts", mock.AsyncMock(return_value=accounts)),
            mock.patch.object(
                plugin,
                "_search_page_distances",
                mock.AsyncMock(return_value={"https://i0.hdslb.com/7.jpg": 2}),
            ),
        ):
            info = asyncio.run(plugin._hunt_bilibili_name(["小仓千代w"], 4242))
        assert info is not None
        self.assertEqual(info["status"], "hit")
        self.assertEqual(info["mid"], "7")
        self.assertEqual(info["distance"], 2)

    def test_miss_reports_what_was_searched(self) -> None:
        module, plugin = self._plugin()
        accounts = [
            {"mid": "7", "name": "同名号", "url": "https://space.bilibili.com/7", "images": []}
        ]
        with (
            mock.patch.object(module.ImageSearcherPlugin, "_vlog", lambda *a, **k: None),
            mock.patch.object(plugin, "_bilibili_accounts", mock.AsyncMock(return_value=accounts)),
            mock.patch.object(plugin, "_search_page_distances", mock.AsyncMock(return_value={})),
        ):
            info = asyncio.run(plugin._hunt_bilibili_name(["小仓千代w"], 4242))
        assert info is not None
        self.assertEqual(info["status"], "none")
        self.assertEqual(info["names"], ["小仓千代w"])
        self.assertEqual([row["mid"] for row in info["accounts"]], ["7"])


if __name__ == "__main__":
    unittest.main()
