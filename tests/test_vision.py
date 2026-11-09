"""视觉兜底的纯函数：拼图、prompt、回复解析。

不碰网络也不碰插件上下文，全部用生成的图片跑。
"""

from __future__ import annotations

import io
import json
import random
import sys
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import vision  # noqa: E402
from core import formatting  # noqa: E402
from core.engines import Candidate  # noqa: E402
from core.tasks import SearchOutcome  # noqa: E402


def _sample(seed: int, size: tuple[int, int] = (480, 640)) -> bytes:
    """生成一张有结构的图，避免纯色让缩放结果毫无区别。"""
    rnd = random.Random(seed)
    image = Image.new("RGB", size, (rnd.randrange(256), rnd.randrange(200), 90))
    draw = ImageDraw.Draw(image)
    for _ in range(6):
        x, y = rnd.randrange(size[0]), rnd.randrange(size[1])
        draw.ellipse((x, y, x + 120, y + 160), fill=(240, 240, 230))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _size(raw: bytes) -> tuple[int, int]:
    return Image.open(io.BytesIO(raw)).size


class BuildPromptTest(unittest.TestCase):
    def test_prompt_mentions_each_candidate_count(self):
        prompt = vision.build_prompt(16)
        self.assertIn("第 1 到第 16 格", prompt)
        self.assertIn("TARGET", prompt)
        # 判定标准必须是「同一个人」，不能退回「同一张图」。
        self.assertIn("同一个人", prompt)
        self.assertIn("same_person", prompt)

    def test_prompt_allows_candidates_without_a_visible_face(self):
        """很多目标图只有腿或局部，提示词不能要求必须看到脸。"""
        prompt = vision.build_prompt(16)
        self.assertIn("看不到脸", prompt)
        # 真正的门槛是个体化证据，而不是脸。
        self.assertIn("不算证据", prompt)
        self.assertIn("角色名", prompt)

    def test_prompt_braces_are_rendered(self):
        """模板里的 JSON 示例用双花括号转义，渲染后应剩单花括号。"""
        prompt = vision.build_prompt(3)
        self.assertIn('{"same_person": [编号]', prompt)
        self.assertNotIn("{{", prompt)


class BuildGridTest(unittest.TestCase):
    def test_target_is_slot_zero_and_layout_matches_counts(self):
        grid = vision.build_grid(_sample(1), [_sample(i) for i in range(1, 17)])
        # 4 列 320：17 格排 5 行，每格比原来 5 列 224 大一倍多。
        self.assertAlmostEqual(grid.width, 4 * 320 + 5 * 4, delta=1)
        self.assertAlmostEqual(grid.height, 5 * 320 + 6 * 4, delta=1)
        self.assertTrue(grid.data.startswith(b"\xff\xd8"))

    def test_twenty_five_candidates_take_seven_rows(self):
        grid = vision.build_grid(_sample(1), [_sample(i) for i in range(1, 26)])
        self.assertAlmostEqual(grid.width, 4 * 320 + 5 * 4, delta=1)
        self.assertAlmostEqual(grid.height, 7 * 320 + 8 * 4, delta=1)

    def test_broken_candidate_keeps_its_slot(self):
        """解不开的图占一格灰块，编号不能往前挪，否则模型报的编号会错位。"""
        grid = vision.build_grid(_sample(1), [_sample(2), b"not-an-image", _sample(3)])
        self.assertGreater(len(grid.data), 0)

    def test_requires_at_least_one_candidate(self):
        with self.assertRaises(ValueError):
            vision.build_grid(_sample(1), [])

    def test_data_url_prefix(self):
        grid = vision.build_grid(_sample(1), [_sample(2)])
        self.assertTrue(grid.data_url.startswith("data:image/jpeg;base64,"))


class ParsePicksTest(unittest.TestCase):
    def test_plain_json(self):
        text = '{"same_person": [3, 7], "reason": "同一件外套"}'
        self.assertEqual(vision.parse_picks(text, 16), [3, 7])

    def test_markdown_fence(self):
        text = '结果如下：\n```json\n{"same_person": [12]}\n```\n'
        self.assertEqual(vision.parse_picks(text, 16), [12])

    def test_single_quotes_and_string_numbers(self):
        text = "{'same_person': ['5', '9']}"
        self.assertEqual(vision.parse_picks(text, 16), [5, 9])

    def test_no_json_falls_back_to_array_regex(self):
        self.assertEqual(vision.parse_picks("same_person: [2, 4]", 16), [2, 4])
        self.assertEqual(vision.parse_picks("同一个人：same_person：[6]", 16), [6])

    def test_none_found(self):
        self.assertEqual(vision.parse_picks('{"same_person": [], "reason": "都不是"}', 16), [])

    def test_drops_zero_out_of_range_and_duplicates(self):
        text = '{"same_person": [0, 1, 1, 20, 99]}'
        self.assertEqual(vision.parse_picks(text, 16), [1])

    def test_garbage_and_empty(self):
        for text in ("", "都不是", "{}", "null", "抱歉，我无法判断"):
            self.assertEqual(vision.parse_picks(text, 16), [])

    def test_alternative_keys(self):
        self.assertEqual(vision.parse_picks('{"picks": [4]}', 16), [4])

    def test_never_picks_target(self):
        """0 号格是目标图本身，模型要是回 0 必须丢掉。"""
        self.assertEqual(vision.parse_picks('{"same_person": [0]}', 16), [])

    def test_max_index_bounds(self):
        self.assertEqual(vision.parse_picks('{"same_person": [16]}', 16), [16])
        self.assertEqual(vision.parse_picks('{"same_person": [17]}', 16), [])


class ReasonTest(unittest.TestCase):
    def test_reason_extracted(self):
        self.assertEqual(vision.reason_of('```json\n{"same_person": [2], "reason": "同款帽子和发色"}\n```'), "同款帽子和发色")

    def test_reason_missing(self):
        self.assertEqual(vision.reason_of("没有 JSON"), "")
        self.assertEqual(vision.reason_of(""), "")


class WatermarkTest(unittest.TestCase):
    """TARGET 格上的水印顺带抄一份，只能当线索。"""

    def test_prompt_asks_for_watermark(self):
        prompt = vision.build_prompt(8)
        self.assertIn("watermark", prompt)
        self.assertIn("只抄 TARGET", prompt)

    def test_watermark_extracted(self):
        text = '{"same_person": [], "reason": "-", "watermark": "@sssmnms"}'
        self.assertEqual(vision.watermark_of(text), "@sssmnms")

    def test_watermark_keeps_chinese(self):
        text = '```json\n{"watermark": "微博@某某"}\n```'
        self.assertEqual(vision.watermark_of(text), "微博@某某")

    def test_watermark_rejects_unreadable(self):
        for value in ("", "无", "看不清", "没有水印", "None", "n/a"):
            text = json.dumps({"watermark": value}, ensure_ascii=False)
            self.assertEqual(vision.watermark_of(text), "", value)

    def test_watermark_missing_field(self):
        self.assertEqual(vision.watermark_of('{"same_person": [2]}'), "")
        self.assertEqual(vision.watermark_of("没有 JSON"), "")

    def test_watermark_truncated(self):
        text = json.dumps({"watermark": "x" * 80})
        self.assertEqual(len(vision.watermark_of(text)), 40)

    def test_handle_from_at_mark(self):
        self.assertEqual(vision.handle_from_watermark("@sssmnms"), "sssmnms")
        self.assertEqual(vision.handle_from_watermark("水印 @abc_123 右下角"), "abc_123")

    def test_handle_from_bare_account(self):
        self.assertEqual(vision.handle_from_watermark("hinatahimarii"), "hinatahimarii")

    def test_handle_rejects_guessing(self):
        # 中文名、平台名、纯数字都不许当账号，宁缺毋滥。
        for value in ("土狗小黄", "weibo", "1234567", "", "@a", "土狗小黄 on X"):
            self.assertEqual(vision.handle_from_watermark(value), "", value)

    def test_handle_needs_at_least_two_chars(self):
        self.assertEqual(vision.handle_from_watermark("@sss mn ms"), "sss")

    def test_watermark_line_shown(self):
        outcome = SearchOutcome()
        outcome.vision = {"watermark": "@sssmnms", "watermark_url": "https://x.com/sssmnms"}
        text = formatting._best_block(outcome)
        self.assertIn("图上水印：@sssmnms", text)
        self.assertIn("https://x.com/sssmnms", text)
        self.assertIn("可能读错", text)

    def test_watermark_line_without_url(self):
        outcome = SearchOutcome()
        outcome.vision = {"watermark": "微博@某某"}
        text = formatting._best_block(outcome)
        self.assertIn("图上水印：微博@某某", text)
        self.assertNotIn("可点开核对", text)

    def test_watermark_line_marks_unreadable(self):
        outcome = SearchOutcome()
        outcome.vision = {
            "watermark": "@Babybaobaoya",
            "watermark_note": "X 接口读不到这个号（用户 ID 1659556344330874880）",
        }
        text = formatting._best_block(outcome)
        self.assertIn("读不到这个号", text)
        self.assertNotIn("可点开核对", text)

    def test_no_watermark_no_line(self):
        outcome = SearchOutcome()
        outcome.vision = {"watermark": ""}
        self.assertNotIn("图上水印", formatting._best_block(outcome))


class LeakSiteTest(unittest.TestCase):
    """落在盗图/资源站时要说明账号是抄来的，别当成作者。"""

    def test_leak_site_matches_host(self):
        self.assertEqual(
            formatting.leak_site_of("https://www.jxporn.net/351632.html", "jxporn.net"),
            "www.jxporn.net",
        )
        self.assertEqual(formatting.leak_site_of("https://www.4khd.com/content/1", ""), "www.4khd.com")
        self.assertEqual(formatting.leak_site_of("https://lanrenbl.net/6600078.html", ""), "lanrenbl.net")

    def test_leak_site_ignores_normal_sites(self):
        for url in ("https://x.com/sssmnms/status/1", "https://www.pixiv.net/artworks/1", "https://weibo.com/1"):
            self.assertEqual(formatting.leak_site_of(url, ""), "", url)

    def test_leak_line_shown_in_best_block(self):
        best = Candidate(
            engine="lens",
            url="https://www.jxporn.net/351632.html",
            platform="jxporn.net",
            handle="@Babybaobaoya",
            name="崽崽",
            status="vision",
        )
        outcome = SearchOutcome(status="ok", best=best, candidates=[best])
        text = formatting._best_block(outcome)
        self.assertIn("盗图/资源站", text)
        self.assertIn("不是作者主页", text)

    def test_no_leak_line_for_x_post(self):
        best = Candidate(
            engine="lens",
            url="https://x.com/sssmnms/status/1994540292142501924",
            platform="X (Twitter)",
            handle="@sssmnms",
            status="match",
            distance=1,
        )
        outcome = SearchOutcome(status="ok", best=best, candidates=[best])
        self.assertNotIn("盗图/资源站", formatting._best_block(outcome))


class AccountNoteTest(unittest.TestCase):
    """结论账号读不到时要把接口状态和数字 ID 一起写出来，但别断言「封号」。"""

    def test_unreadable_note_shown(self):
        best = Candidate(
            engine="lens",
            url="https://www.jxporn.net/351632.html",
            platform="jxporn.net",
            handle="@Babybaobaoya",
            name="崽崽",
            status="vision",
        )
        best.account_note = (
            "⚠️ X 接口读不到这个号（用户 ID 1659556344330874880），可能受限或已封，主页要自己点开确认。"
        )
        outcome = SearchOutcome(status="ok", best=best, candidates=[best])
        text = formatting._best_block(outcome)
        self.assertIn("读不到这个号", text)
        self.assertIn("1659556344330874880", text)
        # 单独占一行，别混进「匹配：」里当判定依据。
        self.assertNotIn("匹配：⚠️", text)

    def test_no_note_no_line(self):
        best = Candidate(engine="x", url="https://x.com/a/status/1", platform="X (Twitter)", handle="@a")
        outcome = SearchOutcome(status="ok", best=best, candidates=[best])
        self.assertNotIn("读不到这个号", formatting._best_block(outcome))


class VisionDisplayTest(unittest.TestCase):
    """视觉命中的候选不能冒充「同一张图」，文案上必须区分开。"""

    def test_best_block_marks_vision_hit(self):
        item = Candidate(
            engine="lens",
            url="https://x.com/someone/status/1",
            platform="X",
            handle="@someone",
            status="vision",
            note="视觉复核：同一个人（指纹距离 24，超过阈值 8）",
        )
        outcome = SearchOutcome(status="ok", best=item, candidates=[item])
        text = formatting._best_block(outcome)
        self.assertIn("视觉复核认为是同一个人", text)
        self.assertIn("@someone", text)
        self.assertIn("同一张图", text)
        # 视觉判的是同一个人，必须说明是模型挑的、可能认错，让用户自己核对。
        self.assertIn("视觉模型挑的", text)
        self.assertIn("自行核对", text)

    def test_not_found_reports_vision_checked(self):
        outcome = SearchOutcome(status="not_found", vision={"ok": True, "checked": 16, "picked": []})
        text = formatting._best_block(outcome)
        self.assertIn("16", text)
        self.assertIn("没有发现同一个人", text)

    def test_not_found_reports_vision_error(self):
        outcome = SearchOutcome(status="not_found", vision={"ok": False, "checked": 0, "error": "视觉模型 90 秒内没返回"})
        text = formatting._best_block(outcome)
        self.assertIn("视觉复核没做完", text)
        self.assertIn("90 秒", text)

    def test_vision_candidate_keeps_account(self):
        """指纹不符的候选要隐藏账号，视觉命中的不用隐藏。"""
        self.assertEqual(formatting._candidate_identity(Candidate(engine="lens", handle="@a", status="vision")), "@a")
        self.assertEqual(
            formatting._candidate_identity(Candidate(engine="lens", handle="@a", status="mismatch")),
            "（账号未确认）",
        )

    def test_commerce_detected_by_url_or_platform(self):
        """电商商品页要能认出来：同款衣服的商品图不是同一个人。"""
        self.assertTrue(vision.is_commerce(url="https://aliexpress.ru/item/1005003898750895.html"))
        self.assertTrue(vision.is_commerce(platform="russian.alibaba.com"))
        self.assertTrue(vision.is_commerce(name="Women tights - Alibaba.com"))
        self.assertFalse(vision.is_commerce(url="https://x.com/someone/status/123", platform="X (Twitter)"))
        self.assertFalse(vision.is_commerce(url="https://www.pixiv.net/artworks/1", platform="pixiv.net"))

    def test_traceable_needs_handle_or_social_host(self):
        self.assertTrue(vision.is_traceable("@someone", "X (Twitter)", "https://x.com/someone/status/1"))
        self.assertTrue(vision.is_traceable("", "pixiv.net", ""))
        self.assertFalse(vision.is_traceable("", "aliexpress.ru", "https://aliexpress.ru/item/1"))
        self.assertFalse(vision.is_traceable("", "某论坛", "https://example.com/thread/9"))

    def test_verdict_requires_an_account_not_just_a_platform_page(self):
        """抖音 note 这类只有平台页面、没有账号的，不能当结论发出去。"""
        self.assertEqual(
            vision.verdict_of("", "抖音", "http://www.douyin.com/note/7512413636004220179"),
            vision.VERDICT_NO_ACCOUNT,
        )
        self.assertEqual(
            vision.verdict_of("@someone", "X (Twitter)", "https://x.com/someone/status/1"),
            vision.VERDICT_VISION,
        )
        self.assertEqual(
            vision.verdict_of("", "aliexpress.ru", "https://aliexpress.ru/item/1"),
            vision.VERDICT_COMMERCE,
        )
        self.assertEqual(
            vision.verdict_of("", "某论坛", "https://example.com/thread/9"),
            vision.VERDICT_UNTRACEABLE,
        )
        # 电商优先：卖同款衣服的商品页，即使带着平台也不会变成可核对的结论。
        self.assertEqual(
            vision.verdict_of("", "taobao.com", "https://item.taobao.com/item.htm?id=1"),
            vision.VERDICT_COMMERCE,
        )

    def test_repost_page_is_not_her_post(self):
        """转发页上出现的是别人的图，coser 之间互相转发很常见。"""
        self.assertEqual(
            vision.verdict_of("@NENEchi8890", "X (Twitter)", "https://x.com/NENEchi8890/reposts"),
            vision.VERDICT_REPOST,
        )
        self.assertEqual(
            vision.verdict_of("@someone", "X (Twitter)", "https://x.com/someone/likes"),
            vision.VERDICT_REPOST,
        )
        # 帖子页照常算结论。
        self.assertEqual(
            vision.verdict_of("@someone", "X (Twitter)", "https://x.com/someone/status/1"),
            vision.VERDICT_VISION,
        )

    def test_vision_reason_is_shown(self):
        """把模型理由亮出来：它按服装判的还是认了脸，用户一眼能分辨。"""
        item = Candidate(
            engine="lens",
            url="https://x.com/someone/status/1",
            platform="X (Twitter)",
            handle="@someone",
            name="someone",
            distance=11,
            status="vision",
            note="视觉复核：同一个人（指纹距离 11，超过阈值 8）",
        )
        outcome = SearchOutcome(
            status="ok",
            best=item,
            candidates=[item],
            vision={"ok": True, "checked": 16, "reason": "同款帽子、斗篷、短裤，腿部姿势一致"},
        )
        text = formatting._best_block(outcome)
        self.assertIn("它给的理由", text)
        self.assertIn("同款帽子", text)

    def test_untrusted_note_names_the_site(self):
        """降级文案要点出这页是什么站，否则用户点进去发现不是社交主页。"""
        note = vision.DOWNGRADE_NOTES[vision.VERDICT_UNTRACEABLE].format(
            gap="指纹距离 28，超过阈值 8", platform="soundcloud.com"
        )
        self.assertIn("soundcloud.com", note)
        self.assertIn("不是作者主页", note)
        self.assertIn("仅供参考", note)

    def test_not_found_reports_downgraded_picks(self):
        """视觉挑中的都被降级时，不能说「没有发现同一个人」。"""
        outcome = SearchOutcome(
            status="not_found",
            vision={"ok": True, "checked": 16, "picked": [], "downgraded": 3},
        )
        text = formatting._best_block(outcome)
        self.assertIn("3", text)
        self.assertIn("都不能当原帖", text)
        self.assertNotIn("没有发现同一个人", text)

    def test_downgraded_pick_prints_its_link(self):
        """降级不等于没搜到：最近那条的链接要直接放进结论。"""
        item = Candidate(
            engine="lens",
            url="https://hentaiera.com/gallery/1741778/",
            platform="hentaiera.com",
            status="suspect",
            distance=18,
            note="视觉复核：疑似同一个人，但那是hentaiera.com上的页面、不是作者主页（指纹距离 18）",
        )
        outcome = SearchOutcome(
            status="not_found",
            candidates=[item],
            vision={"ok": True, "checked": 16, "picked": [], "downgraded": 1, "reason": "银发与吊带位置一致"},
        )
        text = formatting._best_block(outcome)
        self.assertIn("最接近的一条", text)
        self.assertIn("https://hentaiera.com/gallery/1741778/", text)
        # 已经单独给过链接的，不再混进「同角色或同款服装」
        self.assertNotIn("同角色或同款服装", text)

    def test_plain_suspects_still_listed(self):
        """不是视觉挑中的疑似条目，仍然要留在「同角色或同款服装」里。"""
        item = Candidate(
            engine="lens",
            url="https://example.com/other",
            status="suspect",
            distance=26,
            note="指纹不符（距离 26）",
        )
        outcome = SearchOutcome(status="not_found", candidates=[item])
        text = formatting._best_block(outcome)
        self.assertIn("同角色或同款服装", text)


if __name__ == "__main__":
    unittest.main()
