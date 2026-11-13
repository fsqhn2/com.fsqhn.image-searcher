"""转载页反查的纯函数测试。

`plugin._hunt_weibo_repost` 联网部分不进单测：这里锁死「认页面、拼检索词、
解析搜索结果卡片」三件事——它们错了，后面比指纹再准也是白搭。
"""

import unittest

from core import identity, weibopost


SINA_PAGE = """<html><head>
<meta property="og:title" content="黑雪姬 Cosplay 写真：蝴蝶与霓虹的加速世界" />
<script>var junk = "导航不要";</script>
</head><body>
<p>黑雪姬 Cosplay 写真：蝴蝶与霓虹的加速世界|广州 P2 空间|加速世界|黑雪姬_新浪新闻</p>
<p>G44不会受伤</p>
<p>26-11-12 11:40 微博认证：动漫博主</p>
<p>加速世界-黑雪姬</p>
<p>“少年，你想不想……“加速”到更快的境界？”</p>
<p>phx<a href="//weibo.com/n/xxx">@小明明不要受伤</a></p>
<p><a>#二次元cos大赏#</a></p>
<p>大图见评</p>
<p>发布于 福建</p>
</body></html>
"""


CARD_HTML = """<div class="card-wrap" action-type="feed_list_item" mid="5364394628221146" >
  <div class="card"><div class="content">
    <a href="javascript:void(0);" @click="copyurl('https://weibo.com/2727362643/Rr1ciyuH8?refer_flag=1001030103_')">复制微博地址</a>
    <a href="//weibo.com/2727362643?refer_flag=1001030103_" class="name" target="_blank" nick-name="G44不会受伤">G44不会受伤</a>
    <p class="txt" node-type="feed_list_content" nick-name="G44不会受伤" >
      加速世界-黑雪姬<br/>大图见评
    </p>
    <div action-data="uid=2727362643&mid=5364394628221146&pic_ids=a2904053gy1iiz1vymf9mj22o840a7wj,a2904053gy1iiz23w5panj241e311b2a"></div>
  </div></div>
</div>
<div class="card-wrap" action-type="feed_list_item" mid="1" >
  <div class="card"><div class="content">
    <a href="javascript:void(0);" @click="copyurl('https://weibo.com/2727362643/Rr1ciyuH8?refer_flag=1')">复制</a>
    <p class="txt" node-type="feed_list_content" nick-name="G44不会受伤">重复卡片</p>
  </div></div>
</div>
"""


class RepostPageTest(unittest.TestCase):
    def test_recognises_sina_repost_page(self):
        self.assertTrue(
            weibopost.is_weibo_repost_page(
                "https://www.sina.cn/news/detail/5364394628221146.html", "新浪"
            )
        )
        self.assertTrue(
            weibopost.is_weibo_repost_page("https://k.sina.com.cn/article_1.html", "")
        )

    def test_other_sites_are_not_repost_pages(self):
        self.assertFalse(weibopost.is_weibo_repost_page("https://www.sohu.com/a/1", "搜狐"))
        self.assertFalse(weibopost.is_weibo_repost_page("https://weibo.com/u/1", "微博"))
        self.assertFalse(weibopost.is_weibo_repost_page("", ""))

    def test_page_text_drops_scripts_and_keeps_lines(self):
        title, body = weibopost.page_text(SINA_PAGE)
        self.assertIn("黑雪姬", title)
        self.assertNotIn("导航不要", body)
        self.assertIn("加速世界-黑雪姬", body)

    def test_page_author_reads_the_certified_line(self):
        _, body = weibopost.page_text(SINA_PAGE)
        self.assertEqual(weibopost.page_author(body), "G44不会受伤")

    def test_page_author_without_certification_returns_empty(self):
        self.assertEqual(weibopost.page_author("随便一段正文\n没有认证信息"), "")

    def test_post_queries_use_body_head_and_hashtag(self):
        title, body = weibopost.page_text(SINA_PAGE)
        author = weibopost.page_author(body)
        queries = weibopost.post_queries(title, body, author)
        self.assertEqual(queries[0], "加速世界-黑雪姬 #二次元cos大赏#")
        self.assertIn("黑雪姬", queries[1])
        self.assertLessEqual(len(queries[0]), weibopost.MAX_QUERY)

    def test_post_queries_fall_back_to_title(self):
        questions = weibopost.post_queries("标题：某某 cosplay 正片", "太短", "")
        self.assertEqual(questions[0], "标题：某某 cosplay 正片")

    def test_large_pic_urls_stops_at_limit(self):
        urls = weibopost.large_pic_urls("a,b,c,d", limit=2)
        self.assertEqual(
            urls, ["https://wx1.sinaimg.cn/large/a.jpg", "https://wx1.sinaimg.cn/large/b.jpg"]
        )
        self.assertEqual(weibopost.large_pic_urls("", limit=3), [])

    def test_parse_post_search_extracts_author_and_pics(self):
        rows = weibopost.parse_post_search(CARD_HTML, limit=5)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["uid"], "2727362643")
        self.assertEqual(row["mblogid"], "Rr1ciyuH8")
        self.assertEqual(row["mid"], "5364394628221146")
        self.assertEqual(row["name"], "G44不会受伤")
        self.assertEqual(row["url"], "https://weibo.com/2727362643/Rr1ciyuH8")
        self.assertIn("a2904053gy1iiz1vymf9mj22o840a7wj", row["pic_ids"])


class WeiboPostApiTest(unittest.TestCase):
    """联网函数在不该跑的时候要立刻拒绝：别把「没登录」说成「搜不到」。"""

    def test_post_search_needs_login_cookie(self):
        with self.assertRaises(identity.IdentityError):
            identity.search_weibo_posts("加速世界 黑雪姬", "", 5.0, "", 3)

    def test_query_too_short_returns_empty(self):
        self.assertEqual(identity.search_weibo_posts("啊", "", 5.0, "SUB=x", 3), [])

    def test_timeline_needs_login_and_digits(self):
        with self.assertRaises(identity.IdentityError):
            identity.fetch_weibo_timeline("2727362643", "", 5.0, "", 1, 5)
        self.assertEqual(identity.fetch_weibo_timeline("abc", "", 5.0, "SUB=x", 1, 5), [])


if __name__ == "__main__":
    unittest.main()
