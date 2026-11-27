"""域名白名单匹配：``core/hosts.py`` 是唯一一处规则。

这条规则在插件里被抄过九遍（社交平台、图集站、搬运站、论坛、电商页、付费图包站、
停服站、搜索页、直连名单），每抄一遍就多一点分叉的机会。这里把它钉住，顺便钉住
两条踩过的坑：``x.com.evil.example`` 不是 ``x.com``，``de.hentai-cosplay-xxx.com``
也不是——早期用 ``"x.com" in host`` 把带 xxx 的资源站当成了 X。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import hosts  # noqa: E402
from core.hosts import host_of, is_host, matched_token  # noqa: E402

CORE = Path(__file__).resolve().parents[1] / "core"
X_HOSTS = ("x.com", "twitter.com")


class HostOfTest(unittest.TestCase):
    """取域名：链接与裸域名都要认，去掉账号、端口与大小写。"""

    def test_url_and_bare_host(self) -> None:
        self.assertEqual(host_of("https://www.pixiv.net/artworks/145241123"), "www.pixiv.net")
        self.assertEqual(host_of("kurocore.com/illust/145241123"), "kurocore.com")
        self.assertEqual(host_of("bbs.nga.cn"), "bbs.nga.cn")

    def test_strips_userinfo_and_port(self) -> None:
        self.assertEqual(host_of("https://user:pw@x.com:443/a"), "x.com")

    def test_empty(self) -> None:
        self.assertEqual(host_of(""), "")
        self.assertEqual(host_of("   "), "")


class IsHostTest(unittest.TestCase):
    def test_exact_and_subdomain(self) -> None:
        self.assertTrue(is_host("x.com", X_HOSTS))
        self.assertTrue(is_host("mobile.x.com", X_HOSTS))
        self.assertTrue(is_host("https://twitter.com/a/status/1", X_HOSTS))

    def test_lookalike_is_not_the_same_site(self) -> None:
        # 这两条是实测踩出来的：后缀必须带点，不能是子串
        self.assertFalse(is_host("x.com.evil.example", X_HOSTS))
        self.assertFalse(is_host("de.hentai-cosplay-xxx.com", X_HOSTS))
        self.assertFalse(is_host("notx.com", X_HOSTS))

    def test_empty_list_and_empty_value(self) -> None:
        self.assertFalse(is_host("x.com", ()))
        self.assertFalse(is_host("", X_HOSTS))

    def test_tokens_are_case_insensitive(self) -> None:
        self.assertTrue(is_host("kurocore.com", ("KuroCore.com",)))

    def test_platform_label_is_not_a_domain(self) -> None:
        """候选的 platform 字段常常是「X (Twitter)」这种标签，不能被当成域名。"""
        self.assertFalse(is_host("X (Twitter)", X_HOSTS))
        self.assertFalse(is_host("Instagram", ("instagram.com",)))


class MatchedTokenTest(unittest.TestCase):
    def test_returns_the_token_itself(self) -> None:
        # 停服站要拿命中项查站名、付费图包站要把品牌名写进提示
        self.assertEqual(matched_token("bcy.net", {"bcy.net": "半次元"}), "bcy.net")
        self.assertEqual(matched_token("https://m.bcy.net/a", {"bcy.net": "半次元"}), "bcy.net")
        self.assertEqual(matched_token("example.com", {"bcy.net": "半次元"}), "")


class NoModuleKeepsItsOwnCopyTest(unittest.TestCase):
    """规则只许有一份：core 里除了 hosts.py 不该再出现那行判断。"""

    def test_rule_lives_only_in_hosts(self) -> None:
        offenders: list[str] = []
        for path in sorted(CORE.glob("*.py")):
            if path.name == "hosts.py":
                continue
            source = path.read_text(encoding="utf-8")
            if 'endswith("." +' in source:
                offenders.append(path.name)
        self.assertEqual(offenders, [], "这些模块里又抄了一份域名后缀判断，改用 core.hosts.is_host()")

    def test_names_do_not_collide(self) -> None:
        """`weibopost` 里那份名单是「正文抄自微博的新闻站」，不是图集 / 搬运站。"""
        from core import gallery, weibopost

        self.assertTrue(hasattr(gallery, "REPOST_HOSTS"))
        self.assertFalse(hasattr(weibopost, "REPOST_HOSTS"))

    def test_hosts_module_has_no_dependencies(self) -> None:
        """hosts 是叶子模块：谁都能引它，它不引别人，别在这里加业务判断。"""
        source = (CORE / "hosts.py").read_text(encoding="utf-8")
        self.assertNotIn("from . import", source)
        self.assertNotIn("from .extract", source)
        self.assertIsNotNone(hosts.host_of)


if __name__ == "__main__":
    unittest.main()
