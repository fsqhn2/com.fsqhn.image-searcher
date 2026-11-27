"""按名字找号两条腿：微博昵称直搜 + 站外各平台主页检索。

起因是 `s-20261122-2219-c077`：图只落在 hotgirlpix / misskon 这类图集站上，
页面一个社媒账号都不挂；标题里明明写着 coser 的名字，可旧逻辑只拿它去 X 按
显示名核对（中文名在 X 上查无此号），整单只能收在「只找到别的网站的转发」。
这两组用例守住的是同一件事：**名字只是线索，改结论必须指纹对上**。
"""

from __future__ import annotations

import asyncio
import logging
import types
import unittest
from unittest import mock

from test_plugin_import import import_plugin


def _plugin(config_setup=None):
    """照 `test_searchpage.py` 的写法建一个不跑生命周期的插件实例。

    宿主的 `config` 是只读 property（内部读 `_plugin_config_instance`），本地桩里
    是可写类属性，所以两边都写一遍；只写 `plugin.config = …` 在服务器上会炸。
    """
    module = import_plugin()
    plugin = object.__new__(module.ImageSearcherPlugin)
    plugin._ctx = types.SimpleNamespace(logger=logging.getLogger("test"))
    config = mock.Mock()
    config.plugin.hash_threshold = 8
    config.plugin.verbose_log = False
    config.weibo.enabled = True
    config.weibo.min_fans = 200
    config.weibo.by_name = True
    config.name_hunt.enabled = True
    config.name_hunt.max_names = 2
    config.name_hunt.budget = 45.0
    if config_setup is not None:
        config_setup(config)
    plugin._plugin_config_instance = config
    try:
        plugin.config = config
    except AttributeError:
        pass
    plugin._pools = {"lens": object()}
    plugin._verify_pool = None
    return module, plugin, config


class TitleNameCandidatesTest(unittest.TestCase):
    """标题里的名字怎么抽：只认核过图的候选，冒号右边是作品名不是人。"""

    def _pages(self, module):
        return [
            module.Candidate(
                engine="lens",
                url="https://cosplay.pixibb.com/coser-w-swimsuit-21-photos/",
                title="小仓千代w: W泳装(21 photos) Sexy Cosplay | PixiBB",
                platform="cosplay.pixibb.com",
                distance=0,
                status="suspect",
            ),
            module.Candidate(
                engine="lens",
                url="https://misskon.com/coser-xiao-cang-qian-dai-w-w-yong-zhuang-21-photos/",
                title="Coser@小仓千代w: W泳装(21 photos) – MissKon.com",
                platform="misskon.com",
                distance=0,
                status="match",
            ),
            # SauceNAO 的插画候选：没量过距离，标题跟这张图没关系
            module.Candidate(
                engine="saucenao",
                url="https://deviantart.com/view/863603795",
                title="girlmachine",
                status="suspect",
            ),
        ]

    def test_colon_title_yields_the_name(self) -> None:
        module, plugin, _ = _plugin()
        names = plugin._title_name_candidates(self._pages(module))
        self.assertIn("小仓千代w", names)
        # 同一个名字的两种写法只留一个
        self.assertEqual(names.count("小仓千代w"), 1)

    def test_unverified_title_names_are_ignored(self) -> None:
        """没量过距离的候选不算数：19ab 那单抽出了 deviantart 的 girlmachine。"""
        module, plugin, _ = _plugin()
        names = plugin._title_name_candidates(self._pages(module))
        self.assertNotIn("girlmachine", names)
        # 站名与整句标题也不能当名字
        for junk in ("PixiBB", "MissKon.com"):
            self.assertNotIn(junk, names)


class TitleNameCjkHeadTest(unittest.TestCase):
    """中文标题里的作品名不能跟着人名一起进检索词。

    5832 那单的标题是「Cosplay] 小仓千代w 明日方舟W泳装| Xasiat」，旧逻辑切出来
    的是整串「小仓千代w 明日方舟W泳装」（带空白，被丢弃），名字一个都没进微博那
    条腿，反查白跑。
    """

    def test_work_name_after_the_name_is_cut(self) -> None:
        module, plugin, _ = _plugin()
        pages = [
            module.Candidate(
                engine="lens",
                url="https://xasiat.com/p/coser-w-yong-zhuang/",
                title="Cosplay] 小仓千代w 明日方舟W泳装| Xasiat",
                platform="xasiat.com",
                distance=0,
                status="match",
            )
        ]
        self.assertEqual(plugin._title_name_candidates(pages), ["小仓千代w"])

    def test_category_tag_leftover_is_not_a_name(self) -> None:
        """5832 那单从某条候选的 name 字段里抽出过一个「Cosplay]」。"""
        module, plugin, _ = _plugin()
        page = module.Candidate(
            engine="lens",
            url="https://xasiat.com/p/x/",
            title="小仓千代w - W泳装",
            name="Cosplay]",
            platform="xasiat.com",
            distance=0,
            status="match",
        )
        self.assertNotIn("Cosplay]", plugin._title_name_candidates([page]))


class TitleNamePixivArtworkTest(unittest.TestCase):
    """pixiv 作品页的标题是作品名，不是人名。

    9b22 那单：出处已经指纹对上 pixiv 作品《永恒的新生(1)》，作者是 `lol`；旧逻辑
    把作品名当成人名拿去各平台搜，搜回来一条不相干的 Facebook 视频页。作品标题既
    不该进检索词，也不该出现在「按名字找出这些主页」那一行里。
    """

    def _page(self, module, url, title):
        return module.Candidate(
            engine="pixiv",
            url=url,
            title=title,
            platform="pixiv",
            distance=0,
            status="match",
        )

    def test_pixiv_artwork_title_is_not_a_name(self) -> None:
        module, plugin, _ = _plugin()
        page = self._page(
            module,
            "https://www.pixiv.net/artworks/152342429",
            "永恒的新生(1) - lol",
        )
        self.assertEqual(plugin._title_name_candidates([page]), [])

    def test_old_pixiv_illust_link_is_not_a_name(self) -> None:
        """老式 `member_illust.php?mode=medium` 链接同样是作品页。"""
        module, plugin, _ = _plugin()
        page = self._page(
            module,
            "https://www.pixiv.net/member_illust.php?mode=medium&illust_id=152342429",
            "永恒的新生(1)",
        )
        self.assertEqual(plugin._title_name_candidates([page]), [])

    def test_blocked_artwork_title_is_refused(self) -> None:
        """别的站把同一个作品名抄进标题时也要挡住（调用方把作品标题传进 blocked）。"""
        module, plugin, _ = _plugin()
        page = module.Candidate(
            engine="lens",
            url="https://honkaihaven.com/gallery/1129766/x",
            title="永恒的新生 - honkaihaven",
            platform="honkaihaven.com",
            distance=0,
            status="match",
        )
        self.assertEqual(
            plugin._title_name_candidates([page], blocked={"永恒的新生(1)"}), []
        )

    def test_page_count_suffix_is_stripped(self) -> None:
        module, plugin, _ = _plugin()
        page = module.Candidate(
            engine="lens",
            url="https://example.com/gallery/1",
            title="永恒的新生(1) - 搬运站",
            platform="example.com",
            distance=0,
            status="match",
        )
        self.assertEqual(plugin._title_name_candidates([page]), ["永恒的新生"])

    def test_half_sentence_with_stray_bracket_is_not_a_name(self) -> None:
        """只剩右半边括号的是被切掉的半句：9b22 的候选里混进过一条攻略标题。"""
        module, plugin, _ = _plugin()
        page = module.Candidate(
            engine="lens",
            url="https://www.233leyuan.com/post-detail/2124918463764197376",
            title="明日方舟》卫界大战缪缪分队超详细攻略",
            platform="www.233leyuan.com",
            distance=8,
            status="match",
        )
        self.assertEqual(plugin._title_name_candidates([page]), [])


class TitleNameLlmTest(unittest.TestCase):
    """搬运站标题里的名字交给模型抽：规则切不准，但模型不能凭空造人。

    649c／9b22／411f 三单的错法各不相同——作品名当人名、半句攻略标题当名字、
    「漫画预告」这种分类词当名字——共同点都是拿固定分隔符硬切。资源站的标题是
    搬运工自己写的，切准是偶然。
    """

    def _page(self, module, title, url="https://www.costuan.com/14090.html"):
        return module.Candidate(
            engine="lens",
            url=url,
            title=title,
            platform=url.split("//")[1].split("/")[0],
            distance=0,
            status="match",
        )

    def _reply(self, plugin, text, calls=None):
        async def fake(capability, **kwargs):
            if calls is not None:
                calls.append((capability, kwargs))
            return {"success": True, "response": text}

        plugin._ctx.call_capability = fake

    def test_skips_when_landing_is_not_a_repost_site(self) -> None:
        module, plugin, _ = _plugin()
        calls: list = []

        async def boom(*args, **kwargs):  # noqa: ANN002, ANN003
            calls.append(args)
            raise AssertionError("正经帖子不该问模型")

        plugin._ctx.call_capability = boom
        page = self._page(
            module, "小仓千代w: W泳装(21 photos)", url="https://x.com/someone/status/1"
        )
        self.assertEqual(asyncio.run(plugin._llm_title_names([page])), [])
        self.assertEqual(calls, [])

    def test_name_must_appear_in_a_title(self) -> None:
        """模型说出来的名字在标题里找不到，当幻觉丢掉。"""
        module, plugin, _ = _plugin()
        self._reply(plugin, '{"names": ["纸悦Etsu_ko", "某个不存在的号"]}')
        page = self._page(module, "纸悦Etsu_ko 蔚蓝档案 鬼方佳代子 [34P-182MB]")
        self.assertEqual(asyncio.run(plugin._llm_title_names([page])), ["纸悦Etsu_ko"])

    def test_llm_name_feeds_the_shared_legs(self) -> None:
        """模型给的名字要能进「按名字找号」那两个名字列表。"""
        module, plugin, _ = _plugin()
        page = self._page(module, "纸悦Etsu_ko 蔚蓝档案 鬼方佳代子 [34P-182MB]")
        names = plugin._title_name_candidates([page], trusted=["纸悦Etsu_ko"])
        self.assertIn("纸悦Etsu_ko", names)

    def test_space_in_name_is_kept_for_trusted(self) -> None:
        """两段名字带空格（Mio Kuuko）也要留下，别被「带空白就是句子」挡掉。"""
        module, plugin, _ = _plugin()
        page = self._page(module, "《Mio Kuuko》漫展返图", url="https://www.costuan.com/1.html")
        self.assertIn("Mio Kuuko", plugin._title_name_candidates([page], trusted=["Mio Kuuko"]))

    def test_known_name_from_title_author_skips_the_second_call(self) -> None:
        """「标题作者」刚刚问过模型并拿到名字时，不再问第二遍。"""
        module, plugin, _ = _plugin()

        async def boom(*args, **kwargs):  # noqa: ANN002, ANN003
            raise AssertionError("不该问第二遍")

        plugin._ctx.call_capability = boom
        page = self._page(module, "纸悦Etsu_ko 蔚蓝档案 鬼方佳代子 [34P-182MB]")
        self.assertEqual(
            asyncio.run(plugin._llm_title_names([page], known="纸悦Etsu_ko")),
            ["纸悦Etsu_ko"],
        )

    def test_broken_reply_falls_back_to_rules(self) -> None:
        module, plugin, _ = _plugin()

        async def fake(capability, **kwargs):  # noqa: ANN001, ANN003
            raise RuntimeError("llm down")

        plugin._ctx.call_capability = fake
        page = self._page(module, "纸悦Etsu_ko 蔚蓝档案 鬼方佳代子 [34P-182MB]")
        self.assertEqual(asyncio.run(plugin._llm_title_names([page])), [])
        self.assertEqual(plugin._title_name_candidates([page]), ["纸悦Etsu_ko"])


class TitleNameQuotedTest(unittest.TestCase):
    """新闻／内容号的标题把名字挂在书名号里，取名字别取前半句。

    411f 那单的标题是「穿衣v.s脫衣哪個好？貧乳細腰《Mio Kuuko》白皙纖瘦體態別有
    一番…」，「取开头那一截」的结果是「穿衣v.s脫衣哪個好？貧乳細腰《Mio」——拿这半句
    去搜一条都对不上，而真名就在书名号里。
    """

    def _page(self, module, title, **extra):
        return module.Candidate(
            engine="lens",
            url="https://clickme.net/62904",
            title=title,
            platform="clickme.net",
            distance=0,
            status="match",
            **extra,
        )

    def test_quoted_name_wins(self) -> None:
        module, plugin, _ = _plugin()
        page = self._page(
            module,
            "穿衣v.s脫衣哪個好？貧乳細腰《Mio Kuuko》白皙纖瘦體態別有一番 ...",
        )
        self.assertEqual(plugin._title_name_candidates([page]), ["Mio Kuuko"])

    def test_sentence_head_without_quotes_is_dropped(self) -> None:
        """没有书名号的整句标题，别把前半句当名字拿去搜。"""
        module, plugin, _ = _plugin()
        page = self._page(module, "穿衣v.s脫衣哪個好？貧乳細腰的白皙女孩")
        self.assertEqual(plugin._title_name_candidates([page]), [])


class SignatureHuntScopeTest(unittest.TestCase):
    """标题署名只从「真的比过图」的候选里抽。

    411f 那单：命中的是 clickme 那篇文章（d=0），署名却从四条**从没下载比对过**的
    danbooru 插画页里抽，抽出 nemo 一路查到 `@NemoNevermore_` 挂到首屏。没量过距离
    的页面根本没确认过「这页上有这张图」，它的标题说的是另一张图。
    """

    def _pages(self, module):
        return [
            module.Candidate(
                engine="lens",
                url="https://clickme.net/62904",
                title="穿衣v.s脫衣哪個好？貧乳細腰《Mio Kuuko》白皙纖瘦體態別有一番 ...",
                platform="clickme.net",
                source="ClickMe",
                distance=0,
                status="match",
            ),
            module.Candidate(
                engine="lens",
                url="https://danbooru.donmai.us/posts/7480594",
                title="nozomi and hikari (blue archive) drawn by nemo_(leafnight ..",
                platform="danbooru.donmai.us",
                source="Danbooru",
                status="unverified",
            ),
            module.Candidate(
                engine="saucenao",
                url="https://www.pixiv.net/member_illust.php?mode=medium&illust_id=30930214",
                title="cosplay by nemo",
                platform="www.pixiv.net",
                source="www.pixiv.net",
                status="suspect",
            ),
        ]

    def test_uncompared_pages_do_not_supply_signatures(self) -> None:
        module, plugin, _ = _plugin()
        plugin._search_x_handles = mock.AsyncMock(return_value=[])
        plugin._profile = mock.AsyncMock(return_value=None)
        found = asyncio.run(plugin._hunt_signature_author(self._pages(module), b"img"))
        self.assertIsNone(found)
        plugin._search_x_handles.assert_not_called()

    def test_falls_back_when_nothing_was_compared(self) -> None:
        """整单一条都没比过图时，仍然按老办法从搬运页标题里找署名。"""
        module, plugin, _ = _plugin()
        pages = [
            module.Candidate(
                engine="lens",
                url="https://www.reddit.com/r/x/comments/1/",
                title="Vivian cosplay by KURU : r/Vivian_Mains",
                platform="reddit.com",
                source="reddit.com",
                status="suspect",
            )
        ]
        plugin._search_x_handles = mock.AsyncMock(return_value=[])
        plugin._profile = mock.AsyncMock(return_value=None)
        asyncio.run(plugin._hunt_signature_author(pages, b"img"))
        plugin._search_x_handles.assert_awaited()
        self.assertEqual(plugin._search_x_handles.await_args.args[0], "KURU")


class _FakePool:
    def acquire(self):
        return "k" * 64

    def mark_success(self, key) -> None:
        pass

    def mark_failure(self, key, cooldown=None) -> None:
        pass


class ProfileSearchFallbackTest(unittest.TestCase):
    """按名字搜主页必须带着 fallback 去搜。

    411f 的「Mio Kuuko」加上 cosplay 之后九条全是 pinterest 图板，一条主页都没有；
    裸名字才搜到 x.com/Kuukow002（150 万粉，本人）与 instagram.com/kuuko_w。
    调用方以前写死 ``fallback=False``，这一步等于白搜。
    """

    def test_fallback_is_enabled(self) -> None:
        module, plugin, _ = _plugin()
        plugin._pools = {"lens": _FakePool()}
        seen: dict = {}

        def fake(name, key, proxy="", **kwargs):
            seen.update(kwargs)
            return []

        with mock.patch.object(module.identity, "search_profiles", fake):
            asyncio.run(plugin._search_profiles_with_rotation("Mio Kuuko"))
        self.assertIs(seen.get("fallback"), True)



class LeadPagesHandlePriorityTest(unittest.TestCase):
    """标题里写着 (@handle) 的页面必须先占名额（2c76 的 cosplayrule34 排在第六）。"""

    def _page(self, module, url, title):
        page = module.Candidate(
            engine="lens", url=url, title=title, platform=url.split("/")[2], distance=0
        )
        page.status = "match"
        return page

    def test_written_handle_page_wins_a_slot(self) -> None:
        module, plugin, _ = _plugin()
        pages = [
            self._page(module, f"https://site{i}.example/p/x/", f"coser {i}") for i in range(5)
        ]
        pages.append(
            self._page(
                module,
                "https://cosplayrule34.com/model/kokura-chiyo/",
                "Model Kokura Chiyo (@yummychiyo) in cosplay W from Ark",
            )
        )
        # 站方给的 name 是洗过的模特名，号只在 title 里（2c76 的真实形态）。
        pages[-1].name = "Model Kokura Chiyo in cosplay W from Arknights ..."
        picked = plugin._lead_pages(pages, pages[0])
        self.assertEqual(len(picked), 5)
        self.assertEqual(picked[0].url, "https://cosplayrule34.com/model/kokura-chiyo/")

    def test_page_with_handle_in_title_is_still_hunted(self) -> None:
        """extract 已经把标题里的号填进 handle 时，这条也要追（2c76 的 rule34）。"""
        module, plugin, _ = _plugin()
        page = self._page(
            module,
            "https://cosplayrule34.com/post/94599",
            "Model Kokura Chiyo in cosplay W from Arknights ...",
        )
        page.name = "Model Kokura Chiyo in cosplay W from Arknights ..."
        page.title = "Model Kokura Chiyo (@yummychiyo) in cosplay W from Arknights ..."
        page.handle = "@yummychiyo"
        picked = plugin._lead_pages([page], page)
        self.assertEqual([item.url for item in picked], [page.url])

    def test_url_only_account_page_is_not_hunted(self) -> None:
        """URL 里带出来的账号页是账号页，不是写着作者的转载页。"""
        module, plugin, _ = _plugin()
        page = self._page(module, "https://x2.example/kuukow002/", "KuukoW")
        page.handle = "@kuukow002"
        self.assertEqual(plugin._lead_pages([page], page), [])


class TitleDirectHandleTest(unittest.TestCase):
    """页面标题里直接写着的 X 号先核：2c76 的 @yummychiyo 就写在标题里。

    旧逻辑从这条标题里按分隔符切，只切出 `CoserwW21photos` 这种垃圾变体，真号
    连试都没试；而括号里的号是站方抄的原作者账号，比猜出来的变体硬得多。
    """

    def _page(self, module):
        return module.Candidate(
            engine="lens",
            url="https://cosplayrule34.com/model/kokura-chiyo/",
            title="Model Kokura Chiyo (@yummychiyo) in cosplay W from Ark",
            platform="cosplayrule34.com",
            distance=0,
            status="match",
        )

    def _lead(self):
        return (
            "Model Kokura Chiyo",
            ["yummychiyo"],
            "title",
            "Kokura Chiyo",
            "https://cosplayrule34.com/model/kokura-chiyo/",
        )

    def test_direct_handle_lead_comes_first(self) -> None:
        module, plugin, _ = _plugin(lambda config: setattr(config.title_author, "enabled", False))
        page = self._page(module)
        # 站方给的 name 里没有号，号只在 title 里（2c76 的真实形态）。
        page.name = "Model Kokura Chiyo in cosplay W from Arknights ..."
        leads = asyncio.run(plugin._title_author_leads([page]))
        self.assertTrue(leads)
        self.assertEqual(leads[0][2], "title")
        self.assertEqual(leads[0][1][0], "yummychiyo")
        self.assertEqual(leads[0][0], "Model Kokura Chiyo")

    def _scan(self, module, plugin, display, hit, search_hit=None):
        scanned: list[str] = []

        async def profile(self, handle):
            return {"name": display}

        async def media(self, handle, upload):
            scanned.append(handle)
            return hit

        async def search(self, handle, upload):
            return search_hit

        with (
            mock.patch.object(module.ImageSearcherPlugin, "_profile", profile),
            mock.patch.object(module.ImageSearcherPlugin, "_media_lookup", media),
            mock.patch.object(module.ImageSearcherPlugin, "_search_media_lookup", search),
        ):
            result = asyncio.run(plugin._scan_author_lead(self._lead(), b"x"))
        return result, scanned

    def test_display_name_mismatch_still_scans_the_post(self) -> None:
        """页面写的是账号、不是名字：显示名对不上也要翻一次她的带图帖。"""
        module, plugin, _ = _plugin()
        hit = {"url": "https://x.com/yummychiyo/status/9", "distance": 1}
        result, scanned = self._scan(module, plugin, "こくらちよ", hit)
        self.assertEqual(scanned, ["yummychiyo"])
        self.assertEqual(result["status"], "hit")
        self.assertEqual(result["handle"], "yummychiyo")

    def test_no_post_keeps_it_as_a_lead(self) -> None:
        """帖子里没这张就只当线索（seen），不拿页面写着的那句话改结论。"""
        module, plugin, _ = _plugin()
        result, _ = self._scan(module, plugin, "こくらちよ", None)
        self.assertEqual(result["status"], "seen")
        self.assertEqual(result["handle"], "yummychiyo")
        self.assertTrue(result["explicit"])

    def test_guessed_handle_still_needs_the_name(self) -> None:
        """靠名字猜出来的号不打这个折扣：显示名对不上仍然直接丢。"""
        module, plugin, _ = _plugin()
        guessed = ("Model Kokura Chiyo", ["yummychiyo"], "rule", "Kokura Chiyo", "")

        async def profile(self, handle):
            return {"name": "こくらちよ"}

        async def media(self, handle, upload):  # pragma: no cover - 不该被调用
            raise AssertionError("名字对不上就不该去翻帖")

        with (
            mock.patch.object(module.ImageSearcherPlugin, "_profile", profile),
            mock.patch.object(module.ImageSearcherPlugin, "_media_lookup", media),
        ):
            result = asyncio.run(plugin._scan_author_lead(guessed, b"x"))
        self.assertEqual(result["status"], "none")
        self.assertEqual(result["scanned"], [])


class WeiboSameNameRowsTest(unittest.TestCase):
    """同名号全翻：以前只看 `rows[0]`，正主排在第二个就永远翻不到。

    微博搜索结果按相关度排，第一个常是「只包含关键词」的重名号（c077 搜出来的
    就是「小仓千代空水」这类）。判据仍然只有图：翻带图帖对上了才算她。
    """

    def _pick(self, module, plugin, rows, name="小仓千代w", by_title=True):
        async def with_post(self, row, target_hash=None):
            if str(row.get("uid") or "") == "2":
                return {
                    **row,
                    "post": "https://weibo.com/status/2",
                    "distance": 3,
                }
            return dict(row)

        with (
            mock.patch.object(module.ImageSearcherPlugin, "_vlog", lambda *a, **k: None),
            mock.patch.object(module.ImageSearcherPlugin, "_with_weibo_post", with_post),
        ):
            return asyncio.run(
                plugin._weibo_pick_same_name(
                    name,
                    rows,
                    floor=200,
                    by_title=by_title,
                    source="候选标题里的名字",
                    target_hash=4242,
                )
            )

    def _row(self, uid, name, followers):
        return {
            "uid": uid,
            "name": name,
            "followers": followers,
            "url": f"https://weibo.com/u/{uid}",
        }

    def test_second_row_wins_when_its_post_has_the_image(self) -> None:
        module, plugin, _ = _plugin()
        rows = [
            self._row("1", "小仓千代空水", 900000),
            self._row("2", "小仓千代w", 50),
        ]
        found = self._pick(module, plugin, rows)
        self.assertEqual(found["status"], "hit")
        self.assertEqual(found["uid"], "2")
        self.assertEqual(found["post"], "https://weibo.com/status/2")

    def test_all_three_rows_are_probed(self) -> None:
        """前三个都翻：第二个没有这张图时，第三个还有机会。"""
        module, plugin, _ = _plugin()
        rows = [
            self._row("1", "小仓千代w", 40),
            self._row("2", "小仓千代w", 50),
            self._row("3", "小仓千代w", 60),
        ]

        async def with_post(self, row, target_hash=None):
            if str(row.get("uid") or "") == "3":
                return {**row, "post": "https://weibo.com/status/3", "distance": 1}
            return dict(row)

        with (
            mock.patch.object(module.ImageSearcherPlugin, "_vlog", lambda *a, **k: None),
            mock.patch.object(module.ImageSearcherPlugin, "_with_weibo_post", with_post),
        ):
            found = asyncio.run(
                plugin._weibo_pick_same_name(
                    "小仓千代w",
                    rows,
                    floor=200,
                    by_title=True,
                    source="候选标题里的名字",
                    target_hash=4242,
                )
            )
        self.assertEqual(found["status"], "hit")
        self.assertEqual(found["uid"], "3")

    def test_fourth_row_is_out_of_budget(self) -> None:
        """第四个不再翻：一次翻一个号十几秒，翻到底会把整单拖成几分钟。"""
        module, plugin, _ = _plugin()
        rows = [self._row(str(index), "小仓千代w", 30 + index) for index in range(1, 5)]

        async def with_post(self, row, target_hash=None):
            if str(row.get("uid") or "") == "4":
                return {**row, "post": "https://weibo.com/status/4", "distance": 1}
            return dict(row)

        with (
            mock.patch.object(module.ImageSearcherPlugin, "_vlog", lambda *a, **k: None),
            mock.patch.object(module.ImageSearcherPlugin, "_with_weibo_post", with_post),
        ):
            found = asyncio.run(
                plugin._weibo_pick_same_name(
                    "小仓千代w",
                    rows,
                    floor=200,
                    by_title=True,
                    source="候选标题里的名字",
                    target_hash=4242,
                )
            )
        self.assertEqual(found["status"], "none")
        self.assertEqual(found["max_fans"], 33)

    def test_nickname_mismatch_still_records_the_biggest(self) -> None:
        """昵称只是包含关系的重名号：不认，但仍记下它是搜到过最多粉的那个。"""
        module, plugin, _ = _plugin()
        rows = [self._row("1", "小仓千代空水", 900000)]
        found = self._pick(module, plugin, rows)
        self.assertEqual(found["status"], "none")
        self.assertEqual(found["max_fans"], 900000)
        self.assertEqual(found["top_name"], "小仓千代空水")


class WeiboTitledNameTest(unittest.TestCase):
    """标题里只有中文名、页面没写「微博」时，这个名字也要去微博搜一次。"""

    def _pages(self, module):
        return [
            module.Candidate(
                engine="lens",
                url="https://hotgirlpix.com/p/coser-xiao-cang-qian-dai-w",
                title="小仓千代w - W 泳装 - hotgirlpix",
                platform="hotgirlpix.com",
                distance=2,
                status="suspect",
            )
        ]

    def _run(self, plugin, module, *, rows, best=None, config=None):
        async def search(self, name):
            search.calls.append(name)
            return rows

        async def with_post(self, row, target_hash=None):
            return dict(row)

        search.calls = []
        with (
            mock.patch.object(module.ImageSearcherPlugin, "_vlog", lambda *a, **k: None),
            mock.patch.object(module.ImageSearcherPlugin, "_search_weibo_users", search),
            mock.patch.object(module.ImageSearcherPlugin, "_with_weibo_post", with_post),
        ):
            found = asyncio.run(
                plugin._hunt_weibo(
                    self._pages(module), {}, {}, target_hash=4242, best=best
                )
            )
        return found, search.calls

    def test_title_name_triggers_weibo_search(self) -> None:
        """c077 那种局面：页面不挂账号，标题里的中文名就是唯一入口。"""
        module, plugin, _ = _plugin()
        rows = [
            {
                "uid": "6791234567",
                "name": "小仓千代w",
                "followers": 52000,
                "url": "https://weibo.com/u/6791234567",
            }
        ]
        found, calls = self._run(plugin, module, rows=rows)
        self.assertEqual(calls, ["小仓千代w"])
        self.assertIsNotNone(found)
        self.assertEqual(found["status"], "hit")
        # 来源要写清是「标题里的名字」，跟水印 / Grok 读数分开
        self.assertEqual(found["source"], "候选标题里的名字")
        self.assertEqual(found["followers"], 52000)

    def test_nickname_mismatch_keeps_only_a_clue(self) -> None:
        """搜到的人昵称对不上（或一个都没有）→ 只写一句，不改结论。"""
        module, plugin, _ = _plugin()
        found, calls = self._run(plugin, module, rows=[])
        self.assertEqual(calls, ["小仓千代w"])
        self.assertIsNotNone(found)
        self.assertEqual(found["status"], "none")
        self.assertNotIn("post", found)
        self.assertEqual(found["query"], "小仓千代w")

    def test_high_fans_lookalike_nickname_is_not_accepted(self) -> None:
        """粉丝够多但昵称只是包含关系的重名号，不能当「同名号」写进结果。

        实测搜「小仓千代w」还会带出「小仓千代空水」这类号；从标题读出来的名字
        必须昵称也对得上才认，否则粉丝数一高就会报错人（c077）。
        """
        module, plugin, _ = _plugin()
        rows = [
            {
                "uid": "2810197180",
                "name": "小仓千代空水",
                "followers": 900000,
                "url": "https://weibo.com/u/2810197180",
            }
        ]
        found, calls = self._run(plugin, module, rows=rows)
        self.assertEqual(calls, ["小仓千代w"])
        self.assertIsNotNone(found)
        self.assertEqual(found["status"], "none")
        self.assertNotIn("post", found)
        self.assertEqual(found["top_name"], "小仓千代空水")

    def test_low_fans_never_becomes_conclusion(self) -> None:
        """粉丝低于门槛且带图帖里没这张 → 仍是同名路人号。"""
        module, plugin, _ = _plugin()
        rows = [
            {
                "uid": "6000000001",
                "name": "小仓千代w",
                "followers": 98,
                "url": "https://weibo.com/u/6000000001",
            }
        ]
        found, _calls = self._run(plugin, module, rows=rows)
        self.assertEqual(found["status"], "none")
        self.assertEqual(found["max_fans"], 98)
        self.assertNotIn("post", found)

    def test_concluded_account_skips_the_leg(self) -> None:
        """结论已经是指纹对上的账号帖时不搜：人已经知道了。"""
        module, plugin, _ = _plugin()
        best = module.Candidate(
            engine="lens",
            url="https://x.com/someone/status/1",
            platform="X (Twitter)",
            handle="@someone",
            distance=1,
            status="match",
        )
        found, calls = self._run(plugin, module, rows=[], best=best)
        self.assertEqual(calls, [])
        self.assertIsNone(found)

    def test_switch_off_keeps_old_behaviour(self) -> None:
        """`weibo.by_name` 关掉就回到老样子：标题里的名字不去搜。"""
        module, plugin, _ = _plugin(lambda config: setattr(config.weibo, "by_name", False))
        found, calls = self._run(plugin, module, rows=[])
        self.assertEqual(calls, [])
        self.assertIsNone(found)

    def test_site_name_watermark_is_not_searched(self) -> None:
        """水印写着站名（PixiBB.COM）时不许拿它去微博搜人。

        19ab 那单水印读到 `PixiBB` / `MissKON.com` 都被判定成站名了，可微博那条腿
        又从水印兜底里把「PixiBB」捞出来搜了一次，纯属白跑。
        """
        module, plugin, _ = _plugin()
        page = module.Candidate(
            engine="lens",
            url="https://cosplay.pixibb.com/coser-w-swimsuit-21-photos/",
            title="W Swimsuit 21 photos Sexy Cosplay",
            platform="cosplay.pixibb.com",
            distance=0,
            status="suspect",
        )

        async def search(self, name):
            search.calls.append(name)
            return []

        async def with_post(self, row, target_hash=None):
            return dict(row)

        search.calls = []
        with (
            mock.patch.object(module.ImageSearcherPlugin, "_vlog", lambda *a, **k: None),
            mock.patch.object(module.ImageSearcherPlugin, "_search_weibo_users", search),
            mock.patch.object(module.ImageSearcherPlugin, "_with_weibo_post", with_post),
        ):
            found = asyncio.run(
                plugin._hunt_weibo(
                    [page],
                    {"watermark": "PixiBB.COM"},
                    {},
                    target_hash=1,
                    best=None,
                )
            )
        self.assertEqual(search.calls, [])
        self.assertIsNone(found)


class NameHuntGuardTest(unittest.TestCase):
    """`_should_search_name`：什么时候值得花一次检索额度。"""

    def test_runs_with_a_real_name_and_no_conclusion(self) -> None:
        module = import_plugin()
        self.assertTrue(
            module.ImageSearcherPlugin._should_search_name(
                enabled=True,
                has_upload=True,
                names=["小仓千代w"],
                best_status="",
            )
        )

    def test_skips_when_conclusion_is_a_verified_post(self) -> None:
        module = import_plugin()
        self.assertFalse(
            module.ImageSearcherPlugin._should_search_name(
                enabled=True,
                has_upload=True,
                names=["小仓千代w"],
                best_status="match",
                best_handle="@chiyoko_w",
            )
        )

    def test_skips_site_name_and_short_ascii(self) -> None:
        module = import_plugin()
        decide = module.ImageSearcherPlugin._should_search_name
        # 图集站的分类名 / 站名不是人
        self.assertFalse(
            decide(
                enabled=True,
                has_upload=True,
                names=["pixibb"],
                best_status="",
                urls=["https://cosplay.pixibb.com/x"],
            )
        )
        # 纯 ASCII 短词不值得花额度
        self.assertFalse(
            decide(enabled=True, has_upload=True, names=["ran"], best_status="")
        )

    def test_skips_site_boilerplate_titles(self) -> None:
        """求助站的页面标题不是人：whos.tv 那页叫「求出处或资源」（826c）。"""
        module = import_plugin()
        decide = module.ImageSearcherPlugin._should_search_name
        for name in ("求出处或资源", "求出处", "求资源", "Help post", "untitled"):
            self.assertFalse(
                decide(enabled=True, has_upload=True, names=[name], best_status=""),
                name,
            )
        # 真名里带「求助」不算模板：整串匹配才挡
        self.assertTrue(
            decide(enabled=True, has_upload=True, names=["求助的小仓"], best_status="")
        )

    def test_skips_when_switch_off_or_already_searched(self) -> None:
        module = import_plugin()
        decide = module.ImageSearcherPlugin._should_search_name
        self.assertFalse(
            decide(enabled=False, has_upload=True, names=["小仓千代w"], best_status="")
        )
        self.assertFalse(
            decide(
                enabled=True,
                has_upload=True,
                names=["小仓千代w"],
                best_status="",
                searched=["小仓千代w"],
            )
        )

    def test_content_site_match_still_searches(self) -> None:
        """结论停在内容号文章（匹配但没账号）时照样要按名字查。

        411f 落在 clickme 那种资讯站上：指纹对得上只说明这页有这张图，页面本身不是
        作者主页，不查就只剩一篇介绍别人的文章。
        """
        module = import_plugin()
        self.assertTrue(
            module.ImageSearcherPlugin._should_search_name(
                enabled=True,
                has_upload=True,
                names=["Mio Kuuko"],
                best_status="match",
                best_handle="",
                urls=["https://clickme.net/62904"],
            )
        )


class TitleAuthorSearchedTest(unittest.TestCase):
    """标题作者那条线「查过没找到」不能当成「已经搜过」。"""

    def test_missing_name_is_not_marked_searched(self) -> None:
        module = import_plugin()
        # c077：X 上按显示名没找到时，author 也会被带回来，但这不是「搜过」——
        # 拿它去挡「按名字找号」，正好会把最需要换平台再试的情况挡掉。
        self.assertEqual(
            module.ImageSearcherPlugin._title_author_searched(
                {"status": "none", "author": "小仓千代w"}
            ),
            "",
        )
        self.assertEqual(module.ImageSearcherPlugin._title_author_searched(None), "")

    def test_hit_or_named_marks_the_name(self) -> None:
        module = import_plugin()
        decide = module.ImageSearcherPlugin._title_author_searched
        self.assertEqual(decide({"status": "named", "author": "染三ran3"}), "染三ran3")
        # 比较用小写，免得大小写不同被当成两个名字
        self.assertEqual(decide({"status": "hit", "author": "Ran3"}), "ran3")


class HuntNameProfilesTest(unittest.TestCase):
    """`_hunt_name_profiles`：只认 X / 微博 的指纹，其它平台一律线索。"""

    def _run(self, module, plugin, rows, *, profile=None, weibo=None, check=None, names=None):
        async def search(self, name):
            search.calls.append(name)
            return rows(name) if callable(rows) else rows

        async def read_profile(self, handle):
            return profile

        async def read_weibo(self, uid):
            return weibo

        async def probe(self, handle, upload, holder):
            if check is not None:
                holder.update(check)

        async def with_post(self, row, target_hash=None):
            return dict(row, post="https://weibo.com/6791234567/Pd1abcd", distance=2)

        search.calls = []
        with (
            mock.patch.object(module.ImageSearcherPlugin, "_vlog", lambda *a, **k: None),
            mock.patch.object(module.ImageSearcherPlugin, "_search_profiles_with_rotation", search),
            mock.patch.object(module.ImageSearcherPlugin, "_profile", read_profile),
            mock.patch.object(module.ImageSearcherPlugin, "_weibo_profile", read_weibo),
            mock.patch.object(module.ImageSearcherPlugin, "_check_watermark_post", probe),
            mock.patch.object(module.ImageSearcherPlugin, "_with_weibo_post", with_post),
        ):
            info = asyncio.run(
                plugin._hunt_name_profiles(
                    names if names is not None else ["小仓千代w"], b"img", 4242
                )
            )
        return info, search.calls

    def test_x_handle_with_matching_name_and_post_is_hit(self) -> None:
        """X 显示名对得上、她自己的带图帖里也有这张 → 命中。"""
        module, plugin, _ = _plugin()
        rows = [{"platform": "X", "handle": "chiyoko_w", "url": "https://x.com/chiyoko_w"}]
        info, _calls = self._run(
            module,
            plugin,
            rows,
            profile={"name": "小仓千代w", "screen_name": "chiyoko_w", "followers": 9000},
            check={"watermark_post": "https://x.com/chiyoko_w/status/9", "watermark_distance": 3},
        )
        self.assertIsNotNone(info)
        self.assertEqual(info["status"], "hit")
        self.assertEqual(info["platform"], "X")
        self.assertEqual(info["handle"], "chiyoko_w")
        self.assertEqual(info["distance"], 3)

    def test_weibo_uid_with_matching_post_is_hit(self) -> None:
        """微博 UID → 昵称对得上 + 粉丝过门槛，翻帖核到这张才算。"""
        module, plugin, _ = _plugin()
        rows = [
            {
                "platform": "微博",
                "handle": "6791234567",
                "url": "https://weibo.com/u/6791234567",
            }
        ]
        info, _calls = self._run(
            module,
            plugin,
            rows,
            weibo={"name": "小仓千代w", "followers": 52000, "url": "https://weibo.com/u/6791234567"},
        )
        self.assertEqual(info["status"], "hit")
        self.assertEqual(info["platform"], "微博")
        # 主页列表里带上昵称与粉丝数，展示时要写出来
        self.assertEqual(info["profiles"][0]["followers"], 52000)

    def test_weibo_below_floor_never_probes_posts(self) -> None:
        """粉丝不够的微博号只留在主页列表里，不往下核图。"""
        module, plugin, _ = _plugin()
        rows = [{"platform": "微博", "handle": "6000000001"}]
        info, _calls = self._run(
            module,
            plugin,
            rows,
            weibo={"name": "小仓千代w", "followers": 98, "url": "https://weibo.com/u/6000000001"},
        )
        self.assertEqual(info["status"], "seen")
        self.assertNotIn("post", info)

    def test_only_ig_found_keeps_conclusion(self) -> None:
        """只捞到 IG / TikTok 主页 → 只当线索，不改结论。"""
        module, plugin, _ = _plugin()
        rows = [
            {"platform": "Instagram", "handle": "chiyoko.w", "url": "https://instagram.com/chiyoko.w"},
            {"platform": "TikTok", "handle": "chiyoko.w", "url": "https://tiktok.com/@chiyoko.w"},
        ]
        info, _calls = self._run(module, plugin, rows)
        self.assertEqual(info["status"], "seen")
        self.assertEqual(len(info["profiles"]), 2)
        # 没有 post 就意味着调用点不会换结论
        self.assertNotIn("post", info)

    def test_x_name_matched_without_post_is_reported(self) -> None:
        """显示名对得上但帖子里没这张：号要交出去，别只留在 profiles 里。

        411f 的 @Kuukow002（150.8 万粉）就是这样：显示名折平后是「miokuuko」，认得出
        是她本人，只是她最近的图里没有这张。这个号还得拿去查付费页、写进线索行。
        """
        module, plugin, _ = _plugin()
        rows = [{"platform": "X", "handle": "Kuukow002", "url": "https://x.com/Kuukow002"}]
        info, _calls = self._run(
            module,
            plugin,
            rows,
            profile={"name": "𝑴𝒊𝒐 𝑲𝒖𝒖𝒌𝒐🍀", "screen_name": "Kuukow002", "followers": 1508078},
            check={"watermark_post": ""},
            names=["Mio Kuuko"],
        )
        self.assertEqual(info["status"], "seen")
        self.assertEqual(info["x_handle"], "Kuukow002")
        self.assertEqual(info["x_followers"], 1508078)

    def test_each_name_searched_once_and_capped(self) -> None:
        """一个名字只搜一次，最多两个名字。"""
        module, plugin, _ = _plugin(lambda config: setattr(config.name_hunt, "max_names", 2))
        rows = [{"platform": "Instagram", "handle": "x", "url": "https://instagram.com/x"}]
        _info, calls = self._run(
            module, plugin, rows, names=["阿甲", "阿乙", "阿丙"]
        )
        self.assertEqual(calls, ["阿甲", "阿乙"])


class NameHuntPromoteTest(unittest.TestCase):
    """命中之后换结论的两种平台。"""

    def test_x_hit_becomes_conclusion(self) -> None:
        module, plugin, _ = _plugin()
        best = None
        promoted = plugin._promote_name_hit(
            best,
            {
                "name": "小仓千代w",
                "platform": "X",
                "handle": "chiyoko_w",
                "account": "小仓千代w",
                "post": "https://x.com/chiyoko_w/status/9",
                "distance": 3,
            },
        )
        self.assertIsNotNone(promoted)
        self.assertEqual(promoted.status, "match")
        self.assertEqual(promoted.handle, "@chiyoko_w")
        self.assertEqual(promoted.distance, 3)

    def test_weibo_hit_becomes_conclusion(self) -> None:
        module, plugin, _ = _plugin()
        promoted = plugin._promote_name_hit(
            None,
            {
                "name": "小仓千代w",
                "platform": "微博",
                "handle": "6791234567",
                "account": "小仓千代w",
                "post": "https://weibo.com/6791234567/Pd1abcd",
                "distance": 2,
            },
        )
        self.assertIsNotNone(promoted)
        self.assertEqual(promoted.platform, "微博")
        self.assertEqual(promoted.url, "https://weibo.com/6791234567/Pd1abcd")
        self.assertEqual(promoted.distance, 2)


class NameHuntSummaryTest(unittest.TestCase):
    """落盘与回「更多」：`name_hunt` 要跟着摘要一起存回来。"""

    def test_summary_roundtrip(self) -> None:
        module = import_plugin()
        outcome = module.SearchOutcome(
            status="ok",
            name_hunt={
                "name": "小仓千代w",
                "status": "seen",
                "profiles": [{"platform": "Instagram", "handle": "chiyoko.w"}],
            },
        )
        restored = module.SearchOutcome.from_summary(outcome.to_summary())
        self.assertEqual(restored.name_hunt["name"], "小仓千代w")
        self.assertEqual(restored.name_hunt["status"], "seen")


class WatermarkNameCandidatesTest(unittest.TestCase):
    """水印名要进「按名字找号」的名单，不然 X 那条腿永远不知道她叫什么。"""

    def _plugin(self):
        module = import_plugin()
        return module, object.__new__(module.ImageSearcherPlugin)

    def test_watermark_name_goes_first(self) -> None:
        _module, plugin = self._plugin()
        names = plugin._with_watermark_names(["Tsuki_月隐"], {"watermark": "@咲鱼咔咔"})
        self.assertEqual(names[0], "咲鱼咔咔")
        self.assertIn("Tsuki_月隐", names)

    def test_display_name_from_profile_is_used(self) -> None:
        _module, plugin = self._plugin()
        names = plugin._with_watermark_names([], {"watermark_name": "咲鱼咔咔"})
        self.assertEqual(names, ["咲鱼咔咔"])

    def test_tieba_board_and_site_names_are_dropped(self) -> None:
        """贴吧板块名不是人：b55a 的「cosplay吧」拿去搜只会搜回一个被封的老号。"""
        _module, plugin = self._plugin()
        self.assertEqual(plugin._with_watermark_names([], {"watermark": "@cosplay吧"}), [])
        self.assertEqual(plugin._with_watermark_names([], {"watermark": "@cosplay"}), [])

    def test_no_duplicate_and_empty_input(self) -> None:
        _module, plugin = self._plugin()
        self.assertEqual(plugin._with_watermark_names([], None), [])
        names = plugin._with_watermark_names(["咲鱼咔咔"], {"watermark": "@咲鱼咔咔"})
        self.assertEqual(names, ["咲鱼咔咔"])


class NameHuntDisplayTest(unittest.TestCase):
    """展示：只找到主页时列出来，并注明名字是从标题／模型读出来的。"""

    def _make_outcome(self, module, **kwargs):
        return module.SearchOutcome(status="ok", **kwargs)

    def test_seen_profiles_are_listed_with_fans(self) -> None:
        module = import_plugin()
        outcome = self._make_outcome(
            module,
            name_hunt={
                "name": "小仓千代w",
                "status": "seen",
                "profiles": [
                    {
                        "platform": "微博",
                        "handle": "6791234567",
                        "name": "小仓千代w",
                        "followers": 52000,
                        "url": "https://weibo.com/u/6791234567",
                    },
                    {"platform": "X", "handle": "chiyoko_w"},
                ],
            },
        )
        lines = module.formatting._name_hunt_lines(outcome)
        text = "\n".join(lines)
        self.assertIn("小仓千代w", text)
        self.assertIn("微博", text)
        self.assertIn("5.2 万", text)
        self.assertIn("@chiyoko_w", text)
        self.assertIn("只是线索", text)

    def test_model_source_is_labelled(self) -> None:
        """走模型读出来的名字要注明，规则的不用。"""
        module = import_plugin()
        outcome = self._make_outcome(
            module,
            name_hunt={
                "name": "小仓千代w",
                "status": "seen",
                "source": "名字是模型读出来的",
                "profiles": [{"platform": "X", "handle": "chiyoko_w"}],
            },
        )
        text = "\n".join(module.formatting._name_hunt_lines(outcome))
        self.assertIn("名字是模型读出来的", text)

    def test_x_line_carries_fans_and_matched_display_name(self) -> None:
        """显示名核得上的那个 X 号要带粉丝数和「显示名对得上」。

        411f 里 @Kuukow002 就是这么认出来的：花体显示名折平后是「miokuuko」。
        粉丝数只挂核过的那个号，列表里别的同名号不许蹭。
        """
        module = import_plugin()
        outcome = self._make_outcome(
            module,
            name_hunt={
                "name": "Mio Kuuko",
                "status": "seen",
                "x_handle": "Kuukow002",
                "x_followers": 1508078,
                "x_name": "𝑴𝒊𝒐 𝑲𝒖𝒖𝒌𝒐🍀",
                "profiles": [
                    {"platform": "X", "handle": "Kuukow002"},
                    {"platform": "X", "handle": "mio_kuuko_fan"},
                ],
            },
        )
        text = "\n".join(module.formatting._name_hunt_lines(outcome))
        self.assertIn("@Kuukow002（150.8 万粉，显示名", text)
        self.assertIn("对得上", text)
        # 同名的路人号不许蹭粉丝数，也不给一个没核实的主页链接（b08b 那个 404 号）
        self.assertNotIn("@mio_kuuko_fan（150", text)
        self.assertNotIn("https://x.com/mio_kuuko_fan", text)
        self.assertIn("@mio_kuuko_fan（没核实", text)

    def test_x_line_has_a_url_like_the_weibo_one(self) -> None:
        """X 号也要给链接：只写 @号时，用户会当成一句线索而不是一个能点的主页。"""
        module = import_plugin()
        outcome = self._make_outcome(
            module,
            name_hunt={
                "name": "纸悦Etsu_ko",
                "status": "seen",
                "x_handle": "Etsuko827",
                "x_followers": 212866,
                "x_name": "纸悦 Etsu_ko",
                "profiles": [{"platform": "X", "handle": "Etsuko827"}],
            },
        )
        text = "\n".join(module.formatting._name_hunt_lines(outcome))
        self.assertIn("https://x.com/Etsuko827", text)

    def test_x_line_says_how_many_posts_were_scanned(self) -> None:
        """翻过多少条要写出来：只写「没核到」时，看起来就跟没查一样（8bc9）。"""
        module = import_plugin()
        outcome = self._make_outcome(
            module,
            name_hunt={
                "name": "纸悦Etsu_ko",
                "status": "seen",
                "x_handle": "Etsuko827",
                "x_followers": 212866,
                "x_name": "纸悦 Etsu_ko",
                "x_scanned_posts": 39,
                "x_scanned_images": 42,
                "x_scan_truncated": True,
                "profiles": [{"platform": "X", "handle": "Etsuko827"}],
            },
        )
        text = "\n".join(module.formatting._name_hunt_lines(outcome))
        self.assertIn("39 条带图帖", text)
        self.assertIn("42 张图", text)
        self.assertIn("更早的还没翻完", text)

    def test_x_line_stays_quiet_when_nothing_was_scanned(self) -> None:
        """没翻过帖（比如没原图）就别编「翻了 0 条」。"""
        module = import_plugin()
        outcome = self._make_outcome(
            module,
            name_hunt={
                "name": "纸悦Etsu_ko",
                "status": "seen",
                "x_handle": "Etsuko827",
                "profiles": [{"platform": "X", "handle": "Etsuko827"}],
            },
        )
        text = "\n".join(module.formatting._name_hunt_lines(outcome))
        self.assertNotIn("带图帖", text)

    def test_hit_line_names_the_account(self) -> None:
        module = import_plugin()
        outcome = self._make_outcome(
            module,
            name_hunt={
                "name": "小仓千代w",
                "status": "hit",
                "platform": "X",
                "handle": "chiyoko_w",
                "distance": 3,
            },
        )
        text = "\n".join(module.formatting._name_hunt_lines(outcome))
        self.assertIn("@chiyoko_w", text)
        self.assertIn("指纹距离 3", text)


class XMediaScanNoteTest(unittest.TestCase):
    """/media 翻过一遍之后，搜索兜底的失败不能把「翻过」改写成「没翻成」。

    8bc9 那单：@Etsuko827 的 17 条带图帖都翻过了、没有这张；紧接着
    ``from:Etsuko827 filter:images`` 回 503，旧逻辑把整条结论换成「这次没翻成它的
    帖子」，于是首屏看不出 X 到底查过没有。
    """

    def _run(self, module, plugin, media, search):
        holder: dict = {}
        with (
            mock.patch.object(module.ImageSearcherPlugin, "_media_lookup", media),
            mock.patch.object(module.ImageSearcherPlugin, "_search_media_lookup", search),
            mock.patch.object(module.ImageSearcherPlugin, "_vlog", lambda *a, **k: None),
        ):
            asyncio.run(plugin._check_watermark_post("Etsuko827", b"x", holder))
        return holder

    def test_search_failure_keeps_the_scan_result(self) -> None:
        module, plugin, _ = _plugin()

        async def media(self, handle, upload):
            return {
                "total_posts": 39,
                "scanned_posts": 39,
                "scanned_images": 42,
                "truncated": False,
            }

        async def search(self, handle, upload):
            return {"error": "搜索没返回结果"}

        holder = self._run(module, plugin, media, search)
        note = holder["watermark_note"]
        self.assertIn("翻了她最近 39 条带图帖", note)
        self.assertIn("42 张图", note)
        self.assertIn("没有这张", note)
        self.assertIn("搜索这条兜底这次没成", note)
        self.assertNotIn("没翻成", note)
        self.assertEqual(holder["watermark_scan"]["scanned_posts"], 39)

    def test_both_legs_failing_is_still_reported_as_not_checked(self) -> None:
        """两条腿都没成时才写「没翻成」：这句不能当成否定证据。"""
        module, plugin, _ = _plugin()

        async def media(self, handle, upload):
            return {"error": "HTTP 503"}

        async def search(self, handle, upload):
            return {"error": "搜索没返回结果"}

        holder = self._run(module, plugin, media, search)
        self.assertIn("没翻成", holder["watermark_note"])

    def test_search_hit_still_wins(self) -> None:
        """搜索兜底真翻到了，照样按命中处理。"""
        module, plugin, _ = _plugin()

        async def media(self, handle, upload):
            return {"total_posts": 14, "scanned_posts": 14, "scanned_images": 15, "truncated": False}

        async def search(self, handle, upload):
            return {"url": "https://x.com/Etsuko827/status/9", "distance": 2}

        holder = self._run(module, plugin, media, search)
        self.assertIn("自己的带图帖里有这张图", holder["watermark_note"])
        self.assertEqual(holder["watermark_post"], "https://x.com/Etsuko827/status/9")


class NameHuntTraceTest(unittest.TestCase):
    """trace 一步四态：hit / ran / none / skip，理由要能写出来。"""

    def test_trace_states_are_wired(self) -> None:
        module = import_plugin()
        source = (module.__file__ and __import__("pathlib").Path(module.__file__).read_text(encoding="utf-8")) or ""
        self.assertIn('"按名字找号"', source)
        for state in ('"hit"', '"ran"', '"none"', '"skip"'):
            self.assertIn(state, source)

    def test_trace_notes(self) -> None:
        module = import_plugin()
        note = module.ImageSearcherPlugin._name_hunt_trace_note
        hit = note(
            {
                "name": "小仓千代w",
                "status": "hit",
                "platform": "X",
                "handle": "chiyoko_w",
                "distance": 3,
            }
        )
        self.assertIn("@chiyoko_w", hit)
        seen = note(
            {
                "name": "小仓千代w",
                "status": "seen",
                "profiles": [{"platform": "Instagram", "handle": "chiyoko.w"}],
            }
        )
        self.assertIn("Instagram/chiyoko.w", seen)
        self.assertIn("没核到这张图", seen)
        self.assertIn("没找到", note({"name": "小仓千代w", "status": "none"}))


class ContentPageAuthorTest(unittest.TestCase):
    """百度动态这类内容号落地页：候选标题里没账号，得去页面 <title> 里抽。"""

    def _page(self, module):
        return module.Candidate(
            engine="baidu",
            url="http://mbd.baidu.com/newspage/data/dtlandingsuper?nid=dt_4737138101278019876",
            title="夜色温柔，风也轻，jk女孩出没#jk元气少女# #jk制服#",
            name="夜色温柔，风也轻，jk女孩出没#jk元气少女# #jk制服#",
            platform="mbd.baidu.com",
            distance=1,
            status="match",
        )

    def test_account_comes_from_page_title(self) -> None:
        module, plugin, _ = _plugin(
            lambda config: setattr(config.title_author, "enabled", False)
        )
        calls: list[str] = []

        async def fake_excerpt(url: str):
            calls.append(url)
            return "...@诗诗爱生活的动态", "夜色温柔，风也轻，jk女孩出没"

        plugin._page_excerpt_for_author = fake_excerpt
        leads = asyncio.run(plugin._title_author_leads([self._page(module)]))
        self.assertEqual(len(leads), 1)
        author, variants, via, name, site = leads[0]
        self.assertEqual(author, "诗诗爱生活")
        # 纯中文名展开不出 X 变体，但 lead 不能因此被丢掉：名字还要交给按名字找号。
        self.assertEqual(variants, [])
        self.assertEqual(via, "rule")
        self.assertEqual(name, "诗诗爱生活")
        self.assertEqual(site, "http://mbd.baidu.com/newspage/data/dtlandingsuper?nid=dt_4737138101278019876")
        self.assertEqual(calls, [self._page(module).url])

    def test_other_sites_do_not_fetch_page_title(self) -> None:
        """普通站点没有「@xxx的动态」这种标题，别为每个落点多抓一次页面。"""
        module, plugin, _ = _plugin(
            lambda config: setattr(config.title_author, "enabled", False)
        )
        page = module.Candidate(
            engine="lens",
            url="https://example.com/post/1",
            title="some sentence without a name",
            name="some sentence without a name",
            platform="example.com",
            distance=3,
            status="match",
        )
        calls: list[str] = []

        async def fake_excerpt(url: str):
            calls.append(url)
            return "@诗诗爱生活的动态", ""

        plugin._page_excerpt_for_author = fake_excerpt
        leads = asyncio.run(plugin._title_author_leads([page]))
        self.assertEqual(leads, [])
        self.assertEqual(calls, [])

    def test_name_reaches_name_hunt_candidates(self) -> None:
        """抽到的名字要真的进「按名字找号」的候选列表。"""
        module, plugin, _ = _plugin(
            lambda config: setattr(config.title_author, "enabled", False)
        )
        page = self._page(module)
        found = plugin._title_name_candidates([page], ["诗诗爱生活"])
        self.assertIn("诗诗爱生活", found)

    def test_name_survives_without_x_variants(self) -> None:
        """中文名在 X 上查不到号，也要留下 status=none 的结果把名字带出去。"""
        module, plugin, _ = _plugin()
        lead = (
            "诗诗爱生活",
            [],
            "rule",
            "诗诗爱生活",
            "http://mbd.baidu.com/newspage/data/dtlandingsuper?nid=dt_1",
        )
        result = asyncio.run(plugin._scan_author_lead(lead, b"img"))
        self.assertIsNotNone(result)
        self.assertEqual(result["status"], "none")
        self.assertEqual(result["name"], "诗诗爱生活")
        self.assertEqual(result["variants"], [])


class NameSourceDistanceTest(unittest.TestCase):
    """名字只从「差不多就是这张图」的候选里读，远距离页面的作者名不算。

    起因是 `s-20261125-1414-fbd6` 与 `s-20261125-1255-cf7d`：两单都没搜到，但都
    把别人的名字当成作者拿去查了微博、X、B 站。fbd6 的「摇摇乐yoyo」来自 4khd 一条
    距离 21 的图集页（那是另一个 coser 2024 年的套图），cf7d 的「Tsuki_月隐」来自
    `x.com/tsuki_kakushi` 一条距离 31 的帖子。两个名字都是「另一张图、另一个人」，
    查回来的主页全是不相干的号，比不查更糟。
    """

    def _page(self, module, title, *, distance, status="suspect", url=None):
        url = url or "https://www.4khd.com/content/20/yoyo-murasame-senren-banka.html/8"
        return module.Candidate(
            engine="lens",
            url=url,
            title=title,
            platform=url.split("//")[1].split("/")[0],
            distance=distance,
            status=status,
        )

    def test_far_gallery_page_contributes_no_name(self) -> None:
        module, plugin, _ = _plugin()
        page = self._page(
            module, "摇摇乐yoyo - Murasame (Senren Banka)[101MB-153photos] - Page ...", distance=21
        )
        self.assertEqual(plugin._title_name_candidates([page]), [])

    def test_far_x_post_contributes_no_author_name(self) -> None:
        module, plugin, _ = _plugin()
        page = self._page(
            module,
            "Tsuki_月隐on X / X",
            distance=31,
            url="https://x.com/tsuki_kakushi/status/2109660774285324309/photo/1",
        )
        self.assertEqual(plugin._title_name_candidates([page]), [])

    def test_close_page_still_contributes(self) -> None:
        """阈值 + 余量以内的照旧算数：裁剪、重压、翻拍都在这个范围里。"""
        module, plugin, _ = _plugin()
        page = self._page(
            module,
            "Coser@小仓千代w: W泳装(21 photos) – MissKon.com",
            distance=9,
            status="suspect",
            url="https://misskon.com/coser-xiao-cang-qian-dai-w-w-yong-zhuang-21-photos/",
        )
        self.assertEqual(plugin._title_name_candidates([page]), ["小仓千代w"])

    def test_vision_pick_without_distance_still_counts(self) -> None:
        """视觉挑中过但没量出距离的页面照样能贡献名字（b08b 那种落点）。"""
        module, plugin, _ = _plugin()
        page = self._page(
            module,
            "Coser@小仓千代w: W泳装(21 photos) – MissKon.com",
            distance=None,
            status="vision",
            url="https://misskon.com/coser-xiao-cang-qian-dai-w-w-yong-zhuang-21-photos/",
        )
        self.assertEqual(plugin._title_name_candidates([page]), ["小仓千代w"])

    def test_llm_rows_are_filtered_by_the_same_rule(self) -> None:
        """模型也只看这个池子：远距离页面的标题根本不该送进去。"""
        module, plugin, _ = _plugin()

        async def boom(*args, **kwargs):  # noqa: ANN002, ANN003
            raise AssertionError("远距离页面不该问模型")

        plugin._ctx.call_capability = boom
        page = self._page(
            module, "摇摇乐yoyo - Murasame (Senren Banka)[101MB-153photos]", distance=21
        )
        self.assertEqual(asyncio.run(plugin._llm_title_names([page])), [])

    def test_limit_follows_the_hash_threshold(self) -> None:
        """阈值调宽，名字来源也跟着放宽，不是写死的 12。"""
        module, plugin, config = _plugin()
        config.plugin.hash_threshold = 20
        page = self._page(module, "摇摇乐yoyo - Murasame (Senren Banka)[101MB-153photos]", distance=21)
        self.assertEqual(plugin._title_name_candidates([page]), ["摇摇乐yoyo"])


if __name__ == "__main__":
    unittest.main()
