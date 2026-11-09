"""指令正则的回归测试：带图消息的前置占位符不能挡住匹配。"""

from __future__ import annotations

import re
import unittest

from core.commands import build_command_pattern, build_more_pattern, split_force


class CommandPatternTest(unittest.TestCase):
    def setUp(self) -> None:
        self.pattern = re.compile(build_command_pattern(["麦麦寻人", "找鸡"]))
        self.more = re.compile(build_more_pattern(["更多", "more"]))

    def test_plain(self) -> None:
        match = self.pattern.search("麦麦寻人")
        self.assertIsNotNone(match)
        self.assertIsNone(match.groupdict()["arg"])

    def test_with_image_placeholder(self) -> None:
        match = self.pattern.search("[image] 麦麦寻人")
        self.assertIsNotNone(match)
        self.assertIsNone(match.groupdict()["arg"])

    def test_leading_space_from_image_segment(self) -> None:
        """图 + 指令同条消息时，纯文本是「 麦麦寻人」——图片段被吞掉只剩前导空格。"""
        match = self.pattern.search(" 麦麦寻人")
        self.assertIsNotNone(match)
        self.assertIsNone(match.groupdict()["arg"])

    def test_leading_space_with_argument(self) -> None:
        match = self.pattern.search("   麦麦寻人 重搜")
        self.assertIsNotNone(match)
        self.assertEqual(match.groupdict()["arg"], "重搜")

    def test_leading_newline(self) -> None:
        self.assertIsNotNone(self.pattern.search("\n麦麦寻人"))

    def test_more_with_leading_space(self) -> None:
        match = self.more.search(" 更多 s-20261103-1631-4c8e")
        self.assertIsNotNone(match)
        self.assertEqual(match.groupdict()["task"], "s-20261103-1631-4c8e")

    def test_with_image_and_argument(self) -> None:
        match = self.pattern.search("[image] 麦麦寻人 更多")
        self.assertIsNotNone(match)
        self.assertEqual(match.groupdict()["arg"], "更多")

    def test_multiple_placeholders(self) -> None:
        self.assertIsNotNone(self.pattern.search("[image] [face] 麦麦寻人"))

    def test_quoted_message_placeholder(self) -> None:
        """引用消息时占位符里套方括号，不能在内层右括号处截断。"""
        text = "[回复了男的也能当娘吗？的消息: [image]] 麦麦寻人"
        match = self.pattern.search(text)
        self.assertIsNotNone(match)
        self.assertIsNone(match.groupdict()["arg"])

    def test_quoted_message_with_nested_task(self) -> None:
        text = "[回复了某人的消息: [image]] 更多 s-20261103-0832-d7d9"
        match = self.more.search(text)
        self.assertIsNotNone(match)
        self.assertEqual(match.groupdict()["task"], "s-20261103-0832-d7d9")

    def test_with_bot_mention_after_quote(self) -> None:
        """@机器人 再写指令：引用占位符后面压着「@盆地」，不能被当成多余内容。"""
        text = "[回复了山上的小丘的消息: [image]] @盆地  麦麦寻人"
        match = self.pattern.search(text)
        self.assertIsNotNone(match)
        self.assertIsNone(match.groupdict()["arg"])

    def test_with_bot_mention_alone(self) -> None:
        match = self.pattern.search("@盆地 麦麦寻人")
        self.assertIsNotNone(match)
        self.assertIsNone(match.groupdict()["arg"])

    def test_with_bot_mention_and_argument(self) -> None:
        match = self.pattern.search("@盆地  麦麦寻人 重搜")
        self.assertIsNotNone(match)
        self.assertEqual(match.groupdict()["arg"], "重搜")

    def test_more_with_bot_mention(self) -> None:
        match = self.more.search("[回复了某人的消息: [image]] @盆地 更多 s-20261103-0832-d7d9")
        self.assertIsNotNone(match)
        self.assertEqual(match.groupdict()["task"], "s-20261103-0832-d7d9")

    def test_mention_without_command_does_not_match(self) -> None:
        self.assertIsNone(self.pattern.search("@盆地 在吗"))

    def test_alias(self) -> None:
        self.assertIsNotNone(self.pattern.search("找鸡"))

    def test_not_matched_in_sentence(self) -> None:
        self.assertIsNone(self.pattern.search("今天用麦麦寻人试了一下"))

    def test_more_with_placeholder(self) -> None:
        self.assertIsNotNone(self.more.search("[image] 更多"))

    def test_more_with_task_id(self) -> None:
        """「更多 + 任务编号」要能取出编号；裸「更多」保持原来的用法。"""
        match = self.more.search("更多 s-20261103-0832-d7d9")
        self.assertIsNotNone(match)
        self.assertEqual(match.groupdict()["task"], "s-20261103-0832-d7d9")
        plain = self.more.search("更多")
        self.assertIsNotNone(plain)
        self.assertIsNone(plain.groupdict()["task"])

    def test_more_does_not_swallow_sentences(self) -> None:
        self.assertIsNone(self.more.search("更多这个我不知道"))


class SplitForceTest(unittest.TestCase):
    """「重搜」前缀解析：无视缓存、剩余参数照旧。"""

    def test_plain_arg(self) -> None:
        self.assertEqual(split_force("更多"), (False, "更多"))

    def test_force_alone(self) -> None:
        self.assertEqual(split_force("重搜"), (True, ""))

    def test_force_with_rest(self) -> None:
        self.assertEqual(split_force("重搜 更多 s-1"), (True, "更多 s-1"))

    def test_english_alias(self) -> None:
        self.assertEqual(split_force("force"), (True, ""))

    def test_not_force(self) -> None:
        self.assertEqual(split_force("状态"), (False, "状态"))
        self.assertEqual(split_force(""), (False, ""))


if __name__ == "__main__":
    unittest.main()
