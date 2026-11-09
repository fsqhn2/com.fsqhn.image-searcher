"""链接解析、账号提取与 key 池的单元测试。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import extract  # noqa: E402
from core.extract import (  # noqa: E402
    account_key,
    canonical_account_url,
    dedupe_accounts,
    extract_identity,
    is_avatar_image,
    is_pixiv_platform,
    is_social_platform,
    is_x_platform,
    match_account,
    platform_of,
    promote_mirror,
    relevance_rank,
)
from core.engines import Candidate  # noqa: E402
from core.keypool import KeyPool  # noqa: E402


class ExtractIdentityTest(unittest.TestCase):
    """账号直接从链接或标题取，不抓页面。"""

    def test_x_status_url(self) -> None:
        name, handle = extract_identity(
            "https://x.com/NERUchi890/status/2111815792396493110",
            "NENE on X: \"...\"",
        )
        self.assertEqual(handle, "@NERUchi890")

    def test_x_profile_url(self) -> None:
        _name, handle = extract_identity("https://x.com/usagi_rr", "荣小兔头")
        self.assertEqual(handle, "@usagi_rr")

    def test_title_handle(self) -> None:
        name, handle = extract_identity("https://example.com/post/1", "荣小兔头 @usagi_rr - Twitter")
        self.assertEqual(handle, "@usagi_rr")
        self.assertEqual(name, "荣小兔头")

    def test_bilibili_space(self) -> None:
        _name, handle = extract_identity("https://space.bilibili.com/123456", "某个UP")
        self.assertEqual(handle, "UID 123456")

    def test_bilibili_video(self) -> None:
        _name, handle = extract_identity("https://www.bilibili.com/video/BV1xx411c7mD", "视频标题")
        self.assertEqual(handle, "BV1xx411c7mD")

    def test_douyin_user(self) -> None:
        _name, handle = extract_identity("https://www.douyin.com/user/MS4wLjABAAAA", "某人")
        self.assertEqual(handle, "@MS4wLjABAAAA")

    def test_x_post_title_name_drops_body(self) -> None:
        """正文里的词不能当名字：``Happy Halloween!`` 曾推出 @halloween。"""
        self.assertEqual(
            extract.x_title_name('kiko on X: "Happy Halloween!" / X'), "kiko"
        )
        self.assertEqual(extract.x_title_name("普通标题"), "普通标题")

    def test_no_identity(self) -> None:
        name, handle = extract_identity("https://example.com/gallery/9", "一张相册图")
        self.assertEqual(handle, "")
        self.assertEqual(name, "一张相册图")

    def test_x_post_title_keeps_only_name(self) -> None:
        """X 帖子的标题是「名字 on X: "正文"」，正文不能当名字发出去。"""
        name, handle = extract_identity(
            "https://x.com/YuMIkAnime/status/2117959676180701586",
            '□카🕯 on X: "그러니까 중국 사이트 듣도보도못한 계정 찾아서 ...',
        )
        self.assertEqual(name, "□카🕯")
        self.assertEqual(handle, "@YuMIkAnime")


class AccountPageTest(unittest.TestCase):
    """账号页识别：只认账号页，帖子页/搜索页不硬猜。"""

    def test_instagram_profile(self) -> None:
        _name, handle = extract_identity("https://www.instagram.com/usagi_rr/", "ins")
        self.assertEqual(handle, "@usagi_rr")

    def test_instagram_post_is_not_account(self) -> None:
        _name, handle = extract_identity("https://www.instagram.com/p/Cabc123/", "一条帖子")
        self.assertEqual(handle, "")

    def test_pixiv_member_normalized(self) -> None:
        _name, handle = extract_identity("https://www.pixiv.net/member.php?id=123456", "画师")
        self.assertEqual(handle, "UID 123456")
        self.assertEqual(
            canonical_account_url("https://www.pixiv.net/member.php?id=123456"),
            "https://www.pixiv.net/users/123456",
        )

    def test_weibo_uid(self) -> None:
        _name, handle = extract_identity("https://weibo.com/u/1234567890", "")
        self.assertEqual(handle, "UID 1234567890")

    def test_weibo_nickname(self) -> None:
        _name, handle = extract_identity("https://weibo.com/n/%E5%B0%8F%E7%99%BD", "")
        self.assertEqual(handle, "@小白")

    def test_weibo_status_is_not_account(self) -> None:
        self.assertIsNone(match_account("https://weibo.com/status/1234567890"))
        self.assertIsNone(match_account("https://m.weibo.cn/status/1234567890"))

    def test_x_intent_is_not_account(self) -> None:
        self.assertIsNone(match_account("https://x.com/intent/follow?screen_name=abc"))

    def test_x_status_gives_profile(self) -> None:
        account = match_account("https://x.com/usagi_rr/status/123")
        self.assertIsNotNone(account)
        assert account is not None
        self.assertEqual(account[1], "usagi_rr")
        self.assertEqual(account[2], "https://x.com/usagi_rr")


class AccountKeyTest(unittest.TestCase):
    def test_uid_and_at_normalized(self) -> None:
        self.assertEqual(account_key("微博", "UID 123"), account_key("微博", "123"))
        self.assertEqual(account_key("X (Twitter)", "@Usagi_RR"), account_key("X (Twitter)", "usagi_rr"))

    def test_empty(self) -> None:
        self.assertEqual(account_key("微博", ""), "")

    def test_dedupe_keeps_better_one(self) -> None:
        weak = Candidate(engine="lens", handle="@usagi_rr", platform="X (Twitter)", status="unverified")
        strong = Candidate(engine="yandex", handle="@Usagi_RR", platform="X (Twitter)", status="match", distance=0)
        kept = dedupe_accounts([weak, strong])
        self.assertEqual(kept, [strong])

    def test_dedupe_keeps_first(self) -> None:
        first = Candidate(engine="lens", handle="@usagi_rr", platform="X (Twitter)", distance=0)
        dup = Candidate(engine="yandex", handle="@usagi_rr", platform="X (Twitter)", distance=12)
        other = Candidate(engine="baidu", handle="@someone", platform="X (Twitter)")
        blank = Candidate(engine="lens", platform="aliexpress.ru")
        blank2 = Candidate(engine="baidu", platform="aliexpress.ru")
        kept = dedupe_accounts([first, dup, other, blank, blank2])
        self.assertEqual(kept, [first, other, blank, blank2])


class PromoteSotweTest(unittest.TestCase):
    """X 镜像站还原成 x.com；handle 以标题为准，避免路径多余下划线。"""

    def test_promote_uses_title_handle(self) -> None:
        url, title = promote_mirror("https://sotwe.com/usagi_rr_", "荣小兔头 @usagi_rr - Twitter")
        self.assertEqual(url, "https://x.com/usagi_rr")
        self.assertEqual(title, "荣小兔头 @usagi_rr")

    def test_promote_falls_back_to_path(self) -> None:
        url, _title = promote_mirror("https://sotwe.com/miaomiaonyaw", "喵喵")
        self.assertEqual(url, "https://x.com/miaomiaonyaw")

    def test_promote_twstalker_subdomain(self) -> None:
        url, title = promote_mirror(
            "https://site.twstalker.com/www_wdzzsa",
            "白武男.帝者战神@www_wdzzsa - Twitter Profile | TwStalker",
        )
        self.assertEqual(url, "https://x.com/www_wdzzsa")
        self.assertEqual(title, "白武男.帝者战神@www_wdzzsa")

    def test_non_sotwe_untouched(self) -> None:
        url, title = promote_mirror("https://x.com/a/status/1", "标题")
        self.assertEqual((url, title), ("https://x.com/a/status/1", "标题"))


class PlatformTest(unittest.TestCase):
    def test_platform_names(self) -> None:
        self.assertEqual(platform_of("https://x.com/a/status/1"), "X (Twitter)")
        self.assertEqual(platform_of("https://www.bilibili.com/video/BV1"), "B站")
        self.assertEqual(platform_of("https://www.douyin.com/user/x"), "抖音")
        self.assertEqual(platform_of("https://weibo.com/u/123"), "微博")
        self.assertEqual(platform_of("https://www.pinterest.com/pin/1"), "Pinterest")

    def test_xxx_domain_not_x(self) -> None:
        # `"x.com" in "de.hentai-cosplay-xxx.com"` 会命中，曾经把带 xxx 的资源站判成 X。
        self.assertEqual(platform_of("https://de.hentai-cosplay-xxx.com/a"), "de.hentai-cosplay-xxx.com")
        self.assertEqual(platform_of("https://xxx.com/a"), "xxx.com")

    def test_x_subdomain_still_x(self) -> None:
        self.assertEqual(platform_of("https://mobile.x.com/a/status/1"), "X (Twitter)")
        self.assertEqual(platform_of("https://twitter.com/a/status/1"), "X (Twitter)")
        self.assertEqual(platform_of("https://sotwe.com/a"), "X (Twitter)")

    def test_relevance_rank(self) -> None:
        self.assertEqual(relevance_rank("https://x.com/a"), 0)
        self.assertEqual(relevance_rank("https://pinterest.com/pin/1"), 2)

    def test_forum_pages(self) -> None:
        """论坛帖的标题不是人名：inven 那单把韩文帖子标题当名字发了出去。"""
        self.assertTrue(extract.is_forum_page("https://www.inven.co.kr/board/lostark/6271/4036037"))
        self.assertTrue(extract.is_forum_page("https://gall.dcinside.com/board/view/?id=1"))
        self.assertFalse(extract.is_forum_page("https://x.com/NERUchi890/status/1"))
        self.assertFalse(extract.is_forum_page("https://galleryepic.com/a"))
        self.assertFalse(extract.is_forum_page(""))

    def test_x_page_kind(self) -> None:
        """只有 status/<id> 能当原帖，主页和转发/回复列表页都不行。"""
        self.assertEqual(extract.x_page_kind("https://x.com/NERUchi890/status/2117586176714612824"), "post")
        self.assertEqual(extract.x_page_kind("https://x.com/a/status/1/photo/1"), "post")
        self.assertEqual(extract.x_page_kind("https://twitter.com/i/web/status/800792643705962496"), "post")
        self.assertEqual(extract.x_page_kind("https://x.com/NENEchi8890"), "profile")
        self.assertEqual(extract.x_page_kind("https://x.com/"), "profile")
        self.assertEqual(extract.x_page_kind("https://x.com/AlbertGreen69/with_replies"), "with_replies")
        self.assertEqual(extract.x_page_kind("https://x.com/a/reposts"), "reposts")
        self.assertEqual(extract.x_page_kind("https://x.com/a/media"), "media")
        self.assertEqual(extract.x_page_kind("https://x.com/search?q=abc"), "search")
        self.assertEqual(extract.x_page_kind("https://sotwe.com/a"), "profile")
        self.assertEqual(extract.x_page_kind("https://nudogram.com/models/x/1/"), "")

    def test_only_post_pages_count_as_original(self) -> None:
        self.assertTrue(extract.is_x_post_page("https://x.com/a/status/1"))
        self.assertFalse(extract.is_x_post_page("https://x.com/a/with_replies"))
        self.assertFalse(extract.is_x_post_page("https://x.com/a"))


class KeyPoolTest(unittest.TestCase):
    """多 key 轮询与冷却。"""

    def test_round_robin(self) -> None:
        pool = KeyPool(["a", "b"])
        self.assertEqual(pool.acquire(), "a")
        self.assertEqual(pool.acquire(), "b")
        self.assertEqual(pool.acquire(), "a")

    def test_failure_switches_key(self) -> None:
        pool = KeyPool(["a", "b"])
        first = pool.acquire()
        pool.mark_failure(first, cooldown_seconds=60)
        self.assertEqual(pool.acquire(), "b")
        pool.mark_failure("b", cooldown_seconds=60)
        self.assertIsNone(pool.acquire())
        self.assertEqual(pool.available_count(), 0)

    def test_success_clears_cooldown(self) -> None:
        pool = KeyPool(["a"])
        pool.mark_failure("a", cooldown_seconds=60)
        self.assertIsNone(pool.acquire())
        pool.mark_success("a")
        self.assertEqual(pool.acquire(), "a")

    def test_empty_pool(self) -> None:
        pool = KeyPool([])
        self.assertFalse(pool)
        self.assertIsNone(pool.acquire())


class XPlatformPreferenceTest(unittest.TestCase):
    def test_detects_x_platform(self) -> None:
        self.assertTrue(is_x_platform("X (Twitter)"))
        self.assertTrue(is_x_platform("x.com"))
        self.assertTrue(is_x_platform("twitter"))

    def test_other_platforms_are_not_x(self) -> None:
        for platform in ("picazor.com", "B站", "抖音", "pixiv.net", ""):
            with self.subTest(platform=platform):
                self.assertFalse(is_x_platform(platform))


class PixivPlatformTest(unittest.TestCase):
    def test_detects_pixiv(self) -> None:
        self.assertTrue(is_pixiv_platform("pixiv.net"))
        self.assertTrue(is_pixiv_platform("i.pximg.net"))
        self.assertTrue(is_pixiv_platform("", "https://www.pixiv.net/artworks/12345"))

    def test_fanbox_is_not_main_site(self) -> None:
        for platform, url in (("Pixiv FANBOX", "https://xxx.fanbox.cc/posts/1"), ("Twitter", "https://x.com/a")):
            with self.subTest(platform=platform):
                self.assertFalse(is_pixiv_platform(platform, url))


class SocialPlatformTest(unittest.TestCase):
    """社交平台候选要能认出来，普通站点不能被误判。"""

    def test_detects_main_social_sites(self) -> None:
        for url in (
            "https://x.com/NERUchi890/status/2111815792396493110",
            "https://twitter.com/a/status/1",
            "https://weibo.com/1234/abcd",
            "https://m.weibo.cn/detail/456",
            "https://www.xiaohongshu.com/explore/abc",
            "https://www.instagram.com/p/abc/",
            "https://www.bilibili.com/video/BV1xx",
            "https://b23.tv/abc",
            "https://www.pixiv.net/artworks/12345",
            "https://t.me/somechannel/12",
            "https://www.pinterest.com/pin/1/",
        ):
            with self.subTest(url=url):
                self.assertTrue(is_social_platform(url))

    def test_falls_back_to_source(self) -> None:
        self.assertTrue(is_social_platform("", "x.com"))
        self.assertTrue(is_social_platform("https://example.org/a", "weibo.com"))

    def test_plain_sites_are_not_social(self) -> None:
        for url in (
            "https://netflix.com/title/1",
            "https://skebetter.com/author/1",
            "https://dto.to/title/157409",
            "https://www.google.com/search?q=1",
            "",
        ):
            with self.subTest(url=url):
                self.assertFalse(is_social_platform(url))


if __name__ == "__main__":
    unittest.main()

class AvatarImageTest(unittest.TestCase):
    """Lens 会把站点账号头像当结果图返回，这种命中不算原帖。"""

    def test_avatar_path_is_detected(self) -> None:
        self.assertTrue(is_avatar_image("https://pxlorbit.example.com/avatar/ab/12/cd.png"))

    def test_uploaded_avatar_path_is_detected(self) -> None:
        self.assertTrue(is_avatar_image("https://cdn.example.com/uploads/avatars/u123.jpg"))

    def test_logo_and_favicon_are_detected(self) -> None:
        self.assertTrue(is_avatar_image("https://site.example.com/static/logo-256.png"))
        self.assertTrue(is_avatar_image("https://site.example.com/favicon.ico"))

    def test_x_profile_image_is_detected(self) -> None:
        """X 的头像地址不带 avatar 字样，s-20261106-1642 就是栽在这里。"""
        self.assertTrue(
            is_avatar_image(
                "https://pbs.twimg.com/profile_images/2062750521325391872/Gk7_NOC2_400x400.jpg"
            )
        )

    def test_normal_image_url_is_not_avatar(self) -> None:
        self.assertFalse(is_avatar_image("https://cdn.example.com/photos/2026/miku-01.jpg"))
        self.assertFalse(is_avatar_image("https://hentaiera.com/gallery/1741778/cover.jpg"))

    def test_empty_and_pathless_url_are_not_avatar(self) -> None:
        self.assertFalse(is_avatar_image(""))
        self.assertFalse(is_avatar_image("https://example.com"))


class CrawlerPlaceholderTest(unittest.TestCase):
    """Instagram / Facebook 的爬虫占位图：取回来是登录页 HTML，不是图。"""

    def test_detects_lookaside_hosts(self) -> None:
        self.assertTrue(
            extract.is_crawler_placeholder(
                "https://lookaside.instagram.com/seo/google_widget/crawler/?media_id=4008272337751746138"
            )
        )
        self.assertTrue(
            extract.is_crawler_placeholder(
                "https://lookaside.fbsbx.com/lookaside/crawler/media/?media_id=1060386696364398"
            )
        )

    def test_normal_cdn_is_not_placeholder(self) -> None:
        self.assertFalse(extract.is_crawler_placeholder("https://pbs.twimg.com/media/HWMtZrJbcAA82KO.jpg"))
        self.assertFalse(extract.is_crawler_placeholder(""))

    def test_rescue_instagram(self) -> None:
        """短码能拼出免登录直链；实测 5/5 返回 JPEG。"""
        self.assertEqual(
            extract.rescue_image_url(
                "https://www.instagram.com/reel/C2xmUF8LrMd/",
                "https://lookaside.instagram.com/seo/google_widget/crawler/?media_id=1",
            ),
            "https://www.instagram.com/p/C2xmUF8LrMd/media/?size=l",
        )

    def test_rescue_gives_up_on_facebook(self) -> None:
        self.assertEqual(
            extract.rescue_image_url(
                "https://www.facebook.com/61558497137816/videos/sunna-cosplay",
                "https://lookaside.fbsbx.com/lookaside/crawler/media/?media_id=1",
            ),
            "",
        )

    def test_rescue_ignores_real_images(self) -> None:
        self.assertEqual(
            extract.rescue_image_url(
                "https://www.instagram.com/p/DX18cbMCLqr/",
                "https://scontent.cdninstagram.com/v/t51/abc.jpg",
            ),
            "",
        )


class XPageHandleTest(unittest.TestCase):
    """从 X 页面链接里取账号名，回复页反查要用它去翻这个号的回复。"""

    def test_reply_page(self) -> None:
        self.assertEqual(
            extract.x_page_handle("https://x.com/wn40536699/with_replies"), "wn40536699"
        )

    def test_post_url(self) -> None:
        self.assertEqual(
            extract.x_page_handle("https://twitter.com/hinatahimarii/status/2118658490315276514?s=20"),
            "hinatahimarii",
        )

    def test_function_paths_and_other_hosts(self) -> None:
        self.assertEqual(extract.x_page_handle("https://x.com/i/status/2118658490315276514"), "")
        self.assertEqual(extract.x_page_handle("https://x.com/search?q=abc"), "")
        self.assertEqual(extract.x_page_handle("https://sotwe.com/someone"), "")
        self.assertEqual(extract.x_page_handle(""), "")
