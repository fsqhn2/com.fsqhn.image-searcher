"""额度用尽记忆的测试：落盘格式、跨月失效、轮询跳过。"""
from __future__ import annotations

import datetime
import os
import tempfile
import time
import unittest

from core import keystate
from core.keypool import KeyPool


def _utc(year: int, month: int, day: int) -> float:
    return datetime.datetime(year, month, day, tzinfo=datetime.timezone.utc).timestamp()


def _utc_of(stamp: float) -> tuple[int, int, int]:
    got = datetime.datetime.fromtimestamp(stamp, datetime.timezone.utc)
    return got.year, got.month, got.day


class KeystateTest(unittest.TestCase):
    def test_key_id_is_stable_and_hides_key(self) -> None:
        value = keystate.key_id("serpapi-secret-key")
        self.assertEqual(value, keystate.key_id("serpapi-secret-key"))
        self.assertNotEqual(value, keystate.key_id("another-key"))
        self.assertEqual(len(value), 16)
        self.assertNotIn("secret", value)

    def test_month_end_crosses_year(self) -> None:
        # 12 月 → 次年 1 月 1 日 + 一天缓冲
        self.assertEqual(_utc_of(keystate.month_end(_utc(2026, 12, 15))), (2027, 1, 2))

    def test_month_end_rolls_to_next_month(self) -> None:
        self.assertEqual(_utc_of(keystate.month_end(_utc(2026, 11, 14))), (2026, 12, 2))

    def test_renewal_deadline_follows_the_key_own_date(self) -> None:
        """SerpAPI 免费档按各把 key 的开号日重置，不是每月 1 号。"""
        stamp = keystate.renewal_deadline("2026-11-22")
        self.assertIsNotNone(stamp)
        self.assertEqual(_utc_of(stamp), (2026, 11, 22))
        self.assertGreater(stamp, _utc(2026, 11, 22))

    def test_renewal_deadline_rejects_garbage(self) -> None:
        for text in ("", "   ", "unknown", "22/11/2026", None):
            self.assertIsNone(keystate.renewal_deadline(text), text)

    def test_load_drops_expired_and_tolerates_garbage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, keystate.FILE_NAME)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write('{"lens": {"alive": %s, "dead": %s}, "bad": 3}' % (time.time() + 3600, time.time() - 10))
            state = keystate.load(tmp)
            self.assertEqual(list(state), ["lens"])
            self.assertEqual(list(state["lens"]), ["alive"])

    def test_load_missing_or_broken_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(keystate.load(tmp), {})
            with open(os.path.join(tmp, keystate.FILE_NAME), "w", encoding="utf-8") as fh:
                fh.write("not json")
            self.assertEqual(keystate.load(tmp), {})

    def test_save_then_load_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            until = time.time() + 3600
            keystate.save(tmp, {"lens": {"abc": until}})
            self.assertEqual(keystate.load(tmp), {"lens": {"abc": until}})


class KeyPoolQuotaTest(unittest.TestCase):
    def test_exhausted_key_is_skipped(self) -> None:
        keys = ["key-a", "key-b"]
        pool = KeyPool(keys, exhausted={keystate.key_id("key-a"): time.time() + 3600})
        picked = {pool.acquire() for _ in range(4)}
        self.assertEqual(picked, {"key-b"})
        self.assertTrue(pool.is_exhausted("key-a"))
        self.assertEqual(pool.available_count(), 1)

    def test_expired_record_lets_key_back(self) -> None:
        key = "key-a"
        pool = KeyPool([key], exhausted={keystate.key_id(key): time.time() + 3600})
        self.assertIsNone(pool.acquire())
        pool._exhausted[keystate.key_id(key)] = time.time() - 1
        self.assertEqual(pool.acquire(), key)

    def test_mark_exhausted_notifies_and_clears_cooldown(self) -> None:
        key = "key-a"
        seen: list[tuple[str, dict]] = []
        pool = KeyPool([key], name="lens", on_change=lambda name, snap: seen.append((name, snap)))
        pool.mark_failure(key)
        self.assertTrue(pool.cooldown_remaining(key) > 0)
        until = time.time() + 3600
        pool.mark_exhausted(key, until=until)
        self.assertEqual(pool.cooldown_remaining(key), 0)
        self.assertIsNone(pool.acquire())
        self.assertEqual(seen[-1][0], "lens")
        self.assertEqual(seen[-1][1], {keystate.key_id(key): until})

    def test_snapshot_drops_expired(self) -> None:
        pool = KeyPool(["key-a"])
        pool._exhausted[keystate.key_id("key-a")] = time.time() - 1
        self.assertEqual(pool.snapshot(), {})

    def test_callback_failure_does_not_break_pool(self) -> None:
        def boom(name, snapshot):
            raise RuntimeError("disk full")

        pool = KeyPool(["key-a"], name="lens", on_change=boom)
        pool.mark_exhausted("key-a", until=time.time() + 60)
        self.assertIsNone(pool.acquire())


if __name__ == "__main__":
    unittest.main()
