"""插件本体至少得能导入。

这一步是给「测试全绿但插件根本加载不起来」兜底的：0.7.76 上线时
``plugin.py`` 里有个方法定义被挤成一行（``def …:        \"\"\"…``），
core 的 504 项测试一个都不碰 plugin.py，全过了，可插件在服务器上直接
`unexpected indent` 起不来。所以这里按包名导入一次，语法或缩进有问题当场报。
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

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

    def test_cross_author_ignores_watermark_from_another_picture(self) -> None:
        """4631：候选只是「疑似同一个人」时，它身上的水印不能拿来定作者。

        那单 Lens 给的是一条距离 24 的 reddit 帖（白发带角拿镰刀，别人另一套图），
        上面带微博水印 @Natsuko_夏夏子；插件据此认定作者，既认错了人，还把微博号
        当 X 号去查。
        """
        module = import_plugin()

        class Quiet:
            def _vlog(self, *args, **kwargs) -> None:
                return None

        suspect = module.Candidate(
            engine="lens",
            url="https://www.reddit.com/r/arknights/comments/1bynepe/x",
            platform="reddit.com",
            name="Shining 'Silent Night DN02' cosplay by Natsuko_夏夏子",
            distance=24,
            status="suspect",
        )
        kept = module.ImageSearcherPlugin._promote_cross_author(
            Quiet(),
            suspect,
            {"handle": "natsuko_", "name": "Natsuko_夏夏子", "x_name": "おおらい なつこ"},
        )
        self.assertIs(kept, suspect)

    def test_cross_author_ignores_watermark_with_no_source_at_all(self) -> None:
        module = import_plugin()

        class Quiet:
            def _vlog(self, *args, **kwargs) -> None:
                return None

        self.assertIsNone(
            module.ImageSearcherPlugin._promote_cross_author(
                Quiet(), None, {"handle": "natsuko_", "name": "Natsuko_夏夏子"}
            )
        )


class PlainFirstReplySwitchTest(unittest.TestCase):
    """`plain_first_reply` 开关：关着（默认）走合并转发，打开换成一条普通图文消息。

    合并转发卡片偶发拿不到新的 real_seq，客户端整条不画，插件又补救不了；
    所以留了这个开关让用户在「一定看得见」和「省地方」之间自己选。不管开关
    怎么设，「更多」始终是合并转发。
    """

    def _plugin(self, plain: bool, calls: list[tuple[str, str]]):
        module = import_plugin()
        plugin = object.__new__(module.ImageSearcherPlugin)
        plugin._self_id = ""
        plugin._tasks = object()

        class Sender:
            async def forward(self, nodes, where):
                calls.append(("forward", where))
                return True

            async def text(self, text, where, **kwargs):
                calls.append(("text", where))
                return True

            async def hybrid(self, segments, where):
                calls.append(("hybrid", where))
                return True

        plugin._ctx = types.SimpleNamespace(logger=logging.getLogger("test"), send=Sender())
        config = mock.Mock()
        config.plugin.plain_first_reply = plain
        config.plugin.show_reference = True
        config.plugin.nickname = "麦麦寻人"
        config.plugin.task_ttl_minutes = 15
        return module, plugin, config

    def _run(self, *, plain: bool, replay: bool) -> list[tuple[str, str]]:
        import asyncio

        from core.tasks import SearchOutcome, SearchTask

        calls: list[tuple[str, str]] = []
        module, plugin, config = self._plugin(plain, calls)

        async def noop_image(self, task) -> None:
            return None

        async def noop_engine_images(self, task, *, limit, whole) -> None:
            return None

        task = SearchTask(
            task_id="s-test",
            stream_id="g1",
            user_id="u1",
            status="done",
            outcome=SearchOutcome(status="ok", image_b64="IMG"),
        )
        with (
            mock.patch.object(module.ImageSearcherPlugin, "_vlog", lambda *a, **k: None),
            mock.patch.object(module.ImageSearcherPlugin, "_ensure_image", noop_image),
            mock.patch.object(module.ImageSearcherPlugin, "_ensure_engine_images", noop_engine_images),
            mock.patch.object(
                module.ImageSearcherPlugin,
                "config",
                new_callable=mock.PropertyMock,
                return_value=config,
            ),
        ):
            asyncio.run(plugin._send_forward(task, 5, replay=replay))
        return calls

    def test_defaults_to_forward(self) -> None:
        """发版默认还是合并转发，老部署升级上来行为不变。"""
        module = import_plugin()
        self.assertFalse(module.PluginSectionConfig().plain_first_reply)

    def test_switch_off_sends_forward(self) -> None:
        calls = self._run(plain=False, replay=False)
        self.assertEqual(calls[0][0], "forward")

    def test_switch_on_sends_hybrid(self) -> None:
        calls = self._run(plain=True, replay=False)
        self.assertEqual(calls[0][0], "hybrid")

    def test_more_always_forwards(self) -> None:
        """「更多」不受开关影响：它是用户主动要的，卡片发得出来就够。"""
        calls = self._run(plain=True, replay=True)
        self.assertEqual(calls[0][0], "forward")


class WeiboLowFansHuntTest(unittest.TestCase):
    """微博昵称搜到的是小号（粉丝低于门槛）时，也得翻她的带图帖比指纹。

    s-20261117-0741-4631 就栽在这：水印「昭君睡不醒」只搜到 98 粉的同名号，
    被 200 粉门槛直接挡回去，于是结论停在「同名路人号」。粉丝少不等于不是本人，
    让图说话才是实锤。
    """

    def _plugin(self):
        module = import_plugin()
        plugin = object.__new__(module.ImageSearcherPlugin)
        config = mock.Mock()
        config.weibo.enabled = True
        config.weibo.min_fans = 200
        return module, plugin, config

    def _run(self, plugin, module, config, lookup):
        async def search(self, name):
            return [
                {
                    "uid": "6004281123",
                    "name": "昭昭睡不醒-",
                    "followers": 98,
                    "url": "https://weibo.com/u/6004281123",
                }
            ]

        async def with_post(self, row, target_hash=None):
            return lookup(dict(row))

        with (
            mock.patch.object(module.ImageSearcherPlugin, "_vlog", lambda *a, **k: None),
            mock.patch.object(module.ImageSearcherPlugin, "_search_weibo_users", search),
            mock.patch.object(module.ImageSearcherPlugin, "_with_weibo_post", with_post),
            mock.patch.object(module.ImageSearcherPlugin, "config", config, create=True),
        ):
            return asyncio.run(
                plugin._hunt_weibo(
                    [],
                    {"watermark": "@昭君睡不醒 From 小红书"},
                    {},
                    target_hash=12345,
                )
            )

    def test_low_fans_with_matching_post_becomes_hit(self) -> None:
        """小号自己的帖子里有这张图 → 实锤，落到那条微博上。"""
        module, plugin, config = self._plugin()
        seen: list[dict] = []

        def lookup(row):
            seen.append(row)
            row.update(
                {
                    "post": "https://weibo.com/6004281123/Pd1abcd",
                    "distance": 4,
                    "image": "https://wx1.sinaimg.cn/large/x.jpg",
                }
            )
            return row

        found = self._run(plugin, module, config, lookup)
        # 得真的去比过指纹，不能因为粉丝少就跳过
        self.assertEqual(seen[0]["uid"], "6004281123")
        self.assertIsNotNone(found)
        self.assertEqual(found["status"], "hit")
        self.assertEqual(found["post"], "https://weibo.com/6004281123/Pd1abcd")
        self.assertEqual(found["distance"], 4)

    def test_low_fans_without_matching_post_stays_clue(self) -> None:
        """翻过但没这张图 → 仍按同名路人号处理，不能升级成作者。"""
        module, plugin, config = self._plugin()
        found = self._run(plugin, module, config, lambda row: row)
        self.assertIsNotNone(found)
        self.assertEqual(found["status"], "none")
        self.assertNotIn("post", found)
        self.assertEqual(found["max_fans"], 98)


class RoleHintHuntTest(unittest.TestCase):
    """按角色找人：只在开关开着、Grok 配好的时候才发请求。"""

    def _plugin(self):
        module = import_plugin()
        plugin = object.__new__(module.ImageSearcherPlugin)
        config = mock.Mock()
        config.grok.enabled = True
        config.grok.role_hint = True
        config.grok.api_key = "test-key"
        config.grok.base_url = "https://example.com/v1"
        config.grok.model = "grok-4.7"
        config.grok.models = ["grok-4.7"]
        config.grok.role_timeout = 30
        config.plugin.proxy = ""
        return module, plugin, config

    def _run(self, module, plugin, config):
        body = {
            "model": "grok-4.7",
            "output": [
                {"type": "custom_tool_call", "name": "x_search", "input": "{}"},
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "角色：明日方舟｜死芒\n账号：@tsurumi57 | 1.7K | 无\n依据：黑面纱猫耳",
                        }
                    ],
                },
            ],
        }
        response = mock.Mock()
        response.status_code = 200
        response.text = ""
        response.json.return_value = body
        session = mock.Mock()
        session.post.return_value = response
        with (
            mock.patch.object(module.ImageSearcherPlugin, "_vlog", lambda *a, **k: None),
            mock.patch.object(module.ImageSearcherPlugin, "config", config, create=True),
            mock.patch.object(module, "prepare_upload", lambda *a, **k: (b"img", "")),
            mock.patch.object(module, "make_session", lambda *a, **k: session),
        ):
            return asyncio.run(plugin._hunt_role_hint(b"raw"))

    def test_returns_role_and_accounts(self) -> None:
        module, plugin, config = self._plugin()
        found = self._run(module, plugin, config)
        self.assertIsNotNone(found)
        self.assertEqual(found["roles"], ["明日方舟｜死芒"])
        self.assertEqual(found["accounts"][0]["handle"], "tsurumi57")
        self.assertEqual(found["accounts"][0]["fans"], 1700)

    def test_switch_off_sends_nothing(self) -> None:
        module, plugin, config = self._plugin()
        config.grok.role_hint = False
        with (
            mock.patch.object(module.ImageSearcherPlugin, "config", config, create=True),
            mock.patch.object(
                module, "make_session", side_effect=AssertionError("不该发请求")
            ),
        ):
            self.assertIsNone(asyncio.run(plugin._hunt_role_hint(b"raw")))

    def test_without_key_sends_nothing(self) -> None:
        module, plugin, config = self._plugin()
        config.grok.api_key = ""
        with (
            mock.patch.object(module.ImageSearcherPlugin, "config", config, create=True),
            mock.patch.object(
                module, "make_session", side_effect=AssertionError("不该发请求")
            ),
        ):
            self.assertIsNone(asyncio.run(plugin._hunt_role_hint(b"raw")))


class HostingChainConfigTest(unittest.TestCase):
    """图床链的开关：三家默认都开，关掉的直接跳过，顺序不变。"""

    def test_defaults_to_all_three_in_order(self) -> None:
        module = import_plugin()
        self.assertEqual(module.HostingConfig().order(), ("uguu", "chatglm", "cliim"))

    def test_disabled_host_drops_out(self) -> None:
        module = import_plugin()
        self.assertEqual(module.HostingConfig(uguu=False).order(), ("chatglm", "cliim"))
        self.assertEqual(module.HostingConfig(chatglm=False).order(), ("uguu", "cliim"))
        self.assertEqual(module.HostingConfig(cliim=False).order(), ("uguu", "chatglm"))

    def test_all_disabled_is_empty(self) -> None:
        module = import_plugin()
        config = module.HostingConfig(uguu=False, chatglm=False, cliim=False)
        self.assertEqual(config.order(), ())


class TitleAuthorLlmSwitchTest(unittest.TestCase):
    """「从转载页标题里抽作者名」这一步：开关开着问模型，关着走固定规则。"""

    def _plugin(self, enabled: bool):
        module = import_plugin()
        plugin = object.__new__(module.ImageSearcherPlugin)
        config = mock.Mock()
        config.title_author.enabled = enabled
        config.title_author.task = "utils"
        config.title_author.model = ""
        config.title_author.timeout = 60
        return module, plugin, config

    def _best(self, module):
        best = module.Candidate(
            engine="yandex",
            url="https://xx.knit.bid/en/article/32501/",
            title="Cosplay] ran3 - Cure Nyammy black stockings purple staff ...",
            source="xx.knit.bid",
            platform="xx.knit.bid",
            status="match",
            distance=0,
        )
        best.name = "Cosplay] ran3 - Cure Nyammy black stockings purple staff ..."
        best.finalize()
        return best

    def _run(self, plugin, module, config, best, answer):
        async def ask(self, item):
            return answer

        with (
            mock.patch.object(module.ImageSearcherPlugin, "_vlog", lambda *a, **k: None),
            mock.patch.object(module.ImageSearcherPlugin, "_ask_title_author", ask),
            mock.patch.object(module.ImageSearcherPlugin, "config", config, create=True),
        ):
            return asyncio.run(plugin._title_author_candidates(best))

    def test_llm_answer_wins_and_handle_comes_first(self) -> None:
        module, plugin, config = self._plugin(True)
        best = self._best(module)
        author, variants, via, name = self._run(
            plugin, module, config, best, ("染三ran3", "ran3ran3ran3", "染三")
        )
        self.assertEqual(via, "llm")
        self.assertEqual(author, "染三ran3")
        self.assertEqual(name, "染三")
        # 模型直接给的号排在规则展开的前面，避免先白查一遍 ran3、ran3ran3
        self.assertEqual(variants[0], "ran3ran3ran3")
        self.assertIn("ran3", variants)

    def test_llm_says_no_author_stops_here(self) -> None:
        """模型明确说标题里没作者时，不再拿规则硬抽。"""
        module, plugin, config = self._plugin(True)
        best = self._best(module)
        author, variants, via, name = self._run(plugin, module, config, best, ("", "", ""))
        self.assertEqual(via, "llm")
        self.assertEqual(variants, [])

    def test_timeout_falls_back_to_rules(self) -> None:
        """模型没回（None）就退回规则，规则剥掉分类标签后是 ran3。"""
        module, plugin, config = self._plugin(True)
        best = self._best(module)
        author, variants, via, name = self._run(plugin, module, config, best, None)
        self.assertEqual(via, "rule")
        self.assertEqual(author, "ran3")
        self.assertEqual(name, "")
        self.assertIn("ran3ran3ran3", variants)

    def test_switch_off_never_calls_the_model(self) -> None:
        module, plugin, config = self._plugin(False)
        best = self._best(module)
        called: list[object] = []

        async def ask(self, item):
            called.append(item)
            return ("不该被调用", "", "")

        with (
            mock.patch.object(module.ImageSearcherPlugin, "_vlog", lambda *a, **k: None),
            mock.patch.object(module.ImageSearcherPlugin, "_ask_title_author", ask),
            mock.patch.object(module.ImageSearcherPlugin, "config", config, create=True),
        ):
            author, variants, via, name = asyncio.run(plugin._title_author_candidates(best))
        self.assertEqual(called, [])
        self.assertEqual(via, "rule")
        self.assertIn("ran3ran3ran3", variants)

    def test_default_section_is_on_with_utils(self) -> None:
        """默认开着，任务组是 MaiBot 自带的 utils。"""
        module = import_plugin()
        section = module.TitleAuthorConfig()
        self.assertTrue(section.enabled)
        self.assertEqual(section.task, "utils")
        self.assertEqual(section.model, "")

    def test_page_body_goes_into_the_prompt(self) -> None:
        """模型不联网，光给 URL 没用：页面正文里的署名要一起递过去。"""
        module, plugin, config = self._plugin(True)
        captured: dict = {}

        async def call(name, **kwargs):
            captured.update(kwargs)
            return {"success": True, "response": '{"author": "ran3", "handle": "ran3ran3ran3"}'}

        plugin_ctx = mock.Mock()
        plugin_ctx.call_capability = call

        async def fetch(self, url):
            return "<html><body><p>导航</p><p>Photo by ran3</p><p>x.com/ran3ran3ran3</p></body></html>"

        with (
            mock.patch.object(module.ImageSearcherPlugin, "_vlog", lambda *a, **k: None),
            mock.patch.object(module.ImageSearcherPlugin, "_fetch_page_html", fetch),
            mock.patch.object(module.ImageSearcherPlugin, "config", config, create=True),
            mock.patch.object(module.ImageSearcherPlugin, "ctx", plugin_ctx, create=True),
        ):
            answer = asyncio.run(plugin._ask_title_author(self._best(module)))

        self.assertEqual(answer, ("ran3", "ran3ran3ran3", ""))
        self.assertIn("Photo by ran3", captured["prompt"])
        self.assertIn("x.com/ran3ran3ran3", captured["prompt"])
        self.assertEqual(captured["task_name"], "utils")
        # 纯文本任务用字符串 prompt，不是多模态的消息数组
        self.assertIsInstance(captured["prompt"], str)

    def test_page_fetch_failure_still_asks(self) -> None:
        """页面抓不到（403/超时）时只凭标题问，不能整步跳过。"""
        module, plugin, config = self._plugin(True)
        captured: dict = {}

        async def call(name, **kwargs):
            captured.update(kwargs)
            return {"success": True, "response": '{"author": "ran3", "handle": ""}'}

        plugin_ctx = mock.Mock()
        plugin_ctx.call_capability = call

        async def fetch(self, url):
            return ""

        with (
            mock.patch.object(module.ImageSearcherPlugin, "_vlog", lambda *a, **k: None),
            mock.patch.object(module.ImageSearcherPlugin, "_fetch_page_html", fetch),
            mock.patch.object(module.ImageSearcherPlugin, "config", config, create=True),
            mock.patch.object(module.ImageSearcherPlugin, "ctx", plugin_ctx, create=True),
        ):
            answer = asyncio.run(plugin._ask_title_author(self._best(module)))

        self.assertEqual(answer, ("ran3", "ran3", ""))
        self.assertIn("Cosplay] ran3", captured["prompt"])
        # 抓不到页面就不带正文那一段（规则里「优先看页面正文…」这句是固定文案，不算）
        self.assertNotIn("页面正文（去掉标签后的纯文字", captured["prompt"])


class ProfileLookupRetryTest(unittest.TestCase):
    """查 X 账号资料失败时：404 直接收手，429／5xx 重试一次，失败要留日志。"""

    class _Response:
        def __init__(self, code: int, payload: dict | None = None) -> None:
            self.status_code = code
            self._payload = payload or {}

        def json(self):
            return self._payload

    def _run(self, responses):
        module = import_plugin()
        plugin = object.__new__(module.ImageSearcherPlugin)
        seen: list[str] = []
        queue = list(responses)

        class Session:
            def get(self, url, timeout=0):
                seen.append(url)
                return queue.pop(0) if queue else ProfileLookupRetryTest._Response(500)

            def close(self):
                pass

        config = mock.Mock()
        config.plugin.proxy = ""
        with (
            mock.patch.object(module.ImageSearcherPlugin, "config", config, create=True),
            mock.patch.object(module.ImageSearcherPlugin, "_vlog", lambda *a, **k: None),
            mock.patch.object(module.ImageSearcherPlugin, "_PROFILE_RETRY_DELAY", 0),
            mock.patch.object(module, "make_session", lambda proxy: Session()),
        ):
            user = asyncio.run(plugin._profile("ran3ran3ran3"))
        return user, seen

    def test_retries_once_after_server_error(self):
        """9aa1 那单四个变体全静默返回空，才需要靠重试兜住一次抖动。"""
        user, seen = self._run(
            [self._Response(429), self._Response(200, {"user": {"name": "染三 ran3"}})]
        )
        self.assertEqual(user, {"name": "染三 ran3"})
        self.assertEqual(len(seen), 2)

    def test_404_is_not_retried(self):
        user, seen = self._run([self._Response(404)])
        self.assertIsNone(user)
        self.assertEqual(len(seen), 1)

    def test_gives_up_after_second_failure(self):
        user, seen = self._run([self._Response(502), self._Response(503)])
        self.assertIsNone(user)
        self.assertEqual(len(seen), 2)


if __name__ == "__main__":
    unittest.main()
