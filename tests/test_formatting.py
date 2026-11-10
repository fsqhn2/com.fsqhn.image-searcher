"""合并转发节点拼装：重点是没找到匹配时也要带一张参考图。"""

from __future__ import annotations

import json
import unittest

from core import formatting
from core.engines import Candidate
from core.formatting import (
    build_engine_forward_nodes,
    build_forward_nodes,
    build_result_forwards,
    build_text_summary,
    count_forward_images,
    engine_groups,
    stale_image_hint,
    ttl_text,
)
from core.tasks import SearchOutcome, SearchTask


def _task(outcome: SearchOutcome) -> SearchTask:
    return SearchTask(
        task_id="s-test",
        stream_id="g1",
        user_id="u1",
        status="done",
        outcome=outcome,
    )


def _images(nodes: list[dict]) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for index, node in enumerate(nodes):
        for part in node.get("content") or []:
            if part.get("type") == "image":
                found.append((index, part.get("content") or ""))
    return found


class ForwardNodesTest(unittest.TestCase):
    def test_best_candidate_image_goes_to_best_node(self) -> None:
        best = Candidate(engine="lens", url="https://x.com/a/status/1", distance=4, status="match")
        best.finalize()
        outcome = SearchOutcome(status="ok", best=best, candidates=[best], image_b64="BESTIMG")
        nodes = build_forward_nodes(_task(outcome), command="麦麦寻人")
        images = _images(nodes)
        self.assertEqual(images, [(1, "BESTIMG")])

    def test_not_found_still_carries_a_reference_image(self) -> None:
        other = Candidate(engine="yandex", url="https://www.pixiv.net/artworks/1", distance=21, status="suspect")
        other.finalize()
        outcome = SearchOutcome(
            status="not_found",
            best=None,
            candidates=[other],
            image_b64="REFIMG",
            image_source=other.image_url,
        )
        nodes = build_forward_nodes(_task(outcome), command="麦麦寻人")
        images = _images(nodes)
        self.assertEqual(images, [(2, "REFIMG")])
        text = "\n".join(
            part.get("content", "")
            for node in nodes
            for part in node.get("content") or []
            if part.get("type") == "text"
        )
        self.assertIn("未经指纹确认", text)

    def test_not_found_without_candidates_still_shows_image(self) -> None:
        outcome = SearchOutcome(status="not_found", best=None, candidates=[], image_b64="UPIMG", image_source="upload")
        nodes = build_forward_nodes(_task(outcome), command="麦麦寻人", show_reference=False)
        images = _images(nodes)
        self.assertEqual(images, [(2, "UPIMG")])
        joined = "\n".join(
            part.get("content", "")
            for node in nodes
            for part in node.get("content") or []
            if part.get("type") == "text"
        )
        self.assertIn("参考图", joined)

    def test_upload_reference_is_labelled_as_original(self) -> None:
        """一个候选图都没下到时，参考图其实是用户原图，别写成「候选里最近的一张」。"""
        outcome = SearchOutcome(status="not_found", best=None, candidates=[], image_b64="UPIMG", image_source="upload")
        nodes = build_forward_nodes(_task(outcome), command="麦麦寻人", show_reference=False)
        blob = json.dumps(nodes, ensure_ascii=False)
        self.assertIn("这张是你发的原图", blob)
        self.assertNotIn("引擎候选里跟原图最接近的一张", blob)

    def _grouped_outcome(self, *, hit: bool = True) -> SearchOutcome:
        best = Candidate(
            engine="saucenao",
            url="https://www.pixiv.net/artworks/9",
            distance=3,
            status="match",
        )
        lens_a = Candidate(
            engine="lens",
            url="https://www.pixiv.net/artworks/1",
            image_url="https://img.example/1.jpg",
            distance=9,
            status="suspect",
        )
        lens_b = Candidate(
            engine="lens",
            url="https://www.pixiv.net/artworks/2",
            image_url="https://img.example/2.jpg",
            distance=11,
            status="suspect",
        )
        yandex_a = Candidate(
            engine="yandex",
            url="https://x.com/a/status/1",
            image_url="https://img.example/3.jpg",
            distance=12,
            status="suspect",
        )
        for item in (best, lens_a, lens_b, yandex_a):
            item.finalize()
        return SearchOutcome(
            status="ok" if hit else "not_found",
            best=best if hit else None,
            candidates=[lens_a, lens_b, yandex_a, best],
            image_b64="BEST",
            image_source=best.image_url,
        )

    def test_reference_is_grouped_by_engine(self) -> None:
        """没找到时的参考区按引擎分开，每个引擎一个节点，标题带引擎名和条数。"""
        nodes = build_forward_nodes(_task(self._grouped_outcome(hit=False)), command="麦麦寻人")
        text = "\n".join(
            part.get("content", "")
            for node in nodes
            for part in node.get("content") or []
            if part.get("type") == "text"
        )
        self.assertIn("▍Google Lens（2 条）", text)
        self.assertIn("▍Yandex（1 条）", text)
        # 最佳匹配那一条不重复出现在参考区
        self.assertEqual(text.count("artworks/9"), 1)

    def test_hit_hides_reference(self) -> None:
        """命中时首次回复只给结论，参考区留给「更多」。"""
        nodes = build_forward_nodes(_task(self._grouped_outcome()), command="麦麦寻人")
        text = "\n".join(
            part.get("content", "")
            for node in nodes
            for part in node.get("content") or []
            if part.get("type") == "text"
        )
        self.assertNotIn("▍Google Lens", text)
        self.assertNotIn("▍Yandex", text)
        self.assertIn("pixiv.net/artworks/9", text)

    def test_footer_carries_task_id(self) -> None:
        """首次回复要给出任务编号，用户靠它回「更多」定位到具体那一次。"""
        task = _task(self._grouped_outcome())
        nodes = build_forward_nodes(task, command="麦麦寻人", ttl_minutes=1440)
        text = "\n".join(
            part.get("content", "")
            for node in nodes
            for part in node.get("content") or []
            if part.get("type") == "text"
        )
        self.assertIn(task.task_id, text)
        self.assertIn("1 天", text)
        self.assertIn("更多", text)

    def test_footer_offers_force_research_after_more(self) -> None:
        """页脚在「更多」后面给出重搜入口：缓存秒回时也知道怎么强制重跑。"""
        task = _task(self._grouped_outcome())
        nodes = build_forward_nodes(task, command="麦麦寻人", ttl_minutes=1440)
        text = "\n".join(
            part.get("content", "")
            for node in nodes
            for part in node.get("content") or []
            if part.get("type") == "text"
        )
        self.assertIn("重搜", text)
        self.assertGreater(text.index("重搜"), text.index("更多"))

    def test_ttl_text_reads_in_human_units(self) -> None:
        self.assertEqual(ttl_text(15), "15 分钟")
        self.assertEqual(ttl_text(60), "1 小时")
        self.assertEqual(ttl_text(90), "1.5 小时")
        self.assertEqual(ttl_text(1440), "1 天")
        self.assertEqual(ttl_text(2880), "2 天")

    def test_first_reply_carries_one_image_per_engine(self) -> None:
        """没找到时首次回复：每个引擎最接近的那张带图，其余候选只列文字。"""
        outcome = self._grouped_outcome(hit=False)
        outcome.images = {"https://img.example/1.jpg": "LENS1", "https://img.example/3.jpg": "YANDEX1"}
        nodes = build_forward_nodes(_task(outcome), command="麦麦寻人")
        self.assertEqual(_images(nodes), [(2, "BEST"), (3, "LENS1"), (4, "YANDEX1")])

    def test_more_reply_carries_every_candidate_image(self) -> None:
        outcome = self._grouped_outcome(hit=False)
        outcome.images = {
            "https://img.example/1.jpg": "LENS1",
            "https://img.example/2.jpg": "LENS2",
            "https://img.example/3.jpg": "YANDEX1",
        }
        nodes = build_forward_nodes(_task(outcome), command="麦麦寻人", limit=10, replay=True)
        self.assertEqual(
            _images(nodes),
            [(2, "BEST"), (3, "LENS1"), (3, "LENS2"), (4, "YANDEX1")],
        )

    def test_engine_limit_caps_candidates_per_engine(self) -> None:
        nodes = build_forward_nodes(_task(self._grouped_outcome(hit=False)), command="麦麦寻人", limit=1)
        text = "\n".join(
            part.get("content", "")
            for node in nodes
            for part in node.get("content") or []
            if part.get("type") == "text"
        )
        self.assertIn("▍Google Lens（1 条）", text)
        self.assertNotIn("artworks/2", text)

    def test_every_engine_group_shows_its_closest_image(self) -> None:
        """参考图同时是某个引擎最接近的那张时，该引擎分组里也要再给一次。"""
        outcome = self._grouped_outcome()
        outcome.best = None
        outcome.status = "not_found"
        outcome.image_b64 = "REF"
        outcome.image_source = "https://img.example/1.jpg"
        outcome.images = {"https://img.example/1.jpg": "REF", "https://img.example/3.jpg": "YANDEX1"}
        nodes = build_forward_nodes(_task(outcome), command="麦麦寻人")
        self.assertEqual(_images(nodes), [(2, "REF"), (3, "REF"), (4, "YANDEX1")])

    def test_engine_group_sorted_by_distance(self) -> None:
        """组内按指纹距离从近到远排，第一条就是该引擎最接近的那张。"""
        outcome = self._grouped_outcome(hit=False)
        lens_one = next(item for item in outcome.candidates if item.url.endswith("/1"))
        lens_two = next(item for item in outcome.candidates if item.url.endswith("/2"))
        lens_one.distance, lens_two.distance = 15, 6
        nodes = build_forward_nodes(_task(outcome), command="麦麦寻人", limit=1)
        text = "\n".join(
            part.get("content", "")
            for node in nodes
            for part in node.get("content") or []
            if part.get("type") == "text"
        )
        self.assertIn("artworks/2", text)
        self.assertNotIn("artworks/1", text)

    def test_engine_forward_carries_best_only_on_first_batch(self) -> None:
        """图多时按引擎拆成多条，最佳匹配只在第一条出现，页脚只在最后一条。"""
        outcome = self._grouped_outcome()
        outcome.images = {
            "https://img.example/1.jpg": "LENS1",
            "https://img.example/2.jpg": "LENS2",
            "https://img.example/3.jpg": "YANDEX1",
        }
        groups = engine_groups(outcome, outcome.best, 10)
        self.assertTrue(groups)
        engine, items = groups[0]
        first = build_engine_forward_nodes(
            _task(outcome), engine, items, command="麦麦寻人", part=(1, 2), with_best=True
        )
        last = build_engine_forward_nodes(
            _task(outcome), groups[-1][0], groups[-1][1], command="麦麦寻人", part=(2, 2), include_footer=True
        )
        first_text = "\n".join(
            part.get("content", "")
            for node in first
            for part in node.get("content") or []
            if part.get("type") == "text"
        )
        last_text = "\n".join(
            part.get("content", "")
            for node in last
            for part in node.get("content") or []
            if part.get("type") == "text"
        )
        self.assertIn("1/2", first_text)
        self.assertIn("平台", first_text)  # 最佳匹配块在第一批
        self.assertIn("以上为全部参考结果", last_text)
        self.assertNotIn("平台", last_text)
        # 第一批的图只来自本批候选（含最佳匹配那张），不重复
        self.assertEqual(_images(first)[0], (1, "BEST"))

    def test_header_follows_configured_command(self) -> None:
        """指令可配置，转发标题也要跟着改，不能写死默认名。"""
        outcome = SearchOutcome(status="ok", best=None, candidates=[], image_b64="")
        nodes = build_forward_nodes(_task(outcome), command="示例指令")
        header = nodes[0]["content"][0]["content"]
        self.assertEqual(header, "🔍 示例指令结果")

    def test_nodes_carry_sender_uin(self) -> None:
        """uin 为空时 QQ 端会提示消息异常，所以每个节点都要带 user_id。"""
        outcome = SearchOutcome(status="not_found", best=None, candidates=[], image_b64="", image_source="")
        nodes = build_forward_nodes(_task(outcome), command="麦麦寻人", user_id="2377510578")
        self.assertTrue(all(node.get("user_id") == "2377510578" for node in nodes))
        self.assertGreaterEqual(len(nodes), 3)


class CandidateAccountTest(unittest.TestCase):
    def test_unverified_candidate_hides_account(self) -> None:
        item = Candidate(
            engine="lens",
            url="https://x.com/someone",
            handle="@someone",
            name="Someone",
            status="unverified",
            note="插画路线参考，未做指纹比对",
        )
        item.finalize()
        outcome = SearchOutcome(status="not_found", best=None, candidates=[item])
        text = build_text_summary(_task(outcome), command="麦麦寻人")
        self.assertIn("账号未确认", text)
        self.assertNotIn("@someone", text)

    def test_matched_candidate_keeps_account(self) -> None:
        item = Candidate(engine="lens", url="https://x.com/someone", handle="@someone", distance=4, status="match")
        item.finalize()
        outcome = SearchOutcome(status="ok", best=item, candidates=[item])
        text = build_text_summary(_task(outcome), command="麦麦寻人")
        self.assertIn("@someone", text)


class EtaTextTest(unittest.TestCase):
    def test_running_task_uses_given_eta(self) -> None:
        task = SearchTask(task_id="s-test", stream_id="g1", user_id="u1", status="running")
        text = build_text_summary(task, command="麦麦寻人", eta_text="约 2 分钟")
        self.assertIn("约 2 分钟", text)
        self.assertNotIn("40 秒", text)


class StaleImageHintTest(unittest.TestCase):
    def test_hint_mentions_minutes_and_msg_id(self) -> None:
        text = stale_image_hint(15_600)
        self.assertIn("260 分钟", text)
        self.assertIn("msg_id", text)
        self.assertIn("引用", text)
        self.assertIn("不是图片服务故障", text)

    def test_under_one_minute_rounds_up(self) -> None:
        self.assertIn("1 分钟", stale_image_hint(30))


class GrokReferenceTest(unittest.TestCase):
    """Grok 给的帖子配图对不上时，链接仍要放出来，但必须标明是存疑参考。"""
    def test_account_tier_keeps_post_as_reference(self) -> None:
        outcome = SearchOutcome(
            status="ok",
            best=None,
            candidates=[],
            grok={
                "tier": "account",
                "handle": "mian84232594",
                "post_url": "https://x.com/mian84232594/status/1",
                "reason": "帖子配图对不上（最近距离 28），帖子只当参考",
            },
            total_candidates=0,
        )
        text = build_text_summary(_task(outcome), command="盆地找鸡")
        self.assertIn("没核到原帖", text)
        self.assertIn("存疑参考", text)
        self.assertIn("https://x.com/mian84232594/status/1", text)
        self.assertIn("请自行核对", text)

    def test_account_without_post_has_no_reference_line(self) -> None:
        outcome = SearchOutcome(
            status="ok",
            best=None,
            candidates=[],
            grok={"tier": "account", "handle": "catxiu233", "post_url": "", "reason": "名字对上了"},
            total_candidates=0,
        )
        text = build_text_summary(_task(outcome), command="盆地找鸡")
        self.assertIn("catxiu233", text)
        self.assertNotIn("存疑参考", text)

    def test_reference_image_comes_from_closest_tweet_photo(self) -> None:
        """Grok 帖子核对失败时，参考图用帖子里距离最近的那张，并说明来源。"""
        ref = "https://pbs.twimg.com/media/abc.jpg"
        outcome = SearchOutcome(
            status="ok",
            best=None,
            candidates=[],
            grok={
                "tier": "account",
                "handle": "mian84232594",
                "post_url": "https://x.com/mian84232594/status/1",
                "reference_url": ref,
                "distance": 20,
                "reason": "帖子配图对不上（最近距离 20），帖子只当参考",
            },
            image_b64="AAAA",
            image_source=ref,
            total_candidates=0,
        )
        nodes = build_forward_nodes(_task(outcome), command="盆地找鸡")
        blob = json.dumps(nodes, ensure_ascii=False)
        self.assertIn("Grok 帖子配图里跟原图最接近", blob)
        # 距离要写在图上，免得把这张图当成结论。
        self.assertIn("指纹距离 20", blob)
        self.assertIn("不是同一张", blob)

    def test_reference_image_on_hit_is_the_matched_photo(self) -> None:
        """找到原帖那单也得带图：命中的帖子配图直接当参考图，别再写「没有可用的候选图」。"""
        ref = "https://pbs.twimg.com/media/HWuoYYraMAAnwqS.jpg"
        outcome = SearchOutcome(
            status="ok",
            best=None,
            candidates=[],
            grok={
                "tier": "post",
                "handle": "Banbanko_",
                "post_url": "https://x.com/Banbanko_/status/2119974818808492177",
                "reference_url": ref,
                "distance": 0,
                "verified": "hit",
                "reason": "帖子配图对得上（距离 0）",
            },
            image_b64="AAAA",
            image_source=ref,
            total_candidates=0,
        )
        blob = json.dumps(build_forward_nodes(_task(outcome), command="盆地找鸡"), ensure_ascii=False)
        self.assertIn("Grok 帖子配图里跟原图最接近", blob)
        self.assertIn("同一张", blob)
        self.assertNotIn("没有可用的候选图", blob)

    def test_reference_image_reports_candidate_distance(self) -> None:
        """参考图来自候选池时，也要标出它跟原图的距离。"""
        item = Candidate(
            engine="lens",
            url="https://example.com/a",
            image_url="https://img/a.jpg",
            distance=22,
            status="mismatch",
        )
        outcome = SearchOutcome(
            status="not_found",
            best=None,
            candidates=[item],
            image_b64="AAAA",
            image_source="https://img/a.jpg",
            total_candidates=1,
        )
        nodes = build_forward_nodes(_task(outcome), command="盆地找鸡")
        blob = json.dumps(nodes, ensure_ascii=False)
        self.assertIn("候选里跟原图最接近的一张", blob)
        self.assertIn("指纹距离 22", blob)

    def test_reference_from_downgraded_vision_pick_is_labelled_vision(self) -> None:
        """视觉挑中但被降级的那张当参考图时，也要写「视觉方案挑中的那张」。"""
        item = Candidate(
            engine="lens",
            url="https://nowbestshirt.com/p/1",
            image_url="https://img/p.jpg",
            distance=24,
            status="suspect",
        )
        item.note = "视觉复核：疑似同一个人，但来自电商商品页（指纹距离 24），仅供参考"
        outcome = SearchOutcome(
            status="not_found",
            best=None,
            candidates=[item],
            image_b64="AAAA",
            image_source="https://img/p.jpg",
            vision={"ok": True, "picked": [], "picked_images": ["https://img/p.jpg"], "downgraded": 1},
            total_candidates=1,
        )
        blob = json.dumps(build_forward_nodes(_task(outcome), command="盆地找鸡"), ensure_ascii=False)
        self.assertIn("视觉方案挑中的那张", blob)
        self.assertIn("指纹距离 24", blob)

    def test_x_profile_image_is_treated_as_avatar_even_without_flag(self) -> None:
        """s-20261106-1642：X 头像地址没被旧版识别，视觉还把它当成命中。"""
        item = Candidate(
            engine="lens",
            url="https://x.com/lonely82089351",
            title="lonely (@lonely82089351) / X",
            image_url="https://pbs.twimg.com/profile_images/2062750521325391872/Gk7_NOC2_400x400.jpg",
            distance=22,
            status="suspect",
        )
        item.note = "视觉复核：疑似同一个人，但没在 @lonely82089351 最近的带图帖里翻到这张，仅供参考"
        outcome = SearchOutcome(
            status="not_found",
            best=None,
            candidates=[item],
            image_b64="AAAA",
            image_source=item.image_url,
            total_candidates=1,
        )
        blob = json.dumps(build_forward_nodes(_task(outcome), command="盆地找鸡"), ensure_ascii=False)
        self.assertIn("另有 1 条是站点账号头像", blob)
        self.assertIn("这张是站点账号头像", blob)
        self.assertNotIn("最接近的一条", blob)
        self.assertNotIn("同角色或同款服装", blob)

    def test_reference_at_distance_zero_is_not_called_different(self) -> None:
        """距离 0 就是同一张，不能写成「不是同一张」。"""
        item = Candidate(
            engine="lens",
            url="https://x.com/someone",
            image_url="https://img/a.jpg",
            distance=0,
            status="suspect",
            note="指纹一致（距离 0），但这是 X 的账号主页，不是哪条帖子",
        )
        outcome = SearchOutcome(
            status="not_found",
            best=None,
            candidates=[item],
            image_b64="AAAA",
            image_source="https://img/a.jpg",
            total_candidates=1,
        )
        blob = json.dumps(build_forward_nodes(_task(outcome), command="盆地找鸡"), ensure_ascii=False)
        self.assertIn("指纹距离 0", blob)
        self.assertNotIn("不是同一张", blob)

    def test_avatar_reference_is_labelled_as_avatar(self) -> None:
        """参考图本身是头像命中时，要写清是账号头像，而不是「最接近的一张」。"""
        item = Candidate(
            engine="lens",
            url="https://dzmm.ai/user/x/following",
            image_url="https://pxlorbit.com/avatar/a.png",
            distance=0,
            status="suspect",
            avatar=True,
        )
        item.note = "视觉复核：疑似同一个人，但那是dzmm.ai上的页面、不是作者主页（指纹距离 0）"
        outcome = SearchOutcome(
            status="not_found",
            best=None,
            candidates=[item],
            image_b64="AAAA",
            image_source="https://pxlorbit.com/avatar/a.png",
            total_candidates=1,
        )
        blob = json.dumps(build_forward_nodes(_task(outcome), command="盆地找鸡"), ensure_ascii=False)
        self.assertIn("这张是站点账号头像", blob)
        self.assertIn("页面里看不到它", blob)
        self.assertNotIn("不是同一张", blob)

    def test_reference_image_from_vision_is_labelled(self) -> None:
        """参考图是视觉方案挑的那张时，标题要写明，别让人以为是引擎直接搜到的。"""
        item = Candidate(
            engine="lens",
            url="https://example.com/a",
            image_url="https://img/v.jpg",
            distance=19,
            status="suspect",
            note="视觉复核：疑似同一个人，仅供参考",
        )
        outcome = SearchOutcome(
            status="not_found",
            best=None,
            candidates=[item],
            vision={
                "ok": True,
                "checked": 10,
                "picked": ["https://img/v.jpg"],
                "downgraded": 1,
                "reason": "同款服装",
            },
            image_b64="VIS",
            image_source="https://img/v.jpg",
            total_candidates=1,
        )
        nodes = build_forward_nodes(_task(outcome), command="盆地找鸡")
        blob = json.dumps(nodes, ensure_ascii=False)
        self.assertIn("视觉方案挑中的那张", blob)
        self.assertNotIn("不是视觉方案挑的", blob)

    def test_reference_image_says_when_it_is_neither_vision_nor_grok(self) -> None:
        item = Candidate(
            engine="lens",
            url="https://example.com/a",
            image_url="https://img/a.jpg",
            distance=25,
            status="mismatch",
        )
        outcome = SearchOutcome(
            status="not_found",
            best=None,
            candidates=[item],
            vision={"ok": True, "checked": 8, "picked": [], "downgraded": 0, "reason": "都不像"},
            image_b64="AAAA",
            image_source="https://img/a.jpg",
            total_candidates=1,
        )
        nodes = build_forward_nodes(_task(outcome), command="盆地找鸡")
        blob = json.dumps(nodes, ensure_ascii=False)
        self.assertIn("不是视觉方案挑的，也不是 Grok 给的", blob)

    def test_footer_explains_grok_reference_image(self) -> None:
        ref = "https://pbs.twimg.com/media/abc.jpg"
        outcome = SearchOutcome(
            status="ok",
            best=None,
            candidates=[],
            grok={"tier": "account", "handle": "x", "post_url": "https://x.com/x/status/1",
                  "reference_url": ref, "reason": "对不上"},
            image_b64="AAAA",
            image_source=ref,
            total_candidates=0,
        )
        nodes = build_forward_nodes(_task(outcome), command="盆地找鸡")
        blob = json.dumps(nodes, ensure_ascii=False)
        self.assertIn("Grok 给的帖子里跟原图最接近的一张", blob)


class TwoSuggestionsTest(unittest.TestCase):
    """视觉和 Grok 同时给出结论时，两张图都要发。"""

    def _make_outcome(self, with_grok_image: bool) -> SearchOutcome:
        best = Candidate(
            engine="lens",
            url="https://x.com/a/status/1",
            distance=6,
            status="vision",
            image_url="https://img/vision.jpg",
        )
        return SearchOutcome(
            status="ok",
            best=best,
            candidates=[best],
            image_b64="VIS",
            image_source="https://img/vision.jpg",
            grok_image_b64="GROK" if with_grok_image else "",
            grok={
                "tier": "account",
                "handle": "someone",
                "post_url": "https://x.com/someone/status/2",
                "reference_url": "https://img/grok.jpg",
                "reason": "帖子配图对不上（最近距离 20），帖子只当参考",
            },
            total_candidates=1,
        )

    def test_both_images_are_sent(self) -> None:
        nodes = build_forward_nodes(_task(self._make_outcome(True)), command="盆地找鸡")
        images = [
            part.get("content")
            for node in nodes
            for part in node.get("content") or []
            if part.get("type") == "image"
        ]
        self.assertIn("VIS", images)
        self.assertIn("GROK", images)

    def test_only_vision_image_when_grok_has_none(self) -> None:
        nodes = build_forward_nodes(_task(self._make_outcome(False)), command="盆地找鸡")
        images = [
            part.get("content")
            for node in nodes
            for part in node.get("content") or []
            if part.get("type") == "image"
        ]
        self.assertEqual(images, ["VIS"])

    def test_vision_best_stays_in_reference_groups(self) -> None:
        """结论说「见参考区」，那条就得真的在参考区里。"""
        outcome = self._make_outcome(False)
        groups = engine_groups(outcome, outcome.best, 8)
        flat = [item for _, items in groups for item in items]
        self.assertIn(outcome.best, flat)


class SplitForwardsTest(unittest.TestCase):
    """视觉、Grok、页脚各自成一条，方便对照，不再挤在同一条转发里。"""

    def _make_outcome(self, *, with_grok: bool = True, best: bool = True) -> SearchOutcome:
        item = Candidate(
            engine="baidu",
            url="https://www.douyin.com/note/123",
            distance=27,
            status="vision",
            note="视觉复核：同一个人（距离 27）",
            handle="someone",
        )
        item.finalize()
        outcome = SearchOutcome(
            status="ok",
            best=item if best else None,
            candidates=[item],
            image_b64="VISIMG",
            image_source=item.image_url,
            grok={
                "ok": True,
                "tier": "account" if with_grok else "none",
                "handle": "someone",
                "post_url": "https://x.com/someone/status/1",
                "reference_url": "https://pbs.twimg.com/media/AAA.jpg",
                "distance": 25,
                "reason": "帖子配图对不上（最近距离 25），帖子只当参考",
            }
            if with_grok
            else None,
        )
        outcome.grok_image_b64 = "GROKIMG"
        return outcome

    def test_vision_and_grok_split_into_three_messages(self) -> None:
        blocks = build_result_forwards(_task(self._make_outcome()), command="盆地找鸡")
        self.assertEqual(len(blocks), 3)

        vision_text = blocks[0][0]["content"][0]["content"]
        self.assertIn("视觉复核认为是同一个人", vision_text)
        self.assertIn("可能认错", vision_text)
        self.assertEqual(
            [p["content"] for p in blocks[0][0]["content"] if p["type"] == "image"], ["VISIMG"]
        )

        grok_text = "\n".join(
            part["content"] for part in blocks[1][0]["content"] if part["type"] == "text"
        )
        self.assertIn("Grok 核对", grok_text)
        self.assertIn("someone", grok_text)
        self.assertIn("不是同一张", grok_text)
        self.assertEqual(
            [p["content"] for p in blocks[1][0]["content"] if p["type"] == "image"], ["GROKIMG"]
        )

        footer = blocks[2][0]["content"][0]["content"]
        self.assertIn("任务编号", footer)

    def test_not_found_with_grok_keeps_footer_block_separate(self) -> None:
        outcome = self._make_outcome(best=False)
        blocks = build_result_forwards(_task(outcome), command="盆地找鸡")
        self.assertEqual(len(blocks), 2)
        # Grok 单独一条，尾部那条只讲「没找到」和参考区，不再重复 Grok 结论。
        tail = "\n".join(
            part["content"]
            for node in blocks[1]
            for part in node.get("content") or []
            if part["type"] == "text"
        )
        self.assertIn("未找到可靠出处", tail)
        self.assertNotIn("X 搜索指向这个账号", tail)
        self.assertIn("任务编号", tail)

    def test_grok_found_post_does_not_contradict_with_not_found(self) -> None:
        """Grok 已给出原帖时，尾部再说「未找到可靠出处」就是自相矛盾（s-20261106-1659）。"""
        item = Candidate(
            engine="lens",
            url="https://x.com/other/status/1",
            image_url="https://img/a.jpg",
            distance=22,
            status="mismatch",
        )
        outcome = SearchOutcome(
            status="ok",
            best=None,
            candidates=[item],
            grok={
                "ok": True,
                "tier": "post",
                "handle": "Wunaijiangonly",
                "post_url": "https://x.com/Wunaijiangonly/status/2118547517633933360",
                "distance": 0,
                "reason": "翻了该号的带图帖子，这张图就在里面",
            },
            image_b64="AAAA",
            image_source="upload",
            total_candidates=1,
        )
        blocks = build_result_forwards(_task(outcome), command="盆地找鸡")
        blob = "\n".join(
            part["content"]
            for block in blocks
            for node in block
            for part in node.get("content") or []
            if part.get("type") == "text"
        )
        self.assertIn("找到 X 上的原帖", blob)
        self.assertIn("见上一条", blob)
        self.assertNotIn("未找到可靠出处", blob)

    def test_not_found_suspects_say_distance_range(self) -> None:
        """未找到时的疑似条目要说清是同角色/同款，并标出指纹距离区间。"""
        low = Candidate(engine="yandex", url="https://example.com/a", distance=24, status="suspect")
        low.finalize()
        high = Candidate(engine="yandex", url="https://example.com/b", distance=31, status="suspect")
        high.finalize()
        outcome = SearchOutcome(
            status="not_found",
            candidates=[low, high],
            total_candidates=2,
        )
        blocks = build_result_forwards(_task(outcome), command="盆地找鸡")
        tail = "\n".join(
            part["content"]
            for node in blocks[-1]
            for part in node.get("content") or []
            if part["type"] == "text"
        )
        self.assertIn("另有 2 条是同角色或同款服装的图", tail)
        self.assertIn("指纹距离 24～31", tail)
        self.assertNotIn("疑似同图", tail)

    def test_avatar_hits_are_not_counted_as_same_character(self) -> None:
        """头像命中不算「同角色或同款服装」，note 被视觉改写后也要认出来。"""
        avatar = Candidate(
            engine="lens",
            url="https://dzmm.ai/user/following",
            distance=0,
            status="suspect",
            avatar=True,
        )
        avatar.finalize()
        # 视觉降级会覆盖 note，头像标记必须还在。
        avatar.note = "视觉复核：疑似同一个人，但那是dzmm.ai上的页面、不是作者主页（指纹距离 0）"
        other = Candidate(engine="yandex", url="https://example.com/a", distance=26, status="suspect")
        other.finalize()
        outcome = SearchOutcome(
            status="not_found",
            candidates=[avatar, other],
            total_candidates=2,
        )
        blocks = build_result_forwards(_task(outcome), command="盆地找鸡")
        tail = "\n".join(
            part["content"]
            for node in blocks[-1]
            for part in node.get("content") or []
            if part["type"] == "text"
        )
        self.assertIn("另有 1 条是站点账号头像", tail)
        self.assertIn("指纹对上了，但那是头像不是原帖", tail)
        self.assertIn("另有 1 条是同角色或同款服装的图", tail)
        self.assertNotIn("另有 2 条", tail)

    def test_three_messages_fit_in_one_forward(self) -> None:
        """三条合起来只有两张图，能塞进同一条合并转发（NapCat 单条约 6 张）。"""
        blocks = build_result_forwards(_task(self._make_outcome()), command="盆地找鸡")
        merged = [node for block in blocks for node in block]
        self.assertEqual(len(merged), 3)
        self.assertEqual(count_forward_images(merged), 2)
        self.assertLessEqual(count_forward_images(merged), 6)

    def test_no_fallback_content_stays_single_message(self) -> None:
        outcome = self._make_outcome(with_grok=False, best=False)
        outcome.grok = {"ok": True, "tier": "none", "error": ""}
        blocks = build_result_forwards(_task(outcome), command="盆地找鸡")
        self.assertEqual(len(blocks), 1)


class XGuessTest(unittest.TestCase):
    """疑似 X 账号：必须带上「从哪条候选推出来的」，并说明不是原帖。"""

    def _make_outcome(self, *, with_grok: bool = False) -> SearchOutcome:
        item = Candidate(
            engine="lens",
            url="https://galleryepic.com/en/cosplay/5881",
            distance=2,
            status="match",
            platform="galleryepic.com",
            name="KuukoW – Nahida Cosplay",
        )
        item.finalize()
        return SearchOutcome(
            status="ok",
            best=item,
            candidates=[item],
            grok={"tier": "account", "handle": "Kuukow002"} if with_grok else None,
            x_guess={
                "handle": "Kuukow002",
                "followers": 1508263,
                "source_platform": "masterfap.net",
                "source_title": "Kuuko / kuukow002 / kuuko_w Nude OnlyFans Leak Picture",
            },
        )

    def test_shows_source_and_says_not_original(self) -> None:
        text = formatting._best_block(self._make_outcome())
        self.assertIn("@Kuukow002", text)
        # 账号后面直接给主页地址，方便点开核对；不要包在括号里。
        self.assertIn("@Kuukow002 https://x.com/Kuukow002（粉丝 150.8 万）", text)
        self.assertIn("150.8 万", text)
        self.assertIn("masterfap.net", text)
        self.assertIn("kuukow002", text)
        self.assertIn("不是这张图的原帖", text)
        # 原始结果仍然在，不能被疑似账号替换掉。
        self.assertIn("galleryepic.com/en/cosplay/5881", text)

    def test_hidden_when_grok_names_the_same_handle(self) -> None:
        text = formatting._best_block(self._make_outcome(with_grok=True))
        self.assertNotIn("疑似同一个人的 X", text)


class ForumTitleTest(unittest.TestCase):
    """论坛帖的标题不能写成「名字」：inven 那单发了串韩文帖子标题出去。"""

    def test_forum_title_is_labelled_as_title(self) -> None:
        item = Candidate(
            engine="lens",
            url="https://www.inven.co.kr/board/lostark/6271/4036037",
            distance=1,
            status="match",
            platform="inven.co.kr",
            name="ㅇㅎ)키가 크고싶은 코스어 | 로아 인벤",
        )
        item.finalize()
        outcome = SearchOutcome(status="ok", best=item, candidates=[item])
        text = formatting._best_block(outcome)
        self.assertIn("📄 标题：ㅇㅎ", text)
        self.assertNotIn("👤 名字：", text)

    def test_social_page_still_shows_name(self) -> None:
        item = Candidate(
            engine="lens",
            url="https://x.com/NERUchi890/status/2117586176714612824",
            distance=0,
            status="match",
            platform="X (Twitter)",
            name="NERU",
        )
        item.finalize()
        outcome = SearchOutcome(status="ok", best=item, candidates=[item])
        self.assertIn("👤 名字：NERU", formatting._best_block(outcome))


class PaidGuessTest(unittest.TestCase):
    """付费内容线索：给主页、X、IG，但必须说清不是原帖。"""

    def _make_outcome(self) -> SearchOutcome:
        item = Candidate(
            engine="lens",
            url="https://de.hentai-cosplay-xxx.com/image/dreamtamu-bride-vermeil/",
            distance=0,
            status="match",
            platform="de.hentai-cosplay-xxx.com",
            name="Dreamtamu - Bride Vermeil - Hentai Cosplay",
        )
        item.finalize()
        return SearchOutcome(
            status="ok",
            best=item,
            candidates=[item],
            paid={
                "handle": "dreamtamu",
                "name": "Dream Tamu",
                "page_url": "https://leakgallery.com/dreamtamu",
                "x_handle": "DreamTamuu",
                "x_followers": 16560,
                "instagram": "https://instagram.com/dream_tamu_",
                "source_platform": "de.hentai-cosplay-xxx.com",
                "source_title": "Dreamtamu - Bride Vermeil - Hentai Cosplay",
            },
        )

    def test_shows_profile_x_and_instagram(self) -> None:
        text = formatting._best_block(self._make_outcome())
        self.assertIn("疑似同一个人的 OnlyFans：Dream Tamu", text)
        self.assertIn("https://onlyfans.com/dreamtamu", text)
        self.assertIn("https://x.com/DreamTamuu", text)
        self.assertIn("1.7 万", text)
        self.assertIn("https://instagram.com/dream_tamu_", text)
        self.assertIn("https://leakgallery.com/dreamtamu", text)
        self.assertIn("不是这张图的原帖", text)
        # 原始结果仍然在，不能被付费线索替换掉。
        self.assertIn("hentai-cosplay-xxx.com/image/dreamtamu-bride-vermeil", text)

    def test_absent_when_no_paid_info(self) -> None:
        text = formatting._best_block(SearchOutcome(status="ok", candidates=[]))
        self.assertNotIn("OnlyFans", text)


class GrokMissTest(unittest.TestCase):
    """Grok 跑了但没找到时要给一句交代，否则分不清「没跑」和「跑了没结果」。"""

    def _make(self, tier: str = "none", ok: bool = True) -> SearchOutcome:
        return SearchOutcome(
            status="ok",
            candidates=[],
            grok={"ok": ok, "tier": tier, "handle": "", "reason": "图无文字ID，X 搜无此原图帖"},
        )

    def test_miss_line_appended(self) -> None:
        text = formatting._best_block(self._make())
        self.assertIn("Grok 也拿这张图去 X 搜过，没撞见原帖", text)

    def test_not_added_when_grok_has_a_result(self) -> None:
        text = formatting._best_block(self._make(tier="account"))
        self.assertNotIn("没撞见原帖", text)

    def test_not_added_when_grok_failed(self) -> None:
        text = formatting._best_block(self._make(ok=False))
        self.assertNotIn("没撞见原帖", text)

    def test_vision_block_carries_the_line(self) -> None:
        item = Candidate(
            engine="lens",
            url="https://x.com/someone/status/1",
            distance=28,
            status="vision",
            platform="X (Twitter)",
            name="someone",
        )
        item.finalize()
        outcome = SearchOutcome(status="ok", best=item, candidates=[item], grok={"ok": True, "tier": "none"})
        block = formatting._vision_block(outcome)
        self.assertIsNotNone(block)
        self.assertIn("没撞见原帖", block[0]["content"])


class SamePersonTest(unittest.TestCase):
    """几个号挂同一条外链：结论要带依据。"""

    def _make(self) -> SearchOutcome:
        item = Candidate(
            engine="lens",
            url="https://x.com/NENEchi8890/reposts",
            distance=28,
            status="vision",
            platform="X (Twitter)",
            name="NERU",
            handle="@NENEchi8890",
        )
        item.finalize()
        return SearchOutcome(
            status="ok",
            best=item,
            candidates=[item],
            same_person={
                "link": "patreon.com/NENE318",
                "members": [
                    {"handle": "NENEchi8890", "name": "NERU", "followers": 185456},
                    {"handle": "haru007312", "name": "nene", "followers": 59411},
                ],
            },
        )

    def test_lists_both_accounts_with_the_link(self) -> None:
        text = formatting._best_block(self._make())
        self.assertIn("这2个号是同一个人", text)
        self.assertIn("@NENEchi8890（18.5 万）", text)
        self.assertIn("@haru007312（5.9 万）", text)
        self.assertIn("patreon.com/NENE318", text)

    def test_says_it_is_not_the_author(self) -> None:
        """同一个人 ≠ 这张图的作者，必须写清楚，否则等于把无关的人挂到结论下面。"""
        text = formatting._best_block(self._make())
        self.assertIn("不表示他们就是这张图的作者", text)

    def test_marks_the_account_tied_to_the_image(self) -> None:
        outcome = self._make()
        outcome.same_person["members"][0]["tied"] = True
        text = formatting._best_block(outcome)
        self.assertIn("@NENEchi8890（18.5 万，跟这张图对上的那条）", text)
        self.assertNotIn("@haru007312（5.9 万，跟这张图", text)

    def test_absent_without_a_group(self) -> None:
        text = formatting._best_block(SearchOutcome(status="ok", candidates=[]))
        self.assertNotIn("个号是同一个人", text)


class BilibiliVideoIdTest(unittest.TestCase):
    """B 站的 BV 号是视频号，不是账号。"""

    @staticmethod
    def _make(handle: str) -> SearchOutcome:
        best = Candidate(
            engine="baidu",
            url="https://www.bilibili.com/video/BV16m4y1E7Tf",
            title="【ASMR/Cosplay】湊阿库娅",
            platform="B站",
            handle=handle,
            status="vision",
            note="视觉复核：同一个人（指纹距离 26，超过阈值 8）",
        )
        return SearchOutcome(status="ok", best=best, candidates=[best])

    def test_bv_is_labelled_video_id(self) -> None:
        text = formatting._best_block(self._make("BV16m4y1E7Tf"))
        self.assertIn("🎬 视频号：BV16m4y1E7Tf", text)
        self.assertNotIn("🆔 账号：BV", text)

    def test_real_account_keeps_account_label(self) -> None:
        text = formatting._best_block(self._make("@some_user"))
        self.assertIn("🆔 账号：@some_user", text)
        self.assertNotIn("视频号", text)


class EtaTextTest(unittest.TestCase):
    """耗时提示的口径：普通单 1 分钟，难找的单交代 2~6 分钟。"""

    def test_fast_eta(self) -> None:
        self.assertEqual(formatting.ETA_FAST, "约 1 分钟")

    def test_full_eta_has_slow_window(self) -> None:
        self.assertEqual(formatting.eta_full(), "约 1 分钟，如果这图比较难找，会需要 2 到 6 分钟")

    def test_slow_ping_seconds_is_three_minutes(self) -> None:
        self.assertEqual(formatting.SLOW_PING_SECONDS, 180)

    def test_slow_ping_text(self) -> None:
        self.assertEqual(formatting.SLOW_PING_TEXT, "这图有点难找，我需要细细对比，再稍等下哦。")

    def test_trace_ping_text_says_whats_happening(self) -> None:
        """回复页反查要花几十秒，这句得交代在干什么、还要多久。"""
        self.assertIn("蛛丝马迹", formatting.TRACE_PING_TEXT)
        self.assertIn("原帖", formatting.TRACE_PING_TEXT)
        self.assertIn("1 分钟", formatting.TRACE_PING_TEXT)


class DowngradedNearMissTest(unittest.TestCase):
    """距离够近但落点不对时，结论不能写成「均未通过阈值」。"""

    def _near_miss_outcome(self) -> SearchOutcome:
        hit = Candidate(
            engine="lens",
            url="https://x.com/someone/with_replies",
            image_url="https://pbs.twimg.com/media/abc.jpg",
            distance=0,
            status="suspect",
            platform="X (Twitter)",
            handle="@someone",
            note="视觉复核：疑似同一个人，但那是转发页、图不是她发的（指纹距离 0，超过阈值 8），仅供参考",
        )
        other = Candidate(
            engine="yandex",
            url="https://example.com/a",
            distance=31,
            status="mismatch",
        )
        vision = {"ok": True, "checked": 16, "downgraded": 1, "reason": "姿势一致"}
        return SearchOutcome(status="not_found", best=None, candidates=[hit, other], vision=vision)

    def test_no_false_threshold_claim(self) -> None:
        text = formatting.build_text_summary(
            self._task(), command="麦麦寻人", show_reference=True, limit=3
        )
        self.assertNotIn("均未通过阈值", text)
        self.assertIn("最近的一条指纹距离 0", text)
        self.assertIn("但那条不能当原帖", text)

    def test_downgrade_reason_comes_from_candidate(self) -> None:
        text = formatting.build_text_summary(
            self._task(), command="麦麦寻人", show_reference=True, limit=3
        )
        self.assertIn("但那是转发页、图不是她发的", text)
        self.assertNotIn("落点在图库站、商品页这类页面", text)

    def _task(self) -> SearchTask:
        task = SearchTask(task_id="s-test", stream_id="s1", user_id="u1")
        task.status = "done"
        task.outcome = self._near_miss_outcome()
        return task


class ResultPresentationTest(unittest.TestCase):
    """首次回复只给结论，「怎么找的」留在「更多」里。"""

    def _task(self) -> SearchTask:
        best = Candidate(
            engine="lens",
            url="https://x.com/hinatahimarii/status/2118658490315276514",
            platform="X (Twitter)",
            handle="@hinatahimarii",
            name="土狗小黄",
            distance=0,
            status="match",
            note="指纹一致（距离 0）：这是 @wn40536699 的回复页，顺着她的回复找到原帖在 @hinatahimarii 名下",
        )
        outcome = SearchOutcome(status="ok", best=best, candidates=[best], route="photo")
        task = SearchTask(task_id="s-test", stream_id="s1", user_id="u1")
        task.status = "done"
        task.outcome = outcome
        return task

    def test_first_reply_hides_the_search_path(self) -> None:
        text = build_text_summary(self._task(), command="麦麦搜图", show_reference=True, limit=3)
        self.assertIn("指纹一致（距离 0）", text)
        self.assertNotIn("回复页", text)
        self.assertNotIn("匹配路线", text)

    def test_more_keeps_the_full_note(self) -> None:
        nodes = build_forward_nodes(
            self._task(), command="麦麦搜图", show_reference=True, limit=3, replay=True
        )
        text = "\n".join(
            str(part.get("content"))
            for node in nodes
            for part in node.get("content") or []
            if part.get("type") == "text"
        )
        self.assertIn("回复页", text)
        self.assertIn("匹配路线", text)

    def test_first_reply_footer_has_no_route(self) -> None:
        blocks = build_result_forwards(self._task(), command="麦麦搜图", show_reference=True, limit=3)
        nodes = [node for block in blocks for node in block]
        text = "\n".join(
            str(part.get("content"))
            for node in nodes
            for part in node.get("content") or []
            if part.get("type") == "text"
        )
        self.assertNotIn("匹配路线", text)

    def test_more_explains_why_it_is_not_an_illustration(self) -> None:
        """照片路线也要说清原因：常见的疑问是「明明是插画，怎么没走 pixiv」。"""
        task = self._task()
        task.outcome.route_reason = "SauceNAO 最高 37.3%，不到插画阈值 75%"
        nodes = build_forward_nodes(
            task, command="麦麦搜图", show_reference=True, limit=3, replay=True
        )
        text = "\n".join(
            str(part.get("content"))
            for node in nodes
            for part in node.get("content") or []
            if part.get("type") == "text"
        )
        self.assertIn("匹配路线：照片", text)
        self.assertIn("不到插画阈值 75%", text)


class WatermarkProfileTest(unittest.TestCase):
    """水印号核到存在就把主页与数字 ID 给出来，别让用户自己去试。"""

    def test_verified_account_shows_profile_and_id(self) -> None:
        outcome = SearchOutcome()
        outcome.vision = {
            "watermark": "X@Miharu",
            "watermark_handle": "Miharu",
            "watermark_url": "https://x.com/Miharu",
            "watermark_state": "ok",
            "watermark_user_id": "6143712",
            "watermark_note": "@Miharu 这个号存在，但这次没翻成它的帖子（HTTP 500），不能据此说没有",
        }
        outcome.grok = {"watermark": "X@Moliuwy"}
        text = formatting._best_block(outcome)
        self.assertIn("https://x.com/Miharu", text)
        self.assertIn("6143712", text)
        self.assertIn("不是同一个", text)

    def test_unknown_account_keeps_no_link(self) -> None:
        outcome = SearchOutcome()
        outcome.vision = {"watermark": "X@Miharu", "watermark_url": "https://x.com/Miharu"}
        outcome.grok = {"watermark": "X@Moliuwy"}
        text = formatting._best_block(outcome)
        self.assertNotIn("https://x.com/Miharu", text)

    def test_misread_handle_explains_the_fix(self) -> None:
        """号名读偏、靠近似拼写才找回来时，必须写清楚「读的是啥、核到的是谁」。"""
        outcome = SearchOutcome()
        outcome.vision = {
            "watermark": "X@Miharu",
            "watermark_readings": [
                {"source": "vision", "raw": "X@Miharu", "handle": "Miharu", "state": "ok"},
                {
                    "source": "grok",
                    "raw": "X@Moliuwy",
                    "handle": "Moliww",
                    "from": "Moliuwy",
                    "fixed": "Moliww",
                    "state": "ok",
                    "distance": 0,
                    "post": "https://x.com/Moliww/status/2119651416868540557",
                    "note": "自己的带图帖里有这张图，指纹距离 0",
                },
            ],
        }
        outcome.grok = {"watermark": "X@Moliuwy"}
        text = formatting._best_block(outcome)
        self.assertIn("读偏了", text)
        self.assertIn("X@Moliuwy", text)
        self.assertIn("@Moliww", text)
        self.assertIn("x.com/Moliww/status/2119651416868540557", text)

    def test_reread_hit_is_not_listed_as_misread(self) -> None:
        """原图重读直接读对时，这个写法算读对了，不列进「读偏了」。"""
        outcome = SearchOutcome()
        outcome.vision = {
            "watermark": "X@Miharu",
            "watermark_readings": [
                {"source": "vision", "raw": "X@Miharu", "handle": "miharu", "state": "ok"},
                {"source": "grok", "raw": "X@Moliuwy", "handle": "Moliww", "from": "Moliuwy",
                 "fixed": "Moliww", "state": "ok", "distance": 0,
                 "post": "https://x.com/Moliww/status/2119651416868540557"},
                {"source": "vision-original", "raw": "X@Moliww", "handle": "Moliww",
                 "state": "ok", "distance": 0,
                 "post": "https://x.com/Moliww/status/2119651416868540557"},
            ],
        }
        text = formatting._best_block(outcome)
        self.assertIn("X@Miharu、X@Moliuwy", text)
        self.assertNotIn("X@Moliww 都", text)

    def test_readings_without_hit_still_warn(self) -> None:
        """两个读法都没核到这张图时，仍然按线索写，不能变成「核对上了」。"""
        outcome = SearchOutcome()
        outcome.vision = {
            "watermark": "X@Miharu",
            "watermark_handle": "Miharu",
            "watermark_url": "https://x.com/Miharu",
            "watermark_state": "ok",
            "watermark_user_id": "6143712",
            "watermark_note": "最近十几条带图帖和搜索结果里都没有这张，可能是模型读错了",
            "watermark_readings": [
                {"source": "vision", "raw": "X@Miharu", "handle": "Miharu", "state": "ok"},
                {"source": "grok", "raw": "X@Moliuwy", "handle": "Moliuwy", "state": "missing"},
            ],
        }
        outcome.grok = {"watermark": "X@Moliuwy"}
        text = formatting._best_block(outcome)
        self.assertIn("线索，不是结论", text)
        self.assertNotIn("核对上了", text)


class WatermarkSameAccountTest(unittest.TestCase):
    """水印读法跟结论只差一个符号时，不能摆出另一个同名账号来打自己的脸。"""

    def _same_account_case(self) -> SearchOutcome:
        outcome = SearchOutcome()
        outcome.status = "ok"
        outcome.grok = {
            "tier": "post",
            "handle": "Banbanko_",
            "name": "半半子💖BANBANKO",
            "post_url": "https://x.com/Banbanko_/status/2119974818808492177",
            "watermark": "@Banbanko",
            "verified": "hit",
            "distance": 0,
            "reason": "帖子配图对得上（距离 0）",
        }
        outcome.vision = {
            "watermark": "@Banbanko",
            "watermark_handle": "banbanko",
            "watermark_url": "https://x.com/banbanko",
            "watermark_state": "ok",
            "watermark_user_id": "96473546",
            "watermark_name": "ばんこ(高塚ばんこ)",
            "watermark_note": "最近十几条带图帖和搜索结果里都没有这张，可能是模型读错了",
        }
        return outcome

    def test_trailing_underscore_is_the_same_account(self) -> None:
        """@Banbanko 与 @Banbanko_ 只差末尾下划线，就是同一个号没读全。"""
        text = formatting._best_block(self._same_account_case())
        self.assertIn("就是这个号", text)
        self.assertNotIn("与上面的结论不同", text)

    def test_other_account_is_not_offered_as_clue(self) -> None:
        """那个 @banbanko 是另一个人（日本议员号），不该出现在结论下面。"""
        text = formatting._best_block(self._same_account_case())
        self.assertNotIn("https://x.com/banbanko", text)
        self.assertNotIn("96473546", text)

    def test_post_tier_writes_name_and_account(self) -> None:
        """只有一条链接不够：首次回复要写明名字和账号。"""
        text = formatting._best_block(self._same_account_case())
        self.assertIn("半半子", text)
        self.assertIn("@Banbanko_", text)


class PaywallTest(unittest.TestCase):
    """确认作者后找到的售卖页：卖在哪、多少钱、要不要订阅。"""

    def _paid_case(self) -> SearchOutcome:
        outcome = SearchOutcome(status="ok", best=None, candidates=[], total_candidates=0)
        outcome.paywall = {
            "platform": "patreon",
            "handle": "oioioi525",
            "shop": "https://www.patreon.com/oioioi525",
            "post": {
                "title": "Rossi 108p",
                "url": "https://www.patreon.com/oioioi525/posts/rossi-108p-171677785",
                "published": "2026-10-07",
                "min_cents": 0,
                "locked": True,
                "currency": "HKD",
            },
            "tiers": [
                {"title": "Free", "cents": 0, "currency": "HKD", "free": True},
                {"title": "ranran", "cents": 5500, "currency": "HKD", "free": False},
            ],
            "preview": None,
        }
        return outcome

    def test_shows_shop_tiers_and_gate(self) -> None:
        text = formatting._best_block(self._paid_case())
        self.assertIn("售卖页（Patreon）：Rossi 108p", text)
        self.assertIn("rossi-108p-171677785", text)
        self.assertIn("档位", text)
        self.assertIn("ranran HK$55", text)
        self.assertIn("订阅", text)

    def test_preview_replaces_gate_line(self) -> None:
        outcome = self._paid_case()
        outcome.paywall["preview"] = {"title": "Rossi 预告", "url": "https://x.com/oioioi525/status/1"}
        text = formatting._best_block(outcome)
        self.assertIn("免费预览", text)
        self.assertNotIn("🔒", text)

    def test_nothing_when_no_paywall(self) -> None:
        outcome = SearchOutcome(status="not_found", best=None, candidates=[], total_candidates=0)
        self.assertNotIn("售卖页", formatting._best_block(outcome))


class XProfileLinkTest(unittest.TestCase):
    """X 上给的是帖子链接，账号主页还得单独写出来。"""

    def test_best_candidate_shows_profile(self) -> None:
        best = Candidate(
            engine="lens",
            url="https://x.com/oioioi525/status/2119651416868540557",
            platform="X (Twitter)",
            handle="oioioi525",
            name="ran3",
            status="match",
            note="本人发过这张图",
        )
        outcome = SearchOutcome(status="ok", best=best, candidates=[best], total_candidates=1)
        self.assertIn("🐦 X 主页：https://x.com/oioioi525", formatting._best_block(outcome))

    def test_grok_post_shows_profile(self) -> None:
        outcome = SearchOutcome(status="ok", best=None, candidates=[], total_candidates=0)
        outcome.grok = {
            "tier": "post",
            "handle": "Banbanko_",
            "post_url": "https://x.com/Banbanko_/status/2119974818808492177",
        }
        self.assertIn("🐦 X 主页：https://x.com/Banbanko_", formatting._best_block(outcome))


class XIdentityTest(unittest.TestCase):
    """微博 UID 换昵称、再换到 X 号那条线怎么落到文案上。"""

    def _vision_best(self, uid: str = "7652767178") -> Candidate:
        best = Candidate(
            engine="lens",
            url=f"https://weibo.com/{uid}/RqDnJ6SQY",
            platform="微博",
            handle=uid,
            distance=29,
            status="vision",
            note="视觉复核：同一个人（指纹距离 29，超过阈值 8）",
        )
        best.finalize()
        return best

    def test_seen_reports_the_right_account_without_claiming_source(self) -> None:
        """找到她的 X 号、但最近没发这张：要写清「人没错、原帖在别处」。"""
        outcome = SearchOutcome(
            status="not_found",
            best=self._vision_best(),
            x_identity={
                "status": "seen",
                "name": "纸悦Etsu_ko",
                "weibo": {"name": "纸悦Etsu_ko", "followers": 153067},
                "handle": "Etsuko827",
                "x_name": "纸悦Etsu_ko",
                "followers": 12345,
                "reason": "最近十几条带图帖里没这张",
            },
        )
        text = formatting._best_block(outcome)
        self.assertIn("@Etsuko827", text)
        self.assertIn("15.3 万粉", text)
        self.assertIn("这人应该没错", text)
        self.assertIn("没找到同一张图", text)

    def test_hit_explains_how_the_handle_was_found(self) -> None:
        """换到号并命中时，结论是那条帖子，另起一行交代号是怎么来的。"""
        best = Candidate(
            engine="identity",
            url="https://x.com/Etsuko827/status/2119988412052840742",
            platform="X (Twitter)",
            handle="@Etsuko827",
            name="纸悦Etsu_ko",
            distance=0,
            status="match",
            note="按微博昵称找到 X @Etsuko827：它自己的带图帖里有这张图（距离 0）",
        )
        best.finalize()
        outcome = SearchOutcome(
            status="ok",
            best=best,
            x_identity={
                "status": "hit",
                "name": "纸悦Etsu_ko",
                "weibo": {"name": "纸悦Etsu_ko", "followers": 153067},
                "handle": "Etsuko827",
                "x_name": "纸悦Etsu_ko",
                "followers": 12345,
                "post": "https://x.com/Etsuko827/status/2119988412052840742",
                "distance": 0,
            },
        )
        text = formatting._best_block(outcome)
        self.assertIn("本人发过这张图", text)
        self.assertIn("https://x.com/Etsuko827/status/2119988412052840742", text)
        self.assertIn("这个 X 号是按名字找到的", text)

    def test_none_only_mentions_the_weibo_name(self) -> None:
        """检索没给出号时，至少把昵称写出来——UID 本身搜不出人。"""
        outcome = SearchOutcome(
            status="not_found",
            best=self._vision_best(),
            x_identity={
                "status": "none",
                "name": "纸悦Etsu_ko",
                "weibo": {"name": "纸悦Etsu_ko", "followers": 153067},
                "reason": "检索没给出 X 号",
            },
        )
        text = formatting._best_block(outcome)
        self.assertIn("纸悦Etsu_ko", text)
        self.assertIn("没搜到对应的 X 号", text)

    def test_no_identity_leaves_the_block_untouched(self) -> None:
        outcome = SearchOutcome(status="not_found", best=self._vision_best())
        text = formatting._best_block(outcome)
        self.assertNotIn("按名字", text)


class RepostAuthorTest(unittest.TestCase):
    """命中搬运站之后翻没翻候选里的 X 号，要能看出来。"""

    def _site_best(self) -> Candidate:
        best = Candidate(
            engine="lens",
            url="https://www.jh361.com/archives/196738",
            platform="jh361.com",
            name="推特高颜值萝莉染三COS明日方舟终末地洛茜大尺度写真",
            distance=2,
            status="match",
            note="指纹一致（距离 2）",
        )
        best.finalize()
        return best

    def test_scanned_handles_only_show_up_in_more(self) -> None:
        """「翻过哪几个号」属于怎么找的，只在「更多」里出现。"""
        outcome = SearchOutcome(
            status="ok",
            best=self._site_best(),
            repost_author={"status": "none", "scanned": ["oioioi525", "Purugear"], "failed": []},
        )
        first = formatting._best_block(outcome)
        self.assertNotIn("oioioi525", first)
        more = formatting._best_block(outcome, detail=True)
        self.assertIn("@oioioi525", more)
        self.assertIn("@Purugear", more)
        self.assertIn("没找到这张", more)

    def test_failed_lookup_is_not_written_as_absent(self) -> None:
        """某个号这次没翻成，要单独说，不能混进「都没有」。"""
        outcome = SearchOutcome(
            status="ok",
            best=self._site_best(),
            repost_author={"status": "none", "scanned": ["a_one"], "failed": ["a_one"]},
        )
        more = formatting._best_block(outcome, detail=True)
        self.assertIn("没翻成", more)

    def test_hit_says_the_repost_was_just_a_copy(self) -> None:
        outcome = SearchOutcome(
            status="ok",
            best=self._site_best(),
            repost_author={
                "status": "hit",
                "scanned": ["oioioi525"],
                "failed": [],
                "handle": "oioioi525",
                "post": "https://x.com/oioioi525/status/2114312460266090942",
                "distance": 0,
                "x_name": "ran3",
            },
        )
        text = formatting._best_block(outcome)
        self.assertIn("@oioioi525", text)
        self.assertIn("搬运站那条只是转贴", text)

    def test_no_hunt_leaves_no_trace(self) -> None:
        outcome = SearchOutcome(status="ok", best=self._site_best())
        more = formatting._best_block(outcome, detail=True)
        self.assertNotIn("也翻过候选里", more)


if __name__ == "__main__":
    unittest.main()
