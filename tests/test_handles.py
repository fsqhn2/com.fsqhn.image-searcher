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

    def test_paid_platform_names_are_not_extracted(self) -> None:
        """泄漏站标题里那串平台名不是人名：2c76 抽中过 @fanvue（7 万粉）。"""
        title = (
            "Kokura Chiyo - - - 21 utmanande bilder läckta från Onlyfans Patreon "
            "Fansly FriendsOnly Fanvue iFans FanCentro"
        )
        found = [name.lower() for name in handles.extract([title])]
        for junk in ("fanvue", "ifans", "fancentro", "friendsonly"):
            self.assertNotIn(junk, found)


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

    def test_stop_words_are_not_anchors(self) -> None:
        """通用词不能当主干：5832 的 ``@carol`` 就是靠共享 cosplay 混进来的。"""
        anchors = ["Cosplay] 小仓千代w 明日方舟W泳装| Xasiat"]
        texts = ["Hiyuki cosplay by Carol - Xasiat"]
        self.assertFalse(handles.supported_by_context("carol", texts, anchors))
        self.assertFalse(handles.shares_anchor("carol", anchors))

    def test_site_name_is_not_an_anchor(self) -> None:
        """站名同理：2c76 的 ``@Telegraph`` 靠标题里的 telegraph 通过了校验。"""
        anchors = ["Telegraph - cosplay W from Ark"]
        texts = ["Telegraph"]
        self.assertFalse(handles.supported_by_context("telegraph", texts, anchors))


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

    def test_reddit_blocked_for_guessing(self) -> None:
        """reddit 标题是整句话，自由抽词只会抽出 choose、looking 这种词。"""
        self.assertTrue(handles.is_blocked_source("reddit.com"))
        self.assertTrue(handles.is_blocked_source("www.reddit.com"))
        self.assertTrue(handles.is_blocked_source("redd.it"))

    def test_normal_site_kept(self) -> None:
        self.assertFalse(handles.is_blocked_source("galleryepic.com"))
        self.assertFalse(handles.is_blocked_source("masterfap.net"))
        self.assertFalse(handles.is_blocked_source(""))


class SignatureSourceTest(unittest.TestCase):
    """reddit 这类搬运页能认署名，页面主人型站点连署名都不认。"""

    def test_owner_site_blocks_signature(self) -> None:
        self.assertTrue(handles.is_signature_blocked("skeb.jp"))
        self.assertTrue(handles.is_signature_blocked("www.amazon.co.jp"))

    def test_reddit_allows_signature(self) -> None:
        self.assertFalse(handles.is_signature_blocked("reddit.com"))
        self.assertFalse(handles.is_signature_blocked("www.reddit.com"))

    def test_reddit_signature_is_only_a_lead(self) -> None:
        """署名只能拿来找号：翻不到这张图就不提。"""
        self.assertTrue(handles.is_signature_only("reddit.com"))
        self.assertFalse(handles.is_signature_only("galleryepic.com"))
        self.assertFalse(handles.is_signature_only("skeb.jp"))

    def test_reddit_signature_still_recognised(self) -> None:
        """「cosplay by KURU」写在 reddit 标题里照样要能认出来。"""
        names = handles.signature_names(["Vivian cosplay by KURU"])
        self.assertIn("KURU", names)


class LinkHandleTest(unittest.TestCase):
    """标题里直接贴原帖链接时，链接指向的账号就是署名。"""

    def test_twitter_and_x_links(self) -> None:
        texts = ["[Source] https://twitter.com/kuru_xox/status/123", "via x.com/fox_9"]
        found = [item.lower() for item in handles.link_handles(texts)]
        self.assertEqual(found, ["kuru_xox", "fox_9"])

    def test_skips_function_segments(self) -> None:
        texts = ["https://x.com/intent/follow?screen_name=x", "https://x.com/i/status/1"]
        self.assertEqual(handles.link_handles(texts), [])

    def test_dedupes_and_limits(self) -> None:
        texts = ["https://x.com/aaa_1", "https://twitter.com/AAA_1", "https://x.com/bbb_2"]
        self.assertEqual(handles.link_handles(texts), ["aaa_1", "bbb_2"])

    def test_no_link(self) -> None:
        self.assertEqual(handles.link_handles(["Vivian cosplay by KURU"]), [])


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


class WatermarkCjkTest(unittest.TestCase):
    """水印带中文名时，中文名得对得上，否则认成同名的另一个号。

    s-20261117-0741-4631：水印读作 @Natsuko_夏夏子，handle 只剩 natsuko_，
    X 上的 @natsuko_ 其实是日本画师「おおらい なつこ」，八竿子打不着。
    """

    def test_cjk_part_picks_the_longest_run(self) -> None:
        self.assertEqual(handles.cjk_part("@Natsuko_夏夏子"), "夏夏子")
        self.assertEqual(handles.cjk_part("cosplay by 昭君睡不醒"), "昭君睡不醒")
        self.assertEqual(handles.cjk_part("@kuru_xox"), "")

    def test_conflict_when_display_name_has_no_such_chinese(self) -> None:
        self.assertTrue(handles.watermark_name_conflict("@Natsuko_夏夏子", "おおらい なつこ"))
        self.assertTrue(handles.watermark_name_conflict("@Natsuko_夏夏子", ""))

    def test_no_conflict_when_display_name_carries_it(self) -> None:
        self.assertFalse(handles.watermark_name_conflict("@Natsuko_夏夏子", "夏夏子 Natsuko"))
        # 全是拉丁字母的水印不适用这条：还是按原来的 handle 比对走。
        self.assertFalse(handles.watermark_name_conflict("@kuru_xox", "KURU"))



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

    def test_twitter_prefix_forms(self) -> None:
        """泄漏站合集页把「这套是谁的」写在标题里（s-20261113-0106-ed9a）。"""
        self.assertEqual(
            handles.signature_names(["推特lazycataw（仅有足） - Hentai Gallery"]),
            ["lazycataw"],
        )
        self.assertEqual(
            handles.signature_names(["Твиттер: lazycataw (только в Instagram)"]),
            ["lazycataw"],
        )
        self.assertEqual(handles.signature_names(["Twitter: @foo_bar"]), ["foo_bar"])

    def test_twitter_domain_is_not_a_signature(self) -> None:
        """`twitter.com/xxx` 是网址，不能把 com 当成人名抽出来。"""
        self.assertEqual(handles.signature_names(["twitter.com/lazycataw/x"]), [])

    def test_generic_by_is_lowest_priority(self) -> None:
        found = handles.signature_names(["posted by someone", "cosplay by KURU"])
        self.assertEqual(found[0], "KURU")

    def test_artist_credit_is_not_a_coser(self) -> None:
        """画站的「drawn by」是画师笔名，不是 coser 的社交账号。

        s-20261123-1619-411f：四条 danbooru 插画候选的标题都写着
        「… drawn by nemo_(leafnight ..」，抽出来的 nemo 一路撞上 X 上 1.6 万粉的
        `@NemoNevermore_`，被摆到首屏当「疑似同一个人的 X」——跟图毫无关系。
        """
        self.assertEqual(
            handles.signature_names(
                ["nozomi and hikari (blue archive) drawn by nemo_(leafnight .."]
            ),
            [],
        )
        self.assertEqual(handles.signature_names(["Character illustrated by mint_cat"]), [])
        self.assertEqual(handles.signature_names(["painted by xiaoxiaoyifan"]), [])
        # coser 的署名照旧认。
        self.assertEqual(handles.signature_names(["Vivian cosplay by KURU"]), ["KURU"])

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


class WeiboClueTest(unittest.TestCase):
    """微博线索的抽取：明写「微博@某某」、给链接、以及水印上中文的 @昵称。

    实测 s-20261113-1108-73e0：水印读成「轻颜 / @青生w」，Grok 读成「微博@萌生w」，
    老抽取函数只认 ASCII 账号，两个都没抽出来，线索整条丢掉。
    """

    def test_weibo_at_nickname_in_chinese(self) -> None:
        self.assertEqual(handles.weibo_targets(["微博@萌生w"]), [("name", "萌生w")])

    def test_weibo_at_with_colon_and_space(self) -> None:
        self.assertEqual(handles.weibo_targets(["微博：纸悦Etsu_ko 的水印"]), [("name", "纸悦Etsu_ko")])
        self.assertEqual(handles.weibo_targets(["Weibo: 青生w"]), [("name", "青生w")])

    def test_weibo_url_uid_and_name(self) -> None:
        self.assertEqual(
            handles.weibo_targets(["https://weibo.com/u/7012365432"]), [("uid", "7012365432")]
        )
        self.assertEqual(
            handles.weibo_targets(["weibo.com/n/%E9%9D%92%E7%94%9Fw"]), [("name", "青生w")]
        )

    def test_plain_at_is_not_weibo(self) -> None:
        """没有微博标记的 @ 不在这里处理：X 上查无此号才拐去微博，由插件决定。"""
        self.assertEqual(handles.weibo_targets(["轻颜 / @青生w"]), [])
        self.assertEqual(handles.weibo_targets(["@kuru_xox"]), [])

    def test_domain_is_not_a_nickname(self) -> None:
        """`weibo.com/x` 里不能被抽成昵称 com。"""
        self.assertEqual(handles.weibo_targets(["weibo.com"]), [])
        self.assertEqual(handles.weibo_targets(["tweeted on weibo"]), [])

    def test_at_names_accepts_chinese(self) -> None:
        self.assertEqual(handles.at_names(["轻颜 / @青生w"]), ["青生w"])
        self.assertTrue(handles.has_cjk("青生w"))
        self.assertFalse(handles.has_cjk("kuru_xox"))


class TitleAuthorTest(unittest.TestCase):
    """转载站标题里的作者名，以及它能展开成哪些 X 账号。"""

    def test_author_is_the_part_before_the_title_separator(self):
        self.assertEqual(
            handles.title_author("染三ran3 - 露露卡[108P7V-1.23GB]"), "染三ran3"
        )
        self.assertEqual(handles.title_author("Vivian | cosplay"), "Vivian")

    def test_sentence_without_separator_is_not_a_name(self):
        """没有「作者 - 作品」这种分隔就不猜：整句里什么都可能是名字。"""
        self.assertEqual(
            handles.title_author("求这张图出处有没有大佬知道这个coser是谁谢谢"), ""
        )
        self.assertEqual(handles.title_author("染三ran3"), "")

    def test_variants_expand_by_repetition(self):
        self.assertEqual(
            handles.x_variants("染三ran3"), ["ran3", "ran3ran3", "ran3ran3ran3", "ran3_ran3", "ran3ran3_"]
        )

    def test_variants_skip_short_or_long_ascii(self):
        self.assertEqual(handles.x_variants("染三"), [])
        self.assertEqual(handles.x_variants("a" * 16), [])

    def test_display_name_must_match(self):
        """`ran3` 那个号也存在，但显示名是「あんらん」，不能认。"""
        self.assertTrue(handles.name_matches("染三ran3", "染三 ran3"))
        self.assertFalse(handles.name_matches("染三ran3", "あんらん"))
        self.assertTrue(handles.name_matches("Vivian", "vivian cos"))
        self.assertFalse(handles.name_matches("Vivian", ""))

    def test_stylised_display_name_still_matches(self):
        """花体／全角显示名要折回普通字母再比。

        411f 的真号是 1.5 百万粉的 `@Kuukow002`，显示名写成数学粗斜体的
        「𝑴𝒊𝒐 𝑲𝒖𝒖𝒌𝒐🍀」；不折回来，「Mio Kuuko」永远比不中，真号被当路人丢掉。
        """
        self.assertTrue(handles.name_matches("Mio Kuuko", "𝑴𝒊𝒐 𝑲𝒖𝒖𝒌𝒐🍀"))
        self.assertTrue(handles.name_matches("Mio Kuuko", "ＭＩＯ ＫＵＵＫＯ"))
        self.assertTrue(handles.name_matches("染三ran3", "染三 ｒａｎ３"))
        self.assertFalse(handles.name_matches("Mio Kuuko", "𝑯𝒊𝒅𝒐𝒓𝒊 𝑹𝒐𝒔𝒆"))

    def test_leading_category_tag_is_not_the_author(self):
        """s-20261119-1722-fe9b：标题「Cosplay] ran3 - …」，开括号被站点吃掉了。

        改之前抽到「Cosplay] ran3」，展开成 ``Cosplayran3``，当然查不到人；
        剥掉分类标签后是 ``ran3``，三连就是 ``ran3ran3ran3``。
        """
        title = "Cosplay] ran3 - Cure Nyammy black stockings purple staff ..."
        self.assertEqual(handles.title_author(title), "ran3")
        self.assertIn("ran3ran3ran3", handles.x_variants(handles.title_author(title)))
        self.assertEqual(
            handles.title_author("[Cosplay] ran3 - Cure Nyammy"), "ran3"
        )
        self.assertEqual(
            handles.title_author("【Cosplay】染三ran3 - 露露卡[108P7V-1.23GB]"), "染三ran3"
        )

    def test_trailing_bracket_splits_title(self):
        """方括号也当分隔符：``[18+] KURU / Seia cosplay`` 要取到 KURU。"""
        self.assertEqual(handles.title_author("[18+] KURU / Seia cosplay"), "KURU")
        self.assertEqual(handles.title_author("[Photo] maonu164196 - Cure Nyammy"), "maonu164196")

    def test_category_word_is_not_a_name(self):
        """标签没剥干净、只剩分类词时不算名字，省得白跑几个请求。"""
        self.assertEqual(handles.title_author("[Cosplay] Cosplay - something"), "")
        self.assertEqual(handles.x_variants("Cosplay"), [])


class TitleHandleTest(unittest.TestCase):
    """标题里直接写着 X 号的那种（``Model Kokura Chiyo (@yummychiyo)``）。

    这是 2c76 那单的漏洞：真号就在标题括号里，旧逻辑只按分隔符切名字，切出来的
    是 `CoserwW21photos` 这种垃圾变体，真号一次都没试。
    """

    def test_parenthesised_handle(self):
        self.assertEqual(
            handles.title_handle("Model Kokura Chiyo (@yummychiyo) in cosplay W from Ark"),
            ("yummychiyo", "Model Kokura Chiyo"),
        )

    def test_fullwidth_and_bracket_forms(self):
        self.assertEqual(handles.title_handle("染三ran3（@ran3ran3）"), ("ran3ran3", "染三ran3"))
        self.assertEqual(handles.title_handle("[Cosplay] 渔蛋Sakana [@Sakana_fish]"), ("Sakana_fish", "渔蛋Sakana"))

    def test_bare_at_handle(self):
        self.assertEqual(handles.title_handle("KURU 的新套图 @kuru_xox"), ("kuru_xox", "KURU 的新套图"))

    def test_email_and_cjk_account_are_not_handles(self):
        """邮箱里的 a@b 不是账号；``@诗诗爱生活的动态`` 也不是 X 号。"""
        self.assertEqual(handles.title_handle("contact me at coser@example.com"), ("", ""))
        self.assertEqual(handles.title_handle("夜色温柔 @诗诗爱生活的动态"), ("", ""))

    def test_stop_word_is_not_a_handle(self):
        self.assertEqual(handles.title_handle("精选 (@cosplay) 套图"), ("", ""))


class BoardNameTest(unittest.TestCase):
    """贴吧水印 ``@cosplay吧`` 是板块名，不是账号。"""

    def test_tieba_board_is_not_an_account(self):
        self.assertTrue(handles.is_board_name("@cosplay吧"))
        self.assertTrue(handles.is_board_name("崩坏星穹铁道吧"))

    def test_real_names_are_not_boards(self):
        self.assertFalse(handles.is_board_name("咲鱼咔咔"))
        self.assertFalse(handles.is_board_name("@kuru_xox"))
        # 名字里带「吧」但不是在结尾，照旧当人名
        self.assertFalse(handles.is_board_name("吧花小美"))


class LlmAuthorReplyTest(unittest.TestCase):
    """把「抽作者名」这一步交给模型时，模型那句话怎么解析。

    返回的是 (author, handle, name)：author 是原样整串（比对显示名用它），
    handle 是账号那半截，name 是名字那半截——「染三ran3」这种人名和账号黏在
    一起的写法，必须拆开。
    """

    def test_json_reply(self):
        self.assertEqual(
            handles.llm_author_reply('{"author": "染三ran3", "handle": "ran3ran3ran3"}'),
            ("染三ran3", "ran3ran3ran3", "染三"),
        )

    def test_name_and_handle_are_split(self):
        """中文名 + 英文 id 写在一起时，模型拆没拆都能得到两截。"""
        self.assertEqual(
            handles.llm_author_reply(
                '{"author": "染三ran3", "name": "染三", "handle": "ran3"}'
            ),
            ("染三ran3", "ran3", "染三"),
        )
        self.assertEqual(handles.split_author_name("渔蛋Sakana"), ("渔蛋", "Sakana"))
        self.assertEqual(handles.split_author_name("染三"), ("染三", ""))
        self.assertEqual(handles.split_author_name("Vivian"), ("", "Vivian"))

    def test_page_x_link_wins_over_guessed_handle(self):
        """页面里直接写了 x.com/xxx 时以它为准，别用名字里拆出来的那截。"""
        self.assertEqual(
            handles.llm_author_reply(
                '{"author": "染三ran3", "handle": "ran3", "x": "https://x.com/ran3ran3ran3"}'
            ),
            ("染三ran3", "ran3ran3ran3", "染三"),
        )

    def test_fenced_json_reply(self):
        """小模型爱包一层代码围栏。"""
        self.assertEqual(
            handles.llm_author_reply('```json\n{"author": "ran3", "handle": ""}\n```'),
            ("ran3", "ran3", ""),
        )

    def test_labelled_lines(self):
        self.assertEqual(
            handles.llm_author_reply("作者：染三ran3\n账号：ran3ran3ran3"),
            ("染三ran3", "ran3ran3ran3", "染三"),
        )

    def test_bare_name_line(self):
        self.assertEqual(handles.llm_author_reply("染三ran3"), ("染三ran3", "ran3", "染三"))

    def test_bare_sentence_is_not_a_name(self):
        """模型解释一句「标题里没有写作者」，不能把这句当名字去展开变体。"""
        self.assertEqual(handles.llm_author_reply("这个标题里没有写作者"), ("", "", ""))
        self.assertEqual(handles.llm_author_reply("抱歉，看不出来"), ("", "", ""))

    def test_explicit_empty_json_means_no_author(self):
        """模型明确回空对象要当「页面里没作者」，不能退回把整段当名字。"""
        self.assertEqual(handles.llm_author_reply('{"author": "", "handle": ""}'), ("", "", ""))
        self.assertEqual(handles.llm_author_reply('{"author": null, "handle": null}'), ("", "", ""))

    def test_parenthetical_explanation_is_dropped(self):
        self.assertEqual(handles.llm_author_reply("作者名：ran3（X 上叫 ran3ran3ran3）")[0], "ran3")


class LlmAuthorListTest(unittest.TestCase):
    """一次问好几个落点（搬运站／图集站）时，模型回的那串名字怎么解析。

    返回 None = 这轮什么都没说（退回规则），空列表 = 明确答了「这些页都没写作者」。
    两者不能混：前者要退回规则再抽一遍，后者表示白跑一趟。
    """

    def test_array_reply(self):
        found = handles.llm_author_list(
            '[{"site": "https://a.com/1", "author": "染三ran3", "handle": "ran3"},'
            ' {"site": "https://b.com/2", "author": "渔蛋Sakana"}]'
        )
        self.assertEqual(len(found), 2)
        self.assertEqual(found[0]["author"], "染三ran3")
        self.assertEqual(found[0]["handle"], "ran3")
        self.assertEqual(found[0]["site"], "https://a.com/1")
        # 没给 handle 时从名字里自己拆。
        self.assertEqual(found[1]["handle"], "Sakana")

    def test_wrapped_and_fenced(self):
        text = '```json\n{"authors": [{"author": "ran3"}]}\n```'
        found = handles.llm_author_list(text)
        self.assertIsNotNone(found)
        self.assertEqual(found[0]["author"], "ran3")

    def test_single_object(self):
        found = handles.llm_author_list('{"author": "ran3", "site": "https://a.com"}')
        self.assertEqual(found[0]["author"], "ran3")

    def test_x_link_wins(self):
        found = handles.llm_author_list(
            '[{"author": "染三ran3", "handle": "ran3", "x": "https://x.com/ran3ran3ran3"}]'
        )
        self.assertEqual(found[0]["handle"], "ran3ran3ran3")

    def test_explicit_empty_list_means_no_author(self):
        """模型答了「都没写」，这是明确结论，不是失败。"""
        self.assertEqual(handles.llm_author_list("[]"), [])
        self.assertEqual(handles.llm_author_list('[{"author": "", "handle": ""}]'), [])

    def test_silence_is_not_a_conclusion(self):
        """空回复、只肯解释、JSON 坏了，都算这轮没说话，要退回规则。"""
        self.assertIsNone(handles.llm_author_list(""))
        self.assertIsNone(handles.llm_author_list("这几个页面都是搬运站，看不出作者"))
        self.assertIsNone(handles.llm_author_list("[{author: ran3}]"))

    def test_explanation_around_json_is_ignored(self):
        text = '我看了这几页，结果如下：\n[{"author": "ran3"}]\n以上。'
        self.assertEqual(handles.llm_author_list(text)[0]["author"], "ran3")


class TitleAtAuthorTest(unittest.TestCase):
    def test_reads_account_from_content_page_title(self) -> None:
        """百度动态的 <title> 就长这样，正文标题里没有账号（b3b4）。"""
        title = "夜色温柔，风也轻，jk女孩出没...@诗诗爱生活的动态"
        self.assertEqual(handles.title_at_author(title), "诗诗爱生活")

    def test_accepts_ascii_and_spaces(self) -> None:
        self.assertEqual(handles.title_at_author("@ran3 的主页"), "ran3")
        self.assertEqual(handles.title_at_author("今天出门玩 @ran3ran3ran3的动态"), "ran3ran3ran3")

    def test_bare_mention_is_not_an_account(self) -> None:
        """正文里随口 @ 一个人，不是这个号的账号名。"""
        self.assertEqual(handles.title_at_author("谢谢 @诗诗爱生活 帮我拍照"), "")
        self.assertEqual(handles.title_at_author("@诗诗爱生活"), "")

    def test_ignores_empty_and_platform_words(self) -> None:
        self.assertEqual(handles.title_at_author(""), "")
        self.assertEqual(handles.title_at_author("@porn的动态"), "")
