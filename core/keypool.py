"""多 key 轮询与冷却。

每个需要 key 的引擎都配一个 KeyPool：轮询取用，某个 key 遇到 401/403/429
或超时就进入冷却，自动换下一个；全部冷却时该引擎本次跳过，不影响其他引擎。

额度用尽（月额度跑完）跟临时失败要分开：前者再试一个月也没用，按自然月记住，
重启也不丢；后者只冷却几分钟。
"""

from __future__ import annotations

import threading
import time

from .keystate import key_id, month_end


class KeyPool:
    """线程安全的 key 轮询池。"""

    def __init__(
        self,
        keys: list[str] | None = None,
        cooldown_seconds: float = 300.0,
        *,
        name: str = "",
        exhausted: dict[str, float] | None = None,
        on_change=None,
    ) -> None:
        cleaned = [str(key).strip() for key in (keys or []) if str(key).strip()]
        self._keys = cleaned
        self._cooldown = float(cooldown_seconds)
        self._lock = threading.Lock()
        self._cursor = 0
        self._cooldown_until: dict[str, float] = {}
        self._failures: dict[str, int] = {}
        self._name = str(name or "")
        self._on_change = on_change
        now = time.time()
        self._exhausted: dict[str, float] = {}
        for kid, until in (exhausted or {}).items():
            try:
                stamp = float(until)
            except (TypeError, ValueError):
                continue
            if stamp > now:
                self._exhausted[str(kid)] = stamp

    @property
    def keys(self) -> list[str]:
        return list(self._keys)

    def __bool__(self) -> bool:
        return bool(self._keys)

    def _usable(self, key: str, now: float) -> bool:
        """额度没用尽、也不在冷却里。"""
        if self._exhausted.get(key_id(key), 0.0) > now:
            return False
        return self._cooldown_until.get(key, 0.0) <= now

    def acquire(self) -> str | None:
        """取一个当前可用的 key；全部冷却或用尽时返回 None。"""
        with self._lock:
            if not self._keys:
                return None
            now = time.time()
            total = len(self._keys)
            for offset in range(total):
                index = (self._cursor + offset) % total
                key = self._keys[index]
                if self._usable(key, now):
                    self._cursor = (index + 1) % total
                    return key
            return None

    def mark_failure(self, key: str, cooldown_seconds: float | None = None) -> None:
        """标记失败并进入冷却，默认 300 秒。"""
        if not key:
            return
        duration = self._cooldown if cooldown_seconds is None else float(cooldown_seconds)
        with self._lock:
            self._failures[key] = self._failures.get(key, 0) + 1
            self._cooldown_until[key] = time.time() + duration

    def mark_exhausted(self, key: str, until: float | None = None) -> None:
        """额度用尽：记到下个月，这期间不再拿它试。"""
        if not key:
            return
        kid = key_id(key)
        deadline = month_end() if until is None else float(until)
        with self._lock:
            self._exhausted[kid] = deadline
            self._cooldown_until.pop(key, None)
            self._failures.pop(key, None)
            snapshot = self._snapshot_locked()
        self._notify(snapshot)

    def is_exhausted(self, key: str) -> bool:
        with self._lock:
            return self._exhausted.get(key_id(key), 0.0) > time.time()

    def mark_success(self, key: str) -> None:
        """成功一次就解除冷却并清零失败计数。"""
        if not key:
            return
        with self._lock:
            self._cooldown_until.pop(key, None)
            self._failures.pop(key, None)

    def snapshot(self) -> dict[str, float]:
        """当前有效的用尽记录（过期的顺手清掉），用于落盘。"""
        with self._lock:
            return self._snapshot_locked()

    def _snapshot_locked(self) -> dict[str, float]:
        now = time.time()
        self._exhausted = {kid: until for kid, until in self._exhausted.items() if until > now}
        return dict(self._exhausted)

    def _notify(self, snapshot: dict[str, float]) -> None:
        if self._on_change is None:
            return
        try:
            self._on_change(self._name, snapshot)
        except Exception:  # noqa: BLE001 - 记不住也不影响本次搜索
            pass

    def available_count(self) -> int:
        with self._lock:
            now = time.time()
            return sum(1 for key in self._keys if self._usable(key, now))

    def cooldown_remaining(self, key: str) -> float:
        with self._lock:
            return max(0.0, self._cooldown_until.get(key, 0.0) - time.time())
