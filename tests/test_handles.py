"""从候选标题里抽疑似 X 账号：只测纯函数，联网验证在插件里。"""

from __future__ import annotations

import unittest

from core import handles


class ExtractTest(unittest.TestCase):
    def test_picks_names_from_a_messy_title(self) -> None:
        """搬运站标题里的别名列表要能挑出来，平台和通用词要滤掉。"""
        title = 'Kuuko / kuukow002 / kuuko_w Nude OnlyFans Leak Picture'
        found = [name.lower() for name in handles.extract([title])]
        self.assertIn("kuukow002", found)
        self.assertIn("kuuko_w", found)
        self.assertIn("kuuko", found)
        for junk in ("nude", "onlyfans", "leak", "picture"):
            self.assertNotIn(junk, found)

    def test_skips_role_and_platform_words(self) -> None:
        title = "KuukoW - Nahida 18+ Cosplay (Genshin Impact), 35 Photos"
        found = [name.lower() for name in handles.extract([title])]
        self.assertIn("kuukow", found)
        for junk in ("nahida", "cosplay", "genshin", "impact", "photos"):
            self.assertNotIn(junk, found)

    def test_dedupes_and_respects_limit(self) -> None:
        texts = ["alpha_one beta_two", "alpha_one gamma_three", "delta_four"]
        found = handles.extract(texts, limit=2)
        self.assertEqual(len(found), 2)
        self.assertEqual(found[0].lower(), "alpha_one")

    def test_ignores_repeated_characters(self) -> None:
        self.assertEqual(handles.extract(["xxxxx aaaaa"]), [])

    def test_skips_plain_english_words(self) -> None:
        """实测：'Nothing To Hide (Apron)' 抽出 106 万粉的 @nothing，跟图无关。"""
        found = [name.lower() for name in handles.extract(["Nothing To Hide (Apron) by Annham"])]
        self.assertNotIn("nothing", found)
        self.assertNotIn("apron", found)
        self.assertEqual(handles.extract(["Design"]), [])


class MatchScoreTest(unittest.TestCase):
    def test_trailing_digits_fall_back_to_the_stem(self) -> None:
        """kuukow002 的主干 kuukow 出现在 KuukoW 里就算命中。"""
        texts = ["KuukoW - Nahida 18+ Cosplay"]
        self.assertEqual(handles.stem("kuukow002"), "kuukow")
        self.assertGreater(handles.match_score("kuukow002", texts), 0)

    def test_unknown_name_scores_zero(self) -> None:
        self.assertEqual(handles.match_score("someone999", ["KuukoW - Cosplay"]), 0)

    def test_too_short_stem_is_rejected(self) -> None:
        """主干太短的名字误报率太高，直接不算。"""
        self.assertEqual(handles.match_score("abc1", ["abc def"]), 0)

    def test_common_words_do_not_count(self) -> None:
        """4~5 字的常用词不算：站名和作品名会把无关账号顶上来。"""
        # jxporn.net 的「精选Porn」里抽出来的 @porn 是个 9.8 万粉成人号。
        self.assertEqual(handles.match_score("porn", ["精选Porn"]), 0)
        # 「Ghost Sweeper Mikami」是动画名，@Ghost 有 5 万粉但跟 coser 无关。
        self.assertEqual(handles.match_score("ghost", ["Ghost Sweeper Mikami"]), 0)

    def test_site_category_words_are_not_extracted(self) -> None:
        found = [name.lower() for name in handles.extract(["精选Porn hentai"])]
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()


class AnchorTest(unittest.TestCase):
    """推出来的名字必须和「已经靠图像对上」的那几条对得上。"""

    def test_nene_family_matches(self) -> None:
        anchors = ["NENE - Sunna Maid-erohere24 | NENE - Sunna Maid Cosplay"]
        self.assertTrue(handles.shares_anchor("nenechi890", anchors))
        self.assertTrue(handles.shares_anchor("nenechi", anchors))

    def test_kuukow_family_matches(self) -> None:
        anchors = ["KuukoW - Nahida 18+ Cosplay, 35 photos"]
        self.assertTrue(handles.shares_anchor("kuukow002", anchors))

    def test_unrelated_artist_is_dropped(self) -> None:
        """skeb 那条接稿人的名字跟这张图没关系，不能顶成疑似账号。"""
        anchors = ["NENE - Sunna Maid-erohere24 | NENE - Sunna Maid Cosplay"]
        self.assertFalse(handles.shares_anchor("murabitoufu", anchors))

    def test_empty_anchors(self) -> None:
        self.assertFalse(handles.shares_anchor("murabitoufu", []))

    def test_alias_in_same_title_is_kept(self) -> None:
        """同一条标题里并列的别名可以采信：NERUchi890 跟 NENEchi890 是同一个人。"""
        anchors = ["NENE - Sunna Maid-erohere24 | NENE - Sunna Maid Cosplay"]
        texts = ["NENEchi890, NERUchi890 #1304 - Cosplay Girl Porn - Fapello"]
        self.assertFalse(handles.shares_anchor("neruchi890", anchors))
        self.assertTrue(handles.supported_by_context("neruchi890", texts, anchors))

    def test_lone_handle_title_is_refused(self) -> None:
        """标题只有 @murabitoufu、同条里没有任何已命中名字，不能采信。"""
        anchors = ["NENE - Sunna Maid-erohere24"]
        self.assertFalse(
            handles.supported_by_context("murabitoufu", ["@murabitoufu"], anchors)
        )


class SlugMatchTest(unittest.TestCase):
    def test_name_in_url_path_wins(self) -> None:
        """fapeza/fapello 的路径就是它认定的主人名，标题里的别名不算。"""
        urls = ["https://fapeza.com/heyymimimi/62/", "https://fapello.com/heyymimimi/85/"]
        self.assertTrue(handles.in_url_slug("heyymimimi", urls))
        self.assertFalse(handles.in_url_slug("heyimbee", urls))

    def test_short_or_empty_names_do_not_match(self) -> None:
        urls = ["https://example.com/mimi/1/"]
        self.assertFalse(handles.in_url_slug("mi", urls))
        self.assertFalse(handles.in_url_slug("", urls))

    def test_pick_prefers_url_slug_over_followers(self) -> None:
        """1658 那单：标题并列 heyimbee 与 heyymimimi，路径里是后者，就选后者。"""
        options = [
            {"guess": "heyimbee", "handle": "heyimbee", "followers": 568882},
            {"guess": "heyymimimi", "handle": "heyymimimi", "followers": 22775},
        ]
        urls = ["https://fapeza.com/heyymimimi/62/"]
        self.assertEqual(handles.pick_by_slug(options, urls)["handle"], "heyymimimi")

    def test_pick_falls_back_to_followers(self) -> None:
        """路径里谁都没出现时，仍按粉丝数挑最像的那个。"""
        options = [
            {"guess": "kuuko_w", "handle": "kuuko_w", "followers": 1200},
            {"guess": "kuukow002", "handle": "kuukow002", "followers": 98000},
        ]
        self.assertEqual(
            handles.pick_by_slug(options, ["https://example.com/gallery/1/"])["handle"],
            "kuukow002",
        )

    def test_pick_empty_options(self) -> None:
        self.assertIsNone(handles.pick_by_slug([], []))


class BlockedSourceTest(unittest.TestCase):
    """约稿平台、电商页的标题写的是页面主人，不能拿来推作者。"""

    def test_skeb_blocked(self) -> None:
        self.assertTrue(handles.is_blocked_source("skeb.jp"))
        self.assertTrue(handles.is_blocked_source("www.skeb.jp"))

    def test_ecommerce_blocked(self) -> None:
        self.assertTrue(handles.is_blocked_source("joom.com"))
        self.assertTrue(handles.is_blocked_source("aliexpress.ru"))

    def test_normal_site_kept(self) -> None:
        self.assertFalse(handles.is_blocked_source("galleryepic.com"))
        self.assertFalse(handles.is_blocked_source("masterfap.net"))
        self.assertFalse(handles.is_blocked_source(""))


class TypoVariantTest(unittest.TestCase):
    """水印号被读散时的候选写法：Moliww 被读成 Moliuwy 这种要能找回来。"""

    def test_w_read_as_uw(self) -> None:
        self.assertIn("moliww", handles.typo_variants("Moliuwy"))

    def test_most_likely_variant_first(self) -> None:
        """w 读散是最常见的错法，应该排在最前面，少发几次请求。"""
        self.assertEqual(handles.typo_variants("Moliuwy")[0], "moliww")

    def test_original_not_included(self) -> None:
        self.assertNotIn("moliuwy", handles.typo_variants("Moliuwy"))

    def test_invalid_handle_gives_nothing(self) -> None:
        self.assertEqual(handles.typo_variants("ab"), [])
        self.assertEqual(handles.typo_variants("中文昵称"), [])
        self.assertEqual(handles.typo_variants(""), [])


class SignatureFixTest(unittest.TestCase):
    """标题署名能拿来回读错的号：7496 那单 kufu_xox 其实是 kuru_xox。"""

    def test_one_letter_off(self) -> None:
        self.assertEqual(handles.signature_fix("kufu_xox", ["KURU"]), ["kuru_xox"])

    def test_works_when_name_has_extra_words(self) -> None:
        self.assertEqual(handles.signature_fix("kufu_xox", ["クル / KURU"]), ["kuru_xox"])

    def test_unrelated_name_changes_nothing(self) -> None:
        self.assertEqual(handles.signature_fix("kufu_xox", ["Moliww"]), [])
        self.assertEqual(handles.signature_fix("kufu_xox", ["AB"]), [])

    def test_never_returns_the_same_handle(self) -> None:
        self.assertNotIn("kufu_xox", handles.signature_fix("kufu_xox", ["kufu"]))
        self.assertEqual(handles.signature_fix("ab", ["kuru"]), [])


class NameMatchesHandleTest(unittest.TestCase):
    """署名跟水印号是不是同一个人：1710 那单是「图上 @kuru_xox + 标题 by KURU」。"""

    def test_name_as_leading_segment(self) -> None:
        self.assertTrue(handles.name_matches_handle("KURU", "kuru_xox"))
        self.assertTrue(handles.name_matches_handle("クル / KURU", "kuru_xox"))
        self.assertTrue(handles.name_matches_handle("KURU", "kuru"))

    def test_one_letter_off_still_matches(self) -> None:
        self.assertTrue(handles.name_matches_handle("KURU", "kufu_xox"))

    def test_other_people_do_not_match(self) -> None:
        self.assertFalse(handles.name_matches_handle("KURU", "kuruupdates"))
        self.assertFalse(handles.name_matches_handle("KURU", "kuru88303829"))
        self.assertFalse(handles.name_matches_handle("KURU", "kuru8k"))
        self.assertFalse(handles.name_matches_handle("Moliww", "kuru_xox"))

    def test_too_short_names_are_ignored(self) -> None:
        self.assertFalse(handles.name_matches_handle("KU", "ku_ru"))
        self.assertFalse(handles.name_matches_handle("", "kuru_xox"))

    def test_limit_applies(self) -> None:
        self.assertLessEqual(len(handles.typo_variants("menruinyanko", limit=5)), 5)



class SignatureNameTest(unittest.TestCase):
    """标题里的明确署名：搬运帖常把原作者名连动词一起抄过来。

    s-20261112-0752-4fdf 的标题是「Vivian cosplay by KURU」，KURU 就是原作者；
    插件当时只从候选 URL 里抽 handle，抽到两个都不对，这一句完全没用上。
    """

    def test_reddit_credit_in_title(self) -> None:
        self.assertEqual(
            handles.signature_names(["Vivian cosplay by KURU : r/Vivian_Mains"]),
            ["KURU"],
        )

    def test_at_handle_comes_first(self) -> None:
        found = handles.signature_names(
            ["ZZZero - VIVIAN Model (@RayWing) cosplay by somebody"]
        )
        self.assertEqual(found[0], "RayWing")

    def test_label_forms(self) -> None:
        self.assertEqual(handles.signature_names(["coser: Sakana"]), ["Sakana"])
        self.assertEqual(handles.signature_names(["Credit to @Moliww"]), ["Moliww"])

    def test_generic_by_is_lowest_priority(self) -> None:
        found = handles.signature_names(["posted by someone", "cosplay by KURU"])
        self.assertEqual(found[0], "KURU")

    def test_stopwords_dropped(self) -> None:
        self.assertEqual(handles.signature_names(["Cosplay by Unknown"]), [])
        self.assertEqual(handles.signature_names(["photo by reddit"]), [])

    def test_limit_applies(self) -> None:
        found = handles.signature_names(
            ["a by KURUA", "b by KURUB", "c by KURUC"], limit=2
        )
        self.assertEqual(len(found), 2)

    def test_empty_input(self) -> None:
        self.assertEqual(handles.signature_names([]), [])
        self.assertEqual(handles.signature_names(["", "   "]), [])
