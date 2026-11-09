"""fxtwitter（FixTweet）解析：从链接/ID 认出推文，把返回的 JSON 变成可核对的数据。"""

from __future__ import annotations

import unittest

from core import fxtwitter


class ParseTweetIdTest(unittest.TestCase):
    def test_plain_status_url(self) -> None:
        self.assertEqual(
            fxtwitter.parse_tweet_id("https://x.com/muyuqwq233/status/2105595040806707433"),
            "2105595040806707433",
        )

    def test_url_with_query_and_trailing_text(self) -> None:
        text = "看这个 https://twitter.com/a/status/1234567890123456789?s=20 哈哈"
        self.assertEqual(fxtwitter.parse_tweet_id(text), "1234567890123456789")

    def test_bare_id(self) -> None:
        self.assertEqual(fxtwitter.parse_tweet_id("2105595040806707433"), "2105595040806707433")

    def test_internal_link_without_handle(self) -> None:
        """x.com/i/status/<id> 是 Grok 最常给的形式，也得能取出 ID。"""
        self.assertEqual(
            fxtwitter.parse_tweet_id("https://x.com/i/status/2104951546119410067"),
            "2104951546119410067",
        )

    def test_short_number_is_not_an_id(self) -> None:
        self.assertEqual(fxtwitter.parse_tweet_id("12345"), "")

    def test_empty(self) -> None:
        self.assertEqual(fxtwitter.parse_tweet_id(""), "")


class BuildApiUrlTest(unittest.TestCase):
    def test_without_handle_uses_i_path(self) -> None:
        self.assertEqual(
            fxtwitter.build_api_url("2105595040806707433"),
            "https://api.fxtwitter.com/i/status/2105595040806707433",
        )

    def test_with_handle(self) -> None:
        self.assertEqual(
            fxtwitter.build_api_url("2105595040806707433", "@muyuqwq233"),
            "https://api.fxtwitter.com/muyuqwq233/status/2105595040806707433",
        )


class ParseTweetTest(unittest.TestCase):
    payload = {
        "code": 200,
        "message": "OK",
        "tweet": {
            "id": "2105595040806707433",
            "url": "https://x.com/muyuqwq233/status/2105595040806707433",
            "text": "之前的自己腿好瘦 现在好粗呜呜呜 #男娘",
            "author": {
                "screen_name": "muyuqwq233",
                "name": "暮雨不是笨蛋雨喵",
                "protected": False,
            },
            "media": {
                "photos": [
                    {"url": "https://pbs.twimg.com/media/HTiThDBbkAAvqrE.jpg?name=orig"},
                    {"url": "https://pbs.twimg.com/media/HTiThmmboAASCMe.jpg?name=orig"},
                ]
            },
        },
    }

    def test_parses_author_and_media(self) -> None:
        tweet = fxtwitter.parse_tweet(self.payload)
        self.assertTrue(tweet.ok)
        self.assertEqual(tweet.handle, "muyuqwq233")
        self.assertEqual(tweet.name, "暮雨不是笨蛋雨喵")
        self.assertEqual(len(tweet.media), 2)
        self.assertFalse(tweet.protected)

    def test_video_uses_thumbnail(self) -> None:
        payload = {
            "code": 200,
            "tweet": {
                "id": "1",
                "author": {"screen_name": "a"},
                "media": {"videos": [{"thumbnail_url": "https://pbs.twimg.com/t.jpg"}]},
            },
        }
        tweet = fxtwitter.parse_tweet(payload)
        self.assertEqual(tweet.media, ["https://pbs.twimg.com/t.jpg"])

    def test_error_code(self) -> None:
        tweet = fxtwitter.parse_tweet({"code": 404, "message": "Tweet not found"})
        self.assertFalse(tweet.ok)
        self.assertIn("404", tweet.error)

    def test_missing_tweet(self) -> None:
        self.assertFalse(fxtwitter.parse_tweet({"code": 200}).ok)
        self.assertFalse(fxtwitter.parse_tweet(None).ok)

    def test_parses_quote_author(self) -> None:
        """纯引用帖要能拿到被引用那条的作者，否则会把引用者当成作者。"""
        payload = {
            "code": 200,
            "tweet": {
                "id": "2117959676180701586",
                "url": "https://x.com/YuMIkAnime/status/2117959676180701586",
                "author": {"screen_name": "YuMIkAnime", "name": "□카"},
                "media": None,
                "quote": {
                    "url": "https://x.com/qyuuuz/status/2117875583355076703",
                    "id": "2117875583355076703",
                    "text": "138cm 합법로리 탐정",
                    "author": {"screen_name": "qyuuuz", "name": "uewe"},
                },
            },
        }
        tweet = fxtwitter.parse_tweet(payload)
        self.assertEqual(tweet.media, [])
        self.assertEqual(tweet.quote_url, "https://x.com/qyuuuz/status/2117875583355076703")
        self.assertEqual(tweet.quote_handle, "qyuuuz")
        self.assertEqual(tweet.quote_name, "uewe")
        self.assertEqual(tweet.quote_text, "138cm 합법로리 탐정")

    def test_parses_reposted_by(self) -> None:
        """候选链接是别人的转发地址时，要认得出「这条是转的」，并拿到原作者。"""
        payload = {
            "code": 200,
            "tweet": {
                "id": "1981232065648742873",
                "url": "https://x.com/sssmnms/status/1981232065648742873",
                "author": {"screen_name": "sssmnms", "name": "哎我黑切訥"},
                "media": {
                    "photos": [
                        {"type": "photo", "url": "https://pbs.twimg.com/media/G37ADiRWsAAACDO.jpg?name=orig"}
                    ]
                },
                "reposted_by": {
                    "id": "742854226372501504",
                    "name": "렌",
                    "screen_name": "redwine278",
                },
            },
        }
        tweet = fxtwitter.parse_tweet(payload)
        self.assertEqual(tweet.reposted_by, "redwine278")
        self.assertEqual(tweet.handle, "sssmnms")
        self.assertEqual(tweet.name, "哎我黑切訥")
        self.assertEqual(tweet.url, "https://x.com/sssmnms/status/1981232065648742873")

    def test_plain_tweet_has_no_reposter(self) -> None:
        payload = {
            "code": 200,
            "tweet": {
                "id": "2117959676180701586",
                "url": "https://x.com/a/status/2117959676180701586",
                "author": {"screen_name": "a", "name": "A"},
            },
        }
        self.assertEqual(fxtwitter.parse_tweet(payload).reposted_by, "")


class JudgeTest(unittest.TestCase):
    """链接核对的三态：命中、对不上、没法判断。"""

    def test_hit(self) -> None:
        self.assertEqual(fxtwitter.judge([1, 30], 8), "hit")

    def test_miss(self) -> None:
        self.assertEqual(fxtwitter.judge([12, 34], 8), "miss")

    def test_unknown_when_no_distance(self) -> None:
        self.assertEqual(fxtwitter.judge([], 8), "unknown")
        self.assertEqual(fxtwitter.judge([-1], 8), "unknown")


class MediaListTest(unittest.TestCase):
    """账号自己的带图帖子：用来证明「这张图是不是本人发的」。"""

    def test_build_url(self) -> None:
        self.assertEqual(
            fxtwitter.build_media_url("@NERUchi890"),
            "https://api.fxtwitter.com/2/profile/NERUchi890/media",
        )
        self.assertEqual(fxtwitter.build_media_url(""), "")
        self.assertEqual(fxtwitter.build_media_url("a/b"), "")

    def test_parse_photos(self) -> None:
        payload = {
            "code": 200,
            "results": [
                {
                    "id": "2117586176714612824",
                    "url": "https://x.com/NERUchi890/status/2117586176714612824",
                    "text": "どこ見てるの",
                    "created_at": "Wed Nov 04 12:01:16 +0000 2026",
                    "media": {
                        "all": [
                            {
                                "type": "photo",
                                "url": "https://pbs.twimg.com/media/HWMtZrJbcAA82KO.jpg",
                            }
                        ]
                    },
                }
            ],
        }
        posts = fxtwitter.parse_media_list(payload)
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0].tweet_id, "2117586176714612824")
        self.assertEqual(posts[0].url, "https://x.com/NERUchi890/status/2117586176714612824")
        self.assertEqual(posts[0].media, ["https://pbs.twimg.com/media/HWMtZrJbcAA82KO.jpg"])

    def test_skip_items_without_media(self) -> None:
        """纯文字帖不参与比图，留着只会白占名额。"""
        payload = {"results": [{"id": "1", "text": "纯文字", "media": {"all": []}}]}
        self.assertEqual(fxtwitter.parse_media_list(payload), [])

    def test_bad_payload(self) -> None:
        self.assertEqual(fxtwitter.parse_media_list(None), [])
        self.assertEqual(fxtwitter.parse_media_list({"code": 404}), [])


class MediaKeyTest(unittest.TestCase):
    """同一张图在 X 上只对应一个媒体键，靠它判断父帖里有没有这张图。"""

    def test_key_from_media_url(self) -> None:
        self.assertEqual(
            fxtwitter.media_key("https://pbs.twimg.com/media/HWb8qgsasAA9bUk?format=webp&name=large"),
            "HWb8qgsasAA9bUk",
        )
        self.assertEqual(
            fxtwitter.media_key("https://pbs.twimg.com/media/HWb8qgsasAA9bUk.jpg"),
            "HWb8qgsasAA9bUk",
        )

    def test_other_hosts_and_empty(self) -> None:
        self.assertEqual(fxtwitter.media_key("https://i.imgur.com/abc.jpg"), "")
        self.assertEqual(fxtwitter.media_key(""), "")
        self.assertEqual(fxtwitter.media_key("https://pbs.twimg.com/profile_images/1/x.jpg"), "")


class RepliesSearchTest(unittest.TestCase):
    """回复页反查：靠搜索接口顺着 replying_to 回到父帖。"""

    def test_build_replies_query(self) -> None:
        self.assertEqual(fxtwitter.build_replies_query("@wn40536699"), "from:wn40536699 filter:replies")
        self.assertEqual(fxtwitter.build_replies_query("wn40536699"), "from:wn40536699 filter:replies")
        self.assertEqual(fxtwitter.build_replies_query(""), "")
        self.assertEqual(fxtwitter.build_replies_query("a/b"), "")

    def test_build_reposts_query(self) -> None:
        """转发页靠 filter:retweets 搜，返回的是被转的原帖。"""
        self.assertEqual(fxtwitter.build_reposts_query("@wn40536699"), "from:wn40536699 filter:retweets")
        self.assertEqual(fxtwitter.build_reposts_query(""), "")

    def test_parse_keeps_author_for_retweets(self) -> None:
        """搜转发时 author 是原作者，结论要用的就是这个名字。"""
        payload = {
            "results": [
                {
                    "type": "status",
                    "id": "2075962158274445404",
                    "url": "https://x.com/hinatahimarii/status/2075962158274445404",
                    "author": {"screen_name": "hinatahimarii", "name": "土狗小黄"},
                    "reposted_by": {"screen_name": "wn40536699"},
                    "media": {"all": [{"type": "photo", "url": "https://pbs.twimg.com/media/HWb8qgsasAA9bUk.jpg"}]},
                }
            ]
        }
        hits, _ = fxtwitter.parse_search(payload)
        self.assertEqual(hits[0].author_handle, "hinatahimarii")
        self.assertEqual(hits[0].author_name, "土狗小黄")
        # 有配图时帖子地址不能被写成图片直链，结论要贴的是帖子。
        self.assertEqual(hits[0].url, "https://x.com/hinatahimarii/status/2075962158274445404")
        self.assertEqual(hits[0].media, ["https://pbs.twimg.com/media/HWb8qgsasAA9bUk.jpg"])

    def test_build_search_url_with_cursor(self) -> None:
        url = fxtwitter.build_search_url("from:a filter:replies")
        self.assertTrue(url.startswith("https://api.fxtwitter.com/2/search?q=from%3Aa+filter%3Areplies"))
        url = fxtwitter.build_search_url("from:a filter:replies", "CUR")
        self.assertIn("cursor=CUR", url)
        self.assertEqual(fxtwitter.build_search_url(""), "")

    def test_parse_results_and_cursor(self) -> None:
        payload = {
            "code": 200,
            "results": [
                {
                    "type": "status",
                    "id": "2119652310125310082",
                    "url": "https://x.com/wn40536699/status/2119652310125310082",
                    "text": "@qun1150028 屁股我进入",
                    "created_at": "Mon Nov 09 04:44:26 +0000 2026",
                    "media": {"all": []},
                    "replying_to": {
                        "screen_name": "qun1150028",
                        "status": "2119305690091332085",
                    },
                }
            ],
            "cursor": {"top": "T", "bottom": "B"},
        }
        hits, cursor = fxtwitter.parse_search(payload)
        self.assertEqual(cursor, "B")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].tweet_id, "2119652310125310082")
        self.assertEqual(hits[0].parent_id, "2119305690091332085")

    def test_parse_skips_non_status_and_empty(self) -> None:
        payload = {"results": [{"type": "user", "id": "1"}, {}]}
        self.assertEqual(fxtwitter.parse_search(payload)[0], [])
        self.assertEqual(fxtwitter.parse_search(None), ([], ""))


class ProfileStateTest(unittest.TestCase):
    """账号存在性靠 fxtwitter 的 profile：查无此号与被封要分开。"""

    def test_build_profile_url(self) -> None:
        self.assertEqual(
            fxtwitter.build_profile_url("@wn40536699"),
            "https://api.fxtwitter.com/2/profile/wn40536699",
        )
        self.assertEqual(fxtwitter.build_profile_url("bad handle"), "")
        self.assertEqual(fxtwitter.build_profile_url(""), "")

    def test_ok(self) -> None:
        payload = {"code": 200, "message": "OK", "user": {"screen_name": "wn40536699"}}
        self.assertEqual(fxtwitter.parse_profile(payload), fxtwitter.PROFILE_OK)

    def test_suspended(self) -> None:
        payload = {"code": 404, "message": "User is suspended", "reason": "suspended"}
        self.assertEqual(fxtwitter.parse_profile(payload), fxtwitter.PROFILE_SUSPENDED)

    def test_suspended_keeps_user_id(self) -> None:
        # 封号后 handle 可能被释放，数字 ID 是唯一还能对上人的标识。
        payload = {
            "code": 404,
            "message": "User is suspended",
            "reason": "suspended",
            "id": "1659556344330874880",
        }
        profile = fxtwitter.parse_profile_info(payload)
        self.assertEqual(profile.state, fxtwitter.PROFILE_SUSPENDED)
        self.assertEqual(profile.user_id, "1659556344330874880")

    def test_ok_keeps_user_id_and_name(self) -> None:
        payload = {
            "code": 200,
            "user": {"id": "1325253093705969664", "screen_name": "wn40536699", "name": "葡獭"},
        }
        profile = fxtwitter.parse_profile_info(payload)
        self.assertEqual(profile.state, fxtwitter.PROFILE_OK)
        self.assertEqual(profile.user_id, "1325253093705969664")
        self.assertEqual(profile.handle, "wn40536699")

    def test_missing(self) -> None:
        payload = {"code": 404, "message": "User not found", "reason": "not_found"}
        self.assertEqual(fxtwitter.parse_profile(payload), fxtwitter.PROFILE_MISSING)

    def test_unknown(self) -> None:
        self.assertEqual(fxtwitter.parse_profile(None), fxtwitter.PROFILE_UNKNOWN)
        self.assertEqual(fxtwitter.parse_profile({}), fxtwitter.PROFILE_UNKNOWN)
        self.assertEqual(
            fxtwitter.parse_profile({"code": 500, "message": "oops"}),
            fxtwitter.PROFILE_UNKNOWN,
        )
