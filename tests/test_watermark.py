"""图上读到的字够不够格当账号：读歪撞上的同名号不算线索。

起因是 `s-20261126-1334-832b`：地毯上一串白色连笔签名被读成 ``Stellach``，
X 上真有这个号——2010 年注册、71 个粉丝、韩文简介的私人号。旧逻辑把「号存在」
当成「读得准」，于是首屏摆出一条带主页链接的线索，还顺着这个名字去搜了
Instagram（搜回一个 giovanna_cosplay）和 B 站。

这里守两件事：① 什么样的读法才算「图上明写」；② 什么样的画像算「不像那个人」。
"""

from __future__ import annotations

import logging
import types
import unittest
from unittest import mock

from test_plugin_import import import_plugin

from core import fxtwitter, watermark


class ExplicitAccountTest(unittest.TestCase):
    """带 ``@``／网址的算明写，光一个词的算签名——后者要多核一道画像。"""

    def test_at_sign_is_explicit(self) -> None:
        self.assertTrue(watermark.explicit_account("@Sakana_Yudan", "Sakana_Yudan"))

    def test_full_url_is_explicit(self) -> None:
        self.assertTrue(watermark.explicit_account("x.com/kwaterin", "kwaterin"))
        self.assertTrue(watermark.explicit_account("微博@兔子兔子兔w", "兔子兔子兔w"))

    def test_bare_word_is_not_explicit(self) -> None:
        """832b 读出来的就是一个光秃秃的词：这才需要回头看看那个号像不像。"""
        self.assertFalse(watermark.explicit_account("Stellach", "STELLACH"))
        self.assertFalse(watermark.explicit_account("KWATERIN", "kwaterin"))

    def test_empty_text_is_not_explicit(self) -> None:
        self.assertFalse(watermark.explicit_account("", "abc"))


class ProfilePlausibleTest(unittest.TestCase):
    """粉丝太少、或者是私人号，就不当「图上那个人」。"""

    JOINED = "Mon Mar 01 12:30:25 +0000 2010"

    def test_protected_small_account_is_implausible(self) -> None:
        reason = watermark.implausible(
            followers=71, protected=True, joined=self.JOINED
        )
        self.assertIn("私人号", reason)
        self.assertIn("71 个粉丝", reason)
        self.assertIn("2010 年注册", reason)

    def test_small_unprotected_account_is_still_implausible(self) -> None:
        self.assertIn("71 个粉丝", watermark.implausible(followers=71))

    def test_normal_account_passes(self) -> None:
        self.assertEqual(watermark.implausible(followers=213000, protected=False), "")
        self.assertEqual(watermark.implausible(followers=100), "")

    def test_unknown_followers_is_not_a_reason(self) -> None:
        """接口没给粉丝数时不能拿它当证据——那只是没查到。"""
        self.assertEqual(watermark.implausible(followers=-1, protected=False), "")

    def test_joined_year_parsing(self) -> None:
        self.assertEqual(watermark.joined_year(self.JOINED), "2010")
        self.assertEqual(watermark.joined_year(""), "")
        self.assertEqual(watermark.joined_year("2019-04-01"), "2019")


class StatsPayloadTest(unittest.TestCase):
    """老版 ``/<号>`` 端点的字段解析。"""

    PAYLOAD = {
        "code": 200,
        "user": {
            "id": "118676125",
            "screen_name": "STELLACH",
            "name": "Giovanna",
            "followers": 71,
            "following": 771,
            "protected": True,
            "joined": "Mon Mar 01 12:30:25 +0000 2010",
            "description": "강아지, 고양이",
        },
    }

    def test_parses_profile_stats(self) -> None:
        stats = fxtwitter.parse_stats(self.PAYLOAD)
        self.assertTrue(stats.ok)
        self.assertEqual(stats.handle, "STELLACH")
        self.assertEqual(stats.followers, 71)
        self.assertTrue(stats.protected)
        self.assertEqual(watermark.joined_year(stats.joined), "2010")

    def test_missing_user_is_not_ok(self) -> None:
        self.assertFalse(fxtwitter.parse_stats(None).ok)
        self.assertFalse(fxtwitter.parse_stats({"code": 404}).ok)
        self.assertFalse(fxtwitter.parse_stats({"user": {}}).ok)

    def test_stats_url_only_for_real_handles(self) -> None:
        self.assertEqual(
            fxtwitter.build_stats_url("@STELLACH"), "https://api.fxtwitter.com/STELLACH"
        )
        self.assertEqual(fxtwitter.build_stats_url(""), "")
        self.assertEqual(fxtwitter.build_stats_url("有中文"), "")


def _plugin():
    """建一个不跑生命周期的插件实例，只用到日志和配置。"""
    module = import_plugin()
    plugin = object.__new__(module.ImageSearcherPlugin)
    plugin._ctx = types.SimpleNamespace(logger=logging.getLogger("test"))
    config = mock.Mock()
    config.plugin.verbose_log = False
    plugin._plugin_config_instance = config
    try:
        plugin.config = config
    except AttributeError:
        pass
    return module, plugin


class WatermarkPrimaryTest(unittest.TestCase):
    """主读法的挑选：读歪撞名的排在后面，主字段里不留它的链接和名字。"""

    def test_weak_reading_keeps_no_url_or_name(self) -> None:
        _module, plugin = _plugin()
        info: dict = {}
        plugin._apply_watermark_primary(
            info,
            [
                {
                    "source": "vision-original",
                    "raw": "Stellach",
                    "handle": "STELLACH",
                    "url": "https://x.com/STELLACH",
                    "state": "ok",
                    "user_id": "118676125",
                    "name": "Giovanna",
                    "weak": "是私人号、只有 71 个粉丝（2010 年注册）",
                }
            ],
        )
        self.assertEqual(info["watermark"], "Stellach")
        self.assertIn("私人号", info["watermark_weak"])
        self.assertEqual(info["watermark_url"], "")
        self.assertEqual(info["watermark_handle"], "")
        self.assertNotIn("watermark_name", info)

    def test_sound_reading_wins_over_weak_one(self) -> None:
        """两条读法都没核到图时，先挑画像说得过去的那条当主读法。"""
        _module, plugin = _plugin()
        info: dict = {}
        plugin._apply_watermark_primary(
            info,
            [
                {
                    "source": "vision-original",
                    "raw": "Stellach",
                    "handle": "STELLACH",
                    "url": "https://x.com/STELLACH",
                    "state": "ok",
                    "weak": "是私人号、只有 71 个粉丝",
                },
                {
                    "source": "vision-候选2",
                    "raw": "@Sakana_Yudan",
                    "handle": "Sakana_Yudan",
                    "url": "https://x.com/Sakana_Yudan",
                    "state": "ok",
                    "name": "魚蛋",
                },
            ],
        )
        self.assertEqual(info["watermark_handle"], "Sakana_Yudan")
        self.assertEqual(info["watermark_url"], "https://x.com/Sakana_Yudan")
        self.assertEqual(info["watermark_weak"], "")

    def test_hit_reading_always_wins(self) -> None:
        """核到帖子的读法就是结论，哪怕它的画像被判过不像。"""
        _module, plugin = _plugin()
        info: dict = {}
        plugin._apply_watermark_primary(
            info,
            [
                {
                    "source": "vision-original",
                    "raw": "Stellach",
                    "handle": "STELLACH",
                    "url": "https://x.com/STELLACH",
                    "state": "ok",
                    "weak": "是私人号",
                },
                {
                    "source": "vision-候选2",
                    "raw": "@someone",
                    "handle": "someone",
                    "url": "https://x.com/someone",
                    "state": "ok",
                    "post": "https://x.com/someone/status/1",
                    "distance": 0,
                },
            ],
        )
        self.assertEqual(info["watermark_post"], "https://x.com/someone/status/1")
        self.assertEqual(info["watermark_handle"], "someone")


class WatermarkNamesTest(unittest.TestCase):
    """水印名字并进「按名字找号」名单：读歪的名字不许进。"""

    def test_weak_watermark_name_is_not_used(self) -> None:
        _module, plugin = _plugin()
        names = plugin._with_watermark_names(
            ["标题里的名字"],
            {
                "watermark": "Stellach",
                "watermark_name": "Giovanna",
                "watermark_weak": "是私人号、只有 71 个粉丝",
            },
        )
        self.assertEqual(names, ["标题里的名字"])

    def test_sound_watermark_name_goes_first(self) -> None:
        _module, plugin = _plugin()
        names = plugin._with_watermark_names(
            ["标题里的名字"], {"watermark": "咲鱼咔咔", "watermark_name": "咲鱼咔咔"}
        )
        self.assertEqual(names[0], "咲鱼咔咔")

    def test_weak_reading_is_skipped_by_hints(self) -> None:
        """标题署名的交叉验证也拿不到读歪的号。"""
        _module, plugin = _plugin()
        hints = plugin._watermark_hints(
            {
                "watermark_readings": [
                    {
                        "source": "vision-original",
                        "raw": "Stellach",
                        "handle": "STELLACH",
                        "state": "ok",
                        "weak": "是私人号",
                    }
                ]
            }
        )
        self.assertEqual(hints, [])


if __name__ == "__main__":
    unittest.main()
