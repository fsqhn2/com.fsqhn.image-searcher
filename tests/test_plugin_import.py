"""插件本体至少得能导入。

这一步是给「测试全绿但插件根本加载不起来」兜底的：0.7.76 上线时
``plugin.py`` 里有个方法定义被挤成一行（``def …:        \"\"\"…``），
core 的 504 项测试一个都不碰 plugin.py，全过了，可插件在服务器上直接
`unexpected indent` 起不来。所以这里按包名导入一次，语法或缩进有问题当场报。
"""

from __future__ import annotations

import importlib
import logging
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def import_plugin():
    """plugin.py 用的是包内相对导入，必须挂在包名下面导入。"""
    if "isplug" not in sys.modules:
        package = types.ModuleType("isplug")
        package.__path__ = [str(ROOT)]
        sys.modules["isplug"] = package
    return importlib.import_module("isplug.plugin")


class PluginImportTest(unittest.TestCase):
    def test_imports_and_exposes_plugin_class(self) -> None:
        module = import_plugin()
        self.assertTrue(hasattr(module, "ImageSearcherPlugin"))

    def test_sauce_default_threshold_matches_drawing_route(self) -> None:
        """SauceNAO 的命中阈值要和插画阈值一致：低于 75% 既不算插画也不算命中。

        s-20261111-1403-e15a 那条 62.6% 就是两个阈值打架的产物——不够插画、
        又被当照片命中，结论给出一张相似但不相干的图。
        """
        module = import_plugin()
        self.assertEqual(module.SauceNaoConfig().min_similarity, 75)

    def test_serper_is_its_own_section(self) -> None:
        """Serper 单独一节、单独开关，key 不跟 SerpAPI 混在一起。

        两家的 key 长度都不一样（Serper 40 位、SerpAPI 64 位），混进 Lens 那一栏
        会互相报 401；额度算法也不同，Serper 是预付总量、用完不按月刷新。
        """
        module = import_plugin()
        config = module.SerperConfig()
        self.assertTrue(config.enabled)
        self.assertTrue(config.lens_fallback)
        self.assertTrue(config.upload)
        self.assertEqual(config.keys, [])
        self.assertIn("serper", module.ImageSearcherConfig.model_fields)

    def test_watermark_following_defaults_on(self) -> None:
        """水印号的关注列表里找小号，默认开。"""
        module = import_plugin()
        self.assertTrue(module.VisionConfig().watermark_following)

    def test_name_core_strips_punctuation(self) -> None:
        module = import_plugin()
        self.assertEqual(module.ImageSearcherPlugin._name_core("NENE・890"), "nene890")
        self.assertEqual(module.ImageSearcherPlugin._name_core("  "), "")

    def test_hunts_alt_account_in_following(self) -> None:
        """s-20261112-1030-176c：水印号 @NERUchi890 本人没发这张，小号挂在她的关注里。

        她只关注 5 个人，其中 @oharapekopeko 挂的是同一个 patreon.com/NENE318，
        也发的正是这张（距离 0）。这条要能顺着关注列表摸过去，并且只翻筛出来的
        那几个号，别人不翻。
        """
        import asyncio
        from unittest import mock

        module = import_plugin()
        plugin = object.__new__(module.ImageSearcherPlugin)

        async def following(self, handle):
            return [
                {
                    "screen_name": "oharapekopeko",
                    "name": "ne",
                    "website": {"url": "https://www.patreon.com/NENE318"},
                },
                {"screen_name": "stranger", "name": "other", "website": {"url": "https://example.com/z"}},
                {"screen_name": "NERUchi890", "name": "NENE", "website": {}},
            ]

        async def profile(self, handle):
            return {"name": "NENE", "website": {"url": "https://www.patreon.com/NENE318"}}

        scanned: list[str] = []

        async def media(self, handle, upload):
            scanned.append(handle)
            if handle == "oharapekopeko":
                return {"url": "https://x.com/oharapekopeko/status/2120454779109519482", "distance": 0}
            return None

        with (
            mock.patch.object(module.ImageSearcherPlugin, "_vlog", lambda *a, **k: None),
            mock.patch.object(module.ImageSearcherPlugin, "_following_accounts", following),
            mock.patch.object(module.ImageSearcherPlugin, "_profile", profile),
            mock.patch.object(module.ImageSearcherPlugin, "_media_lookup", media),
        ):
            found = asyncio.run(plugin._hunt_following_account("NERUchi890", b"x"))

        self.assertIsNotNone(found)
        self.assertEqual(found["handle"], "oharapekopeko")
        self.assertEqual(found["distance"], 0)
        # 无关的人不能翻，自己也不翻
        self.assertEqual(scanned, ["oharapekopeko"])

    def test_hunts_alt_account_by_similar_name_without_links(self) -> None:
        """外链对不上时按昵称同词根兜底：NENE 与 ne 算同一个词根。"""
        import asyncio
        from unittest import mock

        module = import_plugin()
        plugin = object.__new__(module.ImageSearcherPlugin)

        async def following(self, handle):
            return [
                {"screen_name": "somebody", "name": "ne", "website": {}},
                {"screen_name": "unrelated", "name": "totally different", "website": {}},
            ]

        async def profile(self, handle):
            return {"name": "NENE", "website": {}}

        async def media(self, handle, upload):
            return {"url": f"https://x.com/{handle}/status/1", "distance": 3}

        with (
            mock.patch.object(module.ImageSearcherPlugin, "_vlog", lambda *a, **k: None),
            mock.patch.object(module.ImageSearcherPlugin, "_following_accounts", following),
            mock.patch.object(module.ImageSearcherPlugin, "_profile", profile),
            mock.patch.object(module.ImageSearcherPlugin, "_media_lookup", media),
        ):
            found = asyncio.run(plugin._hunt_following_account("NERUchi890", b"x"))

        self.assertIsNotNone(found)
        self.assertEqual(found["handle"], "somebody")

    def test_following_hunt_off_when_switch_closed(self) -> None:
        """开关关掉时一次关注列表都不拉。"""
        import asyncio
        from unittest import mock

        module = import_plugin()
        plugin = object.__new__(module.ImageSearcherPlugin)

        called: list[str] = []

        async def following(self, handle):
            called.append(handle)
            return []

        vision_info = {}
        results = [{"state": "ok", "handle": "NERUchi890", "post": ""}]
        config = mock.Mock()
        config.vision.watermark_following = False

        with (
            mock.patch.object(module.ImageSearcherPlugin, "_vlog", lambda *a, **k: None),
            mock.patch.object(module.ImageSearcherPlugin, "_following_accounts", following),
            mock.patch.object(
                module.ImageSearcherPlugin,
                "config",
                new_callable=mock.PropertyMock,
                return_value=config,
            ),
        ):
            asyncio.run(plugin._follow_up_following(vision_info, results, b"x"))

        self.assertEqual(called, [])
        self.assertEqual(vision_info, {})

    def test_account_mentioned_prefers_the_handle_in_candidates(self) -> None:
        """176c 那单视觉读到两个真号，只有 neruchi890 出现在搬运站标题里。"""
        from types import SimpleNamespace

        module = import_plugin()
        plugin = object.__new__(module.ImageSearcherPlugin)

        ordered = [
            SimpleNamespace(
                url="https://fapello.com/video/anime-cosplay/30750978/nenechi890-neruchi890/",
                title="",
            ),
            SimpleNamespace(url="https://leakgallery.com/nene/13052904", title="Nene Exclusive"),
        ]
        self.assertTrue(plugin._account_mentioned("neruchi890", ordered))
        self.assertFalse(plugin._account_mentioned("PINKYKISS", ordered))
        # 太短的名字不作数，免得两三个字母到处都能撞上
        self.assertFalse(plugin._account_mentioned("ne", ordered))

    def test_watermark_reread_defaults_on(self) -> None:
        """「原图再读一次水印」是独立开关，默认开。

        s-20261111-1403-e15a 那单图上写着 KWATERIN，前两条读水印的线都没落到，
        这道兜底本该救回来，却因为开关藏在「拼图读到东西」的分支里被一起跳过。
        """
        module = import_plugin()
        self.assertTrue(module.VisionConfig().watermark_reread)

    def test_should_reread_watermark_when_composite_read_nothing(self) -> None:
        """拼图一个字都没读出来时，兜底重读照样要跑——这就是 1403 那单的翻车点。"""
        module = import_plugin()
        self.assertTrue(
            module.ImageSearcherPlugin._should_reread_watermark(
                vision_enabled=True,
                reread_enabled=True,
                had_best=False,
                watermark_post="",
                grok_post="",
            )
        )

    def test_should_reread_watermark_skips_when_watermark_already_landed(self) -> None:
        """水印号已经翻到本人发的帖子，就不必再多花一次视觉调用。"""
        module = import_plugin()
        self.assertFalse(
            module.ImageSearcherPlugin._should_reread_watermark(
                vision_enabled=True,
                reread_enabled=True,
                had_best=False,
                watermark_post="https://x.com/kwaterin/status/1",
                grok_post="",
            )
        )

    def test_should_reread_watermark_skips_when_grok_already_landed(self) -> None:
        """Grok 已经给出帖子地址，同样收工。"""
        module = import_plugin()
        self.assertFalse(
            module.ImageSearcherPlugin._should_reread_watermark(
                vision_enabled=True,
                reread_enabled=True,
                had_best=False,
                watermark_post="",
                grok_post="https://x.com/kwaterin/status/1",
            )
        )

    def test_should_reread_watermark_skips_when_hit_or_switched_off(self) -> None:
        """有结论就不兜底；开关关掉也不兜底。"""
        module = import_plugin()
        cases = [
            dict(vision_enabled=True, reread_enabled=True, had_best=True),
            dict(vision_enabled=True, reread_enabled=False, had_best=False),
            dict(vision_enabled=False, reread_enabled=True, had_best=False),
        ]
        for case in cases:
            with self.subTest(**case):
                self.assertFalse(
                    module.ImageSearcherPlugin._should_reread_watermark(
                        watermark_post="",
                        grok_post="",
                        **case,
                    )
                )

    def test_watermark_tiles_defaults_off(self) -> None:
        """切块读默认关：六块要四十秒，还最容易读出假号。

        实测 s-20261112-0858-c44f 那单切块读出 MURUA／MIRUI／idcNothinglose
        三个号，一个都没核上，却花了 50 秒；整图那边早就读到了真号。
        """
        module = import_plugin()
        self.assertFalse(module.VisionConfig().watermark_tiles)

    def test_signature_hunt_defaults_on(self) -> None:
        """标题署名反查是独立开关，默认开。"""
        module = import_plugin()
        self.assertTrue(module.VisionConfig().signature_hunt)

    def test_should_reread_watermark_on_repost_page_without_account(self) -> None:
        """命中的是搬运页、又没给出账号时，重读这道也要跑。

        s-20261112-0752-4fdf：命中的是 reddit 转载帖，has_best 为真，旧条件直接
        跳过重读，而图上写着 @kuru_xox。
        """
        module = import_plugin()
        self.assertTrue(
            module.ImageSearcherPlugin._should_reread_watermark(
                vision_enabled=True,
                reread_enabled=True,
                had_best=True,
                repost_unresolved=True,
                watermark_post="",
                grok_post="",
            )
        )

    def test_should_reread_watermark_skips_repost_page_that_already_landed(self) -> None:
        """搬运页那条已经核到本人帖子，就别再读一遍。"""
        module = import_plugin()
        self.assertFalse(
            module.ImageSearcherPlugin._should_reread_watermark(
                vision_enabled=True,
                reread_enabled=True,
                had_best=True,
                repost_unresolved=True,
                watermark_post="https://x.com/kuru_xox/status/1",
                grok_post="",
            )
        )

    def test_deep_trace_defaults_on(self) -> None:
        """追查模式（读候选图的水印）是独立开关，默认开。"""
        module = import_plugin()
        self.assertTrue(module.VisionConfig().deep_trace)

    def test_watermark_hit_beats_vision_guess(self) -> None:
        """水印核到本人发过这张图时，视觉猜的那个号要让位。

        s-20261112-0950-af18：水印那条距离 0（@idtNothinglose 自己发的），视觉
        挑的 @ayawan1012 距离 22，原来水印被「已有结论」挡住，一条回复里两个账号。
        """
        module = import_plugin()
        plugin = module.ImageSearcherPlugin.__new__(module.ImageSearcherPlugin)
        plugin._ctx = types.SimpleNamespace(logger=logging.getLogger("test"))
        guess = module.Candidate(
            engine="lens",
            url="https://x.com/ayawan1012/status/1",
            platform="X (Twitter)",
            handle="@ayawan1012",
            name="Aya Wan",
            distance=22,
            status="vision",
        )
        info = {
            "watermark_post": "https://x.com/idtNothinglose/status/2120509160668409975",
            "watermark_handle": "idtNothinglose",
            "watermark_name": "Reng",
            "watermark_distance": 0,
        }
        promoted = plugin._promote_watermark_hit(guess, info)
        self.assertIsNotNone(promoted)
        self.assertEqual(promoted.handle, "@idtNothinglose")
        self.assertEqual(promoted.distance, 0)

    def test_watermark_hit_keeps_confirmed_match(self) -> None:
        """已经是指纹对上、又落到账号上的帖子不被水印顶掉。"""
        module = import_plugin()
        plugin = module.ImageSearcherPlugin.__new__(module.ImageSearcherPlugin)
        plugin._ctx = types.SimpleNamespace(logger=logging.getLogger("test"))
        solid = module.Candidate(
            engine="lens",
            url="https://x.com/someone/status/2",
            platform="X (Twitter)",
            handle="@someone",
            name="某人",
            distance=3,
            status="match",
        )
        info = {
            "watermark_post": "https://x.com/other/status/9",
            "watermark_handle": "other",
            "watermark_distance": 0,
        }
        kept = plugin._promote_watermark_hit(solid, info)
        self.assertEqual(kept.handle, "@someone")

    def test_id_name_search_defaults_on(self) -> None:
        """按 id= 的名字查各平台是独立开关，默认开。"""
        module = import_plugin()
        self.assertTrue(module.VisionConfig().id_name_search)

    def test_id_name_runs_when_vision_picked_someone(self) -> None:
        """视觉已经给了「同一个人」时，id= 那一步仍要跑。

        s-20261112-0925-cad4：视觉命中 1 条、结论落到微博 UID，而图上写着
        id=NaohingLace，那一步因为「已经有结论」被整条跳过。
        """
        module = import_plugin()
        decide = module.ImageSearcherPlugin._should_search_id_name
        self.assertTrue(
            decide(
                enabled=True,
                has_upload=True,
                names=["NaohingLace"],
                watermark_post="",
                best_status="vision",
                best_is_x=False,
            )
        )

    def test_id_name_skipped_when_watermark_already_verified(self) -> None:
        module = import_plugin()
        decide = module.ImageSearcherPlugin._should_search_id_name
        self.assertFalse(
            decide(
                enabled=True,
                has_upload=True,
                names=["NaohingLace"],
                watermark_post="https://x.com/someone/status/1",
                best_status="vision",
                best_is_x=False,
            )
        )
        # 结论已经是指纹对上的 X 帖，也不用再查。
        self.assertFalse(
            decide(
                enabled=True,
                has_upload=True,
                names=["NaohingLace"],
                watermark_post="",
                best_status="match",
                best_is_x=True,
            )
        )
        # 开关关掉、没有名字、没有图，都不跑。
        self.assertFalse(
            decide(
                enabled=False,
                has_upload=True,
                names=["NaohingLace"],
                watermark_post="",
                best_status="",
                best_is_x=False,
            )
        )
        self.assertFalse(
            decide(
                enabled=True,
                has_upload=True,
                names=[],
                watermark_post="",
                best_status="",
                best_is_x=False,
            )
        )

    def test_watermark_reread_runs_before_id_name(self) -> None:
        """先读水印（含候选图与切块），再拿 id= 的名字去各平台。

        顺序反了的话，水印还没读出来就没有名字可查。
        """
        module = import_plugin()
        source = Path(module.__file__).read_text(encoding="utf-8")
        reread = source.index("await self._reread_watermark(")
        hunt = source.index("await self._hunt_from_id_name(")
        self.assertLess(reread, hunt)

    def test_watermark_candidates_prefers_closest_images(self) -> None:
        """按指纹距离挑候选图：判死的、没图链的都不读，越像越先读。

        s-20261112-0752-4fdf：原图没水印，reddit 那张同套图有，得先读它。
        """
        module = import_plugin()
        candidate = module.Candidate
        rows = [
            candidate(
                engine="lens",
                url="https://example.com/1",
                image_url="https://img/1.jpg",
                status="mismatch",
                distance=0,
            ),
            candidate(
                engine="lens",
                url="https://example.com/2",
                image_url="",
                status="match",
                distance=2,
            ),
            candidate(
                engine="yandex",
                url="https://example.com/3",
                image_url="https://img/3.jpg",
                status="match",
                distance=5,
            ),
            candidate(
                engine="yandex",
                url="https://example.com/4",
                image_url="https://img/4.jpg",
                status="suspect",
                distance=1,
            ),
            candidate(
                engine="lens",
                url="https://example.com/5",
                image_url="https://img/5.jpg",
                status="suspect",
                distance=40,
            ),
        ]
        picked = module.ImageSearcherPlugin._watermark_candidates(rows)
        self.assertEqual(
            [item.image_url for item in picked],
            ["https://img/4.jpg", "https://img/3.jpg", "https://img/5.jpg"],
        )

    def test_watermark_candidates_without_ordered(self) -> None:
        module = import_plugin()
        self.assertEqual(module.ImageSearcherPlugin._watermark_candidates(None), [])
        self.assertEqual(module.ImageSearcherPlugin._watermark_candidates([]), [])

    def test_pick_best_skips_dead_site(self) -> None:
        """半次元已关站，落在那上面的候选不能当结论（s-20261111-1403-e15a）。"""
        module = import_plugin()
        from core.engines import Candidate

        dead = Candidate(
            engine="saucenao",
            url="https://bcy.net/illust/detail/104171",
            similarity=62.6,
            status="match",
            platform="bcy.net",
            name="肆海",
        )
        live = Candidate(
            engine="saucenao",
            url="https://www.pixiv.net/member_illust.php?mode=medium&illust_id=70349455",
            similarity=62.1,
            status="match",
            platform="pixiv.net",
            name="ばむけろ399",
        )
        for item in (dead, live):
            item.finalize()
        best = module.ImageSearcherPlugin._pick_best([dead, live])
        self.assertIsNotNone(best)
        self.assertEqual(best.url, live.url)
        self.assertIsNone(module.ImageSearcherPlugin._pick_best([dead]))

    def test_repost_handles_include_non_x_candidates(self) -> None:
        """号挂在图集站标题里时也要收：s-20261111-1028-1063 整条线因此没跑。"""
        module = import_plugin()
        from core.engines import Candidate

        class Stub:
            _REPOST_AUTHOR_LIMIT = 2

        ordered = [
            Candidate(
                engine="lens",
                url="https://galleryepic.com/en/cosplay/9768",
                status="match",
                platform="galleryepic.com",
                distance=0,
            ),
            Candidate(
                engine="lens",
                url="https://cosplayrule34.com/us/post/102074",
                status="match",
                platform="cosplayrule34.com",
                distance=0,
                handle="@xidaidai",
            ),
        ]
        handles = module.ImageSearcherPlugin._repost_x_handles(Stub(), ordered, ordered[0])
        self.assertEqual(handles, ["xidaidai"])

    def test_paywall_helpers_wired(self) -> None:
        module = import_plugin()
        plugin_class = module.ImageSearcherPlugin
        self.assertTrue(hasattr(plugin_class, "_track_paywall"))
        self.assertTrue(hasattr(plugin_class, "_patreon_track"))
        self.assertTrue(hasattr(plugin_class, "_fanbox_track"))
        self.assertTrue(hasattr(plugin_class, "_hunt_teaser"))

    def test_watermark_hints_dedupes_and_keeps_primary_first(self) -> None:
        """7496：给标题署名那条线当锚点的，只该是图上真读出来的号。"""
        module = import_plugin()
        hints = module.ImageSearcherPlugin._watermark_hints(
            {
                "watermark_handle": "KUFU_xox",
                "watermark_state": "ok",
                "watermark_user_id": "123",
                "watermark_readings": [
                    {"handle": "kufu_xox", "state": "ok", "user_id": "123"},
                    {"handle": ""},
                    {"handle": "KUFU_xox", "state": "ok"},
                    {"handle": "kuru_xox", "state": "missing"},
                ],
            }
        )
        self.assertEqual([item["handle"] for item in hints], ["kufu_xox", "kuru_xox"])
        self.assertEqual(hints[0]["state"], "ok")
        self.assertEqual(hints[0]["user_id"], "123")
        self.assertEqual(module.ImageSearcherPlugin._watermark_hints(None), [])

    def test_cross_author_takes_over_a_repost_page(self) -> None:
        """1710/1713：认出是谁但没有帖子时，别停在搬运页，也别判成没找到。"""
        module = import_plugin()

        class Quiet:
            def _vlog(self, *args, **kwargs) -> None:
                return None

        repost = module.Candidate(
            engine="lens",
            url="https://www.reddit.com/r/Seia/comments/1xgosaw/seia_cosplay_by_kuru",
            platform="reddit.com",
            name="Seia cosplay by KURU",
            distance=1,
            status="match",
        )
        promoted = module.ImageSearcherPlugin._promote_cross_author(
            Quiet(), repost, {"handle": "kuru_xox", "name": "KURU", "x_name": "クル / KURU"}
        )
        self.assertIsNotNone(promoted)
        self.assertEqual(promoted.engine, "author")
        self.assertEqual(promoted.handle, "@kuru_xox")
        self.assertEqual(promoted.status, "match")
        self.assertIn("已经翻不到", promoted.note)

    def test_cross_author_never_overrides_a_confirmed_post(self) -> None:
        module = import_plugin()

        class Quiet:
            def _vlog(self, *args, **kwargs) -> None:
                return None

        solid = module.Candidate(
            engine="lens",
            url="https://x.com/myao_nyaa_/status/1",
            platform="X (Twitter)",
            handle="@myao_nyaa_",
            distance=0,
            status="match",
        )
        kept = module.ImageSearcherPlugin._promote_cross_author(
            Quiet(), solid, {"handle": "kuru_xox", "name": "KURU"}
        )
        self.assertIs(kept, solid)


if __name__ == "__main__":
    unittest.main()
