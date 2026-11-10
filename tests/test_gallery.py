"""图集站来源账号的认站与解析，纯函数，不联网。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import gallery  # noqa: E402


class GallerySiteTest(unittest.TestCase):
    def test_known_hosts(self):
        self.assertTrue(gallery.is_gallery_site("https://4play.to/koleksi/235-angel-chan/1847"))
        self.assertTrue(gallery.is_gallery_site("https://www.v2ph.com/album/z4ox3ona.html"))
        self.assertTrue(gallery.is_gallery_site("https://beauty-draw.02o.app/beauty/EPCZxsR"))
        self.assertTrue(gallery.is_gallery_site(platform="acgtang.com"))

    def test_normal_sites_are_not_galleries(self):
        self.assertFalse(gallery.is_gallery_site("https://x.com/someone/status/1"))
        self.assertFalse(gallery.is_gallery_site("https://www.pixiv.net/artworks/1"))
        self.assertFalse(gallery.is_gallery_site(""))

    def test_suffix_lookalikes_do_not_match(self):
        """域名得按后缀精确匹配，否则仿站会被当成已知站点。"""
        self.assertFalse(gallery.is_gallery_site("https://not4play.to/x"))
        self.assertFalse(gallery.is_gallery_site("https://4play.to.evil.com/x"))


class RepostSiteTest(unittest.TestCase):
    def test_community_boards_count_as_repost(self):
        """arca.live 那单：距离 0 的帖子只是别人转的图，页面里没有账号。"""
        self.assertTrue(gallery.is_repost_site("https://arca.live/b/genshin/188444947?category=짤&p=1"))
        self.assertTrue(gallery.is_repost_site(platform="arca.live"))
        self.assertTrue(gallery.is_repost_site("https://tieba.baidu.com/p/123"))
        self.assertTrue(gallery.is_repost_site("https://www.reddit.com/r/cosplay/x"))

    def test_gallery_hosts_also_count(self):
        self.assertTrue(gallery.is_repost_site("https://4play.to/koleksi/235/1847"))

    def test_author_pages_are_not_repost(self):
        self.assertFalse(gallery.is_repost_site("https://x.com/Sakana_Yudan/status/2118497064795729951"))
        self.assertFalse(gallery.is_repost_site("https://www.instagram.com/p/DQo68QHkx0G/"))
        self.assertFalse(gallery.is_repost_site(""))

    def test_lookalike_hosts_do_not_match(self):
        self.assertFalse(gallery.is_repost_site("https://arca.live.evil.com/b/1"))
        self.assertFalse(gallery.is_repost_site("https://notarca.live/b/1"))


class SourceAccountTest(unittest.TestCase):
    def test_picks_the_most_linked_account(self):
        """4play 那单的真实形状：图集里每张缩略图都链向同一个 IG。"""
        html = """
        <html><head><meta name="description" content="https://www.instagram.com/menruinyanko/"></head>
        <body>
        <a href="https://www.instagram.com/menruinyanko/">1</a>
        <a href="https://www.instagram.com/menruinyanko/">2</a>
        <a href="https://www.instagram.com/menruinyanko/">3</a>
        <a href="https://www.instagram.com/somebody_else/">once</a>
        </body></html>
        """
        found = gallery.parse_source_accounts(html, "https://4play.to/koleksi/235/1847")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["platform"], "Instagram")
        self.assertEqual(found[0]["handle"], "menruinyanko")
        self.assertEqual(found[0]["url"], "https://instagram.com/menruinyanko")
        self.assertGreaterEqual(found[0]["count"], 3)

    def test_reserved_paths_are_not_accounts(self):
        html = """
        <a href="https://instagram.com/p/Cabc123/">post</a>
        <a href="https://instagram.com/p/Cabc123/">post</a>
        <a href="https://instagram.com/reel/Cabc123/">reel</a>
        <a href="https://instagram.com/reel/Cabc123/">reel</a>
        <a href="https://instagram.com/p/Cabc123/">post</a>
        """
        self.assertEqual(gallery.parse_source_accounts(html, "https://4play.to/x"), [])

    def test_one_off_mentions_are_ignored(self):
        """页脚、友链只出现一两次，不能当成「这页讲的就是她」。"""
        html = (
            '<a href="https://instagram.com/friend_one/">1</a>'
            '<a href="https://instagram.com/friend_two/">2</a>'
            '<a href="https://instagram.com/friend_two/">2</a>'
        )
        self.assertEqual(gallery.parse_source_accounts(html, "https://4play.to/x"), [])

    def test_x_accounts_are_found_too(self):
        html = '<a href="https://x.com/someone">1</a>' * 4 + '<a href="https://twitter.com/other">2</a>'
        found = gallery.parse_source_accounts(html, "https://v2ph.com/album/x.html")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["platform"], "X")
        # x.com 和 twitter.com 是同一个平台，取出现最多的那个。
        self.assertEqual(found[0]["handle"], "someone")

    def test_garbage_and_empty(self):
        self.assertEqual(gallery.parse_source_accounts("", "https://4play.to/x"), [])
        self.assertEqual(gallery.parse_source_accounts("<html>nothing</html>", "https://4play.to/x"), [])
