"""按名字换到 X 号的两段纯函数：微博资料解析、检索结果里挑账号、昵称比对。

联网部分（访客 cookie、SerpAPI）不在这里测——它们要真网络，放在插件的
真数据验证里跑；这里只钉住「怎么从返回里取东西」和「什么样的名字算同一个人」。
"""

from __future__ import annotations

import unittest

from core import identity


class WeiboProfileTest(unittest.TestCase):
    def test_reads_name_and_followers(self) -> None:
        """实测返回：data.user 里有 screen_name / followers_count / verified_reason。"""
        payload = {
            "data": {
                "user": {
                    "id": 7652767178,
                    "idstr": "7652767178",
                    "screen_name": "纸悦Etsu_ko",
                    "followers_count": 153067,
                    "verified_reason": "动漫博主",
                    "description": "持续发电中^ 精神状态良好_ 感谢关注！！",
                }
            }
        }
        info = identity.parse_weibo_profile(payload)
        assert info is not None
        self.assertEqual(info["name"], "纸悦Etsu_ko")
        self.assertEqual(info["uid"], "7652767178")
        self.assertEqual(info["url"], "https://weibo.com/u/7652767178")
        self.assertEqual(info["followers"], 153067)
        self.assertEqual(info["verified"], "动漫博主")

    def test_missing_user_is_not_a_name(self) -> None:
        """查不到的 UID 只回空对象，这时必须返回 None，不能编一个昵称出来。"""
        for payload in ({}, {"data": {}}, {"data": {"user": {}}}, {"data": {"user": {"id": 1}}}, None, "x"):
            self.assertIsNone(identity.parse_weibo_profile(payload))


class HandlesFromSearchTest(unittest.TestCase):
    def test_picks_the_repeated_handle(self) -> None:
        """实测搜「纸悦Etsu_ko twitter」9 条结果全是同一个号，标题里都带账号。"""
        payload = {
            "organic_results": [
                {"title": "纸悦Etsu_ko on X", "link": "https://x.com/Etsuko827/status/1", "snippet": "@Etsuko827 · 纸悦Etsu_ko"},
                {"title": "纸悦Etsu_ko on X: 传送门", "link": "https://x.com/Etsuko827/status/2", "snippet": "纸悦Etsu_ko (@Etsuko827) on X"},
                {"title": "某人的推特", "link": "https://twitter.com/other_girl/status/3", "snippet": "other_girl"},
            ]
        }
        found = identity.handles_from_search(payload)
        self.assertEqual(found[0], "Etsuko827")
        self.assertIn("other_girl", found)

    def test_drops_path_words_and_intent_links(self) -> None:
        payload = {
            "organic_results": [
                {"title": "share", "link": "https://x.com/intent/post?text=hi", "snippet": "x.com/share"},
                {"title": "home", "link": "https://x.com/i/status/1", "snippet": "x.com/hashtag"},
            ]
        }
        self.assertEqual(identity.handles_from_search(payload), [])

    def test_keeps_search_order_when_counts_tie(self) -> None:
        payload = {
            "organic_results": [
                {"title": "a", "link": "https://x.com/first_one", "snippet": ""},
                {"title": "b", "link": "https://x.com/second_one", "snippet": ""},
                {"title": "c", "link": "https://x.com/second_one", "snippet": ""},
            ]
        }
        self.assertEqual(identity.handles_from_search(payload), ["second_one", "first_one"])

    def test_limit_and_bad_payload(self) -> None:
        payload = {
            "organic_results": [
                {"title": h, "link": f"https://x.com/{h}", "snippet": ""}
                for h in ("one_a", "two_b", "three_c")
            ]
        }
        self.assertEqual(len(identity.handles_from_search(payload, limit=2)), 2)
        self.assertEqual(identity.handles_from_search({}), [])
        self.assertEqual(identity.handles_from_search(None), [])

    def test_prefers_handles_containing_the_name(self) -> None:
        """7496 那单：显示名正好叫 KURU 的三个号挤在前面，真号 handle 才含着 kuru。"""
        payload = {
            "organic_results": [
                {"title": "KURU on X", "link": "https://x.com/helicopterfood/status/1", "snippet": "KURU"},
                {"title": "KURU on X", "link": "https://x.com/helicopterfood/status/2", "snippet": "KURU"},
                {"title": "KURU on X", "link": "https://x.com/helicopterfood/status/3", "snippet": "KURU"},
                {"title": "kuru updates on X", "link": "https://x.com/kuruupdates/status/4", "snippet": "kuru"},
                {"title": "k on X", "link": "https://x.com/kuru8k/status/5", "snippet": "kuru"},
                {"title": "クル / KURU on X", "link": "https://x.com/kuru_xox/status/6", "snippet": "クル"},
            ]
        }
        found = identity.handles_from_search(payload, limit=4, prefer="KURU")
        self.assertIn("kuru_xox", found)
        # 名字整段开头的排在「只是含在里面」和「只有显示名像」的前面。
        self.assertLess(found.index("kuru_xox"), found.index("kuruupdates"))
        self.assertLess(found.index("kuruupdates"), found.index("helicopterfood"))

    def test_without_prefer_keeps_count_order(self) -> None:
        payload = {
            "organic_results": [
                {"title": "KURU on X", "link": "https://x.com/helicopterfood/status/1", "snippet": ""},
                {"title": "KURU on X", "link": "https://x.com/helicopterfood/status/2", "snippet": ""},
                {"title": "クル / KURU on X", "link": "https://x.com/kuru_xox/status/3", "snippet": ""},
            ]
        }
        self.assertEqual(identity.handles_from_search(payload), ["helicopterfood", "kuru_xox"])


class ProfilesFromSearchTest(unittest.TestCase):
    def test_picks_profiles_across_platforms(self) -> None:
        """id= 那种名字多是中文平台 ID，一次检索把各平台主页都捞出来。"""
        payload = {
            "organic_results": [
                {
                    "title": "NothingLose (@nothinglose) · Instagram",
                    "link": "https://www.instagram.com/nothinglose/",
                    "snippet": "cosplay",
                },
                {
                    "title": "nothinglose",
                    "link": "https://www.tiktok.com/@nothinglose",
                    "snippet": "",
                },
                {
                    "title": "NothingLose的微博",
                    "link": "https://weibo.com/u/7788990011",
                    "snippet": "",
                },
                {
                    "title": "NothingLose | Patreon",
                    "link": "https://www.patreon.com/nothinglose",
                    "snippet": "",
                },
            ]
        }
        found = identity.profiles_from_search(payload)
        got = {(row["platform"], row["handle"]) for row in found}
        self.assertIn(("Instagram", "nothinglose"), got)
        self.assertIn(("TikTok", "nothinglose"), got)
        self.assertIn(("微博", "7788990011"), got)
        self.assertIn(("Patreon", "nothinglose"), got)

    def test_skips_single_post_and_redirect_pages(self) -> None:
        payload = {
            "organic_results": [
                {"title": "post", "link": "https://www.instagram.com/p/Cxyz123/", "snippet": ""},
                {"title": "share", "link": "https://www.facebook.com/profile.php?id=1", "snippet": ""},
                {"title": "x 首页", "link": "https://x.com/home", "snippet": ""},
            ]
        }
        self.assertEqual(identity.profiles_from_search(payload), [])

    def test_same_handle_twice_only_once(self) -> None:
        payload = {
            "organic_results": [
                {"title": "a", "link": "https://x.com/NothingLose", "snippet": ""},
                {"title": "b", "link": "https://twitter.com/nothinglose", "snippet": ""},
            ]
        }
        found = identity.profiles_from_search(payload)
        self.assertEqual(len(found), 1)

    def test_empty_name_or_key_gives_nothing(self) -> None:
        self.assertEqual(identity.search_profiles("", "key"), [])
        self.assertEqual(identity.search_profiles("NothingLose", ""), [])
        self.assertEqual(identity.profiles_from_search(None), [])


class SerperTest(unittest.TestCase):
    """Serper 跟 SerpAPI 是两家：key 长度不同、查询写法受限也不同。"""

    SERPER_KEY = "516421f1ec89b35083b93c9db20e0d235e66d534"
    SERPAPI_KEY = "a" * 64

    def test_detects_key_by_length(self) -> None:
        self.assertTrue(identity.is_serper_key(self.SERPER_KEY))
        self.assertFalse(identity.is_serper_key(self.SERPAPI_KEY))
        self.assertFalse(identity.is_serper_key(""))
        self.assertFalse(identity.is_serper_key("z" * 40))

    def test_serper_query_has_no_quotes(self) -> None:
        """Serper 免费档会拒「引号 + 其它词」，所以查询串必须裸写。"""
        self.assertEqual(
            identity.name_query("kuru_xox", "cosplay", self.SERPER_KEY),
            "kuru_xox cosplay",
        )
        self.assertEqual(
            identity.name_query("kuru_xox", "cosplay", self.SERPAPI_KEY),
            '"kuru_xox" cosplay',
        )

    def test_web_search_normalizes_organic(self) -> None:
        """两家返回结构不同，统一成 organic_results 后解析层才不用分家。"""

        class FakeResponse:
            status_code = 200

            def json(self):
                return {"organic": [{"title": "t", "link": "https://x.com/a_b"}]}

        class FakeSession:
            proxies: dict = {}

            def post(self, *args, **kwargs):
                return FakeResponse()

            def close(self):
                pass

        original = identity.requests.Session
        identity.requests.Session = lambda: FakeSession()
        try:
            payload = identity.web_search("a_b", self.SERPER_KEY)
        finally:
            identity.requests.Session = original
        self.assertEqual(payload["organic_results"][0]["link"], "https://x.com/a_b")


class NameMatchesTest(unittest.TestCase):
    def test_same_name_with_spacing_and_underscores(self) -> None:
        self.assertTrue(identity.name_matches("纸悦Etsu_ko", "纸悦 Etsu_ko"))
        self.assertTrue(identity.name_matches("Etsuko827", "etsuko 827"))

    def test_short_or_numeric_fragments_do_not_match(self) -> None:
        """纯数字和两三个字母的碎片撞上谁都像，一律不算匹配。"""
        self.assertFalse(identity.name_matches("7652767178", "纸悦Etsu_ko"))
        self.assertFalse(identity.name_matches("ko", "纸悦Etsu_ko"))
        self.assertFalse(identity.name_matches("", "纸悦Etsu_ko"))

    def test_different_people_do_not_match(self) -> None:
        self.assertFalse(identity.name_matches("纸悦Etsu_ko", "白银81"))


if __name__ == "__main__":
    unittest.main()


# s.weibo.com/user 搜索页的真实结构（2026-11-13 抓的原始 HTML，删掉了图片地址等无关部分）。
# 用户卡片是 card-user-b，里面有 UID、昵称、粉丝数三项；搜索结果按相关度排，
# 同名的路人号能占满整页，所以粉丝数必须解析出来。
_WEIBO_SEARCH_HTML = """
<div class="card-wrap" id="pl_user_feedList">
<div class="card card-user-b s-brt1 card-user-b-padding">
    <div class="avator"><a href="//weibo.com/u/2729265057" target="_blank" suda-data="key=tblog_search_weibo"><img src="x.jpg"></a></div>
    <div class="info">
        <div>
            <a href="//weibo.com/u/2729265057" class="name" style="vertical-align: middle;" target="_blank">萌生www</a>
        </div>
                <p>
            <span class="s-nobr">粉丝：194</span>
        </p>
    </div>
    <div class="btn"><button uid=2729265057 action-type=login>关注</button></div>
</div>
<div class="card card-user-b s-brt1 card-user-b-padding">
    <div class="avator"><a href="//weibo.com/u/2571733814"><img src="y.jpg"></a></div>
    <div class="info">
        <div>
            <a href="//weibo.com/u/2571733814" class="name" target="_blank">宁次萌生win_454</a>
        </div>
                <p>
            <span class="s-nobr">粉丝：3</span>
        </p>
    </div>
</div>
</div>
"""


class WeiboSearchTest(unittest.TestCase):
    def test_reads_cards(self) -> None:
        """昵称、UID、粉丝数、主页链接四项都要从搜索页里读出来。"""
        rows = identity.parse_weibo_search(_WEIBO_SEARCH_HTML)
        self.assertEqual([row["uid"] for row in rows], ["2729265057", "2571733814"])
        self.assertEqual(rows[0]["name"], "萌生www")
        self.assertEqual(rows[0]["followers"], 194)
        self.assertEqual(rows[0]["url"], "https://weibo.com/u/2729265057")

    def test_empty_page_is_not_an_error(self) -> None:
        """搜不到人就是空列表——登录墙那类整页 HTML 也是空列表，由上层决定要不要报错。"""
        for html in ("", "<html><title>Sina Visitor System</title></html>", None):
            self.assertEqual(identity.parse_weibo_search(html), [])

    def test_followers_text(self) -> None:
        """粉丝数写法有「194」「1,234」「1.2万」三种，别把 1.2 万读成 1。"""
        self.assertEqual(identity.weibo_fans("194"), 194)
        self.assertEqual(identity.weibo_fans("1,234"), 1234)
        self.assertEqual(identity.weibo_fans("1.2万"), 12000)
        self.assertEqual(identity.weibo_fans("15万"), 150000)
        self.assertEqual(identity.weibo_fans(""), 0)
        self.assertEqual(identity.weibo_fans("粉丝"), 0)


class WeiboNameMatchTest(unittest.TestCase):
    """微博搜索结果要过名字关：按「包含」召回的话，粉丝数一排序就是一坨无关号。"""

    def test_exact_and_prefix(self) -> None:
        self.assertTrue(identity.weibo_name_matches("萌生w", "萌生w"))
        self.assertTrue(identity.weibo_name_matches("萌生w", "萌生www"))
        self.assertTrue(identity.weibo_name_matches("青生w", "青生w"))
        self.assertTrue(identity.weibo_name_matches("纸悦Etsu_ko", "纸悦Etsu_ko"))

    def test_brand_noise_is_rejected(self) -> None:
        """实测 6704：水印读成 didasne，微博召回 adidasneo（阿迪官方，109.6 万粉）。"""
        self.assertFalse(identity.weibo_name_matches("didasne", "adidasneo"))
        self.assertFalse(identity.weibo_name_matches("青生w", "阮青生w09"))
        self.assertFalse(identity.weibo_name_matches("kuru", "kuruupdates001"))

    def test_one_typo_still_matches(self) -> None:
        """读错一个字母时还得认，否则「轻颜/@青生w」这类读数全白费。"""
        self.assertTrue(identity.weibo_name_matches("qingsheng", "qinsheng"))
        self.assertTrue(identity.weibo_name_matches("萌生ww", "萌生www"))

    def test_short_strings_never_match(self) -> None:
        self.assertFalse(identity.weibo_name_matches("ab", "abc"))
        self.assertFalse(identity.weibo_name_matches("", "萌生w"))


class WeiboCookieHeaderTest(unittest.TestCase):
    def test_split_fields(self) -> None:
        self.assertEqual(
            identity.weibo_cookie_header("abc", "def"), "SUB=abc; SUBP=def"
        )

    def test_tolerates_prefix_and_quotes(self) -> None:
        self.assertEqual(
            identity.weibo_cookie_header("SUB=abc", '"SUBP=def"'), "SUB=abc; SUBP=def"
        )

    def test_legacy_whole_string(self) -> None:
        self.assertEqual(
            identity.weibo_cookie_header("", "", "SUB=x; SUBP=y"), "SUB=x; SUBP=y"
        )
        self.assertEqual(identity.weibo_cookie_header("", "", ""), "")

    def test_split_fields_win_over_legacy(self) -> None:
        self.assertEqual(
            identity.weibo_cookie_header("new", "newp", "SUB=old"), "SUB=new; SUBP=newp"
        )
