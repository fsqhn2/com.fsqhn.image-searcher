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
