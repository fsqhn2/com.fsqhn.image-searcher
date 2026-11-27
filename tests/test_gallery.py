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
        # c51b 那单：页面 JSON-LD 的 author 就是站名，标题里的人名是站方分类。
        self.assertTrue(gallery.is_gallery_site("https://hentai.yoga/es/gallery/open/mai-khan-han-3"))
        self.assertTrue(gallery.is_gallery_site("https://cdn.hentai.yoga/gallery/x/1.jpg"))
        # c077 那单：图集站按 /model/<名字> 分套搬图，页面里没有任何社媒账号。
        self.assertTrue(
            gallery.is_gallery_site(
                "https://www.hotgirlpix.com/p/coser-xiao-cang-qian-dai-w-8211-ming-ri-fang-zhou-w-yong-zhuang/page/2"
            )
        )
        self.assertTrue(
            gallery.is_gallery_site(
                "https://misskon.com/coser-xiao-cang-qian-dai-w-w-yong-zhuang-21-photos/2/"
            )
        )
        # 5832 那单：xasiat 把套图成套搬走，距离 0 落在它上面时被当成「找到了」，
        # 用户点开只是一堆转载。它进了图集站名单，结论才会降级成线索。
        self.assertTrue(
            gallery.is_gallery_site("https://xasiat.com/p/coser-w-yong-zhuang/")
        )
        self.assertTrue(gallery.is_repost_site("https://xasiat.com/p/coser-w-yong-zhuang/"))
        # 649c：COS团（costuan）是资源打包站（34P-182MB、度盘下载），署名是站方编辑。
        # 不认它的话结论会被当「找到了」，微博那条按名字的腿也整条被挡掉。
        self.assertTrue(gallery.is_gallery_site("https://www.costuan.com/14090.html"))
        self.assertTrue(gallery.is_repost_site("https://www.costuan.com/14090.html"))

    def test_shop_pages(self):
        """838e：可莉背包的图，引擎回的全是同款商品页，参考区不该摆这些。"""
        self.assertTrue(
            gallery.is_shop_site(
                "https://dokidokicos.com/products/dokidoki-game-genshin-impact-cosplay-klee-itabag"
            )
        )
        self.assertTrue(gallery.is_shop_site("https://www.gcosplay.com/products/x", "gcosplay.com"))
        self.assertTrue(gallery.is_shop_site("https://www.aliexpress.com/item/1005006.html"))
        self.assertTrue(gallery.is_shop_site("https://item.taobao.com/item.htm?id=1"))
        # 模板路径 + 商店域名才认；光有 /products/ 不算。
        self.assertTrue(gallery.is_shop_site("https://mystore.example.com/products/x"))
        self.assertFalse(gallery.is_shop_site("https://cosplay.pixibb.com/products/x"))
        self.assertFalse(gallery.is_shop_site("https://www.pixiv.net/artworks/1"))
        self.assertFalse(gallery.is_shop_site(""))

    def test_normal_sites_are_not_galleries(self):
        self.assertFalse(gallery.is_gallery_site("https://x.com/someone/status/1"))
        self.assertFalse(gallery.is_gallery_site("https://www.pixiv.net/artworks/1"))
        self.assertFalse(gallery.is_gallery_site(""))

    def test_baidu_content_pages(self):
        """百度动态/百家号只能当线索，不能当作者主页（b3b4 那单）。"""
        self.assertTrue(
            gallery.is_content_site(
                "http://mbd.baidu.com/newspage/data/dtlandingsuper?nid=dt_4737138101278019876"
            )
        )
        self.assertTrue(gallery.is_content_site("https://baijiahao.baidu.com/s?id=1"))
        self.assertTrue(gallery.is_content_site(platform="mbd.baidu.com"))
        # 百度主域是搜索引擎，不能跟着一起降级。
        self.assertFalse(gallery.is_content_site("https://www.baidu.com/s?wd=x"))
        self.assertFalse(gallery.is_content_site("https://x.com/someone/status/1"))
        self.assertFalse(gallery.is_content_site(""))

    def test_clickme_is_a_content_site(self) -> None:
        """411f 落在 clickme 那篇《Mio Kuuko》文章上：是资讯站，不是作者主页。"""
        self.assertTrue(gallery.is_content_site("https://clickme.net/62904"))
        self.assertTrue(gallery.is_content_site(platform="clickme.net"))
        self.assertFalse(gallery.is_content_site("https://notclickme.net/x"))

    def test_honkaihaven_is_a_repost_site(self) -> None:
        """cdfa 落在 honkaihaven 的 `/gallery/<id>/` 上：扒 pixiv 图的站，不是作者主页。

        它的图床文件名还留着 pixiv 作品号与作者名（`…-by-lol-…-152342429.webp`），
        页面本身只是图集。归搬运站这档（不是付费图包站），降级文案才不会写成
        「付费图包页」。
        """
        self.assertTrue(gallery.is_repost_site("https://honkaihaven.com/gallery/1129766/漫画预告"))
        self.assertFalse(gallery.is_gallery_site("https://honkaihaven.com/gallery/1129766/"))
        self.assertFalse(gallery.is_repost_site("https://nothonkaihaven.com/gallery/1/"))

    def test_forum_threads_are_recognized(self) -> None:
        """b62e／ecaa 只搜到 NGA：那类是讨论帖，答案常在回复里。"""
        self.assertTrue(gallery.is_forum_site("https://bbs.nga.cn/read.php?tid=47962202"))
        self.assertTrue(gallery.is_forum_site(platform="ngabbs.com"))
        self.assertTrue(gallery.is_forum_site("https://tieba.baidu.com/p/123"))
        self.assertFalse(gallery.is_forum_site("https://x.com/someone/status/1"))
        self.assertFalse(gallery.is_forum_site("https://honkaihaven.com/gallery/1/"))

    def test_help_pages_are_recognized(self) -> None:
        """whos.tv 这类求图站公开可读，正文里直接写着出处（826c）。"""
        self.assertTrue(gallery.is_help_page("https://whos.tv/en/helps/18619"))
        self.assertTrue(gallery.is_help_page("https://example.com/questions/123"))
        self.assertFalse(gallery.is_help_page("https://x.com/someone/status/1"))
        self.assertFalse(gallery.is_help_page("https://honkaihaven.com/gallery/1/"))

    def test_source_link_keeps_only_verifiable_links(self) -> None:
        """只有 X 帖和 pixiv 作品能拿去核对，分享按钮与账号页都不收。"""
        post = gallery.source_link("https://x.com/Ahab_Channel/status/1766582341927891150?s=20")
        self.assertEqual(post["kind"], "x_post")
        self.assertEqual(post["handle"], "Ahab_Channel")
        self.assertEqual(post["id"], "1766582341927891150")
        artwork = gallery.source_link("https://www.pixiv.net/artworks/152342429")
        self.assertEqual(artwork["kind"], "pixiv_artwork")
        self.assertEqual(artwork["id"], "152342429")
        self.assertEqual(
            gallery.source_link("https://www.pixiv.net/member_illust.php?mode=medium&illust_id=106409163")["id"],
            "106409163",
        )
        # 分享按钮、站点官号、普通账号页都不是答案
        self.assertIsNone(gallery.source_link("https://x.com/intent/tweet?url=http%3A%2F%2Fwhos.tv"))
        self.assertIsNone(gallery.source_link("https://x.com/whos__tv"))
        self.assertIsNone(gallery.source_link("https://x.com/i/web/status/1766582341927891150"))

    def test_social_links_from_html(self) -> None:
        """页面里同一条链接出现两遍只留一条，按出现顺序，最多四条。"""
        html = (
            '<a href="https://x.com/intent/tweet?url=x">share</a>'
            '<p>原图在这儿 https://x.com/Ahab_Channel/status/1766582341927891150。</p>'
            '<a href="https://x.com/Ahab_Channel/status/1766582341927891150">again</a>'
            '<p>https://www.pixiv.net/artworks/152342429</p>'
            '<p>https://x.com/whos__tv</p>'
        )
        links = gallery.social_links_from_html(html, "https://whos.tv/en/helps/18619")
        self.assertEqual([item["kind"] for item in links], ["x_post", "pixiv_artwork"])
        self.assertEqual(links[0]["url"], "https://x.com/Ahab_Channel/status/1766582341927891150")

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

    def test_image_farm_hosts_count_as_repost(self):
        """s-20261111-1028-1063：结论停在 galleryepic，用户一眼问「不是作者页吧」。"""
        self.assertTrue(gallery.is_repost_site("https://galleryepic.com/en/cosplay/9768"))
        self.assertTrue(gallery.is_repost_site("https://cosplayrule34.com/us/post/102074"))
        self.assertFalse(gallery.is_gallery_site("https://galleryepic.com/en/cosplay/9768"))

    def test_joke_roundups_count_as_repost(self):
        """s-20261111-2327-5138：命中游民星空一篇「囧图」日更合集。

        那页是把网上的图扒下来重发的，指不到作者；插件原来不认它，连图上水印
        都没读，直接把合集页当出处回了群。
        """
        self.assertTrue(
            gallery.is_repost_site("https://www.gamersky.com/ent/202601/13451846.shtml", "gamersky.com")
        )
        # 命中记录里给的是图片域名，平台写的才是主站，两个都得认。
        self.assertTrue(
            gallery.is_repost_site(
                "https://img1.gamersky.com/image2026/01/20260101_ls_red_141_1/134_S.jpg"
            )
        )
        self.assertFalse(gallery.is_repost_site("https://notgamersky.com/a"))

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

    def test_image_host_path_is_not_an_x_account(self):
        """图床路径里的 `x.com/files` 不是 X 号：c077 那单 best 上挂着 count=10 的假账号。"""
        html = (
            '<img src="https://f.hotgirlpix.com/files/2025/03/03/21/idgbfogx70.jpg">' * 10
        )
        self.assertEqual(gallery.parse_source_accounts(html, "https://www.hotgirlpix.com/x"), [])


class AuthorPageTest(unittest.TestCase):
    """落点是泄露站时，翻一层「帖子页 → 发布者主页」找作者账号。"""

    POST_URL = "https://picazor.com/en/maonu164196-1/48"

    def test_author_page_is_the_parent_path(self):
        html = '<a href="/en/maonu164196-1">Maonu164196</a><a href="/en/hot">hot</a>'
        self.assertEqual(
            gallery.author_page_url(html, self.POST_URL),
            "https://picazor.com/en/maonu164196-1",
        )

    def test_missing_parent_link_returns_empty(self):
        html = '<a href="/en/hot">hot</a><a href="/en/top">top</a>'
        self.assertEqual(gallery.author_page_url(html, self.POST_URL), "")

    def test_other_hosts_are_not_taken_as_author_page(self):
        html = '<a href="https://other.example/en/maonu164196-1">x</a>'
        self.assertEqual(gallery.author_page_url(html, self.POST_URL), "")

    def test_picazor_counts_as_author_page_site(self):
        self.assertTrue(gallery.is_author_page_site(self.POST_URL))
        self.assertTrue(gallery.is_author_page_site("", "picazor.com"))
        self.assertFalse(gallery.is_author_page_site("https://4play.to/a/1"))
        self.assertFalse(gallery.is_author_page_site("https://notpicazor.com/a/1"))

    def test_social_link_on_author_page_counts_once(self):
        """主页上的 X 链接只出现一次也要认：那是作者信息区块，不是噪声。"""
        html = """
        <html><head><title>Maonu164196 | Picazor</title></head>
        <body>
        <a href="https://picazor.com/en/maonu164196-1">home</a>
        <a href="https://x.com/maonu164196">X</a>
        </body></html>
        """
        found = gallery.author_from_author_page(html, "https://picazor.com/en/maonu164196-1")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["platform"], "X")
        self.assertEqual(found[0]["handle"], "maonu164196")
        self.assertEqual(found[0]["url"], "https://x.com/maonu164196")
        self.assertTrue(found[0]["from_author_page"])

    def test_author_page_without_social_link(self):
        html = '<a href="https://picazor.com/en/maonu164196-1">home</a>'
        self.assertEqual(
            gallery.author_from_author_page(html, "https://picazor.com/en/maonu164196-1"), []
        )

