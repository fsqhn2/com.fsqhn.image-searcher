"""插件本体至少得能导入。

这一步是给「测试全绿但插件根本加载不起来」兜底的：0.7.76 上线时
``plugin.py`` 里有个方法定义被挤成一行（``def …:        \"\"\"…``），
core 的 504 项测试一个都不碰 plugin.py，全过了，可插件在服务器上直接
`unexpected indent` 起不来。所以这里按包名导入一次，语法或缩进有问题当场报。
"""

from __future__ import annotations

import importlib
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

    def test_watermark_tiles_defaults_on(self) -> None:
        """整图读不出水印时切块再读，是独立开关，默认开。"""
        module = import_plugin()
        self.assertTrue(module.VisionConfig().watermark_tiles)

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


if __name__ == "__main__":
    unittest.main()
