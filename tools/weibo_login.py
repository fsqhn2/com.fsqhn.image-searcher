# -*- coding: utf-8 -*-
"""微博扫码登录：出一张二维码，等你用手机微博 App 扫，成功后把 cookie 写下来。

这一步只在「想用登录态」时做一次。不登录也能核微博主页和粉丝数，所以它是可选
项——见 config.toml 里的 ``[weibo] cookie``。

用法（参数全是默认值时可省）：

    python tools/weibo_login.py --qr C:\\temp\\weibo_qr.png --out C:\\temp\\weibo_cookie.txt

跑起来会立刻把二维码存成 PNG，然后开始等扫码。全程把进展写进 ``--log``（默认跟
二维码同目录），因为 Windows 控制台的中文编码不值得信任。

流程就是网页版那一套：取二维码 → 轮询扫码状态 → 拿到 alt 后换 sso 票据 → 收
SUB/SUBP 这些 cookie。
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import sys
import time
from pathlib import Path

import requests

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
)
LOGIN_URL = "https://login.sina.com.cn/sso/qrcode/image"
CHECK_URL = "https://login.sina.com.cn/sso/qrcode/check"

# 微博扫码接口的状态码
WAITING = "50114001"  # 二维码没被扫
SCANNED = "50114002"  # 扫了，还没在手机上点确认
EXPIRED = "50114004"  # 二维码失效
OK = "20000000"  # 确认登录

# 真正有用的是这几个：SUB/SUBP 是登录态，ALF 是过期时间。
WANTED = ("SUB", "SUBP", "ALF", "SUBP", "SRF", "SRT", "SSOLoginState")


def jsonp(text: str) -> dict:
    """接口回的是 ``STK_1794540167234({...});``，把中间那段 JSON 抠出来。"""
    start = text.find("(")
    end = text.rfind(")")
    if start < 0 or end <= start:
        raise ValueError(f"不是 JSONP：{text[:80]}")
    return json.loads(text[start + 1 : end])


def start(ts: str) -> tuple[str, bytes]:
    """取一张新二维码，返回 (qrid, PNG 字节)。

    ``data.image`` 有两种形态：老接口给 base64，现在线上给的是一个 ``//v2.qr.weibo.cn``
    的图片地址（实测 2026-11-13）。两种都认，别把地址当 base64 解出来一堆乱码。
    """
    response = requests.get(
        LOGIN_URL,
        params={"entry": "weibo", "size": "180", "callback": f"STK_{ts}"},
        headers={"User-Agent": UA, "Referer": "https://weibo.com/"},
        timeout=20,
    )
    payload = jsonp(response.text)
    data = payload.get("data") or {}
    qrid = str(data.get("qrid") or "")
    image = str(data.get("image") or "")
    if not qrid or not image:
        raise ValueError(f"二维码接口没给全：{str(payload)[:120]}")
    if image.startswith("//"):
        image = "https:" + image
    if image.startswith("http"):
        png = requests.get(
            image, headers={"User-Agent": UA, "Referer": "https://weibo.com/"}, timeout=20
        ).content
    else:
        png = base64.b64decode(re.sub(r"^data:image/\w+;base64,", "", image))
    if not png.startswith(b"\x89PNG"):
        raise ValueError(f"拿到的不是 PNG（{len(png)} 字节）：{image[:80]}")
    return qrid, png


def poll(qrid: str, ts: str) -> dict:
    response = requests.get(
        CHECK_URL,
        params={"entry": "weibo", "callback": f"STK_{ts}", "qrid": qrid, "rid": ts},
        headers={"User-Agent": UA, "Referer": "https://weibo.com/"},
        timeout=20,
    )
    return (jsonp(response.text).get("data") or {})


def finish(session: requests.Session, alt: str) -> str:
    """拿 alt 换登录票据，收 SUB/SUBP。"""
    if alt:
        url = alt if alt.startswith("http") else "https://login.sina.com.cn" + alt
        session.get(url, headers={"User-Agent": UA, "Referer": "https://weibo.com/"}, timeout=20)
    # 有些情况下 Set-Cookie 落在 passport 的跳转链上，再踩一次首页把它引出来。
    session.get("https://weibo.com/", headers={"User-Agent": UA}, timeout=20)
    if "SUB" not in session.cookies.keys():
        session.get(
            "https://passport.weibo.com/wbsso/login",
            params={"entry": "miniblog", "url": "https://weibo.com/"},
            headers={"User-Agent": UA, "Referer": "https://weibo.com/"},
            timeout=20,
        )
    parts: list[str] = []
    seen: set[str] = set()
    for cookie in session.cookies:
        if cookie.name in seen or cookie.name not in WANTED:
            continue
        seen.add(cookie.name)
        parts.append(f"{cookie.name}={cookie.value}")
    return "; ".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--qr", default=r"C:\temp\weibo_qr.png")
    parser.add_argument("--out", default=r"C:\temp\weibo_cookie.txt")
    parser.add_argument("--log", default=r"C:\temp\weibo_login.log")
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()

    log_path = Path(args.log)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("", encoding="utf-8")

    def log(text: str) -> None:
        line = f"{time.strftime('%H:%M:%S')} {text}"
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        print(line)

    session = requests.Session()
    ts = str(int(time.time() * 1000))
    deadline = time.time() + max(30, args.timeout)
    try:
        qrid, png = start(ts)
    except Exception as error:  # noqa: BLE001
        log(f"取二维码失败：{error}")
        return 1
    qr_path = Path(args.qr)
    qr_path.parent.mkdir(parents=True, exist_ok=True)
    qr_path.write_bytes(png)
    log(f"二维码已写入 {qr_path}（qrid={qrid}），等你扫码")

    state = ""
    alt = ""
    while time.time() < deadline:
        time.sleep(2)
        try:
            data = poll(qrid, ts)
        except Exception as error:  # noqa: BLE001
            log(f"轮询出错（继续等）：{error}")
            continue
        code = str(data.get("retcode") or "")
        if code != state:
            state = code
            words = {
                WAITING: "还没扫",
                SCANNED: "扫了，等你在手机上点确认",
                EXPIRED: "二维码失效了，重跑一次脚本",
            }
            log(f"状态 {code}：{words.get(code, '未知')}")
        if code == OK:
            alt = str(data.get("alt") or "")
            log("手机上确认了，正在换登录票据")
            break
        if code == EXPIRED:
            log("二维码过期，脚本退出")
            return 2

    if not alt:
        log("等待超时，没拿到登录态")
        return 3

    cookie = finish(session, alt)
    if not cookie or "SUB=" not in cookie:
        log(f"票据换了，但没收到 SUB cookie；收到的是：{sorted(session.cookies.keys())}")
        return 4
    Path(args.out).write_text(cookie, encoding="utf-8")
    log(f"登录成功，cookie 已写入 {args.out}（{len(cookie)} 字符）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
