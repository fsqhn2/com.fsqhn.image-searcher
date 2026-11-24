"""重读水印的并行账：几路一起读，结论按优先级落，额度不超。

起因是 `s-20261123-0034-19ab`：同一套图有三个距离 0 的候选，串行一路路读完，
光读水印就花了七分钟。改成同一道里并发读之后，这组用例守四件事：

1. 几路真的一起跑（不是排着队假装并行）；
2. 命中按优先级并账——先读到的那一路不改写更优先那一路的结论；
3. 一路命中就收工，后面的定位、切块不再花钱；
4. 预算是锁着记的，并发也不会超。
"""

from __future__ import annotations

import asyncio
import logging
import types
import unittest
from unittest import mock

from test_plugin_import import import_plugin


def _plugin(*, tiles: bool, locate: bool, trace: bool = True):
    """照 `test_name_hunt.py` 的写法建一个不跑生命周期的插件实例。"""
    module = import_plugin()
    plugin = object.__new__(module.ImageSearcherPlugin)
    plugin._ctx = types.SimpleNamespace(logger=logging.getLogger("test"))
    config = mock.Mock()
    config.plugin.verbose_log = False
    config.plugin.hash_threshold = 8
    config.vision.enabled = True
    config.vision.watermark_tiles = tiles
    config.vision.deep_trace = trace
    config.vision.watermark_locate = locate
    plugin._plugin_config_instance = config
    try:
        plugin.config = config
    except AttributeError:
        pass
    return module, plugin, config


def _candidates(module, count: int):
    return [
        module.Candidate(
            engine="lens",
            url=f"https://example{index}.com/post",
            image_url=f"https://img.example{index}.com/a.jpg",
            distance=0,
            status="match",
        )
        for index in range(count)
    ]


def _reading(label: str, post: str = "") -> dict:
    return {
        "source": f"vision-{label}" if label else "vision-original",
        "raw": f"@{label or 'orig'}",
        "handle": label or "orig",
        "url": f"https://x.com/{label or 'orig'}",
        "state": "ok",
        "post": post,
        "distance": 0 if post else None,
        "note": "",
    }


def _install_reads(plugin, behavior, calls=None):
    """把 `_reread_one` 换成一个不碰网络的假读：behavior(label) 决定读到了什么。"""

    async def fake(info, readings, known, source, *, data_url="", label="", verify=None):
        if calls is not None:
            calls.append(label)
        await asyncio.sleep(0)
        got = behavior(label) or {}
        if got.get("raw"):
            info["watermark"] = got["raw"]
        if got.get("reading"):
            readings.append(got["reading"])
        return bool(got.get("hit"))

    plugin._reread_one = fake


def _install_download(plugin):
    async def fake(url: str):
        await asyncio.sleep(0)
        return f"bytes:{url}".encode()

    plugin._download = fake


class WatermarkParallelTest(unittest.TestCase):
    def _run(self, plugin, module, upload: bytes = b"orig", ordered=None, **kwargs):
        vision_info: dict = {}
        asyncio.run(
            plugin._reread_watermark(
                vision_info,
                None,
                upload,
                ordered if ordered is not None else [],
                **kwargs,
            )
        )
        return vision_info

    def test_whole_reads_run_at_the_same_time(self) -> None:
        """三道整图读必须真的并发：串行时这一条会卡到超时。"""
        module, plugin, _config = _plugin(tiles=False, locate=False)
        _install_download(plugin)
        started = 0
        peak = 0
        all_started = asyncio.Event()

        async def fake(info, readings, known, source, *, data_url="", label="", verify=None):
            nonlocal started, peak
            started += 1
            peak = max(peak, started)
            if started >= 3:
                all_started.set()
            await asyncio.wait_for(all_started.wait(), timeout=1.0)
            started -= 1
            return False

        plugin._reread_one = fake
        self._run(plugin, module, ordered=_candidates(module, 2))
        self.assertEqual(peak, 3, "原图 + 两张候选图应该同时开读")

    def test_hit_is_absorbed_by_priority(self) -> None:
        """三路都读到东西时，最优先那一路的帖子落地，后面的只留读法。"""
        module, plugin, _config = _plugin(tiles=False, locate=False)

        def behavior(label):
            if not label:
                return {"raw": "", "hit": False}
            if label == "候选1":
                return {"raw": "候选1", "reading": _reading("候选1", "p1"), "hit": True}
            return {"raw": "候选2", "reading": _reading("候选2", "p2"), "hit": True}

        _install_download(plugin)
        _install_reads(plugin, behavior)
        info = self._run(plugin, module, ordered=_candidates(module, 2), prefer_candidates=True)
        self.assertEqual(info.get("watermark_post"), "p1")
        self.assertEqual([item["handle"] for item in info["watermark_readings"]], ["候选1", "候选2"])

    def test_whole_hit_skips_locate_and_tiles(self) -> None:
        """整图有一路读到，定位与切块一次都不该跑。"""
        module, plugin, _config = _plugin(tiles=True, locate=True)
        calls: list[str] = []
        located: list[str] = []
        tiles: list[str] = []

        _install_download(plugin)
        _install_reads(plugin, lambda label: {"reading": _reading(label, "p1"), "hit": True}, calls)

        async def fake_locate(source, label, read):
            located.append(label)
            return False

        plugin._locate_and_read = fake_locate
        with mock.patch.object(
            module.vision, "tile_data_urls", side_effect=lambda source: tiles.append("tile") or []
        ):
            self._run(plugin, module, ordered=_candidates(module, 2), prefer_candidates=True)
        self.assertEqual(located, [])
        self.assertEqual(tiles, [])
        self.assertEqual(len(calls), 3, "原图 + 两张候选图各读一次")

    def test_locate_stage_runs_only_when_whole_read_is_empty(self) -> None:
        """整图都读空才轮到定位；定位命中就不切块。"""
        module, plugin, _config = _plugin(tiles=True, locate=True)
        located: list[str] = []
        tiles: list[str] = []
        _install_download(plugin)
        _install_reads(plugin, lambda label: {"hit": False})

        async def fake_locate(source, label, read):
            located.append(label)
            return label == "候选1"

        plugin._locate_and_read = fake_locate
        with mock.patch.object(
            module.vision, "tile_data_urls", side_effect=lambda source: tiles.append("tile") or []
        ):
            self._run(plugin, module, ordered=_candidates(module, 2), prefer_candidates=True)
        self.assertEqual(located, ["候选1", "候选2"], "定位阶段最多读 _WATERMARK_LOCATE_LIMIT 张")
        self.assertEqual(tiles, [])

    def test_tiles_run_in_parallel_per_image(self) -> None:
        """切块按张并行读；这一张有一套块命中，后面的图就不再切。"""
        module, plugin, _config = _plugin(tiles=True, locate=False)
        _install_download(plugin)
        seen: list[str] = []
        _install_reads(
            plugin,
            lambda label: {"reading": _reading(label), "hit": label.endswith("-t2")},
            seen,
        )
        with mock.patch.object(
            module.vision,
            "tile_data_urls",
            side_effect=lambda source: [(f"t{index}", f"data-{index}") for index in range(4)],
        ):
            self._run(plugin, module, ordered=_candidates(module, 1), prefer_candidates=True)
        self.assertEqual(
            len([label for label in seen if label.startswith("候选1-")]), 4, "这张图的块一起读"
        )
        self.assertEqual(
            len([label for label in seen if label.startswith("原图-")]), 0, "命中后不再切下一张"
        )

    def test_budget_is_not_exceeded_under_concurrency(self) -> None:
        """几路一起开跑也要锁着记账：总视觉调用不超 _WATERMARK_REREAD_BUDGET。"""
        module, plugin, _config = _plugin(tiles=True, locate=True)
        calls: list[str] = []
        _install_download(plugin)
        _install_reads(plugin, lambda label: {"hit": False}, calls)

        async def fake_locate(source, label, read):
            return await read(source, f"{label}-loc")

        plugin._locate_and_read = fake_locate
        with mock.patch.object(plugin, "_WATERMARK_REREAD_BUDGET", 3), mock.patch.object(
            module.vision,
            "tile_data_urls",
            side_effect=lambda source: [(f"t{index}", f"data-{index}") for index in range(6)],
        ):
            self._run(plugin, module, ordered=_candidates(module, 3), prefer_candidates=True)
        self.assertLessEqual(len(calls), 3)
        self.assertEqual(len(calls), 3, "额度没花完说明预算被并发绕过了")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
