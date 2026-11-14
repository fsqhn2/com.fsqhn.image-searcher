"""key 额度用尽的记忆。

有的 key 月额度用完以后，再拿它请求也只是白发一次：服务端要回一次 429/错误，
插件这边还要多等一个来回。这类状态按自然月记住，写进 data 目录，重启也不丢。

key 本身不落盘，只存 sha256 的前 16 位——文件泄露也拿不到可用 key。
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import time

FILE_NAME = "key_quota.json"
# 跨月缓冲：SerpAPI 按 UTC 自然月重置，多等一天，免得月初边界上白试。
_RESET_BUFFER_SECONDS = 86400


def key_id(key: str) -> str:
    """key 的短哈希，用来落盘。"""
    return hashlib.sha256(str(key).encode("utf-8")).hexdigest()[:16]


def month_end(now: float | None = None) -> float:
    """下一个自然月的起点（UTC）+ 一天缓冲。"""
    stamp = time.time() if now is None else float(now)
    current = datetime.datetime.fromtimestamp(stamp, datetime.timezone.utc)
    if current.month == 12:
        nxt = datetime.datetime(current.year + 1, 1, 1, tzinfo=datetime.timezone.utc)
    else:
        nxt = datetime.datetime(current.year, current.month + 1, 1, tzinfo=datetime.timezone.utc)
    return nxt.timestamp() + _RESET_BUFFER_SECONDS


def _path(data_dir: str) -> str:
    return os.path.join(data_dir, FILE_NAME)


def load(data_dir: str) -> dict:
    """读回 {池名: {key 哈希: 到期时间戳}}，过期的条目顺手丢掉。"""
    path = _path(data_dir)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    now = time.time()
    out: dict[str, dict[str, float]] = {}
    for pool, entries in raw.items():
        if not isinstance(entries, dict):
            continue
        kept = {}
        for kid, until in entries.items():
            try:
                until = float(until)
            except (TypeError, ValueError):
                continue
            if until > now and isinstance(kid, str) and kid:
                kept[kid] = until
        if kept:
            out[str(pool)] = kept
    return out


def save(data_dir: str, state: dict) -> None:
    """原子写回，失败只当没记住，不影响搜索。"""
    path = _path(data_dir)
    clean = {
        str(pool): {str(kid): float(until) for kid, until in (entries or {}).items()}
        for pool, entries in (state or {}).items()
        if entries
    }
    tmp = path + ".tmp"
    try:
        os.makedirs(data_dir, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(clean, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
