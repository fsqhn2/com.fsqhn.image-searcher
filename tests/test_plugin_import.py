"""插件本体至少得能导入。

这一步是给「测试全绿但插件根本加载不起来」兜底的：0.7.76 上线时
``plugin.py`` 里有个方法定义被挤成一行（``def …:        \"\"\"…``），
core 的 504 项测试一个都不碰 plugin.py，全过了，可插件在服务器上直接
`unexpected indent` 起不来。所以这里按包名导入一次，语法或缩进有问题当场报。
"""

from __future__ import annotations

import importlib
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def import_plugin():
    """plugin.py 用的是包内相对导入，必须挂在包名下面导入。"""
    if "isplug" not in sys.modules:
        package = types.ModuleType("isplug")
        package.__path__ = [str(ROOT)]
        sys.modules["isplug"] = package
    return importlib.import_module("isplug.plugin")


class PluginImportTest(unittest.TestCase):
    def test_imports_and_exposes_plugin_class(self) -> None:
        module = import_plugin()
        self.assertTrue(hasattr(module, "ImageSearcherPlugin"))

    def test_paywall_helpers_wired(self) -> None:
        module = import_plugin()
        plugin_class = module.ImageSearcherPlugin
        self.assertTrue(hasattr(plugin_class, "_track_paywall"))
        self.assertTrue(hasattr(plugin_class, "_patreon_track"))
        self.assertTrue(hasattr(plugin_class, "_fanbox_track"))


if __name__ == "__main__":
    unittest.main()
