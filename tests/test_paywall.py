"""付费页追踪的纯函数：链接、关键词、帖子、档位、门槛文案。"""

from __future__ import annotations

import unittest
from datetime import date

from core import paywall


class LinksTest(unittest.TestCase):
    def test_reads_patreon_and_fanbox_from_bio(self) -> None:
        bio = (
            "这里是ran 3 定制委托私 唯一小号@ran3ran3ran3 "
            "fanbox∶https://ran3.fanbox.cc/ patreon:https://www.patreon.com/c/oioioi525 微店：香辣ran堡"
        )
        links = paywall.links_of(bio)
        self.assertEqual(links.get("patreon"), "oioioi525")
        self.assertEqual(links.get("fanbox"), "ran3")

    def test_ignores_path_words(self) -> None:
        links = paywall.links_of("https://www.patreon.com/posts/123", "https://x.fanbox.cc/posts/1")
        self.assertNotIn(links.get("patreon"), {"posts", "c"})

    def test_no_link_returns_empty(self) -> None:
        self.assertEqual(paywall.links_of("只写了个微博 weibo.com/u/1"), {})


class KeywordTest(unittest.TestCase):
    def test_keeps_latin_and_cjk(self) -> None:
        words = paywall.keywords("#zzzero #RemielleDan 洛茜 Rossi 108p")
        self.assertIn("remielledan", words)
        self.assertIn("rossi", words)
        self.assertIn("洛茜", words)

    def test_drops_noise(self) -> None:
        words = paywall.keywords("https://x.com/oioioi525/status/1 cosplay")
        self.assertNotIn("cosplay", words)
        self.assertNotIn("https", words)

    def test_cjk_run_also_yields_character_name(self) -> None:
        """``万圣节妃咲`` 整串搜不到，得能拆出末尾的角色名。"""
        words = paywall.keywords("万圣节妃咲 ミサキ（ハロウィン）106p")
        self.assertIn("万圣节妃咲", words)
        self.assertIn("妃咲", words)


class PatreonParseTest(unittest.TestCase):
    def _payload(self) -> dict:
        return {
            "data": [
                {
                    "attributes": {
                        "title": "Rossi 108p",
                        "url": "https://www.patreon.com/oioioi525/posts/rossi-108p-171677785",
                        "published_at": "2026-10-07T14:58:11.000+00:00",
                        "current_user_can_view": False,
                    }
                },
                {
                    "attributes": {
                        "title": "Vesna 108p",
                        "url": "https://www.patreon.com/oioioi525/posts/vesna-108p-174754282",
                        "published_at": "2026-11-08T16:50:25.000+00:00",
                        "current_user_can_view": False,
                    }
                },
                {
                    "attributes": {
                        "title": "",
                        "url": "https://www.patreon.com/x",
                    }
                },
            ]
        }

    def test_parse_posts(self) -> None:
        posts = paywall.patreon_posts(self._payload())
        self.assertEqual(len(posts), 2)
        self.assertEqual(posts[0]["published"], "2026-10-07")
        self.assertTrue(posts[0]["locked"])

    def test_pick_post_by_character(self) -> None:
        posts = paywall.patreon_posts(self._payload())
        post = paywall.pick_post(posts, ["rossi"])
        self.assertIsNotNone(post)
        self.assertEqual(post["title"], "Rossi 108p")

    def test_pick_post_none_when_no_match(self) -> None:
        posts = paywall.patreon_posts(self._payload())
        self.assertIsNone(paywall.pick_post(posts, ["amiya"]))

    def test_prefers_photo_set_over_video(self) -> None:
        """同一期既有 108p 又有 7v，用户要的是图集。"""
        posts = [
            {"title": "Rossi 7v", "url": "https://p/7v", "min_cents": 0},
            {"title": "Rossi 108p", "url": "https://p/108p", "min_cents": 0},
        ]
        self.assertEqual(paywall.pick_post(posts, ["rossi"])["title"], "Rossi 108p")

    def test_rewards_and_tier_text(self) -> None:
        payload = {
            "included": [
                {"type": "reward", "attributes": {"title": "Free", "amount_cents": 0, "currency": "HKD"}},
                {"type": "reward", "attributes": {"title": "super ran", "amount_cents": 20000, "currency": "HKD"}},
                {"type": "reward", "attributes": {"title": "ranran", "amount_cents": 5500, "currency": "HKD"}},
                {"type": "user", "attributes": {"full_name": "ran3"}},
            ]
        }
        tiers = paywall.patreon_rewards(payload)
        self.assertEqual([tier["title"] for tier in tiers], ["Free", "ranran", "super ran"])
        self.assertIn("Free（免费）", paywall.tier_text(tiers))
        self.assertIn("ranran HK$55", paywall.tier_text(tiers))


class FanboxParseTest(unittest.TestCase):
    def test_posts_and_plans(self) -> None:
        payload = {
            "body": {
                "posts": [
                    {
                        "id": "12905409",
                        "title": "万圣节妃咲 ミサキ（ハロウィン）106p",
                        "feeRequired": 4000,
                        "isRestricted": True,
                        "publishedDatetime": "2026-11-09T01:53:17+00:00",
                    }
                ]
            }
        }
        posts = paywall.fanbox_posts(payload, "ran3")
        self.assertEqual(posts[0]["url"], "https://ran3.fanbox.cc/posts/12905409")
        self.assertEqual(posts[0]["min_cents"], 4000)
        plans = paywall.fanbox_plans(
            {"body": {"plans": [{"title": "染兔染兔染", "fee": 1000}, {"title": "终极染", "fee": 10000}]}}
        )
        self.assertEqual([plan["cents"] for plan in plans], [1000, 10000])
        self.assertIn("染兔染兔染 ¥1,000", paywall.tier_text(plans))

    def test_cjk_title_matches(self) -> None:
        posts = paywall.fanbox_posts(
            {
                "body": {
                    "posts": [
                        {"id": "1", "title": "阿米娅 アーミヤ 105p", "feeRequired": 4000},
                        {"id": "2", "title": "万圣节妃咲 ミサキ 106p", "feeRequired": 4000},
                    ]
                }
            },
            "ran3",
        )
        self.assertEqual(paywall.pick_post(posts, ["妃咲"])["title"], "万圣节妃咲 ミサキ 106p")
        self.assertEqual(paywall.pick_post(posts, ["アーミヤ"])["title"], "阿米娅 アーミヤ 105p")


class GateTextTest(unittest.TestCase):
    def test_fanbox_fee_is_written_out(self) -> None:
        text = paywall.gate_text({"min_cents": 4000, "currency": "JPY"})
        self.assertIn("¥4,000", text)
        self.assertIn("订阅", text)

    def test_patreon_locked_uses_tier_floor(self) -> None:
        tiers = [{"title": "ranran", "cents": 5500, "currency": "HKD", "free": False}]
        text = paywall.gate_text({"locked": True}, tiers)
        self.assertIn("HK$55", text)

    def test_public_post(self) -> None:
        self.assertIn("公开帖", paywall.gate_text({"min_cents": 0}))

    def test_money_units(self) -> None:
        self.assertEqual(paywall.money(5500, "HKD"), "HK$55")
        self.assertEqual(paywall.money(4000, "JPY"), "¥4,000")


class PostDateTest(unittest.TestCase):
    def test_iso_date(self) -> None:
        self.assertEqual(paywall.post_date("2026-11-09"), date(2026, 11, 9))

    def test_tweet_date_with_year(self) -> None:
        self.assertEqual(
            paywall.post_date("Fri Oct 03 13:43:00 +0000 2025"), date(2025, 10, 3)
        )

    def test_tweet_date_without_year_uses_anchor(self) -> None:
        """X 的搜索结果有时不写年份，用这一期的日期补。"""
        self.assertEqual(paywall.post_date("Mon Oct 13 15:31", "2026-11-09"), date(2026, 10, 13))
        # 补出来比这一期晚半年以上，说明是去年那一条。
        self.assertEqual(paywall.post_date("Fri Dec 06 01:58", "2026-11-09"), date(2025, 12, 6))

    def test_garbage(self) -> None:
        self.assertIsNone(paywall.post_date(""))
        self.assertIsNone(paywall.post_date("昨天"))


class PickTeaserTest(unittest.TestCase):
    def _item(self, url: str, published: str, shop: bool = False, media: bool = False) -> dict:
        return {
            "url": url,
            "published_raw": published,
            "kind": "announcement" if shop else "teaser",
            "has_media": media,
        }

    def test_prefers_the_same_period(self) -> None:
        """同角色两条帖，选离这一期发布日近的那条。"""
        items = [
            self._item("https://x.com/a/status/1", "Fri Oct 03 13:43:00 +0000 2025", shop=True),
            self._item("https://x.com/a/status/2", "Sun Nov 08 12:00:00 +0000 2026", media=True),
        ]
        picked = paywall.pick_teaser(items, "2026-11-09")
        self.assertEqual(picked["url"], "https://x.com/a/status/2")
        self.assertTrue(picked["same_period"])
        self.assertEqual(picked["when"], "2026-11-08")

    def test_old_announcement_is_not_same_period(self) -> None:
        """上一期的上架通知不能算这一期：标成隔得远的那条。"""
        items = [self._item("https://x.com/a/status/1", "Fri Oct 03 13:43:00 +0000 2025", shop=True)]
        picked = paywall.pick_teaser(items, "2026-11-09")
        self.assertFalse(picked["same_period"])
        self.assertGreater(picked["gap"], 14)

    def test_empty(self) -> None:
        self.assertIsNone(paywall.pick_teaser([], "2026-11-09"))

    def test_unknown_paid_date_stays_unsure(self) -> None:
        """这一期没写发布日时，不能断言是这期的预告。"""
        items = [self._item("https://x.com/a/status/1", "Fri Oct 03 13:43:00 +0000 2025", shop=True)]
        picked = paywall.pick_teaser(items, "")
        self.assertFalse(picked["same_period"])


if __name__ == "__main__":
    unittest.main()
