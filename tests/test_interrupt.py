"""重启打断兜底：卸载时记下在跑的单，重新加载后回群里一句。

背景：插件运行时是整块重启的，任何插件文件被改动都会触发。进程一换，
正在搜的那一单连同 asyncio 任务一起消失，群里只看到「正在搜图」，
之后什么都没了（11-13 20:25 那单就是这么消失的）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import tempfile
import time
import types
import unittest
from pathlib import Path

from tests.test_plugin_import import import_plugin


class _Send:
    """记录往外发了什么，顺便能模拟发送失败。"""

    def __init__(self, ok: bool = True) -> None:
        self.sent: list[tuple[str, str]] = []
        self.ok = ok

    async def text(self, text: str, stream_id: str = "") -> object:
        self.sent.append((text, stream_id))
        return True if self.ok else None


class InterruptedTaskTest(unittest.TestCase):
    def setUp(self) -> None:
        self.module = import_plugin()
        # 正常运行时等 5 秒等 send 通道就绪；测试里没必要真等
        self._delay = self.module._INTERRUPTED_DELAY
        self.module._INTERRUPTED_DELAY = 0
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self.module._INTERRUPTED_DELAY = self._delay
        self._tmp.cleanup()

    def _make(self, send: _Send | None = None):
        plugin = self.module.ImageSearcherPlugin()
        # ctx 是基类的只读属性，背后读的是 _ctx
        plugin._ctx = types.SimpleNamespace(
            paths=types.SimpleNamespace(data_dir=str(self.dir)),
            logger=logging.getLogger("test.interrupt"),
            send=send or _Send(),
        )
        return plugin

    def _file(self) -> Path:
        return self.dir / self.module._INTERRUPTED_FILE

    def test_save_writes_running_task(self) -> None:
        plugin = self._make()
        plugin._live["s-20261113-2025-abcd"] = {
            "stream_id": "06c47b",
            "user_id": "641968394",
            "started_at": time.time() - 120,
        }
        plugin._save_interrupted()

        payload = json.loads(self._file().read_text(encoding="utf-8"))
        self.assertEqual(list(payload), ["s-20261113-2025-abcd"])
        self.assertEqual(payload["s-20261113-2025-abcd"]["stream_id"], "06c47b")
        # 卸载后内存表要清掉，免得下次卸载又记一遍
        self.assertEqual(plugin._live, {})

    def test_save_skips_when_nothing_running(self) -> None:
        plugin = self._make()
        plugin._save_interrupted()
        self.assertFalse(self._file().exists())

    def test_save_drops_stale_task(self) -> None:
        """两小时以前的单不再提示：人早忘了这回事，补一句只会更懵。"""
        plugin = self._make()
        plugin._live["old"] = {
            "stream_id": "06c47b",
            "user_id": "1",
            "started_at": time.time() - self.module._INTERRUPTED_MAX_AGE - 60,
        }
        plugin._save_interrupted()
        self.assertFalse(self._file().exists())

    def test_notify_sends_and_clears(self) -> None:
        send = _Send()
        plugin = self._make(send)
        plugin._live["s-20261113-2025-abcd"] = {
            "stream_id": "06c47b",
            "user_id": "641968394",
            "started_at": time.time() - 600,
        }
        plugin._save_interrupted()
        asyncio.run(plugin._notify_interrupted())

        self.assertEqual(len(send.sent), 1)
        text, stream = send.sent[0]
        self.assertEqual(stream, "06c47b")
        self.assertIn("s-20261113-2025-abcd", text)
        self.assertIn("被打断", text)
        # 发完就把记录删掉，别每次重载都翻旧账
        self.assertFalse(self._file().exists())

    def test_notify_without_file_does_nothing(self) -> None:
        send = _Send()
        plugin = self._make(send)
        asyncio.run(plugin._notify_interrupted())
        self.assertEqual(send.sent, [])

    def test_notify_send_failure_still_clears(self) -> None:
        """发送失败也不留记录：留着只会让下次重载重复提示同一件事。"""
        plugin = self._make(_Send(ok=False))
        plugin._live["t"] = {"stream_id": "s", "user_id": "u", "started_at": time.time()}
        plugin._save_interrupted()
        asyncio.run(plugin._notify_interrupted())
        self.assertFalse(self._file().exists())


if __name__ == "__main__":
    unittest.main()
