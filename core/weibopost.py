"""转载页 → 微博原帖：这里只放纯函数，联网由插件负责。

`src/gallery.py` 解决的是「图集站页面上标了谁的号」；这一份解决另一种情况：
候选落在新浪这类**转载页**上，页面自己不写作者，但正文其实就是原微博的正文。
`s-20261113-1250-26a8` 就是这条：正确答案是 G44不会受伤 的微博，而插件只看到
一条 `sina.cn` 的转载页（指纹距离 11，页面正文与微博原文一字不差）。

拿正文里的一两段去 `s.weibo.com/weibo` 搜帖，第一条就是原帖；作者 UID、帖号、
配图 id 都在搜索结果卡片的属性里，不需要再翻主页。**只有指纹对得上才算命中**——
搜帖只负责找人，认不认还是靠图。
"""

from __future__ import annotations

import re
from html import unescape

# 只会往微博上抓的转载 / 新闻站。**只收确认过的**：误标一个正经站点，会让每单都
# 多抓一次页面、多搜一次微博，白花钱还容易被风控。要加新站先自己验一单。
REPOST_HOSTS = (
    "sina.cn",
    "sina.com.cn",
)

# 卡片里这几样是必须的：没有 uid / 帖号就落不了地。
_CARD_SPLIT = re.compile(r'<div class="card-wrap"')
_MID = re.compile(r'<div class="card-wrap"[^>]*\bmid="(\d{10,})"')
_POST_URL = re.compile(r"copyurl\('https?://weibo\.com/(\d+)/([A-Za-z0-9]{6,})")
_NICK = re.compile(r'nick-name="([^"]+)"')
_PIC_IDS = re.compile(r"pic_ids=([A-Za-z0-9,]+)")
_TEXT_NODE = re.compile(
    r'<p[^>]*node-type="feed_list_content"[^>]*>(.*?)</p>', re.S | re.I
)

_SCRIPT = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)
_BREAK = re.compile(r"<br\s*/?>", re.I)
_TAG = re.compile(r"<[^>]+>")
_META_TITLE = re.compile(
    r'<meta[^>]+(?:property|name)="(?:og:)?title"[^>]+content="([^"]+)"', re.I
)
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)
_SPACES = re.compile(r"[ \t\u3000]+")
_HASHTAG = re.compile(r"#([^#\s]{1,20})#")
_MENTION = re.compile(r"@[^\s@,，。;；]{1,20}")
_URL = re.compile(r"https?://\S+")
# 转载页尾巴上常见的站点署名与发布地，塞进检索词只会稀释召回。
_TAIL_NOISE = (
    "发布于",
    "责任编辑",
    "来源：",
    "原标题",
    "扫码关注",
    "点击查看",
    "未经授权",
)

MAX_QUERY = 40
QUERY_LINE_CHARS = 20


def host_of(url: str = "") -> str:
    """取域名，去掉端口、账号、www。"""
    host = (url or "").split("://", 1)[-1].split("/", 1)[0]
    host = host.rsplit("@", 1)[-1].split(":")[0].strip().lower()
    return host.removeprefix("www.")


def is_weibo_repost_page(url: str = "", platform: str = "") -> bool:
    """这个落点是不是「正文抄自微博」的转载页。"""
    for item in (host_of(url), str(platform or "").strip().lower().removeprefix("www.")):
        if not item:
            continue
        if any(item == token or item.endswith("." + token) for token in REPOST_HOSTS):
            return True
    return False


def _clean(text: str) -> str:
    value = unescape(text or "")
    value = _BREAK.sub("\n", value)
    value = _TAG.sub("", value)
    return value


def page_text(html: str = "") -> tuple[str, str]:
    """从转载页 HTML 里取（标题，正文）。正文保留换行，检索词要按行挑。"""
    raw = _SCRIPT.sub("", html or "")
    title = ""
    found = _META_TITLE.search(raw)
    if found:
        title = _clean(found.group(1))
    if not title:
        found = _TITLE.search(raw)
        if found:
            title = _clean(found.group(1))
    body_raw = _clean(raw)
    lines: list[str] = []
    for line in body_raw.splitlines():
        value = _SPACES.sub(" ", line).strip()
        if value:
            lines.append(value)
    return title.strip(), "\n".join(lines)


def _drop_noise(line: str) -> str:
    value = line
    for token in _TAIL_NOISE:
        index = value.find(token)
        if index > 0:
            value = value[:index]
    return value.strip()


def _body_head(title: str, body: str, author: str = "") -> str:
    """从转载页正文里挑一行当检索词。

    这类页面上下都是导航（新浪网 / 汽车 / 本地生活…），照抄第一行会拿到标题或
    菜单。有「微博认证」块时，块下面那一行就是微博正文的首行，最干净；没有这个
    块就退回「第一行既不是标题、也不像菜单」的那一行。
    """
    lines = [line.strip() for line in (body or "").splitlines() if line.strip()]
    title_key = _SPACES.sub("", title or "")
    start = 0
    # 从「微博认证」那一行之后开始找：上一行是作者名、这一行是认证与时间，都是
    # 页面头部信息，不是正文。
    for index, line in enumerate(lines):
        if "微博认证" in line:
            start = index + 1
            break
    else:
        if author:
            for index, line in enumerate(lines):
                if author in line:
                    start = index + 1
                    break
    for line in lines[start:]:
        value = _SPACES.sub("", _MENTION.sub("", line))
        if len(value) < 4 or "微博认证" in line:
            continue
        if title_key and (value in title_key or value[:6] == title_key[:6]):
            continue
        if re.match(r"^[\d\s\-\:\.年月日]+$", value):
            continue
        return line
    return ""


def post_queries(title: str = "", body: str = "", author: str = "", limit: int = 2) -> list[str]:
    """拼检索词：优先用正文首行 + 第一个话题，其次用标题。

    微博搜帖是按词召回，多塞字反而搜不到（实测把整段正文贴进去，召回一堆同人
    视频）。实测 26a8 那单最有效的是「正文首行 + 话题」，标题版作兜底。
    """
    hashtag = ""
    found = _HASHTAG.search(body or "")
    if found:
        hashtag = f"#{found.group(1)}#"

    queries: list[str] = []
    head = _MENTION.sub("", _URL.sub("", _body_head(title, body, author))).strip()
    if len(head) > QUERY_LINE_CHARS:
        head = head[:QUERY_LINE_CHARS]
    candidate = " ".join(part for part in (head, hashtag) if part)
    if candidate:
        queries.append(candidate[:MAX_QUERY])

    head_title = _drop_noise(title or "").split("|", 1)[0].strip()
    head_title = _SPACES.sub(" ", head_title)
    if head_title:
        queries.append(head_title[:MAX_QUERY])

    out: list[str] = []
    for item in queries:
        item = item.strip()
        if len(item) < 4 or item in out:
            continue
        out.append(item)
        if len(out) >= max(1, limit):
            break
    return out


def page_author(body: str = "") -> str:
    """转载页正文里自己标出来的微博作者。

    新浪这类页会照抄微博的头部信息：作者昵称一行，下一行是「26-11-12 11:40
    微博认证：动漫博主」。有这行就等于页面直接把作者名字告诉我们了，比去搜帖更
    便宜也更准。抽不到就返回空串，不许猜。
    """
    lines = [line.strip() for line in (body or "").splitlines()]
    for index, line in enumerate(lines):
        if "微博认证" not in line:
            continue
        # 同一行可能自带名字（「某某 微博认证：…」），先试这一种。
        head = line.split("微博认证", 1)[0].strip(" ·|/")
        head = re.sub(r"^\d{2,4}[-/.]\d{1,2}[-/.]\d{1,2}\s*\d{1,2}:\d{2}", "", head).strip(" ·|/")
        if 2 <= len(head) <= 20:
            return head
        if index > 0:
            candidate = lines[index - 1].strip(" ·|/")
            if 2 <= len(candidate) <= 20 and "微博认证" not in candidate:
                return candidate
    return ""


def large_pic_urls(pic_ids: str | list[str] = "", limit: int = 3) -> list[str]:
    """配图 id → 大图直链。取 large 版：缩略图比指纹会因缩放、裁切失真。"""
    items = pic_ids.split(",") if isinstance(pic_ids, str) else list(pic_ids or [])
    out: list[str] = []
    for item in items:
        value = str(item or "").strip()
        if not value:
            continue
        out.append(f"https://wx1.sinaimg.cn/large/{value}.jpg")
        if len(out) >= max(1, limit):
            break
    return out


def parse_post_search(html: str = "", limit: int = 5) -> list[dict[str, object]]:
    """解析 `s.weibo.com/weibo?q=` 的结果卡片。

    一条卡片里三样东西都要：`uid`（落主页）、`mblogid`（落帖）、`pic_ids`（比指纹）。
    缺 uid 或帖号的卡片直接丢——那种是「相关用户」「话题推荐」之类的装饰块。
    """
    text = html or ""
    chunks = _CARD_SPLIT.split(text)
    out: list[dict[str, object]] = []
    seen: set[str] = set()
    for chunk in chunks[1:]:
        mid_match = _MID.search('<div class="card-wrap"' + chunk[:200])
        url_match = _POST_URL.search(chunk)
        if not url_match:
            continue
        uid, mblogid = url_match.group(1), url_match.group(2)
        if mblogid in seen:
            continue
        nick = ""
        nick_match = _NICK.search(chunk)
        if nick_match:
            nick = unescape(nick_match.group(1)).strip()
        pics: str = ""
        pic_match = _PIC_IDS.search(chunk)
        if pic_match:
            pics = pic_match.group(1)
        body = ""
        text_match = _TEXT_NODE.search(chunk)
        if text_match:
            body = _TAG.sub("", _BREAK.sub("\n", text_match.group(1))).strip()
        seen.add(mblogid)
        out.append(
            {
                "uid": uid,
                "mblogid": mblogid,
                "mid": mid_match.group(1) if mid_match else "",
                "name": nick,
                "text": unescape(body)[:200],
                "pic_ids": pics,
                "url": f"https://weibo.com/{uid}/{mblogid}",
            }
        )
        if len(out) >= max(1, limit):
            break
    return out
