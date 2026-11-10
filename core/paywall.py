"""确认作者之后，去她自己挂的付费页找「这一期」卖在哪。

搬运站和图包里流出来的图，公开渠道基本永远搜不到原图：作者只在 Patreon／
FANBOX 上卖。所以确认到人之后还要多走一步——读她 X 资料里挂的链接，去付费页
翻帖子，把那期的售卖页、订阅档位、有没有免费预览一并给出来。

两个站都有免登录的公开读接口：

* Patreon：``/api/campaigns?filter[vanity]=<名字>`` 换 campaign id，
  ``/api/posts?filter[campaign_id]=`` 列帖（标题、发布时间、最低订阅额），
  ``/api/campaigns/<id>?include=rewards`` 拿档位。
* FANBOX：``api.fanbox.cc/post.listCreator?creatorId=<名字>``（要带站点 Referer）。

付费帖的正文和封面读不到（非订阅者被挡、封面打糊），所以这里只承诺「卖在哪、
多少钱、有没有免费预览」，不承诺内容。

纯函数：把接口返回读成结构化信息。联网由插件负责。
"""

from __future__ import annotations

import re
from datetime import date

_LINKS = {
    "patreon": re.compile(r"patreon\.com/(?:c/)?([A-Za-z0-9_-]{2,40})", re.I),
    "fanbox": re.compile(
        r"(?:([A-Za-z0-9_-]{2,40})\.fanbox\.cc|fanbox\.cc/(?:@)?([A-Za-z0-9_-]{2,40}))", re.I
    ),
}

# 这些词是链接路径里的固定段，不是作者名。
_NOT_VANITY = {"c", "posts", "api", "join", "explore", "home", "about"}

# 「这一期」的标题一般就是角色名加数量（``Rossi 108p``、``Halloween Kisaki 106p``），
# 拿角色罗马音去对就够。这些词在标题里出现说明不了任何事，不进关键词。
_STOP_WORDS = {
    "http",
    "https",
    "www",
    "com",
    "status",
    "photo",
    "photos",
    "video",
    "twitter",
    "patreon",
    "fanbox",
    "cosplay",
    "costume",
    "follow",
    "thanks",
}

_WORD = re.compile(r"[A-Za-z][A-Za-z0-9]{3,}")
# 图片集写法（``106p`` / ``108p``）；同一期还常有 ``8v`` 这种视频帖。
_PHOTO_SET = re.compile(r"\d+p")
# 中文/日文名：Patreon 那边是罗马音（``Amiya 105p``），FANBOX 那边直接写「阿米娅
# アーミヤ 105p」，只收拉丁词两边永远对不上，所以中日文一起收。
_CJK = re.compile(r"[\u4e00-\u9fff]{2,8}|[\u3040-\u30ffー]{2,10}")
_TAG = re.compile(r"#([^\s#]{2,20})")

# 这些词在标题里出现说明不了是哪个角色。
_STOP_WORDS = {
    "http",
    "https",
    "www",
    "com",
    "status",
    "photo",
    "photos",
    "video",
    "twitter",
    "patreon",
    "fanbox",
    "cosplay",
    "costume",
    "follow",
    "thanks",
    "ハロウィン",
    "コスプレ",
    "万圣节",
    "日常",
    "更新",
}


def links_of(*texts: str) -> dict[str, str]:
    """从 X 资料（简介、外链）里挑出 Patreon 与 FANBOX 的用户名。"""
    blob = " ".join(str(text or "") for text in texts)
    found: dict[str, str] = {}
    for kind, pattern in _LINKS.items():
        for match in pattern.finditer(blob):
            value = next((group for group in match.groups() if group), "")
            value = value.strip("/ ")
            if value and value.lower() not in _NOT_VANITY:
                found[kind] = value
                break
    return found


def keywords(*texts: str) -> list[str]:
    """从帖子正文、候选标题里抽能对上作品名的词（罗马音 + 中日文名）。

    中日文标题常把节令和角色名连在一起（``万圣节妃咲``），整串拿去搜一条都搜
    不到；所以除了整串，再补上尾部 2～3 个字（``妃咲``）——角色名一般就在末尾。
    """
    words: list[str] = []
    for text in texts:
        blob = str(text or "")
        candidates = list(_WORD.findall(blob.lower()))
        for run in _CJK.findall(blob):
            candidates.append(run)
            if len(run) >= 3:
                candidates.append(run[-2:])
            if len(run) >= 4:
                candidates.append(run[-3:])
        candidates.extend(_TAG.findall(blob))
        for word in candidates:
            value = word.strip().lower() if word.isascii() else word.strip()
            if not value or value in _STOP_WORDS or value in words:
                continue
            words.append(value)
    return words


def _squash(text: str) -> str:
    """比标题用：只去掉空白和标点，中日文要留着（``阿米娅 アーミヤ`` → ``阿米娅アーミヤ``）。"""
    return re.sub(r"[\s\-_/·・（）()\[\]【】:：,，.。!！?？~～|]+", "", str(text or "").lower())


def _usable(word: str) -> bool:
    return len(word) >= 2 if not word.isascii() else len(word) >= 4


def pick_post(posts: list[dict], words: list[str]) -> dict | None:
    """在付费页的帖子里挑出跟这张图同一期的那条。

    只认标题里的作品名：``Rossi 108p`` 对得上关键词 ``rossi``，``Vesna 108p``
    对不上。同分取先出现的（列表按发布时间倒序，等于取最近那一期）。
    """
    best: tuple[tuple[int, int], dict] | None = None
    for post in posts:
        title = _squash(post.get("title"))
        if not title:
            continue
        score = max(
            (len(word) for word in words if _usable(word) and word in title),
            default=0,
        )
        if not score:
            continue
        # 同一期常同时发图片集（``106p``）和视频（``8v``），用户要的多半是图集：
        # 同分时优先带「数字+p」的那条。
        photo = 1 if _PHOTO_SET.search(title) else 0
        key = (score, photo)
        if best is None or key > best[0]:
            best = (key, post)
    return best[1] if best else None


def free_post(posts: list[dict], words: list[str]) -> dict | None:
    """同一期有没有公开预览（免费帖）。标题里也得带同一个作品名。

    Patreon 的列表里非登录用户看不到 ``current_user_can_view`` 为真的帖子，
    这类直接算「锁着」，不能因为 ``min_cents`` 是空就当成免费。
    """
    for post in posts:
        if post.get("locked") or int(post.get("min_cents") or 0) > 0:
            continue
        title = _squash(post.get("title"))
        if any(_usable(word) and word in title for word in words):
            return post
    return None


def patreon_posts(payload: dict | None) -> list[dict]:
    """Patreon 帖子列表 → [{title, url, published, min_cents, locked, currency}]。"""
    posts: list[dict] = []
    for item in ((payload or {}).get("data") or []):
        attr = item.get("attributes") or {}
        title = str(attr.get("title") or "").strip()
        url = str(attr.get("url") or "").strip()
        if not title or not url:
            continue
        cents = attr.get("min_cents_pledged_to_view")
        posts.append(
            {
                "title": title,
                "url": url,
                "published": str(attr.get("published_at") or "")[:10],
                "min_cents": int(cents) if isinstance(cents, (int, float)) else 0,
                # Patreon 不登录读不到每期的门槛，只读得到「看不了」。
                "locked": not bool(attr.get("current_user_can_view")),
                "currency": str(attr.get("currency") or "").strip(),
            }
        )
    return posts


def patreon_rewards(payload: dict | None) -> list[dict]:
    """档位：只留订阅档，Free 单独标出来，按价格升序。"""
    tiers: list[dict] = []
    for item in ((payload or {}).get("included") or []):
        if item.get("type") != "reward":
            continue
        attr = item.get("attributes") or {}
        title = str(attr.get("title") or "").strip()
        if not title:
            continue
        cents = attr.get("amount_cents")
        tiers.append(
            {
                "title": title,
                "cents": int(cents) if isinstance(cents, (int, float)) else 0,
                "currency": str(attr.get("currency") or "").strip(),
                "free": not cents,
            }
        )
    tiers.sort(key=lambda item: item["cents"])
    return tiers


def fanbox_posts(payload: dict | None, creator: str = "") -> list[dict]:
    """FANBOX 帖子列表 → 同样的结构；``feeRequired`` 是日元整额，不是分。"""
    body = (payload or {}).get("body") or {}
    items = body.get("posts") or body.get("items") or []
    posts: list[dict] = []
    for item in items:
        title = str(item.get("title") or "").strip()
        post_id = str(item.get("id") or "").strip()
        if not title or not post_id:
            continue
        fee = item.get("feeRequired")
        posts.append(
            {
                "title": title,
                "url": f"https://{creator}.fanbox.cc/posts/{post_id}",
                "published": str(item.get("publishedDatetime") or "")[:10],
                "min_cents": int(fee) if isinstance(fee, (int, float)) else 0,
                "locked": bool(item.get("isRestricted")),
                "currency": "JPY",
            }
        )
    return posts


def fanbox_plans(payload: dict | None) -> list[dict]:
    """FANBOX 的赞助档位：``fee`` 就是日元整额。"""
    body = (payload or {}).get("body") or {}
    tiers: list[dict] = []
    for item in body.get("plans") or []:
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        fee = item.get("fee")
        tiers.append(
            {
                "title": title,
                "cents": int(fee) if isinstance(fee, (int, float)) else 0,
                "currency": "JPY",
                "free": not fee,
            }
        )
    tiers.sort(key=lambda tier: tier["cents"])
    return tiers


def money(cents: int, currency: str = "") -> str:
    """把接口里的金额写成一句人话。

    Patreon 用「分」计数，``5500 HKD`` 实际是 55 港币；FANBOX 的 ``feeRequired``
    本来就是日元整额（4000 = 4000 円），不能再除一百。
    """
    currency = (currency or "").strip().upper()
    if currency == "JPY":
        return f"¥{int(cents):,}"
    value = int(cents) / 100
    text = f"{value:g}"
    symbol = {"HKD": "HK$", "USD": "US$", "TWD": "NT$", "CNY": "CN¥", "EUR": "€"}.get(currency)
    if symbol:
        return f"{symbol}{text}"
    return f"{text} {currency}".strip()


def tier_text(tiers: list[dict], limit: int = 4) -> str:
    """档位写成「Free（免费）/ ranran HK$55 / super ran HK$200」。"""
    parts: list[str] = []
    for tier in tiers[:limit]:
        if tier.get("free"):
            parts.append(f"{tier.get('title')}（免费）")
        else:
            parts.append(f"{tier.get('title')} {money(tier.get('cents') or 0, tier.get('currency') or '')}")
    return " / ".join(parts)


def gate_text(post: dict, tiers: list[dict] | None = None) -> str:
    """这期要不要订阅才看得到。

    FANBOX 每期都带 ``feeRequired``，能写准「这一期多少円」；Patreon 不登录只
    读得到「看不了」，给不出每期的门槛，只能退一步写档位从多少起。
    """
    cents = int(post.get("min_cents") or 0)
    if cents:
        return (
            f"这期要订阅才能看（这一期 {money(cents, post.get('currency') or '')} 档），"
            "非订阅者看不到正文"
        )
    if post.get("locked"):
        low = next((tier for tier in (tiers or []) if not tier.get("free")), None)
        tail = f"，档位从 {money(low.get('cents') or 0, low.get('currency') or '')} 起" if low else ""
        return f"这期要订阅才能看{tail}；非订阅者看不到正文与封面"
    return "这期是公开帖，不用订阅就能看"


_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}

_ISO_DATE = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")
# X 那边的 ``Mon Oct 13 15:31:00 +0000 2025``；有的接口把年份省掉。
_TWEET_DATE = re.compile(r"([A-Za-z]{3})\s+(\d{1,2})\s+\d{1,2}:\d{2}")
_TWEET_YEAR = re.compile(r"\b(20\d{2})\b")


def post_date(text: str, hint: str = "") -> date | None:
    """把发布时间读成日期。

    X 帖和付费帖的写法不一样：付费页是 ``2026-11-09``，X 是 ``Fri Oct 03
    13:43:00 +0000 2025``，有的接口还把年份省了。省了年份时用 hint（付费帖的
    日期）补，补出来比 hint 晚半年以上就往前推一年——上架通知总在该期前后。
    """
    text = (text or "").strip()
    if not text:
        return None
    hit = _ISO_DATE.search(text)
    if hit:
        try:
            return date(int(hit.group(1)), int(hit.group(2)), int(hit.group(3)))
        except ValueError:
            return None
    hit = _TWEET_DATE.search(text)
    if not hit:
        return None
    month = _MONTHS.get(hit.group(1).lower())
    if not month:
        return None
    year_hit = _TWEET_YEAR.search(text)
    if year_hit:
        year = int(year_hit.group(1))
    else:
        anchor = post_date(hint) or date.today()
        year = anchor.year
        try:
            guess = date(year, month, int(hit.group(2)))
        except ValueError:
            return None
        # 预告只会在这期前后几天发；补出来的日期比这一期还晚一周以上，那就是
        # 去年那一条（12-06 对 11-09 这期，指的是去年 12 月）。
        if (guess - anchor).days > 7:
            year -= 1
    try:
        return date(year, month, int(hit.group(2)))
    except ValueError:
        return None


def pick_teaser(
    items: list[dict], paid_published: str = "", window: int = 14
) -> dict | None:
    """在一堆她自己的公开帖里挑最像「这一期预告」的那条。

    光按关键词对上不够：``from:<号> 妃咲`` 会先撞上上一期「妃咲(兔女郎) 44p」的
    上架通知，而这期是「万圣节妃咲 106p」——两条都带「妃咲」，指着不同的期。
    所以再加一道时间的：离这一期发布日近的优先，隔太远的只当顺带看见，文案里
    要写清是哪天的、不是这期。
    """
    if not items:
        return None
    anchor = post_date(paid_published)
    best: dict | None = None
    best_score = 0.0
    for item in items:
        when = post_date(str(item.get("published") or item.get("published_raw") or ""), paid_published)
        gap = abs((when - anchor).days) if (when and anchor) else None
        if gap is None:
            score = 10.0
        elif gap <= window:
            score = 100.0 - gap
        else:
            score = max(1.0, 20.0 - min(gap, 180) / 10)
        if str(item.get("kind")) == "announcement":
            score += 15.0
        if item.get("has_media"):
            score += 8.0
        if score > best_score:
            best_score = score
            best = dict(item)
            best["gap"] = gap
            best["same_period"] = bool(gap is not None and gap <= window)
            best["when"] = when.isoformat() if when else ""
    return best
