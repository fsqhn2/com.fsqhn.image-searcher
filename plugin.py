"""麦麦寻人：以图搜出处的 MaiBot 插件。

Tool 给 planner 用（只回纯文字），Command 给群友用（合并转发）。
两条路的搜索逻辑完全一致，区别只在呈现方式。
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, ClassVar

import requests

from maibot_sdk import Command, Field, HookHandler, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.config import build_plugin_default_config, merge_plugin_config_data
from maibot_sdk.types import HookMode, ToolParameterInfo, ToolParamType

from .core import (
    bilibili,
    fxtwitter,
    formatting,
    grok,
    handles,
    identity,
    keystate,
    paid,
    paywall,
    platforms,
    resultcache,
    same_person,
    searchpage,
    vision,
)
from .core.commands import build_command_pattern, build_more_pattern, split_force
from .core.config import migrate_legacy_sections
from .core.engines import (
    ENGINES_VERSION,
    REFERENCE_MAX_DISTANCE,
    BaiduEngine,
    Candidate,
    EngineError,
    LensEngine,
    SauceNaoEngine,
    YandexEngine,
    detect_ai,
    downgrade_kind,
    is_avatar_hit,
    reference_candidates,
    verify_candidate,
)
from .core.extract import (
    dead_site,
    dedupe_accounts,
    is_pixiv_platform,
    is_repost_page,
    is_request_post,
    is_social_platform,
    is_site_name,
    is_x_platform,
    is_x_post_page,
    listing_entry_link,
    listing_page_kind,
    page_main_image,
    pixiv_artwork_url,
    pixiv_filename_hint,
    x_page_handle,
    x_page_kind,
    x_title_name,
)
from .core.gallery import (
    author_from_author_page,
    author_page_url,
    is_author_page_site,
    is_content_site,
    is_gallery_site,
    is_repost_site,
    is_shop_site,
    parse_source_accounts,
)
from .core import weibopost
from .core.hashing import dhash, hamming, prepare_upload
from .core.httpclient import (
    HttpError,
    decode_body,
    download_image_multi,
    fake_ip_networks,
    make_session,
)
from .core.keypool import KeyPool
from .core.maibot_compat import unwrap_capability
from .core.tasks import SearchOutcome, SearchTask, TaskStore

DEFAULT_COMMAND = "麦麦寻人"
PLUGIN_NAME = "麦麦寻人"
_BASE64_KEYS = ("binary_data_base64", "base64", "image_base64", "file_base64")
_URL_KEYS = ("url", "image_url", "file_url", "download_url")


# ---------------------------------------------------------------------------
# 命令名在模块导入时读取配置，装饰器的 pattern 只能用这种方式做到可配置。
# 改指令不会自动生效：配置热更新只调 on_config_update，不会重新导入本模块，
# 必须手动重载插件（插件管理页或 reload 能力），否则装饰器仍是旧 pattern。
# ---------------------------------------------------------------------------


def _read_command_names() -> tuple[str, list[str]]:
    path = Path(__file__).with_name("config.toml")
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except Exception:  # noqa: BLE001 - 首次加载时配置可能还没生成
        return DEFAULT_COMMAND, []
    section = data.get("plugin") or {}
    command = str(section.get("command") or DEFAULT_COMMAND).strip() or DEFAULT_COMMAND
    aliases = [
        str(item).strip()
        for item in (section.get("aliases") or [])
        if str(item).strip() and str(item).strip() != command
    ]
    return command, aliases


COMMAND, ALIASES = _read_command_names()
_COMMAND_PATTERN = build_command_pattern([COMMAND, *ALIASES])
_MORE_PATTERN = build_more_pattern(["更多", "more"])
# 只含媒体占位符的消息（如 "[image]"），用于接住等待中的单图消息
_CAPTURE_PATTERN = r"^(?:\[[^\]]*\]\s*)+$"
# X 账号只可能是这些字符；图集页编号会挂成 handle（"UID 1856233540"），别去翻它。
_X_HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{2,15}$")
# 标题里的名字有时挂在引号里：「貧乳細腰《Mio Kuuko》白皙…」。
_QUOTED_NAME = re.compile(r"[《「『【〈]([^》」』】〉]{2,24})[》」』】〉]")
# 带这些标点的是整句话，不是名字。
_SENTENCE_PUNCT = re.compile(r"[？?！!。，,、；;…]")
# pixiv 作品页：那里的标题是作品名，作者写在页面里，不能当人名拿去各平台搜。
_PIXIV_ARTWORK_RE = re.compile(
    r"^https?://(?:www\.)?(?:pixiv\.net|i\.pximg\.net)/(?:en/)?(?:artworks/\d+|member_illust\.php)",
    re.I,
)


def _is_pixiv_artwork(url: str) -> bool:
    """这条候选是不是 pixiv 作品页（标题是作品名，不是人）。"""
    return bool(_PIXIV_ARTWORK_RE.match(str(url or "").strip()))


def _quoted_exact(raw: str, text: str) -> bool:
    """``text`` 是不是从 ``raw`` 的引号里原样取出来的那个名字。"""
    match = _QUOTED_NAME.search(str(raw or ""))
    return bool(match and match.group(1).strip(" @") == text)

# 插件运行时被重启（任何插件文件被改都会触发）时，正在搜的单会随进程一起消失。
# 卸载前把在跑的单记到盘上，重新加载后回一句，免得群里一直等不到结果。
_INTERRUPTED_FILE = "image_searcher_interrupted.json"
_INTERRUPTED_MAX_AGE = 2 * 3600
_INTERRUPTED_DELAY = 5.0
_INTERRUPTED_TEXT = "⚠️ 上一单搜图（{task_id}，{minutes} 分钟前）因为插件重启被打断了，没出结果，麻烦再发一次。"


# ---------------------------------------------------------------------------
# 配置模型
# ---------------------------------------------------------------------------


def _ui(label: str, hint: str = "", **extra: Any) -> dict[str, Any]:
    """字段在 WebUI 上显示的中文名与填写提示。"""
    payload: dict[str, Any] = {"label": label}
    if hint:
        payload["hint"] = hint
    payload.update(extra)
    return payload


class EngineConfig(PluginConfigBase):
    """单个搜索引擎的配置。"""

    __ui_label__ = "搜索引擎"

    enabled: bool = Field(
        default=True,
        description="关掉后这个引擎完全不参与搜索，也不再消耗它的额度。",
        json_schema_extra=_ui("启用该引擎"),
    )
    keys: list[str] = Field(
        default_factory=list,
        description="这个引擎的 API Key，可以填多个。插件优先用没被限流的那个，被限流或失效的会自动跳过。",
        json_schema_extra=_ui(
            "API Key",
            "点「添加」可以加多个 key；不需要 key 的引擎留空即可。",
        ),
    )
    max_results: int = Field(
        default=5,
        description="这个引擎最多保留几条候选。取小一点只是少下载、少验证，不省额度（额度按调用次数算）。",
        json_schema_extra=_ui("每次保留几条", "Lens 5、Yandex 3、百度 3、SauceNAO 4（免费档上限）。"),
        ge=1,
        le=20,
    )


class LensConfig(EngineConfig):
    """Google Lens（走 SerpAPI），命中率最高，需要自己的 key。"""

    __ui_label__ = "Lens 引擎（Google 识图 / SerpAPI）"
    __ui_icon__ = "scan-search"

    max_results: int = Field(
        default=5,
        description="Lens 每次最多保留几条候选。建议 5，它本来也只返回四五条。",
        json_schema_extra=_ui("每次保留几条", "建议 5；调小不省额度（按调用次数计费）。"),
        ge=1,
        le=20,
    )
    retries: int = Field(
        default=2,
        description=(
            "Lens 返回 0 条时的重试次数。同一张图它会有大约一半概率随机返回空结果，"
            "而空结果并不代表网上没有这张图，重试往往就能拿到原帖。"
        ),
        json_schema_extra=_ui(
            "空结果重试次数",
            "建议 2。只在返回 0 条时才重试，有结果就停，不会白白多花额度。",
        ),
        ge=0,
        le=5,
    )
    retry_delay: float = Field(
        default=1.5,
        description="Lens 每次重试之间等待的秒数。",
        json_schema_extra=_ui("重试间隔（秒）", "建议 1.5。"),
        ge=0,
        le=10,
    )


class SerperConfig(PluginConfigBase):
    """Serper（google.serper.dev）：跟 SerpAPI 是两家，key 不通用，单独配、单独开关。

    它有两个用途：网页检索（按名字搜 X、按 id= 的名字查各平台）和给 Lens 当替补。
    网页检索一次扣 1 次额度，Lens 一次扣 3 次还得先把图传到公网图床，所以两个用途
    各留一个开关。额度是预付总量、用完不按月刷新，省着点用。
    """

    __ui_label__ = "Serper（另一家的 key，可选）"
    __ui_order__ = 9

    enabled: bool = Field(
        default=True,
        description=(
            "Serper 的总开关。key 留空时它自然不参与，不必特意关；真不想用再关掉。"
            "关掉后既不参与网页检索，也不给 Lens 当替补。"
        ),
        json_schema_extra=_ui("启用 Serper", "填了 key 就生效；不想用把这个关掉。"),
    )
    keys: list[str] = Field(
        default_factory=list,
        description=(
            "Serper 的 API Key，40 位十六进制，跟 SerpAPI 的 64 位 key 不是一回事，"
            "别填进 Lens 那一栏。可填多个轮询。"
        ),
        json_schema_extra=_ui(
            "Serper API Key",
            "40 位十六进制。拿 SerpAPI 的 key 填这里会一直报 401。",
        ),
    )
    lens_fallback: bool = Field(
        default=True,
        description=(
            "两种情况下让 Lens 改用 Serper 顶上：SerpAPI 那几把 key 都用完或全部冷却；"
            "或者 SerpAPI 重试完仍返回 0 条（它会对同一张图随机抽空，实测 6 次里 3 次空）。"
            "一次扣 3 次 Serper 额度，还要先把图传到公网图床，比 SerpAPI 慢几秒。"
        ),
        json_schema_extra=_ui(
            "允许 Serper 替补 Lens",
            "默认开。SerpAPI 不可用或返回空时才会轮到它。",
        ),
    )
    upload: bool = Field(
        default=True,
        description=(
            "Serper 的 Lens 不收图片字节、只收公网链接，走它时必须先把图传到图床换一个直链。"
            "关掉等于不让 Serper 碰 Lens，网页检索不受影响。传到哪家见下面的图床设置。"
        ),
        json_schema_extra=_ui(
            "允许上传图片到图床",
            "默认开。图片会传到公开图床（几分钟到几小时后失效），只在上面的替补开关生效时才发生。",
        ),
    )


class HostingConfig(PluginConfigBase):
    """上传图床：Serper 的 Lens 只认公网链接，得先把图传上去换个直链。

    顺序固定为 uguu → ChatGLM → CliIm，前一家没换成自动换下一家，关掉的那家直接跳过；
    一家都没开就等于不让 Serper 碰 Lens。三家都不需要额外配置，也都被 Google 抓得到
    （2026-11-16 拿 Unsplash 的照片逐一实测，喂 Lens 都回 60 条）。
    """

    __ui_label__ = "图床（Serper 替补 Lens 时用）"
    __ui_icon__ = "upload"
    __ui_order__ = 10

    uguu: bool = Field(
        default=True,
        description=(
            "第一顺位 uguu.se。传完回一个 d.uguu.se 直链，三小时左右自动失效。"
            "实测 Google 抓得到，但它自己偶尔整段回 502，所以后面还留了两家。"
        ),
        json_schema_extra=_ui("uguu.se（第一顺位）", "默认开。它抽风时自动换下一家。"),
    )
    chatglm: bool = Field(
        default=True,
        description=(
            "第二顺位 ChatGLM（智谱）的文件上传，不用 key。回带签名的 t1.chatglm.cn "
            "直链，有时效，换成之后立刻用。"
        ),
        json_schema_extra=_ui("ChatGLM（第二顺位）", "默认开。不用注册，链接带时效。"),
    )
    cliim: bool = Field(
        default=True,
        description=(
            "第三顺位 CliIm（草料二维码）的免费图床，文件名取图片 MD5，"
            "回阿里云杭州 OSS 直链。"
        ),
        json_schema_extra=_ui("CliIm 草料（第三顺位）", "默认开。免注册，回阿里云 OSS 直链。"),
    )

    def order(self) -> tuple[str, ...]:
        """按固定顺序给出开了哪几家，交给上传链依次去试。"""
        pairs = (("uguu", self.uguu), ("chatglm", self.chatglm), ("cliim", self.cliim))
        return tuple(name for name, on in pairs if on)


class YandexConfig(EngineConfig):
    """Yandex 以图搜图，免费、不用 key，返回条数多但噪声大。"""

    __ui_label__ = "Yandex 引擎（免费，不用 key）"
    __ui_icon__ = "globe"

    max_results: int = Field(
        default=3,
        description="Yandex 一次返回近百条，大多是账号主页，取前几条就够。",
        json_schema_extra=_ui("每次保留几条", "建议 3。必须配好代理，否则容易超时。"),
        ge=1,
        le=20,
    )
    routes: list[str] = Field(
        default_factory=list,
        description=(
            "额外线路：按顺序每条都跑，候选去重后并起来。"
            "填 direct 表示直连，也可以填代理地址（如 http://127.0.0.1:10809）。"
            "留空走老规矩：全局代理 → 直连，拿到候选就停。"
        ),
        json_schema_extra=_ui(
            "多条线路（并集）",
            "不同出口地区 Yandex 给的站点完全不同，两条都跑召回更好，代价是多一轮请求。"
            "例：direct、http://127.0.0.1:10809。",
        ),
    )


class BaiduConfig(EngineConfig):
    """百度识图，免费、不用 key，主要覆盖国内站点。"""

    __ui_label__ = "百度引擎（免费，不用 key）"
    __ui_icon__ = "search"

    max_results: int = Field(
        default=3,
        description="百度返回的是裁剪缩略图，后几条基本无关，取前 3 条即可。",
        json_schema_extra=_ui("每次保留几条", "建议 3。结果多标「疑似」，因为缩略图会被裁剪。"),
        ge=1,
        le=20,
    )


class SauceNaoConfig(EngineConfig):
    """SauceNAO 插画库，需要 key，还负责判断这张图是不是插画。"""

    __ui_label__ = "SauceNAO 引擎（插画库，需要 key）"
    __ui_icon__ = "images"

    min_similarity: int = Field(
        default=75,
        description="相似度低于这个分数只算疑似，不会当权威结果。和判定插画的阈值保持一致：低于 75% 既不算插画，也不该当照片的命中。",
        json_schema_extra=_ui("最低相似度（%）", "默认 75，与插画阈值一致。调高更严格、候选更少。"),
        ge=0,
        le=100,
    )
    route_similarity: float = Field(
        default=75.0,
        description="相似度达到这个分数，就认为这张图是插画，自动改用插画路线的引擎顺序。",
        json_schema_extra=_ui("判定插画的相似度（%）", "建议 75。调低更容易走插画路线，调高更保守。"),
        ge=0,
        le=100,
    )
    max_results: int = Field(
        default=4,
        description="SauceNAO 每次最多保留几条候选。免费档单次最多只返回 4 条。",
        json_schema_extra=_ui("每次保留几条", "免费档最多 4 条，调大也拿不到更多。"),
        ge=1,
        le=20,
    )
    db: str = Field(
        default="999",
        description="全量检索时传给 SauceNAO 的 db 参数。999 表示全部数据库。",
        json_schema_extra=_ui("全量检索的库", "默认 999（全库）。一般不用改。", placeholder="999"),
    )
    db_strict: str = Field(
        default="5,6,21,24",
        description="收紧检索时用的 db 参数，逗号分隔。默认只查 pixiv、twitter、anime-pictures 等常用插画库。",
        json_schema_extra=_ui(
            "收紧检索的库",
            "默认 5,6,21,24。收得越窄，越不容易带出成人站点，但冷门图可能搜不到。",
            placeholder="5,6,21,24",
        ),
    )
    db_policy_enabled: bool = Field(
        default=True,
        description="是否按群启用名单策略。关闭时所有群都用上面的「全量检索的库」。",
        json_schema_extra=_ui(
            "启用按群名单",
            "打开后：名单里的群全量检索，其他群收紧。名单留空就是所有群都收紧。想反过来把「名单反转」打开。",
        ),
    )
    db_full_groups: list[str] = Field(
        default_factory=list,
        description="名单里的群号。填群号即可（例如 123456789），也认会话 ID。",
        json_schema_extra=_ui(
            "名单（群号）",
            "填群号，点「添加」可以加多个。这些群走全量，其他群收紧；留空就是所有群都收紧。",
        ),
    )
    db_policy_invert: bool = Field(
        default=False,
        description="反转名单含义：打开后名单里的群收紧，其他群全量。",
        json_schema_extra=_ui(
            "名单反转",
            "默认名单=全量。打开后名单=收紧，其他群=全量，适合只想在个别群限制的情况。",
        ),
    )


class EnginesConfig(PluginConfigBase):
    """引擎组配置。"""

    __ui_label__ = "引擎优先级"

    priority: list[str] = Field(
        default_factory=lambda: ["lens", "yandex", "baidu", "saucenao"],
        description="照片路线（默认）的引擎优先级。数组顺序就是优先级，越靠前越优先采用、越靠前展示。",
        json_schema_extra=_ui(
            "照片路线 · 引擎优先级（调整顺序即可）",
            "可填 lens、yandex、baidu、saucenao。把想优先用的引擎放到前面就是提高它的优先级，删掉某个名字即不使用它。",
        ),
    )
    priority_drawn: list[str] = Field(
        default_factory=lambda: ["saucenao", "yandex", "baidu", "lens"],
        description="插画路线的引擎优先级。SauceNAO 命中到判定分数时自动切到这条顺序。",
        json_schema_extra=_ui(
            "插画路线 · 引擎优先级（调整顺序即可）",
            "通常 SauceNAO 放第一位。可填的值与照片路线相同。",
        ),
    )


class AiDetectConfig(PluginConfigBase):
    """AI 生成图检测配置。"""

    __ui_label__ = "AI 图检测"
    __ui_order__ = 8

    enabled: bool = Field(
        default=False,
        description="开启后，搜不到可靠匹配时会调用 Sightengine 判断这张图是不是 AI 生成的。",
        json_schema_extra=_ui("启用 AI 生成图检测", "需要 Sightengine 账号；不开也能正常搜图。"),
    )
    keys: list[str] = Field(
        default_factory=list,
        description="Sightengine 的密钥，格式是 api_user:api_secret，中间用英文冒号连起来。",
        json_schema_extra=_ui("Sightengine 密钥", "形如 123456:abcdefg；可填多个轮询。"),
    )
    only_when_low_confidence: bool = Field(
        default=True,
        description="只在没有可靠匹配时才调用检测，避免每次搜图都消耗 Sightengine 额度。",
        json_schema_extra=_ui("仅在没找到匹配时检测", "建议保持开启。"),
    )


class VisionConfig(PluginConfigBase):
    """视觉复核：指纹全灭时，把目标图和候选拼成对照图，让视觉模型找「同一个人」。"""

    __ui_label__ = "视觉复核（拼图交给视觉模型）"
    __ui_icon__ = "eye"
    __ui_order__ = 6

    enabled: bool = Field(
        default=True,
        description=(
            "视觉兜底：指纹一条都没通过时，把目标图和最接近的候选拼成一张对照图，"
            "交给 MaiBot 的视觉模型判断有没有「同一个人」。只找同一个人，不要求同一张图。"
        ),
        json_schema_extra=_ui(
            "启用视觉复核",
            "默认开。只在整轮没有任何匹配时触发，指纹判定不受影响；关掉后搜不到就直接报没找到。",
        ),
    )
    watermark_on_repost: bool = Field(
        default=True,
        description=(
            "命中落点没有账号时也读水印：指纹已经对上，但落点是 arca.live、reddit、贴吧"
            "这类社区/搬运站，或者新闻页、活动报道页这种说不出作者是谁的页面时，仍然把"
            "原图交给视觉模型读一遍水印。这类页面往往只是转贴，图上写的水印账号才是唯一"
            "还能指到本人的线索；只在落点没有账号时触发，读到水印仍只当线索，要核到本人"
            "发过这张图才会换掉原来那条。"
        ),
        json_schema_extra=_ui(
            "落点没账号时也读水印",
            "默认开。命中页面说不出作者（搬运站、新闻页、报道页）时额外跑一次视觉读水印，"
            "核到本人的帖才会顶掉原来那条；关掉就沿用原来的行为——有命中就收工，不再花这一次调用。",
        ),
    )
    watermark_variants: bool = Field(
        default=True,
        description=(
            "水印号查无此人时，按近似拼写再试：模型读小字常把 w 读散成 uw／vv，"
            "这时会生成十来个「像是同一个号」的写法去核，只有那个号自己的带图帖里"
            "真有这张图才算命中。猜出来的写法不会直接当结论。"
        ),
        json_schema_extra=_ui(
            "水印号查无时按近似拼写再试",
            "默认开。每单最多试十来个写法，而且必须指纹对得上才采用；关掉就只认读出来的原号。",
        ),
    )
    watermark_reread: bool = Field(
        default=True,
        description=(
            "水印兜底重读：拼图和 Grok 都没读出可用水印（或者读出来的号翻遍带图帖都不是"
            "这张图）时，最后再把原图单独交给视觉模型读一遍。拼图里每格只有 320 像素，"
            "手写体小字常被压没；原图能看清。读到账号照样核账号、翻带图帖，只有那个号"
            "自己的帖子里真有这张图才算命中。"
        ),
        json_schema_extra=_ui(
            "水印兜底重读（原图）",
            "默认开。拼图和 Grok 两条线都没落下结论时才跑，每单最多多一次视觉调用；"
            "关掉就只认拼图和 Grok 读出来的水印。",
        ),
    )
    watermark_locate: bool = Field(
        default=True,
        description=(
            "水印两步读（原图兜底）：整图读不出水印时，先让模型只说「水印在哪」"
            "（一次调用，不抄字），再按那个位置从原图裁下来放大读一次。"
            "比切六块便宜得多——最多两次调用，而且裁的是原图，小字不会被缩放吃掉。"
            "只在整图读完还没读到账号时才触发。"
        ),
        json_schema_extra=_ui(
            "水印两步读（先定位再放大）",
            "默认开。整图读不出水印时最多多两次视觉调用；读到就停。",
        ),
    )
    watermark_tiles: bool = Field(
        default=False,
        description=(
            "水印分块重读：整图读不出水印时，把原图切成六块（上下三行×左右两列，"
            "带重叠），一块一块放大再读，读到账号就停。整图读不出不一定是分辨率不够，"
            "而是那行字在整幅画面里占比太小，模型注意力落不到它身上。"
            "默认关：六块要四十秒上下，而且最容易读出假号（实测读出过 MURUA、MIRUI"
            "这种同音不同字的号，一个都没核上），整图读到就轮不到它。"
        ),
        json_schema_extra=_ui(
            "水印分块重读（原图切块）",
            "默认关。打开后只在整图读不出、或读到的号核不上时触发，最多多六次视觉调用；"
            "整图能读出来就别开，省四十秒。",
        ),
    )
    watermark_following: bool = Field(
        default=True,
        description=(
            "水印里的号本人没发这张图时，去她关注的人里找同一个人。coser 的小号常挂在"
            "自己的关注里：实测水印读成 @NERUchi890（28.7 万粉），她最近 14 条带图帖都"
            "没这张，但她只关注 5 个人，其中一个 @oharapekopeko 发的正是这张（距离 0），"
            "两个号挂的是同一条 patreon.com/NENE318。这条比「按名字搜 X」准，那边搜出来"
            "的是同名的陌生人。只在号存在、本人没发这张时才跑，每单最多一次。"
        ),
        json_schema_extra=_ui(
            "水印号的关注里找小号",
            "默认开。只看关注列表里外链相同或昵称同词根的号，最多翻三个号的带图帖；"
            "关掉就只核水印那个号本人。",
        ),
    )
    deep_trace: bool = Field(
        default=True,
        description=(
            "追查模式：原图读不出水印时，去读搜到的候选图。搬运链里水印常常只留在"
            "转载的那张图上——群里发的原图是干净的，reddit 那张手机边却写着账号；"
            "同一套图的不同张，光读原图永远读不到。读到账号照旧核它自己的带图帖，"
            "核不上就当线索，不会因此改结论。"
        ),
        json_schema_extra=_ui(
            "追查模式（读候选图的水印）",
            "默认开。命中搬运页时先读候选图、再读原图；每单最多三张候选图、"
            "总调用封顶 12 次。关掉就只读用户发的那张图。",
        ),
    )
    signature_hunt: bool = Field(
        default=True,
        description=(
            "标题署名反查：候选标题里写着「cosplay by XXX」「coser: XXX」「@XXX」这类"
            "明确署名时，拿这个名字去检索 X 账号，再翻那个号自己的带图帖比指纹，"
            "命中才算结论。搬运帖常把原作者名一起抄过来，而插件原来只从候选 URL 里"
            "抽 handle，抄在标题里的名字完全用不上。"
        ),
        json_schema_extra=_ui(
            "标题署名反查原作者",
            "默认开。每单最多认三个署名、最多翻三个号的带图帖；翻不到就当没有，"
            "不会因为名字像就改结论。",
        ),
    )
    x_from_name: bool = Field(
        default=True,
        description=(
            "按名字换到 X 上核：结论只到「同一个人」、落点又是微博这类认不了图的站时，"
            "把那个号（微博 UID）换成昵称，拿昵称检索一次 X 账号，逐个翻带图帖比指纹。"
            "只有那个号自己的帖子里真有这张图才算命中；换号本身不会改结论。"
        ),
        json_schema_extra=_ui(
            "按名字去 X 上核",
            "默认开。微博这类站认不了图，这一步能把「同一个人」落到具体 X 号；"
            "每单最多一次网页检索、两个候选号，找不到就照旧按没找到写。",
        ),
    )
    id_name_search: bool = Field(
        default=True,
        description=(
            "水印里写着「id=某某」时，拿这个名字去各平台找主页。这种写法多是中文平台"
            "的 ID（微博、小红书），X 上常常查无此号；一路检索把 X、Instagram、TikTok、"
            "微博、Patreon、Reddit、Bilibili、Facebook、YouTube、小红书的主页都捞出来，"
            "X 上找到号就翻它自己的带图帖比指纹。不核上图的只写成线索。"
        ),
        json_schema_extra=_ui(
            "按 id= 的名字查各平台",
            "默认开。只在整轮没有结论、水印里又读到 id= 时才跑；一次网页检索（捞不到"
            "主页时最多两次），X 上最多核两个号。外国平台只给线索，不当结论。",
        ),
    )
    repost_author: bool = Field(
        default=True,
        description=(
            "搬运站命中时翻候选里的 X 号：指纹对上的那条如果只是博客/图集站上的转贴"
            "（没有账号），就把候选里出现过的 X 号挨个翻一遍带图帖比指纹，谁真有这张图"
            "谁就是结论。找不到也记下来，免得看起来像「命中搬运站就收工」。"
        ),
        json_schema_extra=_ui(
            "搬运站命中时翻候选里的 X 号",
            "默认开。最多两个号、整段 90 秒；只认指纹命中，翻不到只写一句查过哪些号。",
        ),
    )
    paywall: bool = Field(
        default=True,
        description=(
            "确认作者后找售卖页：图包里的图公开渠道永远搜不到，作者只在 Patreon／"
            "FANBOX 上卖。结论落到具体的人之后，读她 X 资料里挂的链接，去付费页的"
            "公开列表里找这一期，给出售卖页、订阅档位和有没有免费预览。"
        ),
        json_schema_extra=_ui(
            "确认作者后找售卖页",
            "默认开。只读两个站的公开接口（不登录、不订阅），付费帖正文读不到；"
            "最多多花二十秒，查不到就不写这一段。",
        ),
    )
    top_n: int = Field(
        default=16,
        description="视觉复核时拼前几张候选，4 列排布，每格 320 像素。格数越多越慢、越容易看错。",
        json_schema_extra=_ui("拼几张候选", "默认 16。格子越大越能看清细节，但格数太多会拖慢并增加看错风险。"),
        ge=4,
        le=25,
    )
    task: str = Field(
        default="vlm",
        description="用哪个模型任务做视觉复核。MaiBot 的模型配置里有专门的视觉任务组，默认值就是它。",
        json_schema_extra=_ui("模型任务名", "默认 vlm。只有改过模型任务名才需要动这里。", placeholder="vlm"),
    )
    model: str = Field(
        default="",
        description=(
            "具体用哪个视觉模型。留空表示按上面的任务组顺序挑，一般不需要填；"
            "只有任务组里排在前面的模型不好用（比如推理型会把输出额度花在思考上、正文被截断）时才点名。"
        ),
        json_schema_extra=_ui(
            "指定模型（留空即可）",
            "留空就用任务组里的模型。填了就强制走这个名字，写错会导致调用失败。",
            placeholder="留空按任务组顺序",
        ),
    )
    timeout: int = Field(
        default=180,
        description=(
            "一次视觉复核最多等多少秒，超时就放弃，不影响已经搜到的文字结果。"
            "这个值是「排队 + 推理」的总时长：宿主在跑记忆整理之类的任务时，视觉请求"
            "可能先排几十秒才轮到模型，所以不要按模型本身的耗时来设。"
        ),
        json_schema_extra=_ui(
            "超时（秒）",
            "默认 180。宿主繁忙时视觉请求要排队，90 秒容易在轮到模型前就被放弃。",
        ),
        ge=10,
        le=600,
    )


class GrokConfig(PluginConfigBase):
    """Grok 兜底：指纹全灭时，带图去 X 上核对候选账号或找原帖。"""

    __ui_label__ = "Grok 兜底（X 上找发布者）"
    __ui_icon__ = "sparkles"
    __ui_order__ = 7

    enabled: bool = Field(
        default=False,
        description=(
            "指纹一条都没通过时，把原图和已有候选一起交给 Grok，让它在 X 上核对哪个账号是发布者。"
            "给出具体帖子链接算找到，只给出账号主页算线索。需要填 API Key。"
        ),
        json_schema_extra=_ui(
            "启用 Grok 找人",
            "默认关。要填 API Key 才会真的发请求；按次计费。和视觉复核同时跑。",
        ),
    )
    base_url: str = Field(
        default="",
        description="兼容 OpenAI Responses 接口、支持 x_search 工具的 API 地址，例如 https://api.x.ai/v1。",
        json_schema_extra=_ui(
            "Base URL",
            "填服务地址，例如 https://api.x.ai/v1。不要带末尾斜杠。",
            placeholder="https://api.x.ai/v1",
        ),
    )
    api_key: str = Field(
        default="",
        description="上面那个服务地址的 API Key。留空时即使打开开关也不会发出请求。",
        json_schema_extra=_ui("API Key", "只写在本机 config.toml，不会上传。留空表示不用这个功能。"),
    )
    model: str = Field(
        default="grok-4.7",
        description="用哪个模型。填服务商实际提供的名字，需要支持图输入和 x_search。",
        json_schema_extra=_ui("模型", "以服务商提供的模型名为准，默认 grok-4.7。", placeholder="grok-4.7"),
    )
    models: list[str] = Field(
        default_factory=lambda: ["grok-4.7", "grok-4.6"],
        description=(
            "按顺序尝试的模型：前一个超时、连不上或服务端 5xx 就换下一个。"
            "实测中转站会轮流挂 4.7 和 4.6，配成两个能明显提高成功率。留空则只用上面的 model。"
        ),
        json_schema_extra=_ui(
            "备用模型（按顺序）",
            "默认依次试 grok-4.7、grok-4.6；前一个挂了自动换下一个。",
        ),
    )
    timeout: int = Field(
        default=210,
        description=(
            "找人最多等多少秒。实测中转站跑一轮带图的 X 检索要 100 秒上下"
            "（grok-4.6 实测 118.8 秒才回，grok-4.7 会超 120 秒），210 秒留足余量；"
            "超时就放弃，视觉结果照常返回。"
        ),
        json_schema_extra=_ui("超时（秒）", "默认 210。嫌久可以调小，但中转站慢时会整单没结果。"),
        ge=15,
        le=300,
    )
    role_hint: bool = Field(
        default=False,
        description=(
            "图搜图完全没有结论时，再问一次「这是哪个角色」，然后按角色名去 X 搜同角色的"
            "coser，把她们当线索列出来（不判定谁发过这张图）。实测只能给名单、核不了图，"
            "还会多等一两分钟，所以默认关。"
        ),
        json_schema_extra=_ui(
            "按角色找同角色账号",
            "默认关。打开后只在没找到结论时跑一次；结果只是线索，不当作答案。",
        ),
    )
    role_timeout: int = Field(
        default=100,
        description=(
            "按角色找人这一路最多等多少秒（整条线另有 +30 秒总预算，避免几个模型"
            "串起来把整单拖长）。实测一次完整检索约 70 秒。"
        ),
        json_schema_extra=_ui("角色线索超时（秒）", "默认 100。实测约 70 秒返回。"),
        ge=15,
        le=300,
    )


class TitleAuthorConfig(PluginConfigBase):
    """转载站标题里的作者名：交给 LLM 抽，还是用固定规则抽。"""

    __ui_label__ = "转载页作者名（LLM 抽取）"
    __ui_icon__ = "user-search"
    __ui_order__ = 11

    enabled: bool = Field(
        default=True,
        description=(
            "指纹命中落在搬运站、图集站时，第一步要从页面标题里认出作者名（「染三ran3 - 作品名」"
            "→ 「染三ran3」），再展开成几个候选 X 账号去核。老办法是固定规则：剥掉开头的分类标签、"
            "按分隔符切第一段。资源站标题写法太杂（分类标签、语言标签、体积、多语言混排），"
            "规则总有漏的。打开后这一步改问一次 MaiBot 的文本模型，让它读出作者名和可能的账号。"
            "模型没回、超时或答复解析不了时，自动退回老规则，不会因为这一步失败就没结果。"
        ),
        json_schema_extra=_ui(
            "用 LLM 抽作者名",
            "默认开。只换「从标题里抽出作者名」这一步，后面照样要显示名对得上、翻到本人带图帖才算命中。",
        ),
    )
    task: str = Field(
        default="utils",
        description=(
            "用哪个模型任务组。默认 utils——MaiBot 自带的文本小任务组，速度快、成本低，"
            "抽个名字不需要推理模型。"
        ),
        json_schema_extra=_ui("模型任务组", "默认 utils。填任务组名，不是模型名。", placeholder="utils"),
    )
    model: str = Field(
        default="",
        description="指定具体模型，留空表示按任务组顺序挑（推荐留空）。",
        json_schema_extra=_ui("指定模型", "默认留空，按任务组顺序。只有组里靠前的模型抽不好时才点名。"),
    )
    timeout: int = Field(
        default=60,
        description=(
            "最多等多少秒。这个时长包含排队：宿主管道忙的时候，一次文本请求也要等十几秒才轮到。"
            "超时就退回老规则，整单照常出结果。"
        ),
        json_schema_extra=_ui("超时（秒）", "默认 60，含排队。超时自动退回规则抽取。"),
        ge=10,
        le=180,
    )


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "基础设置"
    __ui_icon__ = "scan-search"
    __ui_order__ = 0

    enabled: bool = Field(
        default=True,
        description="插件总开关，关掉后指令和工具都不再响应。",
        json_schema_extra=_ui("启用插件"),
    )
    config_version: str = Field(
        default="0.3.0",
        description="配置版本号，由插件维护，不需要手动修改。",
        json_schema_extra=_ui("配置版本", hidden=True),
    )
    command: str = Field(
        default=DEFAULT_COMMAND,
        description="群里发这个词就触发搜图。改完必须重载插件才生效。",
        json_schema_extra=_ui("触发指令", "例如填「麦麦寻人」，群里就发「麦麦寻人」。改完记得重载插件。", placeholder="麦麦寻人"),
    )
    aliases: list[str] = Field(
        default_factory=list,
        description="额外的触发词，可以留空。填几个就多几个出发方式，同样需要重载插件。",
        json_schema_extra=_ui("指令别名", "例如再加一个「搜图」；留空表示只有上面那个指令。"),
    )
    nickname: str = Field(
        default="麦麦寻人",
        description="结果以合并转发发出去时，每条消息上方显示的昵称。",
        json_schema_extra=_ui("转发昵称", "随便改，只影响显示。"),
    )
    plain_first_reply: bool = Field(
        default=False,
        description=(
            "首次结果改成普通图文消息（一段结论 + 最多 3 张图），不再用合并转发。"
            "默认关闭：关着就走合并转发卡片，和以前一样；打开后一定看得见，"
            "但群里会多占一行。不管开关怎么设，「更多」始终是合并转发。"
        ),
        json_schema_extra=_ui(
            "首次结果用普通图文消息",
            "默认关闭 = 用合并转发卡片。打开后首次结果是一条图文消息，"
            "适合合并转发老是显示不出来的环境。",
        ),
    )
    task_ttl_minutes: int = Field(
        default=1440,
        description=(
            "搜完的结果保留多久。这段时间内回复「更多 + 任务编号」可以直接重发，"
            "不用重新搜。超过就清掉缓存，只能重新发图。"
        ),
        json_schema_extra=_ui(
            "结果保留时长（分钟）",
            "默认 1440，也就是 1 天。过期后缓存被清掉，回复「更多」会提示重新搜。",
        ),
        ge=1,
        le=10080,
    )
    cache_enabled: bool = Field(
        default=True,
        description=(
            "同一张图搜过一次后，直接把上次的结果发出来，不再调用引擎。"
            "键是图片内容 + 引擎版本，换张图、或者插件升级解析逻辑就自动重搜。"
        ),
        json_schema_extra=_ui(
            "按图缓存（同一张图复用结果）",
            "默认开启。命中时回复里会标「缓存」，不消耗任何引擎额度。",
        ),
    )
    cache_days: int = Field(
        default=30,
        description="搜到结果后缓存保留多久。同一张图在这段时间内再发，直接复用。",
        json_schema_extra=_ui("缓存保留天数", "默认 30 天。没找到的结果不缓存。"),
        ge=1,
        le=365,
    )
    show_reference: bool = Field(
        default=True,
        description="除了最佳匹配，是否把其他候选也列出来。关掉后只发最佳匹配。",
        json_schema_extra=_ui("展示其他候选"),
    )
    reference_limit: int = Field(
        default=3,
        description="第一次回复里，每个引擎最多列几条候选。",
        json_schema_extra=_ui("首次展示条数（每引擎）", "默认 3。每个引擎单独分组，各列几条。"),
        ge=1,
        le=50,
    )
    more_limit: int = Field(
        default=10,
        description="回复「更多」时，每个引擎最多列几条候选（这些候选都会带图）。",
        json_schema_extra=_ui("「更多」展示条数（每引擎）", "默认 10，基本等于全部结果。"),
        ge=1,
        le=50,
    )
    verify_top_n: int = Field(
        default=3,
        description="Yandex 和百度各下载前 N 条候选做指纹比对；Lens 更准，比对 2N 条。条数越多越慢。",
        json_schema_extra=_ui("每个引擎验证几条", "默认 3。调大更不容易漏，但会多下载几张图。"),
        ge=1,
        le=10,
    )
    deep_scan: bool = Field(
        default=True,
        description=(
            "深度检索：每个引擎不只取前几条，而是按下面的上限尽量多取，并加大指纹比对数量。"
            "能提高找出来的概率，但一次搜索会明显变慢。"
        ),
        json_schema_extra=_ui(
            "深度检索（取更多结果）",
            "默认打开。每个引擎按「深度检索时每引擎上限」取数，找出来的概率更高，代价是慢一倍左右；"
            "嫌慢可以关掉。",
        ),
    )
    deep_scan_limit: int = Field(
        default=20,
        description="深度检索时每个引擎最多取多少条候选。设大接近全量。",
        json_schema_extra=_ui(
            "深度检索时每引擎上限",
            "默认 20。想接近全量可以调到 60~100，但耗时和流量会跟着涨。SauceNAO 免费档每次最多 4 条，不受这个值影响。",
        ),
        ge=5,
        le=100,
    )
    drawing_other_limit: int = Field(
        default=3,
        description=(
            "SauceNAO 判定为插画后，其他引擎每边只留几条做参考，不再下载图片比对。"
            "插画库命中已经足够确定，没必要再逐张验证。"
        ),
        json_schema_extra=_ui(
            "插画路线每引擎留几条",
            "默认 3。判成插画后其他引擎只留这么多条参考，不下载图，速度快很多。",
        ),
        ge=1,
        le=10,
    )
    deep_more_limit: int = Field(
        default=20,
        description=(
            "深度检索时回复「更多」，每个引擎最多列几条候选。"
            "深度模式取到的条数比平时多，这个值要跟着放开才看得到全部结果。"
        ),
        json_schema_extra=_ui(
            "深度检索时「更多」条数（每引擎）",
            "默认 20。开着深度检索却只看到 10 条时，就是这个值没调。",
        ),
        ge=1,
        le=100,
    )
    deep_verify_top_n: int = Field(
        default=8,
        description="深度检索时每个引擎下载前 N 条候选做指纹比对。这个值直接决定耗时。",
        json_schema_extra=_ui(
            "深度检索时每引擎验证几条",
            "默认 8。调大更容易找出漏掉的结果，但每多一条就多下载一张图。",
        ),
        ge=1,
        le=30,
    )
    social_verify_all: bool = Field(
        default=True,
        description=(
            "照片路线优先核对社交平台：X、微博、小红书、Instagram、B站、pixiv 等站点的候选，"
            "即使超出每引擎验证配额，也补进去做一次指纹比对。原帖基本都在这些平台上。"
        ),
        json_schema_extra=_ui(
            "优先核对社交平台结果",
            "默认开。关掉后只按每引擎验证条数取前几条。",
        ),
    )
    social_verify_limit: int = Field(
        default=20,
        description="上面那个开关一次最多补验几条社交平台候选，防止平台结果太多把搜索拖长。",
        json_schema_extra=_ui(
            "社交平台补验上限",
            "默认 20。只有开了「优先核对社交平台结果」才生效。",
        ),
        ge=1,
        le=60,
    )
    image_wait_seconds: int = Field(
        default=60,
        description="用户只发了指令没带图时，插件等他补图的时间。",
        json_schema_extra=_ui("等补图的秒数", "默认 60 秒。这段时间内发一张图就会直接开始搜。"),
        ge=10,
        le=600,
    )
    hash_threshold: int = Field(
        default=8,
        description="两张图的指纹差异小于这个值才算同一张图。数字越小越严格，越大越容易把相似图当成同一张。",
        json_schema_extra=_ui("判为同一张图的严格程度", "默认 8（严格）。裁剪过的图常在 10~20，会被标成「疑似」。"),
        ge=0,
        le=32,
    )
    search_page_resolve: bool = Field(
        default=True,
        description=(
            "候选落在平台搜索页（如 search.bilibili.com/all?keyword=…）时，用页面上的"
            "关键词去反查具体账号，再拿那个号自己的投稿封面比指纹，对上了才当结论。"
            "只用关键词定范围，认人仍然靠指纹：同名不同人不会被认成一个人。"
        ),
        json_schema_extra=_ui(
            "搜索页反解账号",
            "默认开。落点是搜索列表页时用关键词反查账号；反解到但图对不上只写一行说明，不改结论。",
        ),
    )
    other_platforms: bool = Field(
        default=True,
        description=(
            "把候选里写明的其它平台账号（B站 / 微博 / TikTok / Instagram）收成线索；"
            "B 站拿投稿封面、微博翻带图帖去核这张图，核不上就只列账号。"
            "结论落到 X 号时，还会拿这个 handle 去微博和 B 站搜一次同名当线索。"
        ),
        json_schema_extra=_ui(
            "其它平台账号反查",
            "默认开。候选 URL 里写着 TikTok/IG/B站/微博 的号时，能核图的核图，核不上的列成线索；"
            "多花几秒，但同一个人常几个平台用一个昵称。",
        ),
    )
    engine_timeout: int = Field(
        default=50,
        description=(
            "单个搜索引擎每次请求最多等多少秒，超时就跳过它、继续用其他引擎的结果。"
            "Lens 要先上传图片再检索，是两步，实际按两倍算。"
        ),
        json_schema_extra=_ui(
            "单个引擎超时（秒）",
            "默认 50。Yandex 走代理容易抖，网络差可以再调到 60。Lens 自动按两倍给（默认 100 秒）。",
        ),
        ge=5,
        le=180,
    )
    download_timeout: int = Field(
        default=12,
        description="下载一张候选图最多等多少秒。",
        json_schema_extra=_ui("下载图片超时（秒）", "默认 12。"),
        ge=3,
        le=120,
    )
    proxy: str = Field(
        default="",
        description="主代理：所有搜索引擎、候选图下载、AI 检测都走它。留空表示直连。",
        json_schema_extra=_ui(
            "主代理地址",
            "强烈建议填写。例如 http://127.0.0.1:6789。Lens、Yandex 对这些服务的国内直连基本不通，不填代理通常搜不出结果。",
            placeholder="http://127.0.0.1:6789",
        ),
    )
    proxy_alt: str = Field(
        default="",
        description=(
            "备代理：主代理连不上（超时、TLS 被掐断、连接被重置）时自动换它重试同一个请求。"
            "留空表示不设备用线路。"
        ),
        json_schema_extra=_ui(
            "备代理地址（可选）",
            "可以配多个代理来保证更好的稳定性：主代理抽风时次代理顶上，不用等它恢复。"
            "两个代理用不同出口更好（实测不同出口拿到的站点完全不同）。",
            placeholder="http://127.0.0.1:10809",
        ),
    )
    allow_fake_ip: bool = Field(
        default=False,
        description=(
            "放行 fake-ip 的假地址段（Clash / mihomo 默认 198.18.0.0/15）。"
            "开着 fake-ip 时，候选图域名会被解析成这个段，默认被当成非公网地址拒绝，"
            "表现为候选图全部下载失败。具体放行哪些段由下面的 fake_ip_ranges 决定。"
        ),
        json_schema_extra=_ui(
            "放行 fake-ip 段",
            "用 OpenClash 等 fake-ip 模式时打开；普通网络保持关闭。只放行下面列出的段，其余内网地址照旧拒绝。",
        ),
    )
    fake_ip_ranges: list[str] = Field(
        default_factory=lambda: ["198.18.0.0/15"],
        description=(
            "fake-ip 实际使用的网段，仅在「放行 fake-ip 段」打开时生效。"
            "填你的 Clash / mihomo / sing-box 配置里的 fake-ip-range：mihomo 默认 198.18.0.1/16、"
            "sing-box 默认 198.18.0.0/15；改成过别的段（例如 100.64.0.0/10）就照它填。"
            "写错的条目会被跳过，不会变成全部放行。"
        ),
        json_schema_extra=_ui(
            "fake-ip 网段",
            "只在打开上一项后生效。默认 198.18.0.0/15；你的内核改了 fake-ip-range 就填改后的段，例如 100.64.0.0/10。",
            placeholder="198.18.0.0/15",
        ),
    )
    verbose_log: bool = Field(
        default=False,
        description="开启后把取图、每个引擎的耗时与错误、候选验证过程都写进日志，排查问题时打开，平时关着。",
        json_schema_extra=_ui("详细日志开关", "遇到问题打开它，重载插件，复现后把日志贴到 issues。"),
    )
    max_concurrent_tasks: int = Field(
        default=3,
        description="同时最多搜几张图，超过的排队。",
        json_schema_extra=_ui("同时搜索上限", "默认 3，够用就不用改。"),
        ge=1,
        le=10,
    )


class WeiboConfig(PluginConfigBase):
    """微博站内反查：读到「微博@某某」或 X 上查无此号时，去微博搜出主页和粉丝数。

    微博没有独立的英文 handle，昵称就是账号名，所以「按 handle 反查」等于按昵称
    搜人。搜索页免登录就能读（拿一次访客 cookie），所以不配 cookie 也能用；配了
    登录 cookie 就走登录态，结果更全、也不吃访客 cookie 那套偶发的 432。
    """

    __ui_label__ = "微博反查（读到微博号时核主页）"
    __ui_icon__ = "at-sign"
    __ui_order__ = 10

    enabled: bool = Field(
        default=True,
        description=(
            "读到「微博@某某」这类线索，或某个号在 X 上查无此人时，拿这个名字去微博搜一遍，"
            "把主页、昵称、粉丝数当线索写进结果。免登录即可用，不需要填 cookie。"
        ),
        json_schema_extra=_ui(
            "启用微博反查",
            "默认开。读到微博号就去核主页；只当线索，不改已有结论。",
        ),
    )
    sub: str = Field(
        default="",
        description=(
            "微博登录 cookie 里的 SUB 那一项的值（浏览器开发者工具 → Cookies → weibo.com）。"
            "和下面的 SUBP 一起填，两项都空就走免登录。带不带 `SUB=` 前缀都认。"
        ),
        json_schema_extra=_ui(
            "SUB",
            "从浏览器 Cookie 里复制 SUB 的值。约 7 天过期，过期后重填。留空=免登录。",
        ),
    )
    subp: str = Field(
        default="",
        description="微博登录 cookie 里的 SUBP 那一项的值，和 SUB 配套。",
        json_schema_extra=_ui("SUBP", "和 SUB 一起复制过来了就填上；只填 SUB 也能用。"),
    )
    cookie: str = Field(
        default="",
        description=(
            "老写法：整串 `SUB=...; SUBP=...`。留着是为了兼容已经这么填的配置，"
            "新配置请用上面的 SUB / SUBP 两格（填了它们就以它们为准）。"
        ),
        json_schema_extra=_ui(
            "整串 Cookie（老配置兼容）",
            "不用填。要填就填 `SUB=...; SUBP=...` 整段；拆开填上面两格更省事。",
        ),
    )
    min_fans: int = Field(
        default=200,
        description=(
            "粉丝数门槛。微博搜索结果按相关度排，同名的路人号能占满整页（实测搜「青生w」"
            "出来 15 个「阮青生w*」，粉丝 1~102），低于这个数的号不写进结果，只在结论里"
            "说一句「搜到的最多才几只粉」。"
        ),
        json_schema_extra=_ui("粉丝门槛", "默认 200。低于这个数的同名号当路人，不往结果里塞。"),
        ge=0,
        le=1_000_000,
    )
    repost_search: bool = Field(
        default=True,
        description=(
            "转载页反查：候选落在新浪这类转载页上时，抓页面正文去微博搜帖，"
            "或者按页面标出来的微博作者核号，再用帖子配图比指纹——对上了才换结论。"
            "只在配了登录 cookie、且这一单还没找到可靠出处时才会跑。"
        ),
        json_schema_extra=_ui(
            "转载页反查微博原帖",
            "默认开。需要登录 cookie；没配就一直跳过，行为和以前一样。",
        ),
    )
    by_name: bool = Field(
        default=True,
        description=(
            "标题里只写着名字、没写「微博」时，也拿这个名字去微博站内搜一次人。"
            "中文昵称在 X 上本来就搜不着，微博才是它的主场：图只落在图集站、页面不挂"
            "任何账号时，标题里那个名字往往就是唯一剩下的入口。同样只当线索，翻到她"
            "自己的带图帖、指纹对上了才换结论。"
        ),
        json_schema_extra=_ui(
            "按标题里的名字搜微博",
            "默认开。没写「微博」也搜；昵称、粉丝数都会写出来，对不上不改结论。",
        ),
    )


class NameHuntConfig(PluginConfigBase):
    """标题里的名字 → 各平台主页，X / 微博 的再翻本人带图帖比指纹。

    `s-20261122-2219-c077` 那单的图只落在 hotgirlpix / misskon 这类图集站上，页面
    一个社媒账号都不挂；标题里明明写着 coser 的名字，旧逻辑只拿它去 X 按显示名
    核对（她的 X 显示名不是那串中文），于是整单收在「只找到别的网站的转发」。
    这条腿把名字当线索铺开：一次网页检索捞出各平台主页，只有 X 与微博的会往下
    翻本人带图帖比指纹——对上了才换结论，其余平台一律只当线索。
    """

    __ui_label__ = "按名字找号（标题名 → 各平台主页）"
    __ui_icon__ = "user-search"
    __ui_order__ = 12

    enabled: bool = Field(
        default=True,
        description=(
            "结论还停在搬运站 / 图集站时，把标题里那个名字拿去各平台搜一次主页。"
            "X 与微博的会翻她自己的带图帖比指纹，对上了才换结论；Instagram、TikTok、"
            "B 站这些只列出来当线索。不调模型，每个名字只花一次网页检索额度。"
        ),
        json_schema_extra=_ui(
            "启用按名字找号",
            "默认开。名字含中文或够长的英文才查；图集站/搬运站仍然不能当出处。",
        ),
    )
    max_names: int = Field(
        default=2,
        description=(
            "一次最多拿几个名字去检索。标题里常并列好几条候选，两个足够盖住"
            "（同一处名字的不同写法），再多就是白花额度。"
        ),
        json_schema_extra=_ui("最多几个名字", "默认 2。每个名字一次网页检索。"),
        ge=1,
        le=5,
    )
    budget: float = Field(
        default=45.0,
        description=(
            "这一整段最多跑多少秒：检索 + 核 X 号 + 翻带图帖都算在里面。超了就"
            "拿手上已经有的线索收工，不把整单拖成几分钟。"
        ),
        json_schema_extra=_ui("总预算（秒）", "默认 45。超时只保留已经捞到的线索。"),
        ge=10,
        le=180,
    )


class ImageSearcherConfig(PluginConfigBase):
    """插件总配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    engines: EnginesConfig = Field(default_factory=EnginesConfig)
    lens: LensConfig = Field(default_factory=LensConfig)
    yandex: YandexConfig = Field(default_factory=YandexConfig)
    baidu: BaiduConfig = Field(default_factory=BaiduConfig)
    saucenao: SauceNaoConfig = Field(default_factory=SauceNaoConfig)
    serper: SerperConfig = Field(default_factory=SerperConfig)
    hosting: HostingConfig = Field(default_factory=HostingConfig)
    weibo: WeiboConfig = Field(default_factory=WeiboConfig)
    name_hunt: NameHuntConfig = Field(default_factory=NameHuntConfig)
    vision: VisionConfig = Field(default_factory=VisionConfig)
    grok: GrokConfig = Field(default_factory=GrokConfig)
    ai_detect: AiDetectConfig = Field(default_factory=AiDetectConfig)
    title_author: TitleAuthorConfig = Field(default_factory=TitleAuthorConfig)


# ---------------------------------------------------------------------------
# 消息里的图片解析
# ---------------------------------------------------------------------------


def _segments_of(message: Any) -> list[dict]:
    if not isinstance(message, dict):
        return []
    for key in ("message_segments", "raw_message"):
        value = message.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _segment_inline_image(segment: dict) -> str:
    for key in _BASE64_KEYS:
        value = segment.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    nested = segment.get("data")
    if isinstance(nested, dict):
        for key in _BASE64_KEYS:
            value = nested.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _segment_image_url(segment: dict) -> str:
    for key in _URL_KEYS:
        value = segment.get(key)
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            return value.strip()
    nested = segment.get("data")
    if isinstance(nested, dict):
        for key in _URL_KEYS:
            value = nested.get(key)
            if isinstance(value, str) and value.startswith(("http://", "https://")):
                return value.strip()
    if isinstance(nested, str) and nested.startswith(("http://", "https://")):
        return nested.strip()
    return ""


def _reply_target(segments: list[dict], message: dict) -> str:
    for segment in segments:
        if str(segment.get("type") or "") != "reply":
            continue
        data = segment.get("data")
        if isinstance(data, dict):
            target = data.get("target_message_id") or data.get("message_id") or ""
        else:
            target = data or ""
        if str(target).strip():
            return str(target).strip()
    return str(message.get("reply_to") or "").strip()


def _as_bool(value: Any) -> bool:
    """把工具参数转成布尔。模型偶尔会把 false 写成字符串。"""
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "no", "off", "none"}
    return bool(value)


def _decode_base64(value: str) -> bytes | None:
    payload = value.strip()
    if payload.startswith("base64://"):
        payload = payload[len("base64://") :]
    if payload.startswith("data:") and "," in payload:
        payload = payload.split(",", 1)[1]
    try:
        raw = base64.b64decode(payload, validate=False)
    except Exception:  # noqa: BLE001
        return None
    return raw or None


def _first_image(message: Any) -> tuple[bytes | None, str]:
    """从消息里取第一张图的字节与原始链接。"""
    for segment in _segments_of(message):
        if str(segment.get("type") or "") != "image":
            continue
        inline = _segment_inline_image(segment)
        if inline:
            raw = _decode_base64(inline)
            if raw:
                return raw, _segment_image_url(segment)
        url = _segment_image_url(segment)
        if url:
            return None, url
    return None, ""


def _proxy_of(value: str) -> str:
    """只有一个全局代理：direct/none/off/- 表示强制直连，其余原样返回。"""
    text = str(value or "").strip()
    if text.lower() in {"direct", "none", "off", "-"}:
        return ""
    return text


def _stream_id_of(message: Any) -> str:
    """钩子侧可能不给 stream_id，从消息里兜底取会话 ID。"""
    if not isinstance(message, dict):
        return ""
    for key in ("session_id", "stream_id", "chat_id"):
        value = message.get(key)
        if value:
            return str(value)
    info = message.get("message_info")
    if isinstance(info, dict):
        for group_key in ("group_info", "session_info"):
            group = info.get(group_key)
            if isinstance(group, dict):
                for key in ("session_id", "stream_id", "chat_id"):
                    if group.get(key):
                        return str(group[key])
    return ""


def _group_id_of(message: Any) -> str:
    """取群号，SauceNAO 的名单按群号匹配；私聊返回空。"""
    if not isinstance(message, dict):
        return ""
    info = message.get("message_info")
    if isinstance(info, dict):
        group = info.get("group_info")
        if isinstance(group, dict):
            for key in ("group_id", "id", "group_code"):
                value = group.get(key)
                if value:
                    return str(value)
    return ""


def _user_id_of(message: Any) -> str:
    """从消息字典里尽量挖出用户 ID：钩子侧和指令侧的结构不完全一致。"""
    if not isinstance(message, dict):
        return ""
    info = message.get("message_info")
    if isinstance(info, dict):
        user = info.get("user_info")
        if isinstance(user, dict):
            for key in ("user_id", "id", "uid"):
                if user.get(key):
                    return str(user[key])
    for key in ("user_id", "sender_id", "sender"):
        value = message.get(key)
        if isinstance(value, (str, int)) and str(value).strip():
            return str(value)
    return ""


def _has_image(message: Any) -> bool:
    return any(str(segment.get("type") or "") == "image" for segment in _segments_of(message))


def _send_ok(result: Any) -> bool:
    """send 能力返回 bool 或 {success: bool}，两种形态都要认。"""
    if isinstance(result, bool):
        return result
    if isinstance(result, dict):
        return bool(result.get("success", True))
    return result is not None


def _dedupe(names: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        result.append(name)
    return result


# ---------------------------------------------------------------------------
# 插件主体
# ---------------------------------------------------------------------------


class ImageSearcherPlugin(MaiBotPlugin):
    """以图搜出处插件。"""

    config_model = ImageSearcherConfig
    config_reload_subscriptions: ClassVar[tuple[str, ...]] = ()

    def __init__(self) -> None:
        super().__init__()
        self._tasks: TaskStore | None = None
        self._result_cache: resultcache.ResultCache | None = None
        self._pools: dict[str, KeyPool] = {}
        self._engines: dict[str, Any] = {}
        self._background: set[asyncio.Task] = set()
        self._pending: dict[tuple[str, str], float] = {}
        # 正在跑的任务：卸载时靠它把被打断的单记下来，重新加载后回一句。
        self._live: dict[str, dict[str, Any]] = {}
        # 等待补图时记住这次要不要无视缓存重搜（「盆地找鸡 重搜」后只发图）
        self._pending_force: dict[tuple[str, str], bool] = {}
        self._pending_timers: dict[tuple[str, str], asyncio.Task] = {}
        # 跑到 3 分钟还没出结果的任务，各挂一个补话定时器。
        self._slow_pings: dict[str, asyncio.Task] = {}
        # 合并转发节点的 uin 不能为空，否则 QQ 端会提示消息异常；从入站消息里记机器人账号。
        self._self_id: str = ""
        # SauceNAO 的库策略按群配置，但 stream_id 是哈希，需要群号映射。
        self._stream_groups: dict[str, str] = {}
        # 消息入库有延迟：指令触发时按 ID 查库往往还是空，所以在入站钩子里先存一份二进制。
        self._image_cache: dict[str, tuple[float, bytes]] = {}
        # 引擎请求与候选图验证分池：引擎是长请求，验证是短下载，
        # 共用一个默认线程池会让验证排队，wait_for 的超时把排队时间也算进去。
        self._engine_pool = ThreadPoolExecutor(max_workers=16, thread_name_prefix="imgsearch-engine")
        self._verify_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="imgsearch-verify")

    # -- 日志 -------------------------------------------------------------

    def _vlog(self, message: str, *args: Any) -> None:
        """详细日志：开关打开时写 info（默认可见），关闭时降到 debug。"""
        verbose = False
        try:
            verbose = bool(self.config.plugin.verbose_log)
        except Exception:  # noqa: BLE001 - 配置尚未注入时按关闭处理
            verbose = False
        if verbose:
            self.ctx.logger.info(message, *args)
        else:
            self.ctx.logger.debug(message, *args)

    # -- 配置兼容 ---------------------------------------------------------

    def normalize_plugin_config(self, config_data: dict[str, Any] | None) -> tuple[dict[str, Any], bool]:
        """迁移老字段，并按模型补齐所有配置节。

        补齐是给 WebUI 用的：配置页只拿到归一化结果，缺节就渲染不出那一栏。
        """
        migrated, moved = migrate_legacy_sections(config_data)
        merged, filled = merge_plugin_config_data(build_plugin_default_config(ImageSearcherConfig), migrated)
        return merged, moved or filled

    # -- 生命周期 ---------------------------------------------------------

    async def on_load(self) -> None:
        self._rebuild()
        restored = self._tasks.load() if self._tasks else 0
        enabled = [name for name in self.config.engines.priority if self._enabled(name)]
        self.ctx.logger.info(
            "%s已加载：指令=%s，引擎=%s，上限=%s，详细日志=%s，恢复任务=%s",
            PLUGIN_NAME,
            COMMAND,
            "、".join(enabled) or "无",
            self._describe_limits(),
            "开" if self.config.plugin.verbose_log else "关",
            restored,
        )
        if not enabled:
            self.ctx.logger.warning("没有启用任何搜索引擎，插件将无法返回结果")
        self._vlog("指令正则：%s", _COMMAND_PATTERN)
        # 加载阶段 send 通道未必就绪，延后几秒再补话，失败了也只记日志。
        waiting = asyncio.create_task(self._notify_interrupted())
        self._background.add(waiting)
        waiting.add_done_callback(self._background.discard)

    async def on_unload(self) -> None:
        self._save_interrupted()
        for task in list(self._background):
            task.cancel()
        self._background.clear()
        for timer in self._pending_timers.values():
            timer.cancel()
        self._pending_timers.clear()
        for timer in self._slow_pings.values():
            timer.cancel()
        self._slow_pings.clear()
        self._pending.clear()
        self._image_cache.clear()
        self._engine_pool.shutdown(wait=False, cancel_futures=True)
        self._verify_pool.shutdown(wait=False, cancel_futures=True)
        self.ctx.logger.info("%s已卸载", PLUGIN_NAME)

    # -- 重启打断 ---------------------------------------------------------

    def _interrupted_path(self) -> Path:
        return Path(self.ctx.paths.data_dir) / _INTERRUPTED_FILE

    def _save_interrupted(self) -> None:
        """卸载前把还在跑的单写到盘上。

        插件运行时是整块重启的：任何插件文件被改动都会触发，进程一换，
        正在搜的那一单连同 asyncio 任务一起消失，群里只会看到「正在搜图」。
        """
        now = time.time()
        snapshot = {
            task_id: dict(payload)
            for task_id, payload in self._live.items()
            if now - float(payload.get("started_at") or 0) <= _INTERRUPTED_MAX_AGE
        }
        self._live.clear()
        if not snapshot:
            return
        try:
            self._interrupted_path().write_text(
                json.dumps(snapshot, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            self._vlog("已记下被打断的任务：%s", "、".join(snapshot))
        except Exception as error:  # noqa: BLE001 - 记不下来本来也不影响卸载
            self.ctx.logger.warning("记录被打断的任务失败：%s", error)

    async def _notify_interrupted(self) -> None:
        path = self._interrupted_path()
        if not path.is_file():
            return
        # 加载阶段 send 通道未必就绪，等几秒再发；失败只记日志，不重试。
        await asyncio.sleep(_INTERRUPTED_DELAY)
        try:
            snapshot = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            snapshot = {}
        try:
            for task_id, payload in sorted(snapshot.items()):
                stream_id = str((payload or {}).get("stream_id") or "")
                if not stream_id:
                    continue
                minutes = max(1, int((time.time() - float((payload or {}).get("started_at") or 0)) / 60))
                sent = await self.ctx.send.text(
                    _INTERRUPTED_TEXT.format(task_id=task_id, minutes=minutes),
                    stream_id,
                )
                if not _send_ok(sent):
                    self.ctx.logger.warning("中断提示发送失败：task=%s result=%s", task_id, sent)
                    continue
                self._vlog("中断提示已发送：task=%s 发往=%s", task_id, stream_id)
        finally:
            try:
                path.unlink()
            except OSError:
                pass

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        if scope != "self":
            return
        self._rebuild(keep_tasks=True)
        section = config_data.get("plugin") if isinstance(config_data, dict) else None
        if isinstance(section, dict):
            configured = str(section.get("command") or "").strip()
            if configured and configured != COMMAND:
                self.ctx.logger.warning(
                    "搜图指令已改为 %s，但装饰器不会随配置热更新（当前仍是 %s），需重载插件才生效",
                    configured,
                    COMMAND,
                )
        self.ctx.logger.info("%s配置已更新：上限=%s，版本=%s", PLUGIN_NAME, self._describe_limits(), version or "无")

    def _describe_limits(self) -> str:
        parts = []
        for name in self.config.engines.priority:
            engine = getattr(self.config, name, None)
            if engine is None:
                continue
            parts.append(f"{name}={engine.max_results}")
        return "、".join(parts) or "无"

    def _remember_key_quota(self, pool: str, snapshot: dict[str, float]) -> None:
        """某个池的额度用尽状态变了，写回磁盘。

        snapshot 是这个池当前所有「用尽」记录，整份覆盖；空就删掉这一段。
        """
        if not pool:
            return
        state = getattr(self, "_key_quota", None)
        if state is None:
            state = self._key_quota = {}
        if snapshot:
            state[pool] = dict(snapshot)
        else:
            state.pop(pool, None)
        keystate.save(self.ctx.paths.data_dir, state)

    def _rebuild(self, keep_tasks: bool = True) -> None:
        config = self.config
        section = config.plugin
        if not keep_tasks or self._tasks is None:
            self._tasks = TaskStore(self.ctx.paths.data_dir, section.task_ttl_minutes * 60)
        else:
            self._tasks.set_ttl(section.task_ttl_minutes * 60)

        self._result_cache = (
            resultcache.ResultCache(
                self.ctx.paths.data_dir,
                ttl_seconds=max(1, section.cache_days) * 86400,
            )
            if section.cache_enabled
            else None
        )

        global_proxy = self._proxy()
        # 额度用尽的记忆落盘在 data 目录，重启和改配置都不会丢；key 只存哈希。
        self._key_quota = keystate.load(self.ctx.paths.data_dir)
        self._pools = {
            "lens": KeyPool(
                config.lens.keys,
                name="lens",
                exhausted=self._key_quota.get("lens"),
                on_change=self._remember_key_quota,
            ),
            "saucenao": KeyPool(
                config.saucenao.keys,
                name="saucenao",
                exhausted=self._key_quota.get("saucenao"),
                on_change=self._remember_key_quota,
            ),
            "ai": KeyPool(
                config.ai_detect.keys,
                name="ai",
                exhausted=self._key_quota.get("ai"),
                on_change=self._remember_key_quota,
            ),
        }
        # Serper 单独一个池：它跟 SerpAPI 不是一家，key 也不通用，别混进 lens 池。
        # 总开关关掉或者没填 key，这个池就不存在，后面各处自然跳过它。
        serper_keys = list(config.serper.keys) if config.serper.enabled else []
        if serper_keys:
            self._pools["serper"] = KeyPool(
                serper_keys,
                name="serper",
                exhausted=self._key_quota.get("serper"),
                on_change=self._remember_key_quota,
            )
        # Lens 只在「允许替补」且「允许上传图床」时才把 Serper 那把挂上去。
        lens_serper_pool = self._pools.get("serper") if config.serper.lens_fallback else None
        if lens_serper_pool is not None and not config.serper.upload:
            lens_serper_pool = None
        self._engines = {
            "lens": LensEngine(
                self._pools["lens"],
                global_proxy,
                section.engine_timeout,
                config.lens.retries,
                config.lens.retry_delay,
                serper_pool=lens_serper_pool,
                note=self._vlog,
                hosting=config.hosting.order(),
            ),
            "yandex": YandexEngine(global_proxy, section.engine_timeout, config.yandex.routes),
            "baidu": BaiduEngine(global_proxy, section.engine_timeout),
            "saucenao": SauceNaoEngine(
                self._pools["saucenao"],
                global_proxy,
                section.engine_timeout,
                config.saucenao.min_similarity,
                db_full=config.saucenao.db,
                db_strict=config.saucenao.db_strict,
                policy_enabled=config.saucenao.db_policy_enabled,
                invert=config.saucenao.db_policy_invert,
                groups=config.saucenao.db_full_groups,
                group_of=self._group_of,
            ),
        }

    def _group_of(self, stream_id: str) -> str:
        """会话 ID → 群号，SauceNAO 的名单按群号匹配用。"""
        if not stream_id:
            return ""
        return self._stream_groups.get(stream_id, "")

    def _remember_group(self, stream_id: str, group_id: str) -> None:
        self._stream_groups[stream_id] = group_id
        if len(self._stream_groups) > 200:
            self._stream_groups.pop(next(iter(self._stream_groups)), None)

    def _enabled(self, name: str) -> bool:
        config = getattr(self.config, name, None)
        if config is None or not config.enabled:
            return False
        if name in {"lens", "saucenao"} and not config.keys:
            return False
        return True

    def _stream_id(self, kwargs: dict) -> str:
        message = kwargs.get("message")
        if isinstance(message, dict):
            value = message.get("session_id") or message.get("stream_id") or message.get("chat_id")
            if value:
                return str(value)
        for key in ("stream_id", "session_id", "chat_id"):
            if kwargs.get(key):
                return str(kwargs[key])
        return ""

    def _user_id(self, kwargs: dict) -> str:
        message = kwargs.get("message")
        if isinstance(message, dict):
            info = message.get("message_info")
            if isinstance(info, dict):
                user = info.get("user_info")
                if isinstance(user, dict) and user.get("user_id"):
                    return str(user["user_id"])
            if message.get("user_id"):
                return str(message["user_id"])
        return str(kwargs.get("user_id") or "")

    def _message_id(self, kwargs: dict) -> str:
        """消息 ID 可能在 kwargs 顶层，也可能在 message / message_base_info 里。"""
        if kwargs.get("message_id"):
            return str(kwargs["message_id"])
        message = kwargs.get("message")
        if not isinstance(message, dict):
            return ""
        if message.get("message_id"):
            return str(message["message_id"])
        base = message.get("message_base_info")
        if isinstance(base, dict) and base.get("message_id"):
            return str(base["message_id"])
        return ""

    def _latest_task(self, stream_id: str, user_id: str) -> SearchTask | None:
        """先按 群+用户 找，再退回本群最近一条；Tool 与 Command 的 user_id 未必一致。"""
        if self._tasks is None:
            return None
        return self._tasks.latest_for(stream_id, user_id) or self._tasks.latest_in_stream(stream_id)

    def _proxy(self) -> list[str]:
        """引擎、候选图下载、AI 检测共用的线路：主代理在前，备代理顶上。

        返回的是「线路列表」而不是一个地址：底层 ``make_session`` /
        ``download_image_multi`` 认得这种列表，主代理连接层失败时会自己换下一条。
        """
        primary = _proxy_of(self.config.plugin.proxy)
        spare = _proxy_of(getattr(self.config.plugin, "proxy_alt", ""))
        routes = [route for route in (primary, spare) if route]
        # 一个代理都没配时才退化成「直连」这一条线路。
        return routes or [""]

    def _fake_ip_nets(self) -> tuple:
        """按配置解析出 fake-ip 放行网段；开关关着就是空元组。"""
        section = self.config.plugin
        return fake_ip_networks(section.allow_fake_ip, section.fake_ip_ranges)

    # -- 取图 -------------------------------------------------------------

    @HookHandler(
        "chat.receive.before_process",
        name="inbound_image_cache",
        description="在图片二进制被清理前缓存一份，供搜图按消息 ID 取图",
        mode=HookMode.OBSERVE,
    )
    async def cache_inbound_image(self, message: Any = None, stream_id: str = "", **kwargs: Any):
        del kwargs
        if not isinstance(message, dict):
            return True, True, None, None, None
        try:
            account_id = str(message.get("account_id") or message.get("self_id") or "").strip()
            if account_id:
                self._self_id = account_id
            group_id = _group_id_of(message)
            if group_id:
                key = stream_id or _stream_id_of(message)
                if key:
                    self._remember_group(key, group_id)
            raw = self._remember_image(message)
            # 等待窗口内的补图直接在这里接住：靠指令正则的话，VLM 没出描述时
            # 消息文本是空字符串，压根匹配不上。
            if raw:
                self._catch_pending_image(raw, message, stream_id)
        except Exception as error:  # noqa: BLE001
            self.ctx.logger.warning("缓存入站图片失败：%s", error)
        return True, True, None, None, None

    def _catch_pending_image(self, raw: bytes, message: dict, stream_id: str) -> None:
        current = stream_id or _stream_id_of(message)
        if not current:
            self._vlog("入站图片没有会话 ID，无法匹配等待窗口：id=%s", message.get("message_id"))
            return
        user_id = _user_id_of(message)
        key = self._match_pending(current, user_id)
        if key is None:
            if self._pending:
                self._vlog(
                    "入站图片未匹配到等待窗口：stream=%s user=%s pending=%s",
                    current,
                    user_id,
                    list(self._pending),
                )
            return
        task = asyncio.create_task(self._start_from_pending(raw, key))
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    def _match_pending(self, stream_id: str, user_id: str) -> tuple[str, str] | None:
        """只认同会话同一用户的等待窗口：群里可能同时有好几个人在等图。"""
        if not user_id:
            return None
        now = time.time()
        for key, deadline in self._pending.items():
            if key[0] == stream_id and key[1] == user_id and deadline >= now:
                return key
        return None

    async def _start_from_pending(self, raw: bytes, key: tuple[str, str]) -> None:
        if self._pending.pop(key, None) is None:
            return
        force = bool(self._pending_force.pop(key, False))
        timer = self._pending_timers.pop(key, None)
        if timer is not None:
            timer.cancel()
        current, user_id = key
        try:
            self._start_task(raw, "", current, user_id, notify=True, force=force)
        except Exception as error:  # noqa: BLE001
            self.ctx.logger.warning("等待窗口补图启动搜图失败：%s", error)
            await self.ctx.send.text(f"启动搜图失败：{error}", current)
            return
        self.ctx.logger.info("等待窗口内收到图片，开始搜图：stream=%s user=%s", current, user_id)
        await self.ctx.send.text(f"🔍 收到图片，开始搜图，{formatting.eta_full()}。", current)

    def _remember_image(self, message: dict) -> bytes | None:
        message_id = str(message.get("message_id") or "").strip()
        if not message_id:
            return None
        for segment in _segments_of(message):
            if str(segment.get("type") or "") != "image":
                continue
            inline = _segment_inline_image(segment)
            raw = _decode_base64(inline) if inline else None
            if not raw:
                continue
            self._image_cache[message_id] = (time.time(), raw)
            self._vlog("已缓存入站图片：id=%s 体积=%sKB", message_id, len(raw) // 1024)
            self._prune_image_cache()
            return raw
        self._prune_image_cache()
        return None

    def _prune_image_cache(self, ttl: float = 1800.0, keep: int = 60) -> None:
        now = time.time()
        expired = [mid for mid, (stamp, _) in self._image_cache.items() if now - stamp > ttl]
        for mid in expired:
            self._image_cache.pop(mid, None)
        if len(self._image_cache) <= keep:
            return
        ordered = sorted(self._image_cache.items(), key=lambda item: item[1][0])
        for mid, _ in ordered[: len(self._image_cache) - keep]:
            self._image_cache.pop(mid, None)

    def _cached_image(self, message_id: str) -> bytes | None:
        entry = self._image_cache.get(message_id)
        return entry[1] if entry else None

    async def _resolve_image(
        self,
        kwargs: dict,
        *,
        allow_recent: bool = False,
    ) -> tuple[bytes | None, str, str, int]:
        """返回 (图片字节, 图片链接, 来源说明, 过期秒数)。

        过期秒数大于 0 表示「找到了图，但早于取图窗口」——这种情况要让模型去引导用户引用，
        而不是笼统说一句「没有图片」。
        """
        message = kwargs.get("message")
        stream_id = self._stream_id(kwargs)
        segments = _segments_of(message)

        raw, inline_url = _first_image(message)
        url = inline_url
        if raw:
            return raw, url, "当前消息", 0

        message_id = self._message_id(kwargs)
        if message_id:
            cached = self._cached_image(message_id)
            if cached:
                return cached, "", "当前消息", 0

        # Command 传入的消息不带二进制，先按 message_id 找 Host 要一次，
        # 比直接下载 QQ 图链更稳（图链可能过期或被防盗链挡）。
        if message_id:
            found_raw, found_url = await self._image_by_id(message_id, stream_id)
            if found_raw:
                return found_raw, found_url or url, "当前消息", 0

        if url:
            downloaded = await self._download(url)
            if downloaded:
                return downloaded, url, "当前消息", 0

        reply_id = _reply_target(segments, message if isinstance(message, dict) else {})
        if reply_id:
            found_raw, found_url = await self._image_by_id(reply_id, stream_id)
            if found_raw:
                return found_raw, found_url, "引用的消息", 0

        if allow_recent and stream_id:
            found_raw, found_url, stale = await self._recent_images(stream_id)
            if found_raw:
                return found_raw, found_url, "最近的图片消息", 0
            if stale:
                return None, url, "", stale

        return None, url, "", 0

    async def _image_by_id(self, message_id: str, stream_id: str) -> tuple[bytes | None, str]:
        if not message_id:
            return None, ""
        cached = self._cached_image(message_id)
        if cached:
            return cached, ""
        # message_id 全局唯一，先不带 chat_id 查：宿主按 session_id 精确匹配，
        # 而指令侧拿到的 stream_id 和库里存的 session_id 未必同一种形式，带上反而查不到。
        for chat_id in ("", stream_id):
            try:
                result = await self.ctx.message.get_by_id(
                    message_id,
                    chat_id=chat_id,
                    include_binary_data=True,
                )
            except Exception as error:  # noqa: BLE001
                self.ctx.logger.warning("按消息 ID 取图失败：%s", error)
                continue
            payload = unwrap_capability(result, "message")
            if not isinstance(payload, dict):
                continue
            raw, url = _first_image(payload)
            if raw is None and url:
                raw = await self._download(url)
            if raw:
                self._image_cache[message_id] = (time.time(), raw)
                self._prune_image_cache()
                if chat_id:
                    self._vlog("按消息 ID 取图成功（带 chat_id）：id=%s", message_id)
                return raw, url
        self.ctx.logger.warning("按消息 ID 没有取回消息体：id=%s", message_id)
        return None, ""

    async def _recent_images(self, stream_id: str) -> tuple[bytes | None, str, int]:
        """返回 (图片字节, 链接, 过期秒数)。过期秒数只在「有图但太旧」时非 0。"""
        try:
            result = await self.ctx.message.get_recent(chat_id=stream_id, limit=6)
        except Exception as error:  # noqa: BLE001
            self.ctx.logger.warning("读取最近消息失败：%s", error)
            return None, "", 0
        messages = unwrap_capability(result, "messages")
        if not isinstance(messages, list):
            return None, "", 0
        now = time.time()
        stale = 0
        for item in reversed(messages):
            if not _has_image(item):
                continue
            try:
                stamp = float(item.get("timestamp") or 0)
            except (TypeError, ValueError):
                stamp = 0.0
            # 只认最近 3 分钟内的图，避免模型在闲聊时抓到很久以前的图
            if stamp and now - stamp > 180:
                stale = max(stale, int(now - stamp))
                continue
            found_raw, found_url = await self._image_by_id(str(item.get("message_id") or ""), stream_id)
            if found_raw:
                return found_raw, found_url, 0
        return None, "", stale

    async def _download(self, url: str) -> bytes | None:
        try:
            return await asyncio.to_thread(
                download_image_multi,
                url,
                self._proxy(),
                self.config.plugin.download_timeout,
                allow_nets=self._fake_ip_nets(),
            )
        except HttpError as error:
            self.ctx.logger.info("图片下载失败（两条线路都试过）：%s → %s", url[:80], error)
            return None

    async def _best_image(
        self,
        best: Candidate | None,
        ordered: list[Candidate],
        upload: bytes | None,
        picked: list[str] | tuple[str, ...] = (),
    ) -> tuple[str, str]:
        """给结果配图：多个候选并发下载，按优先级取第一张成功的。

        best 为空（没找到可靠出处）时，只有「像得够近」的候选才配图：距离超过
        ``REFERENCE_MAX_DISTANCE`` 的一律不配，免得群里出现「参考图…不是同一张」
        这种自相矛盾的交代（2504 那单给了张距离 27 的同款球衣）。一张都没过线时
        宁可不配图，也不退回用户原图凑数。
        """
        pool = reference_candidates(
            best,
            ordered,
            picked,
            max_distance=None if best is not None else REFERENCE_MAX_DISTANCE,
        )
        if best is None and not pool:
            self._vlog(
                "参考图：没有距离 ≤%s 的候选（或没算出距离），这单不配参考图",
                REFERENCE_MAX_DISTANCE,
            )
            return "", ""
        urls: list[str] = []
        for item in pool:
            if item is None:
                continue
            if item.image_url and item.image_url not in urls:
                urls.append(item.image_url)
            if len(urls) >= 4:
                break
        if urls:
            results = await asyncio.gather(*(self._download(url) for url in urls), return_exceptions=True)
            for url, data in zip(urls, results):
                if isinstance(data, BaseException) or not data:
                    continue
                try:
                    compact, _ = prepare_upload(data, limit=1_200_000, as_jpeg=True)
                except Exception:  # noqa: BLE001 - 解不开就换下一张
                    continue
                return base64.b64encode(compact).decode("ascii"), url
        if upload is None:
            return "", ""
        compact, _ = prepare_upload(upload, limit=1_200_000, as_jpeg=True)
        return base64.b64encode(compact).decode("ascii"), "upload"

    async def _ensure_image(self, task: SearchTask) -> None:
        """重放或磁盘恢复的任务没有图片时，按候选图地址补一张。"""
        outcome = task.outcome
        if outcome is None or outcome.image_b64:
            return
        if outcome.best is None and not outcome.candidates:
            return
        image_b64, source = await self._best_image(
            outcome.best,
            outcome.candidates,
            None,
            (outcome.vision or {}).get("picked_images") or (),
        )
        if image_b64:
            outcome.image_b64 = image_b64
            outcome.image_source = source

    _DISPLAY_IMAGE_BYTES = 60 * 1024
    _DISPLAY_CONCURRENCY = 5

    async def _display_image(self, url: str, semaphore: asyncio.Semaphore) -> str:
        """下载一张候选图并压成展示用大小，失败返回空串。"""
        async with semaphore:
            raw = await self._download(url)
        if not raw:
            return ""
        try:
            compact, _mime = await asyncio.to_thread(prepare_upload, raw, self._DISPLAY_IMAGE_BYTES, as_jpeg=True)
        except Exception:  # noqa: BLE001 - 解不开就不附图，不影响文字结果
            return ""
        return base64.b64encode(compact).decode("ascii")

    async def _ensure_engine_images(self, task: SearchTask, *, limit: int, whole: bool) -> None:
        """给参考区的候选配图。

        首次回复给每个引擎最接近的那张，回「更多」时把展示范围内的候选全配上。
        某个引擎最接近的那张即便已经在顶部当过参考图，这里也照样补一份，否则
        那个引擎的列表看起来就像没配图。展示图压到 100KB 左右，避免合并转发
        因为体积太大被拒。
        """
        outcome = task.outcome
        if outcome is None or not outcome.candidates:
            return
        if outcome.best is not None and not whole:
            # 真有结论时首次回复只给结论，参考区要等「更多」，不必提前下载这些配图。
            # 没有结论时不能跳：没搜到那条要逐引擎摆一条带图的，图得先下好。
            # （s-20261114-1007-5878 就是因为 status=ok 但 best 为空被跳过，整条转发 0 张图。）
            return
        targets: list[str] = []
        for engine, items in formatting.engine_groups(outcome, outcome.best, limit):
            if not whole and outcome.best is not None and engine == outcome.best.engine:
                continue  # 首次回复只补其他引擎的图，命中那条所在引擎不重复给
            picked = items if whole else items[:1]
            targets.extend(item.image_url for item in picked if item.image_url)
        if not whole and outcome.best is None:
            # 没搜到那条里视觉单独占一条，它挑中的图不一定在各引擎的第一条里，
            # 光靠上面那轮会把视觉配成空白。
            picked = [str(url) for url in (outcome.vision or {}).get("picked_images") or [] if url]
            targets.extend(picked[:1])
            if not picked:
                # 视觉没挑中、只给了「最像的」那一条时，那张也要有图。
                fallback = next(
                    (
                        item.image_url
                        for item in outcome.candidates
                        if item.image_url and downgrade_kind(item) == "vision"
                    ),
                    "",
                )
                if fallback:
                    targets.append(fallback)
        pending: list[str] = []
        for url in targets:
            if url in outcome.images or url in pending:
                continue
            pending.append(url)
        if not pending:
            return
        semaphore = asyncio.Semaphore(self._DISPLAY_CONCURRENCY)
        results = await asyncio.gather(*(self._display_image(url, semaphore) for url in pending))
        for url, data in zip(pending, results):
            if data:
                outcome.images[url] = data
        self._vlog(
            "参考区配图：task=%s 目标=%s 成功=%s",
            task.task_id,
            len(pending),
            sum(1 for data in results if data),
        )

    # -- 搜索 -------------------------------------------------------------

    def _call_engine(self, name: str, image: bytes, image_url: str, stream_id: str) -> list[Candidate]:
        engine = self._engines.get(name)
        if engine is None:
            raise EngineError(f"未知引擎 {name}")
        return engine.search(image, image_url, stream_id)

    def _engine_deadline(self, name: str) -> float:
        """外部给某个引擎的总时限。

        大多数引擎一次只发一个请求，engine_timeout 加 5 秒余量就够。Lens 不一样：
        它得先上传图片拿 image_id、再拿 image_id 检索，是两步，得按两步给。否则上
        传那一步稍慢，整条 lens 就先被外层砍掉，内部的重传和 Serper 补枪根本没跑
        到，白占额度还拿不到结果（s-20261114-1646-19f7 就是这么丢的）。
        """
        engine = self._engines.get(name)
        factor = float(getattr(engine, "deadline_factor", 1) or 1)
        return float(self.config.plugin.engine_timeout) * factor + 5.0

    async def _search(self, raw: bytes, image_url: str = "", stream_id: str = "", force: bool = False) -> SearchOutcome:
        config = self.config
        section = config.plugin
        started = time.time()
        # 「怎么找的」记录：每一步的触发与结果，最后只进「更多」。
        trace: list[dict] = []
        target_hash = dhash(raw)
        upload, _mime = prepare_upload(raw)

        cache = self._result_cache
        cache_key = ""
        if cache is not None and upload:
            cache_key = resultcache.cache_key(upload, ENGINES_VERSION)
            hit = None if force else cache.get(cache_key)
            if hit is not None:
                outcome = SearchOutcome.from_summary(hit.payload)
                outcome.cached = True
                outcome.cached_at = hit.stored_at
                outcome.image_b64, outcome.image_source = await self._best_image(
                    outcome.best,
                    outcome.candidates,
                    upload,
                    (outcome.vision or {}).get("picked_images") or (),
                )
                self._vlog(
                    "按图缓存命中：状态=%s 候选=%s 存于=%s（%.1f 天前）",
                    outcome.status,
                    outcome.total_candidates,
                    time.strftime("%Y-%m-%d %H:%M", time.localtime(hit.stored_at)),
                    hit.age_seconds / 86400,
                )
                return outcome

        names = [name for name in config.engines.priority if self._enabled(name)]
        if not names:
            raise RuntimeError("没有启用任何搜索引擎，请先在配置里开启至少一个引擎")

        loop = asyncio.get_running_loop()
        coros = [
            asyncio.wait_for(
                loop.run_in_executor(self._engine_pool, self._call_engine, name, upload, image_url, stream_id),
                timeout=self._engine_deadline(name),
            )
            for name in names
        ]
        settled = await asyncio.gather(*coros, return_exceptions=True)

        grouped: dict[str, list[Candidate]] = {}
        errors: dict[str, str] = {}
        engine_counts: dict[str, int] = {}
        # 深度检索：每个引擎按 deep_scan_limit 取数（不超过引擎实际返回），换更高的命中概率。
        deep = bool(section.deep_scan)
        deep_cap = max(1, int(section.deep_scan_limit))
        for name, outcome in zip(names, settled):
            if isinstance(outcome, BaseException):
                errors[name] = f"{type(outcome).__name__}: {str(outcome)[:120]}"
                continue
            engine_counts[name] = len(outcome)
            limit = max(1, int(getattr(config, name).max_results))
            if deep:
                limit = max(limit, deep_cap)
            grouped[name] = outcome[:limit]

        candidates: list[Candidate] = []
        for name in names:
            candidates.extend(grouped.get(name, []))
        total = len(candidates)
        self._vlog(
            "引擎返回：%s；错误：%s；原始候选=%s；深度检索=%s",
            {name: engine_counts.get(name, 0) for name in names},
            errors or "无",
            total,
            "开" if deep else "关",
        )

        # 结果驱动分流：SauceNAO 索引的是插画站点，真人照片基本没有高相似度命中。
        # 这一步只看相似度、不需要下载图片，所以放在验证之前。
        sauce = self._sauce_route(candidates)
        route = "drawing" if sauce else "photo"
        if sauce:
            route_reason = f"SauceNAO 相似度 {sauce.similarity:g}%"
        else:
            # 照片路线也要说得出为什么：绝大多数疑问是「明明是插画，怎么没走
            # pixiv」。pixiv 只有 SauceNAO 能带出来，它没认出来就没有结果。
            threshold = self.config.saucenao.route_similarity
            top_sauce = max(
                (
                    float(item.similarity or 0)
                    for item in candidates
                    if item.engine == "saucenao"
                ),
                default=0.0,
            )
            if not self._enabled("saucenao"):
                route_reason = "SauceNAO 没开，插画路线不会命中"
            elif top_sauce:
                route_reason = f"SauceNAO 最高 {top_sauce:g}%，不到插画阈值 {threshold:g}%"
            else:
                route_reason = "SauceNAO 一条都没命中"
        # 回复页反查没成功时留给 Grok 的一句话（成功就是空串）。
        reply_hint = ""
        # 搜索页反解的结果（只在照片路线、候选落在平台搜索页时才有）。
        search_page_info: dict | None = None
        if sauce:
            names = _dedupe([name for name in config.engines.priority_drawn if name in names] + names)
            # 插画路线：SauceNAO 自带相似度已经能定论，再逐张下载比对没有意义。
            # 其他引擎只留几条意思一下，全部不做下载验证。
            other_limit = max(1, int(section.drawing_other_limit))
            for name in names:
                if name == "saucenao":
                    continue
                if name not in grouped:
                    continue
                grouped[name] = grouped[name][:other_limit]
                # 这些候选没下载比对过，状态和备注都要如实改写，
                # 否则会把引擎自带的「精确匹配」标签和账号原样发出去。
                for item in grouped[name]:
                    item.status = "unverified"
                    item.note = "插画路线参考，未做指纹比对"
            candidates = [item for name in names for item in grouped.get(name, [])]
        else:
            await self._verify(candidates, grouped, names, target_hash)
            # 命中落在站点的标签／分类列表页上时，那不是出处：页面本身是一整页缩略图。
            # 顺着那张缩略图找到它所属的那篇帖子，再核一次指纹，对上了才当结论。
            expanded = await self._expand_listing_pages(candidates, target_hash)
            if expanded:
                candidates.extend(expanded)
            self._trace(
                trace,
                "列表页挖层",
                "hit" if expanded else "none",
                (
                    f"从列表页挖到 {len(expanded)} 篇具体帖子，指纹也对上了"
                    if expanded
                    else "命中的不是列表页，不用往下挖"
                ),
            )
            # 候选落在平台搜索页（`search.bilibili.com/all?keyword=…`）时，页面本身
            # 解析不出发布者；用页面上的关键词反解出具体账号，再用指纹实锤。
            resolved, resolve_state, resolve_note = await self._resolve_search_pages(
                candidates, target_hash
            )
            if resolved:
                candidates.extend(resolved)
            self._trace(trace, "搜索页反解", resolve_state, resolve_note)
            if resolve_state != "skip":
                # 这句要在结论里露出来：反解到号但图对不上的时候，用户至少知道
                # 这个方向已经试过了，而不是以为插件没看这页。
                search_page_info = {"state": resolve_state, "note": resolve_note}
            # 电商同款不进参考区，但要在过程里留一笔：否则用户看到「候选 60 条」
            # 却只列出几条，会以为参考区漏了东西。
            shop_count = sum(
                1 for item in candidates if is_shop_site(item.url or "", item.platform or "")
            )
            self._trace(
                trace,
                "商品页",
                "hit" if shop_count else "none",
                (
                    f"挡掉 {shop_count} 条电商商品页，不进参考区"
                    if shop_count
                    else "候选里没有电商商品页"
                ),
            )
            # X 的回复页/转发页会把「她回复的、她转的」帖子一起列出来：指纹对上了
            # 也只是说明这张图在这一页出现过。顺回去找到原帖，能确定就换掉。
            reply_hint = await self._resolve_x_list_page(candidates, stream_id)

        # 命中的是「求出处」帖时不能当结论：发帖人自己也在找这张图，那页只是
        # 「别人也在问」的证据。实测 s-20261111-1611-db40 距离 0 命中司机社一个
        # 「有没有大佬知道这个远旅的动图出自哪里-求出处」的帖子，被当成出处回了群，
        # 用户点进去看到的是别人在求图。降成 suspect 留在「更多」里，并写明原因。
        for item in candidates:
            if item.status in {"match", "vision"} and is_request_post(item.title):
                item.status = "suspect"
                item.downgrade = "request"
                item.note = "命中的是求图帖（发帖人也在找出处），不能当原帖"

        ordered = self._rank(candidates, names, prefer_pixiv=sauce is not None)
        # 同一个账号常被 Lens、Yandex、百度一起撞见：只留质量最好的那条，
        # 免得参考区里同一个号重复列三遍。
        ordered = dedupe_accounts(ordered)
        # 搬运站的图床文件名常把 pixiv 作品号抄在身上：读到就回 pixiv 核对，再用
        # pixiv.re 反代取原图比指纹。对上了就是真的原帖——插到候选最前面参与挑选
        # （距离同为 0 时排在转载站前面），对不上才只留「出处是哪个作品」。
        pixiv_source, pixiv_pick = await self._track_pixiv_source(ordered, upload, trace)
        if pixiv_pick is not None:
            ordered.insert(0, pixiv_pick)
        # 最佳匹配按指纹质量挑，不受引擎顺序影响；指纹距离优先，其次相似度。
        # 插画路线优先取 pixiv 的命中。
        best = self._pick_best(ordered, prefer_pixiv=sauce is not None)

        # 指纹一条都没通过时才做兜底：视觉认「同一个人」，Grok 认「X 上的账号」，
        # 两件都不是 dHash 能做的。并行发出，各自超时，谁有结果用谁。
        # 插画路线不走这里：SauceNAO 的相似度已经能定论，没必要再让模型猜。
        #
        # 例外：命中的是搬运站/社区站（arca.live、贴吧这类），页面里没有账号，
        # 只是别人转的图。这时仍读一次水印——图上写的号往往是唯一还能指到本人的
        # 线索（实测 arca.live 那条距离 0，水印却清楚写着 @Sakana_Yudan）。
        # 这条由 vision.watermark_on_repost 控制，且只在落点没有账号时触发。
        had_best = best is not None
        # 水印读哪一次、为什么读，全部由这一个决策给出（行为与以前完全一致）。
        # 以前三处各判各的：兜底这里算 needs_watermark、拼图里顺带读、重读再判一遍，
        # 结果页上「没触发」和「触发了没读到」长得一模一样。
        wm_plan = self._watermark_plan(
            vision_enabled=bool(config.vision.enabled),
            on_repost=bool(config.vision.watermark_on_repost),
            has_best=had_best,
            has_handle=bool(best is not None and (best.handle or "").strip()),
            is_drawing=sauce is not None,
            has_upload=bool(upload),
        )
        needs_watermark = wm_plan["mode"] == "alone"
        if not wm_plan["read"]:
            self._trace(trace, "读水印", "skip", str(wm_plan["reason"]))
        vision_info: dict[str, Any] | None = None
        grok_info: dict[str, Any] | None = None
        if (best is None or needs_watermark) and sauce is None and upload:
            jobs: dict[str, Any] = {}
            if config.vision.enabled:
                if needs_watermark:
                    # 搬运站命中：不做拼图认人，只读原图上的水印——原图能看清小字，
                    # 而且这里唯一想要的线索就是那个账号。
                    jobs["vision"] = self._read_watermark(upload)
                else:
                    jobs["vision"] = self._vision_review(upload, ordered)
            if had_best is False and config.grok.enabled and (config.grok.api_key or "").strip():
                jobs["grok"] = self._grok_lookup(upload, ordered, reply_hint=reply_hint)
            if jobs:
                results = await asyncio.gather(*jobs.values(), return_exceptions=True)
                for kind, result in zip(jobs, results):
                    if isinstance(result, BaseException):
                        # 兜底是加分项，任何一侧炸掉都不能影响已经搜到的文字结果。
                        message = f"{kind} 兜底异常：{result}"
                        errors.setdefault("fallback", message)
                        self.ctx.logger.warning("搜图兜底失败：%s", message)
                        continue
                    if kind == "vision":
                        vision_info = result
                    else:
                        grok_info = result
                if vision_info is not None:
                    if vision_info.get("error"):
                        self.ctx.logger.warning(
                            "读水印/视觉复核未完成：%s（检查=%s 耗时=%ss 模型=%s）",
                            vision_info["error"],
                            vision_info.get("checked"),
                            vision_info.get("elapsed"),
                            vision_info.get("model") or config.vision.model or config.vision.task,
                        )
                    elif needs_watermark:
                        self._vlog(
                            "读水印：耗时=%ss 模型=%s 水印=%s 文字=%s",
                            vision_info.get("elapsed"),
                            vision_info.get("model") or "-",
                            vision_info.get("watermark") or "-",
                            vision_info.get("watermark_text") or "-",
                        )
                    else:
                        self._vlog(
                            "视觉复核：检查=%s 命中=%s 降级=%s 耗时=%ss 模型=%s 水印=%s",
                            vision_info.get("checked"),
                            len(vision_info.get("picked") or []),
                            vision_info.get("downgraded"),
                            vision_info.get("elapsed"),
                            vision_info.get("model") or "-",
                            vision_info.get("watermark") or "-",
                        )
                if vision_info is not None:
                    read = str(vision_info.get("watermark") or "").strip()
                    if needs_watermark:
                        if vision_info.get("error"):
                            self._trace(
                                trace, "读水印", "none", f"原图单独读失败：{vision_info['error']}"
                            )
                        elif read:
                            self._trace(trace, "读水印", "ran", f"原图单独读，图上读到「{read}」")
                        else:
                            self._trace(trace, "读水印", "none", "原图单独读了一次，没读到账号")
                    elif vision_info.get("error"):
                        self._trace(
                            trace, "视觉复核", "none", f"拼图没跑完：{vision_info['error']}"
                        )
                    else:
                        checked = int(vision_info.get("checked") or 0)
                        self._trace(
                            trace,
                            "视觉复核",
                            "ran" if checked else "none",
                            f"拼图比了 {checked} 张候选"
                            + (f"，读到水印「{read}」" if read else "，没读到水印"),
                        )
                # 视觉和 Grok 读到的水印都只当线索：两个号都拿去翻它自己的带图帖，
                # 谁翻到这张图谁就是结论。实测 5b5b 那单视觉读成 @Miharu（一个
                # 无关老号）、Grok 读成 @Moliuwy（w 被读散），真号 @Moliww 两个都
                # 没读对——所以查无此人时还要按近似拼写再核一遍，命中要求仍是
                # 「这个号自己的帖子里有这张图」。
                if vision_info and (
                    vision_info.get("watermark") or (grok_info or {}).get("watermark")
                ):
                    await self._verify_watermarks(
                        vision_info,
                        grok_info,
                        upload,
                        allow_variants=not had_best,
                        ordered=ordered,
                    )
                    watermark_post = str(vision_info.get("watermark_post") or "").strip()
                    scan = vision_info.get("watermark_scan") or {}
                    if watermark_post:
                        note = f"拿水印号翻带图帖，核到 {watermark_post}"
                    elif scan.get("scanned_posts"):
                        note = (
                            f"翻了 {scan['scanned_posts']} 条带图帖／"
                            f"{scan['scanned_images']} 张图，没有这张"
                        )
                    else:
                        note = "拿水印号去翻了她自己的带图帖，没翻到这张"
                    self._trace(
                        trace,
                        "水印核对",
                        "hit" if watermark_post else "none",
                        note,
                    )
                else:
                    self._trace(trace, "水印核对", "skip", "这轮没有读到水印号")
                # 第三道兜底：拼图、Grok 两条读水印的线都扑街时，用原图单独再读一次。
                #
                # 拼图里 TARGET 格只有 320px，手写体小字常被压没（实测同一张纳西妲
                # cos：拼图那版模型回空串，原图版读出 @Sakana_Yudan）；Grok 一侧也
                # 可能读空或者干脆崩掉。s-20261111-1403-e15a 更典型：图右下角清清
                # 楚楚写着 KWATERIN（@kwaterin），拼图一个字没读出来，而那次重读还
                # 挂在上一个 if 里，被一起跳过了——图上写着答案，插件却说没找到。
                #
                # 所以它单独成一道、由 vision.watermark_reread 控制：只看「整轮没有
                # 结论、两条水印线都没落地」，不再看拼图有没有读到东西。
                if self._should_reread_watermark(
                    vision_enabled=bool(config.vision.enabled),
                    reread_enabled=bool(config.vision.watermark_reread),
                    had_best=had_best,
                    repost_unresolved=needs_watermark,
                    watermark_post=str((vision_info or {}).get("watermark_post") or ""),
                    grok_post=str((grok_info or {}).get("post_url") or ""),
                ):
                    if vision_info is None:
                        # 拼图那一轮自己就炸了，连结果 dict 都没留下：补个空壳，
                        # 后面的水印核对链路照原样接。ok=False 且 checked=0，
                        # 呈现层不会因此多出「视觉复核」那一段。
                        vision_info = {
                            "ok": False,
                            "checked": 0,
                            "picked": [],
                            "picked_images": [],
                            "downgraded": 0,
                            "reason": "",
                            "model": "",
                            "error": "",
                            "elapsed": 0.0,
                            "sources": [],
                            "watermark": "",
                        }
                    await self._reread_watermark(
                        vision_info,
                        grok_info,
                        upload,
                        ordered,
                        prefer_candidates=had_best,
                    )
                    self._trace(
                        trace,
                        "重读水印",
                        "ran",
                        "整轮没结论／搬运页没账号，且两次水印都没落地，拿原图再读一次",
                    )
                else:
                    self._trace(
                        trace,
                        "重读水印",
                        "skip",
                        self._reread_watermark_note(
                            vision_enabled=bool(config.vision.enabled),
                            reread_enabled=bool(config.vision.watermark_reread),
                            had_best=had_best,
                            repost_unresolved=needs_watermark,
                            watermark_post=str((vision_info or {}).get("watermark_post") or ""),
                            grok_post=str((grok_info or {}).get("post_url") or ""),
                        )
                        or "不需要重读",
                    )
                if not had_best and vision_info and vision_info.get("picked"):
                    ordered = dedupe_accounts(self._rank(candidates, names))
                    # 模型可能一次报好几格，取指纹距离最近的那条当结论——距离近说明
                    # 画面本来就更像，比编号顺序更靠谱。
                    hits = [item for item in ordered if item.status == "vision"]
                    best = min(
                        hits,
                        key=lambda item: (item.distance if item.distance is not None else 999, item.rank),
                    )
                    # 落在账号主页上的视觉结论撑不起「就是她发的」：翻她自己的帖子
                    # 比指纹，翻不到就降级，别让用户点进主页对不上图。
                    settled = await self._settle_vision_handle(best, upload)
                    if settled is None and best is not None:
                        vision_info["downgraded"] = int(vision_info.get("downgraded") or 0) + 1
                    best = settled

        # 水印读出来的词如果就是候选站点自己的名字，那不是账号，是站名。
        self._drop_site_name_watermarks(vision_info, ordered)

        # 引用帖不是原帖，转发的原帖也不属于转发者：指纹已经对上的 X 候选都换回
        # 原作者与那条帖子。只挑 match/suspect，mismatch 的图本来就不是这张。
        best = await self._follow_quotes(ordered, best)
        # 图上水印核对到本人发过这张图时，这条就是结论：指纹对得上、又是本人
        # 亲手发的帖，比「视觉说是同一个人」硬得多。
        best = self._promote_watermark_hit(best, vision_info)
        # 图上写着「id=某某」时，那个名字往往不是 X 的 handle（中文平台的写法，
        # X 上常查无此号）。拿它去各平台捞一次主页：X 上的翻号比指纹，命中就换
        # 结论；其余平台只记线索。
        #
        # 读法不只一种：整图、各块、Grok 常把同一处读成不同写法（s-20261112-0925-cad4
        # 里整图读 NaohingLace、中左读 idcNothinglose、机器人自己的「理解图片」读
        # id=NothingLee），只拿最后一个去问，等于把别的读法丢了。两个名字为限，
        # 省着花检索额度。
        id_names: list[str] = []
        readings = [
            str((vision_info or {}).get("watermark") or ""),
            str((vision_info or {}).get("watermark_text") or ""),
            str((grok_info or {}).get("watermark") or ""),
        ]
        readings += [
            str(item.get("raw") or "")
            for item in ((vision_info or {}).get("watermark_readings") or [])
            if isinstance(item, dict)
        ]
        for reading in readings:
            found = vision.id_name_from_watermark(reading)
            if found and found not in id_names:
                id_names.append(found)
        id_names = id_names[: self._ID_NAME_LIMIT]
        # 水印已经核到那条帖子时不必再查；其余情况（视觉命中、微博只给 UID、
        # 落在搬运页上）都还得查——c44f／cad4 两单就是卡在这儿：视觉先给了个
        # 「同一个人」，名字那一步整条被跳过。
        if self._should_search_id_name(
            enabled=bool(getattr(config.vision, "id_name_search", True)),
            has_upload=bool(upload and vision_info is not None),
            names=id_names,
            watermark_post=str((vision_info or {}).get("watermark_post") or ""),
            best_status=str(best.status if best is not None else ""),
            best_is_x=bool(best is not None and self._is_x(best)),
        ):
            try:
                id_name_info = await self._hunt_from_id_name(id_names, upload)
            except Exception as error:  # noqa: BLE001 - 查不到不影响别的兜底
                self.ctx.logger.warning("按 id= 的名字查各平台失败：%s", error)
                id_name_info = None
            if id_name_info:
                vision_info["watermark_id_name"] = str(
                    id_name_info.get("name") or id_names[0]
                )
                if id_name_info.get("profiles"):
                    vision_info["watermark_profiles"] = id_name_info["profiles"]
                if id_name_info.get("status") == "hit":
                    vision_info["watermark_id_hit"] = str(id_name_info.get("post") or "")
                    best = self._promote_id_name(best, id_name_info)
        # 结论只到「同一个人」、落点又认不了图（微博这类站只给 UID）时，顺着名字
        # 去 X 上找这个号核图：命中就把结论换成那条帖子，没命中只记一句供呈现用。
        x_identity: dict[str, Any] | None = None
        if (
            upload
            and best is not None
            and best.status == "vision"
            and bool(getattr(config.vision, "x_from_name", True))
        ):
            try:
                x_identity = await self._hunt_x_from_name(best, upload)
            except Exception as error:  # noqa: BLE001 - 换不到号不影响已经搜到的结果
                self.ctx.logger.warning("按名字换 X 号失败：%s", error)
                x_identity = None
            if x_identity and x_identity.get("status") == "hit":
                best = self._promote_x_identity(best, x_identity)
        # 搬运帖常把原作者名一起抄进标题（「Vivian cosplay by KURU」）。候选 URL
        # 里抽不出号、图上水印也读不到时，标题里这个名字往往就是唯一剩下的入口：
        # 拿它去检索 X 账号，再翻那个号自己的带图帖比指纹，对上了才换结论。
        signature_author: dict[str, Any] | None = None
        signature_seen: dict[str, Any] | None = None
        if (
            upload
            and bool(getattr(config.vision, "signature_hunt", True))
            and (
                best is None
                # 视觉命中只是「模型说同一个人」，还没核实过，别让它一个人挡住
                # 标题这条硬线索：ed9a 那单就是被视觉猜的 @sleamPM 挡住的，而
                # lens 三条候选的标题里都写着「推特lazycataw」。
                or best.status == "vision"
                or (
                    best.status == "match"
                    and not (best.handle or "").strip()
                    and not self._is_x(best)
                )
            )
        ):
            try:
                signature_author = await self._hunt_signature_author(
                    ordered, upload, hints=self._watermark_hints(vision_info)
                )
            except Exception as error:  # noqa: BLE001 - 反查不到不影响已经命中的那条
                self.ctx.logger.warning("标题署名反查失败：%s", error)
                signature_author = None
            self._trace(
                trace,
                "标题署名",
                (
                    "hit"
                    if (signature_author or {}).get("status") in {"hit", "author"}
                    else ("ran" if signature_author else "none")
                ),
                self._author_trace_note(signature_author, "候选标题里没有可用署名"),
            )
            if signature_author and signature_author.get("status") == "hit":
                best = self._promote_signature_author(best, signature_author)
            elif signature_author and signature_author.get("status") == "author":
                # 认得出是谁、但翻不到这张：结论给作者，别判成「没找到」。
                best = self._promote_cross_author(best, signature_author)
            elif signature_author and signature_author.get("status") == "seen":
                # 号确实存在、名字也对得上，只是她最近的带图帖里没有这张：
                # 当线索留着，别丢掉——ed9a 那单就能靠这条落到 @lazycataw。
                signature_seen = signature_author
        # 同一张图落在普通网页上（搬运站/图集页，页面上没有账号）时别停在那儿：
        # 候选里出现过的 X 号挨个翻一遍，谁自己的帖子里有这张图谁就是结论。
        repost_author: dict[str, Any] | None = None
        title_author: dict[str, Any] | None = None
        if (
            upload
            and best is not None
            and best.status == "match"
            and not (best.handle or "").strip()
            and not self._is_x(best)
            and bool(getattr(config.vision, "repost_author", True))
        ):
            try:
                title_author = await self._hunt_title_author(best, upload, ordered)
            except Exception as error:  # noqa: BLE001 - 按名字找不到不影响已命中的那条
                self.ctx.logger.warning("标题作者反查失败：%s", error)
                title_author = None
            if title_author and title_author.get("status") == "hit":
                best = self._promote_repost_author(best, title_author)
            elif title_author is None or title_author.get("status") != "named":
                # 标题里没名字、或名字压根没对上号时，再退回翻候选里那几个 X 号。
                # 名字已经对上一个号（named）就不用翻了——那才是本人的号。
                try:
                    repost_author = await self._hunt_repost_author(ordered, best, upload)
                except Exception as error:  # noqa: BLE001 - 翻不到不影响已经命中的那条
                    self.ctx.logger.warning("搬运站转贴反查失败：%s", error)
                    repost_author = None
                if repost_author and repost_author.get("status") == "hit":
                    best = self._promote_repost_author(best, repost_author)
                self._trace(
                    trace,
                    "搬运站翻候选号",
                    "hit" if (repost_author or {}).get("status") == "hit" else ("ran" if repost_author else "none"),
                    (
                        f"翻过 {len(repost_author.get('scanned') or [])} 个候选号，谁都没发这张"
                        if repost_author
                        else "候选里没有可翻的 X 号"
                    ),
                )
            self._trace(
                trace,
                "标题作者",
                (
                    "hit"
                    if (title_author or {}).get("status") == "hit"
                    else ("ran" if title_author else "none")
                ),
                self._author_trace_note(title_author, "标题里没认出名字"),
            )
        # 结论里的账号核一下死活：被封的号 handle 会被释放，只剩数字 ID 能对上人。
        best = await self._check_best_account(best, vision_info)

        # 内容号落地页（百度动态、百家号）只当线索：页面里没有作者字段，图上的水印
        # 是发布者自己打上去的，同名也证明不了图是她拍的。b3b4 落在 mbd.baidu.com，
        # 标题写着「@诗诗爱生活的动态」，指纹距离 1，原样当结论发出去等于替一个
        # 内容号认证。真要是本人，靠按名字反查到带图帖能对上再升回来。
        if (
            best is not None
            and best.status == "match"
            and not (best.handle or "").strip()
            and is_content_site(best.url or "", best.platform or "")
        ):
            account = str(
                (title_author or {}).get("author") or (title_author or {}).get("name") or ""
            ).strip()
            watermark = str((vision_info or {}).get("watermark") or "").strip().lstrip("@＠")
            same = bool(account and watermark and account.lower() == watermark.lower())
            self._vlog(
                "内容号落地页命中，降级成线索：%s（标题账号=%r 水印=%r 同名=%s）",
                best.platform or best.url,
                account,
                watermark,
                same,
            )
            best.status = "suspect"
            best.downgrade = "aggregator"
            best.note = (
                f"内容号文章：这个号（{account}）和图上水印同名，但内容号证明不了图是她拍的"
                if same
                else "内容号文章：这是资讯站/内容号发的，证明不了图是谁的"
            )

        # 反查没命中、落点仍是搬运站/图集页时，那条不能算「找到了」：指纹对得上只
        # 说明这页上有这张图，页面本身是转载。实测 s-20261111-2327-5138 命中游民
        # 星空一篇囧图合集，候选里两个 X 号都翻不到作者，原样当结论发出去，看着
        # 就像找到了出处。降成线索，链接留在参考区。
        if (
            best is not None
            and best.status == "match"
            and not (best.handle or "").strip()
            and is_repost_site(best.url or "", best.platform or "")
        ):
            self._vlog("搬运页命中但没反查到原作者，降级成线索：%s", best.platform or best.url)
            best.status = "suspect"
            best.downgrade = "repost"
            best.note = "搬运页命中：这页上确实有这张图，但那是转载，不是作者发的"

        # 结论落到人身上之后，再去她自己的付费页确认这一期卖在哪。图包里的图
        # 公开渠道搜不到，Patreon／FANBOX 的公开列表是唯一能给到底的地方。
        paywall_info: dict[str, Any] | None = None
        paywall_author = ""
        if bool(getattr(config.vision, "paywall", True)):
            author = ""
            post_url = ""
            if best is not None and (best.handle or "").strip():
                author = best.handle.strip().lstrip("@")
                if self._is_x(best) and "/status/" in (best.url or ""):
                    post_url = best.url
            elif (title_author or {}).get("handle"):
                # 标题里认出的号「名字对得上、但她最近的带图帖里没有这张图」（named）：
                # 图包里的图本来就不会公开发，拿这个号继续查她主页挂的 Patreon／FANBOX，
                # 才可能落到「这是哪一期」上。9aa1 那单就卡在这里——人已经认出来了，
                # 却因为结论还停在搬运站，整段付费页反查没跑。
                author = str((title_author or {}).get("handle") or "").strip().lstrip("@")
            elif (grok_info or {}).get("tier") == "post":
                author = str((grok_info or {}).get("handle") or "").strip().lstrip("@")
                post_url = str((grok_info or {}).get("post_url") or "")
            if author:
                paywall_author = author
                texts = [best.title if best is not None else ""]
                texts.append(str((grok_info or {}).get("reason") or ""))
                texts.extend(str(item.title or "") for item in ordered[:8])
                try:
                    paywall_info = await asyncio.wait_for(
                        self._track_paywall(author, texts, post_url), timeout=30
                    )
                except Exception as error:  # noqa: BLE001 - 付费页查不到不影响结论
                    self.ctx.logger.warning("付费页追踪失败：%s", error)
                    paywall_info = None
            elif (
                best is not None
                and best.status == "match"
                and (direct := paywall.paywall_url(best.url or ""))
            ):
                # 结论本身就是 Patreon／FANBOX 帖子时没有 X 账号可查（图搜给的是售卖
                # 页），但链接里就带着作者名与帖子 id，直接按链接反推这一期。
                texts = [best.title or ""]
                texts.append(str((grok_info or {}).get("reason") or ""))
                texts.extend(str(item.title or "") for item in ordered[:8])
                try:
                    paywall_info = await asyncio.wait_for(
                        self._track_paywall_direct(best.url or "", texts, direct), timeout=40
                    )
                except Exception as error:  # noqa: BLE001 - 付费页查不到不影响结论
                    self.ctx.logger.warning("付费页直查失败：%s", error)
                    paywall_info = None

        self._trace(
            trace,
            "付费页",
            "hit" if paywall_info else "none",
            (
                f"在 {(paywall_info or {}).get('platform') or '付费页'} 找到这一期："
                f"{str(((paywall_info or {}).get('post') or {}).get('title') or '')[:40]}"
                if paywall_info
                else "没查到这一期的售卖页"
            ),
        )

        self._vlog(
            "分流结果：路线=%s 原因=%s；匹配=%s 视觉=%s 疑似=%s 不符=%s 未验证=%s",
            route,
            route_reason,
            sum(1 for c in ordered if c.status == "match"),
            sum(1 for c in ordered if c.status == "vision"),
            sum(1 for c in ordered if c.status == "suspect"),
            sum(1 for c in ordered if c.status == "mismatch"),
            sum(1 for c in ordered if c.status == "unverified"),
        )

        ai = None
        ai_pool = self._pools.get("ai")
        if (
            config.ai_detect.enabled
            and ai_pool
            and (best is None or not config.ai_detect.only_when_low_confidence)
        ):
            ai = await asyncio.to_thread(
                detect_ai,
                upload,
                ai_pool,
                self._proxy(),
                section.engine_timeout,
            )

        # 指纹命中的常是搬运站，作者本人的 X 号不会出现在候选的账号字段里。
        # 从候选标题里认出疑似名字，去 X 验证存在性；它不改 best、也不覆盖 Grok
        # 已经核到的号，只作为「同一个人可能在这个号」的补充线索。
        x_guess: dict[str, Any] | None = None
        if (
            upload
            and ordered
            and not (grok_info or {}).get("tier") in {"post", "account"}
            and not (best is not None and self._is_x(best))
        ):
            try:
                x_guess = await self._hunt_x_handle(ordered)
            except Exception as error:  # noqa: BLE001 - 猜号失败不影响已经搜到的结果
                self.ctx.logger.warning("疑似 X 账号推断失败：%s", error)

        # 标题署名已经认到号、只是翻不到这张时，把那个号当线索挂出去：
        # 它比「从一堆标题里蒙一个」硬得多（名字对得上、账号也核过），
        # 不提的话用户只能看到一句「没找到」。
        if x_guess is None and signature_seen and str(signature_seen.get("handle") or ""):
            x_guess = {
                "handle": str(signature_seen.get("handle") or ""),
                "followers": int(signature_seen.get("followers") or 0),
                "source_title": str(signature_seen.get("source_title") or ""),
                "source_platform": str(signature_seen.get("platform") or "X (Twitter)"),
                "source_url": str(signature_seen.get("source_url") or ""),
                "checked_posts": True,
            }
            self._vlog(
                "署名线索：@%s（翻过她的带图帖，没有这张）", x_guess["handle"]
            )

        # 读到微博号时去微博站内核一遍：水印写着「微博@某某」、@后面跟着中文昵称、
        # 或者那个号在 X 上查无此人，都值得去微博搜一次。只当线索——微博没有独立
        # handle，昵称重名太常见，粉丝数就写在旁边让用户自己判断。
        weibo_clue: dict[str, Any] | None = None
        llm_names: list[str] = []
        # 已经有结论、且结论落在具体账号上时才跳过：那种情况本人已经知道了。
        # 落点没有账号（新闻页、活动报道页那种）仍然要查——s-20261113-1402-6a76
        # 命中斗鱼的报道页，图上却写着 © Ryomky，正主是微博号 Ryomky。
        if (
            upload
            and ordered
            and not (
                best is not None
                and best.status == "match"
                and (best.handle or "").strip()
            )
        ):
            async def run_hunt(label: str, coro: Any, budget: float) -> Any:
                """外挂反查的护栏：超时或报错都只丢这一路，绝不拖住主流程。"""
                try:
                    return await asyncio.wait_for(coro, timeout=budget)
                except asyncio.TimeoutError:
                    self.ctx.logger.warning("%s反查超时（%.0fs），跳过", label, budget)
                    return None
                except Exception as error:  # noqa: BLE001 - 反查失败不影响已有结果
                    self.ctx.logger.warning("%s反查失败：%s", label, error)
                    return None

            # 搬运站／图集站的标题先交给模型读一遍：这类站点的名字藏在「作品名 +
            # 角色 + 张数」中间，规则切不准。问不到就退回规则，不拖住整单。
            llm_names = await self._llm_title_names(
                ordered,
                known=(
                    str((title_author or {}).get("author") or "")
                    if str((title_author or {}).get("via") or "") == "llm"
                    else ""
                ),
            )
            weibo_clue = await run_hunt(
                "微博",
                self._hunt_weibo(
                    ordered,
                    vision_info,
                    grok_info,
                    target_hash,
                    best=best,
                    extra_names=[
                        *llm_names,
                        str((title_author or {}).get("name") or ""),
                        str((title_author or {}).get("author") or ""),
                    ],
                ),
                45,
            )
            self._trace(
                trace,
                "微博反查",
                "hit" if (weibo_clue or {}).get("status") == "hit" else ("ran" if weibo_clue else "none"),
                (
                    (
                        f"微博核到 {weibo_clue.get('name') or weibo_clue.get('uid') or ''}，"
                        "带图比对也对上了"
                        if (weibo_clue or {}).get("post")
                        # 只按名字搜到主页时没有帖子可核：没登录 cookie 翻不了她的帖，
                        # 这里要如实写，不能假装「图也比过了」。
                        else f"微博核到 {weibo_clue.get('name') or weibo_clue.get('uid') or ''} 的主页"
                        "（没登录 cookie，翻不了她的帖）"
                    )
                    if (weibo_clue or {}).get("status") == "hit"
                    else (
                        f"微博上搜了「{weibo_clue.get('query') or ''}」，没有粉丝量像的号"
                        if weibo_clue
                        else "没有可查的微博线索"
                    )
                ),
            )
            weibo_post = weibo_clue.get("post") if weibo_clue else ""
            if weibo_post:
                best = self._promote_weibo_post(
                    best,
                    {
                        "distance": int(weibo_clue.get("distance") or 0),
                        "name": str(weibo_clue.get("name") or ""),
                        "uid": str(weibo_clue.get("uid") or ""),
                        "url": str(weibo_post),
                        "image": str(weibo_clue.get("image") or ""),
                    },
                )

        # 标题里的名字铺到各平台找主页：图集站/搬运站的页面一个账号都不挂时，标题
        # 里那个名字往往是唯一入口（c077 就是这样，只落在 hotgirlpix / misskon）。
        # 只花一次网页检索一个名字；X 与微博的再翻本人带图帖比指纹，对上了才换结论，
        # IG / TikTok 那些只当线索——名字像不等于人就是同一个。
        name_hunt_info: dict[str, Any] | None = None
        name_candidates = self._title_name_candidates(
            ordered,
            [
                str((title_author or {}).get("name") or ""),
                str((title_author or {}).get("author") or ""),
            ],
            # pixiv 作品名不是人名：核对过的那条作品标题直接拉黑，别再拿去各平台搜。
            blocked={str((pixiv_source or {}).get("title") or "")},
            trusted=llm_names,
        )
        # 标题作者那条线「查过但没找到」时不要当成「已经搜过」：c077 那单正是
        # X 上按显示名找不到中文名，才需要换各平台再试一次。只有真的认到号
        # （hit / named）才把那个名字从这一轮里排掉，别白花检索额度。
        title_author_done = self._title_author_searched(title_author)
        if title_author_done:
            name_candidates = [
                name for name in name_candidates if name.strip().lower() != title_author_done
            ]
        # 微博那条腿已经拿这个名字搜过（按昵称搜人），站外这条就别再对她的微博
        # 主页查一遍、翻一遍帖；X 与其它平台照旧。
        weibo_searched: set[str] = set()
        weibo_query = str((weibo_clue or {}).get("query") or "").strip()
        if weibo_query and str((weibo_clue or {}).get("status") or "") in {"hit", "none"}:
            weibo_searched.add(weibo_query)
        name_hunt_on = bool(getattr(config.name_hunt, "enabled", True))
        # 出处已经核到 pixiv 原帖、作者也拿到了：标题里剩下的都是作品名和分类词
        # （9b22 的「永恒的新生(1)」「漫画预告」），拿去各平台搜只会搜回一串对不上
        # 的号。真正的作者写在出处那一行，这里不再花检索额度。
        pixiv_author = str((pixiv_source or {}).get("user_name") or "").strip()
        pixiv_settled = bool((pixiv_source or {}).get("verified")) and bool(pixiv_author)
        if pixiv_settled:
            self._trace(
                trace,
                "按名字找号",
                "skip",
                f"pixiv 原帖已指纹对上，作者是 {pixiv_author}，不再拿标题里的作品名到处搜",
            )
        elif self._should_search_name(
            enabled=name_hunt_on,
            has_upload=bool(upload),
            names=name_candidates,
            best_status=str(best.status if best is not None else ""),
            best_handle=str((best.handle or "") if best is not None else ""),
            searched=[title_author_done] if title_author_done else [],
            urls=[str(item.url or "") for item in ordered[:8]],
        ):
            name_budget = float(getattr(config.name_hunt, "budget", 45.0) or 45.0)
            try:
                name_hunt_info = await asyncio.wait_for(
                    self._hunt_name_profiles(
                        name_candidates,
                        upload,
                        target_hash,
                        source=(
                            "名字是模型读出来的"
                            if llm_names
                            or str((title_author or {}).get("via") or "") == "llm"
                            else ""
                        ),
                        skip_weibo=weibo_searched,
                    ),
                    timeout=name_budget,
                )
            except asyncio.TimeoutError:
                self.ctx.logger.warning("按名字找号超时（%.0fs），跳过", name_budget)
                name_hunt_info = None
            except Exception as error:  # noqa: BLE001 - 找不到不影响已有结论
                self.ctx.logger.warning("按名字找号失败：%s", error)
                name_hunt_info = None
            self._trace(
                trace,
                "按名字找号",
                (
                    "hit"
                    if (name_hunt_info or {}).get("status") == "hit"
                    else ("ran" if (name_hunt_info or {}).get("profiles") else "none")
                ),
                self._name_hunt_trace_note(name_hunt_info, name_candidates),
            )
            if (name_hunt_info or {}).get("status") == "hit":
                best = self._promote_name_hit(best, name_hunt_info or {})

        # 按名字认到的号也拿去查付费页：结论停在内容号／图集站、标题里又没有署名时，
        # 上面那段没有号可查，整段等于没跑。411f 的 @Kuukow002 就是按名字找到的，
        # 而她这一套图很可能只卖在 Patreon／FANBOX 上——那正是公开索引搜不到原帖的原因。
        name_hunt_handle = str(
            (name_hunt_info or {}).get("handle")
            or (name_hunt_info or {}).get("x_handle")
            or ""
        ).strip().lstrip("@")
        if (
            paywall_info is None
            and name_hunt_handle
            and name_hunt_handle.lower() != paywall_author.lower()
            and bool(getattr(config.vision, "paywall", True))
        ):
            texts = [best.title if best is not None else ""]
            texts.append(str((grok_info or {}).get("reason") or ""))
            texts.extend(str(item.title or "") for item in ordered[:8])
            try:
                paywall_info = await asyncio.wait_for(
                    self._track_paywall(name_hunt_handle, texts, ""), timeout=30
                )
            except Exception as error:  # noqa: BLE001 - 查不到不影响结论
                self.ctx.logger.warning("按名字查付费页失败：%s", error)
                paywall_info = None
            if paywall_info:
                self._vlog(
                    "按名字认到的 @%s 查到这一期：%s",
                    name_hunt_handle,
                    str(((paywall_info or {}).get("post") or {}).get("title") or "")[:40],
                )
                for entry in trace:
                    if entry.get("step") == "付费页":
                        entry["state"] = "hit"
                        entry["note"] = (
                            f"在 {(paywall_info or {}).get('platform') or '付费页'} 找到这一期："
                            f"{str(((paywall_info or {}).get('post') or {}).get('title') or '')[:40]}"
                        )
                        break
        else:
            if not name_hunt_on:
                name_trace_note = "按名字找号关着"
            elif not name_candidates:
                name_trace_note = "标题里没有可用的名字"
            else:
                name_trace_note = "已经有指纹对上的结论，不用再按名字找"
            self._trace(trace, "按名字找号", "skip", name_trace_note)

        # 标题里的名字再去 B 站反查一遍：B 站是 X 之外唯一能拿公开图核指纹的平台
        # （检索结果里的投稿封面）。图集站/搬运站的页面一个账号都不挂时，名字就是
        # 唯一入口——c077 那单就卡在这儿，标题里写着名字，却只有「按名字找主页」一条腿。
        bilibili_name: dict[str, Any] | None = None
        already_pinned = bool(
            best is not None and best.status == "match" and (best.handle or "").strip()
        )
        if upload and target_hash is not None and name_candidates and not already_pinned:
            try:
                bilibili_name = await asyncio.wait_for(
                    self._hunt_bilibili_name(name_candidates, target_hash),
                    timeout=self._BILIBILI_NAME_BUDGET + 5,
                )
            except asyncio.TimeoutError:
                self.ctx.logger.warning("B 站按名字反查超时，跳过")
                bilibili_name = None
            except Exception as error:  # noqa: BLE001 - 找不到不影响已有结论
                self.ctx.logger.warning("B 站按名字反查失败：%s", error)
                bilibili_name = None
            self._trace(
                trace,
                "B站按名字",
                (
                    "hit"
                    if (bilibili_name or {}).get("status") == "hit"
                    else ("ran" if bilibili_name else "none")
                ),
                (
                    f"名字「{bilibili_name.get('name')}」搜到 {bilibili_name.get('account')}"
                    f"（mid {bilibili_name.get('mid')}），投稿封面有这张图"
                    f"（距离 {bilibili_name.get('distance')}）"
                    if (bilibili_name or {}).get("status") == "hit"
                    else (
                        f"拿「{'、'.join((bilibili_name or {}).get('names') or [])}」"
                        f"搜了 {len((bilibili_name or {}).get('accounts') or [])} 个号，"
                        "投稿封面都对不上"
                        if bilibili_name
                        else "没有可查的名字"
                    )
                ),
            )
            if (bilibili_name or {}).get("status") == "hit":
                best = self._promote_other_account(
                    best,
                    {
                        "hit": {
                            "platform": "B站",
                            "handle": str(bilibili_name.get("mid") or ""),
                            "name": str(bilibili_name.get("account") or ""),
                            "url": str(bilibili_name.get("url") or ""),
                            "image": str(bilibili_name.get("image") or ""),
                            "distance": int(bilibili_name.get("distance") or 0),
                            "source": "按标题里的名字找到的 B 站号",
                        }
                    },
                )
        else:
            self._trace(trace, "B站按名字", "skip", "已经有指纹对上的结论，不用再按名字找")

        # 候选 URL 里写明的其它平台账号（TikTok / IG / B站 / 微博）全部收成线索：
        # 以前这些号只写进 `Candidate.handle`，没有任何反查读它。能核图的（B 站投稿
        # 封面、微博带图帖）顺手核一下；结论落在 X 号上时，再拿 handle 去这两家搜同名。
        other_accounts: dict[str, Any] | None = None
        if ordered or best is not None:
            try:
                other_accounts = await asyncio.wait_for(
                    self._hunt_other_accounts(ordered, best, target_hash),
                    timeout=self._OTHER_ACCOUNT_BUDGET + 5,
                )
            except asyncio.TimeoutError:
                self.ctx.logger.warning("其它平台账号反查超时，跳过")
                other_accounts = None
            except Exception as error:  # noqa: BLE001 - 查不到不影响已有结论
                self.ctx.logger.warning("其它平台账号反查失败：%s", error)
                other_accounts = None
            self._trace(
                trace,
                "其它平台账号",
                (
                    "hit"
                    if (other_accounts or {}).get("hit")
                    else ("ran" if other_accounts else "none")
                ),
                self._other_account_note(other_accounts),
            )
            if (other_accounts or {}).get("hit"):
                best = self._promote_other_account(best, other_accounts or {})

        # 结论落在 picazor 这类站时，帖子页本身没有账号，但挂着发布者主页，
        # 主页上直接写着作者的 X：翻这一层把它捞回来。s-20261119-1605-e790 就是
        # 停在「平台：picazor.com」没有下文，而 @maonu164196 一直在那页上。
        gallery_sources: list[dict[str, Any]] = []
        if best is not None and best.url and is_author_page_site(best.url, best.platform):
            try:
                gallery_sources = await self._gallery_sources([best], author_pages=True)
            except Exception as error:  # noqa: BLE001 - 翻不到只少一条线索
                self.ctx.logger.warning("发布者主页反查失败：%s", error)
            self._trace(
                trace,
                "发布者主页",
                "hit" if gallery_sources else "none",
                (
                    f"从发布者主页捞到 {len(gallery_sources)} 个账号"
                    if gallery_sources
                    else "发布者主页上没挂社交账号"
                ),
            )

        # 付费内容线索：搬运站和泄露站按名字建页，有页就说明这个人确实在做
        # OnlyFans。它同样不改 best，只多给一个可以核对的号。
        paid_info: dict[str, Any] | None = None
        if upload and ordered:
            try:
                paid_info = await self._hunt_paid(ordered)
            except Exception as error:  # noqa: BLE001 - 查不到付费线索不影响已搜到的结果
                self.ctx.logger.warning("付费内容线索推断失败：%s", error)

        # 候选里出现两个以上 X 号时，看它们是不是挂着同一条外链：coser 常开好多
        # 个小号，也常互相转发，同一个 Patreon 指向同一个人，比名字像要硬。
        same_person_info: dict[str, Any] | None = None
        if ordered:
            try:
                same_person_info = await self._hunt_same_person(ordered)
            except Exception as error:  # noqa: BLE001 - 对号失败不影响已搜到的结果
                self.ctx.logger.warning("同一个人多号推断失败：%s", error)

        # 视觉命中但指纹对不上：翻不到那人自己发的这张，就只算线索，不当结论。
        # 放在所有兜底之后——先让水印、署名、按名字换号这些硬线索把结论换掉。
        best = await self._require_vision_proof(best, upload, route)

        # 转载页（新浪这类）的正文往往就是原微博的正文：顺着它去微博搜帖、或者按
        # 页面标出来的作者核号，再拿配图比指纹。对上了就是找到了，落点换成那条帖。
        # 只在还没有可靠结论时跑，且必须配了登录 cookie（帖搜索要登录态）。
        if (
            upload
            and ordered
            and target_hash is not None
            and (best is None or best.status != "match")
        ):
            try:
                weibo_post = await self._hunt_weibo_repost(ordered, target_hash)
            except Exception as error:  # noqa: BLE001 - 查不到不影响已经搜到的结果
                self.ctx.logger.warning("转载页微博反查失败：%s", error)
                weibo_post = None
            if weibo_post and weibo_post.get("status") == "hit":
                best = self._promote_weibo_post(
                    best, {**weibo_post, "note": "转载页正文反查到原帖"}
                )

        # 图搜图彻底没结论时，退一步问「这是哪个角色」，再按角色名把同角色的
        # coser 列出来当线索。s-20261118-1414-e965 的教训：小号 + 无水的图，
        # Lens/Yandex 一条都不收，但角色认得出来，按角色搜人反而能碰到她。
        role_hint: dict[str, Any] | None = None
        if upload and target_hash is not None and (best is None or best.status != "match"):
            try:
                role_hint = await self._hunt_role_hint(upload, ordered)
            except Exception as error:  # noqa: BLE001 - 这一路失败不影响已有结果
                self.ctx.logger.warning("角色线索检索失败：%s", error)
            self._trace(
                trace,
                "角色线索",
                "hit" if role_hint else "none",
                (
                    f"按角色「{'、'.join((role_hint or {}).get('roles') or [])[:40]}」找到候选号"
                    if role_hint
                    else "认不出角色，没有可查的名字"
                ),
            )
        else:
            self._trace(trace, "角色线索", "skip", "已经有指纹命中的结论，不用退到按角色找人")

        # Grok 给了具体帖子或账号，也算有结论：前者是找到，后者是线索。
        grok_hit = bool(grok_info and grok_info.get("tier") in {"post", "account"})
        outcome = SearchOutcome(
            status="ok" if (best is not None or grok_hit) else "not_found",
            best=best,
            candidates=ordered,
            ai=ai,
            route=route,
            route_reason=route_reason,
            errors=errors,
            engine_counts=engine_counts,
            elapsed_ms=int((time.time() - started) * 1000),
            total_candidates=total,
            vision=vision_info,
            grok=grok_info,
            x_guess=x_guess,
            paid=paid_info,
            same_person=same_person_info,
            x_identity=x_identity,
            repost_author=repost_author,
            title_author=title_author,
            weibo=weibo_clue,
            name_hunt=name_hunt_info,
            role_hint=role_hint,
            paywall=paywall_info,
            pixiv_source=pixiv_source,
            gallery_sources=gallery_sources,
            search_page=search_page_info,
            other_accounts=other_accounts,
            trace=trace,
        )

        # 合并转发的页尾要再贴一次原图：群里连着收到好几单时，只看结论分不清
        # 这堆结果对应哪张图。只留内存不落盘——原图体积大，回「更多」和重放时
        # 也不会重复发。压解不开就干脆没有，页尾少一行而已。
        if upload:
            try:
                compact, _ = prepare_upload(upload, limit=1_200_000, as_jpeg=True)
            except Exception:  # noqa: BLE001 - 压不动就不给页尾原图
                compact = b""
            if compact:
                outcome.upload_b64 = base64.b64encode(compact).decode("ascii")

        if grok_hit and best is None and upload:
            # Grok 给的帖子不在候选池里。核对时距离最近的那张最有用——用户可以直接
            # 拿它跟原图肉眼比，比再放一次原图有意义。没这张（只给账号、图没下到）
            # 才退回原图。
            ref_url = str((grok_info or {}).get("reference_url") or "")
            ref_raw = await self._download(ref_url) if ref_url else None
            compact = None
            if ref_raw:
                try:
                    compact, _ = prepare_upload(ref_raw, limit=1_200_000, as_jpeg=True)
                except Exception:  # noqa: BLE001 - 解不开就用原图
                    compact = None
            if compact is not None:
                outcome.image_b64 = base64.b64encode(compact).decode("ascii")
                outcome.image_source = ref_url
            else:
                compact, _ = prepare_upload(upload, limit=1_200_000, as_jpeg=True)
                outcome.image_b64 = base64.b64encode(compact).decode("ascii")
                outcome.image_source = "upload"
        elif best is not None or ordered or upload:
            outcome.image_b64, outcome.image_source = await self._best_image(
                best,
                ordered,
                upload,
                (vision_info or {}).get("picked_images") or (),
            )

        # 视觉和 Grok 各自挑出一张时，两张都给：视觉那张已经是 best 的首图，
        # 这里补上 Grok 帖子配图里最近的一张，方便一眼对照。
        grok_ref = str((grok_info or {}).get("reference_url") or "")
        if grok_ref and best is not None and outcome.image_source != grok_ref:
            ref_raw = await self._download(grok_ref)
            if ref_raw:
                try:
                    compact, _ = prepare_upload(ref_raw, limit=1_200_000, as_jpeg=True)
                except Exception:  # noqa: BLE001 - 解不开就不给第二张
                    compact = None
                if compact is not None:
                    outcome.grok_image_b64 = base64.b64encode(compact).decode("ascii")

        # 写缓存：只存「搜到了」的结果。没找到的不存——实测同一张图过半天就能搜到，
        # 存下来只会挡住后来才出现的原帖。Grok 依赖当天 X 上的内容，它的结论也不存。
        cacheable = outcome.status == "ok" and outcome.best is not None
        if cache is not None and cache_key and cacheable:
            payload = outcome.to_summary()
            payload.pop("grok", None)
            cache.put(cache_key, payload)
            self._vlog("按图缓存写入：状态=%s 条目=%s", outcome.status, cache.size)
        return outcome

    async def _verify(
        self,
        candidates: list[Candidate],
        grouped: dict[str, list[Candidate]],
        names: list[str],
        target_hash: int | None,
    ) -> None:
        section = self.config.plugin
        deep = bool(section.deep_scan)
        targets: list[Candidate] = []
        social_extra: list[Candidate] = []
        social_enabled = bool(getattr(section, "social_verify_all", False))
        for name in names:
            if name == "saucenao":
                continue
            items = grouped.get(name) or []
            # Lens 会把 visual_matches 与 exact_matches 一起返回，条数可能几十条，
            # 全量下载验证太慢；取 2 倍配置条数，足够覆盖最相关的前几条。
            normal = max(1, section.verify_top_n) * 2 if name == "lens" else max(1, section.verify_top_n)
            limit = max(1, int(section.deep_verify_top_n), normal) if deep else normal
            targets.extend(items[:limit])
            if not social_enabled:
                continue
            # 原帖基本都在社交平台上：X、微博、小红书这些站点的候选即使排在
            # 每引擎配额之外，也补一次指纹比对，否则最该核对的那批反而被截断。
            for item in items[limit:]:
                if is_social_platform(item.url, item.source):
                    social_extra.append(item)
        if social_extra:
            seen = {item.url for item in targets}
            social_extra = [item for item in social_extra if item.url not in seen]
            cap = max(1, int(section.social_verify_limit))
            picked_social = social_extra[:cap]
            targets.extend(picked_social)
            self._vlog(
                "社交平台补验：候选=%s 补验=%s 上限=%s",
                len(social_extra),
                len(picked_social),
                cap,
            )
        if not targets:
            return

        loop = asyncio.get_running_loop()

        def check(candidate: Candidate) -> Candidate:
            return verify_candidate(
                candidate,
                target_hash,
                section.hash_threshold,
                self._proxy(),
                section.download_timeout,
                self._fake_ip_nets(),
            )

        results = await asyncio.gather(
            *(loop.run_in_executor(self._verify_pool, check, candidate) for candidate in targets),
            return_exceptions=True,
        )
        for candidate, result in zip(targets, results):
            if isinstance(result, BaseException):
                candidate.status = "unverified"
                candidate.note = f"验证异常：{str(result)[:50]}"

    _LISTING_PAGE_LIMIT = 2
    _LISTING_FETCH_TIMEOUT = 12

    async def _expand_listing_pages(
        self, candidates: list[Candidate], target_hash: int | None
    ) -> list[Candidate]:
        """命中落在站点列表页上时，从这一页里挖出那张图所属的具体帖子。

        列表页本身不是出处：点进去是一整页缩略图，图只属于其中某一篇。实测
        `s-20261119-2329-c8e9` 的 best 落在 `nudebird.biz/tag/bang-dream/`（距离 4），
        原样发出去用户看到的是标签归档页。列表页里每张缩略图都包在指向具体帖子的
        链接里：定位那张缩略图、往前取最近的链接、抓那一篇的主图再核一次指纹，
        对上了才换成它。抓不到、找不到链接都只是保持原来的降级，不影响整单。
        """
        if target_hash is None or not candidates:
            return []
        section = self.config.plugin
        threshold = int(getattr(section, "hash_threshold", 8) or 8)
        pages = [
            item
            for item in candidates
            if listing_page_kind(item.url or "", item.title or item.name)
            and item.distance is not None
            and item.distance <= threshold
        ]
        if not pages:
            return []
        pages.sort(key=lambda item: item.distance if item.distance is not None else 99)
        loop = asyncio.get_running_loop()
        known = {item.url or "" for item in candidates}
        found: list[Candidate] = []
        for page in pages[: self._LISTING_PAGE_LIMIT]:
            try:
                html = await asyncio.wait_for(
                    self._fetch_page_html(page.url or ""),
                    timeout=self._LISTING_FETCH_TIMEOUT,
                )
            except Exception as error:  # noqa: BLE001 - 列表页抓不到就保持降级
                self._vlog("列表页抓取失败：%s（%s）", page.url, str(error)[:60])
                continue
            entry = listing_entry_link(html, page.image_url or "", page.url or "")
            if not entry or entry in known:
                self._vlog("列表页里没定位到具体帖子：%s", page.url)
                continue
            # 挖层只许往下、不许往旁边：合集页、标签页这类本身还是列表的东西不能
            # 当帖子。实测 c077：真正那篇 `/p/…/page/2` 被当成列表页后，挖到的
            # 是站点合集页 `/collection/coser`，它反而成了距离 0 的结论。
            if listing_page_kind(entry, ""):
                self._vlog("列表页挖到的还是列表页，放弃：%s", entry)
                continue
            known.add(entry)
            try:
                post_html = await asyncio.wait_for(
                    self._fetch_page_html(entry), timeout=self._LISTING_FETCH_TIMEOUT
                )
            except Exception:  # noqa: BLE001 - 帖子页抓不到就没有主图可比
                post_html = ""
            image_url = page_main_image(post_html, entry)
            if not image_url:
                # 不许退回列表页那张缩略图：那张就是原图本身，比出来永远是距离 0，
                # 等于自己跟自己比（c077 那个假命中就是这么来的）。
                self._vlog("挖到的帖子没有主图，跳过：%s", entry)
                continue
            post_title = str(page.title or page.name or "")
            try:
                page_title, _ = weibopost.page_text(post_html)
                if page_title:
                    post_title = str(page_title)
            except Exception:  # noqa: BLE001 - 标题只影响展示
                pass
            # 挖到帖子不等于到此为止：页面自己标着的来源账号要接着往下用，
            # 拿去翻她的带图帖核指纹，命中了才换结论。
            try:
                accounts = parse_source_accounts(post_html, entry)
            except Exception:  # noqa: BLE001 - 抽不到账号不影响指纹判定
                accounts = []
            candidate = Candidate(
                engine=page.engine,
                url=entry,
                title=post_title,
                source=page.source,
                platform=page.platform,
                image_url=image_url,
            )
            try:
                candidate = await loop.run_in_executor(
                    self._verify_pool,
                    lambda item=candidate: verify_candidate(
                        item,
                        target_hash,
                        threshold,
                        self._proxy(),
                        section.download_timeout,
                        self._fake_ip_nets(),
                    ),
                )
            except Exception as error:  # noqa: BLE001 - 验证失败只是少一条
                self._vlog("列表页帖子核指纹失败：%s（%s）", entry, str(error)[:60])
                continue
            if candidate.distance is None or candidate.distance > threshold:
                self._vlog(
                    "列表页里那一篇对不上：%s（距离 %s）", entry, candidate.distance
                )
                continue
            candidate.note = f"{candidate.note}，这是列表页里那一篇"
            if accounts:
                candidate.source_accounts = list(accounts)
            found.append(candidate)
            self._vlog(
                "列表页挖到具体帖子：%s → %s（距离 %s）",
                page.url,
                entry,
                candidate.distance,
            )
        return found

    # 搜索页反解：候选落在平台搜索页时，用页面上的关键词找回具体账号。
    # 四个数都是上限，反解只是加分项，不能为了它把整单拖慢。
    _SEARCH_PAGE_LIMIT = 2
    _SEARCH_PAGE_ACCOUNTS = 3
    _SEARCH_PAGE_IMAGES = 4
    # 检索要过 `_prime_cookies` + 导航 + search（search 自己还会直连/代理各跑一遍），
    # 单次就能到 30~60 秒，20 秒的预算等于必然空手而归。现在整段（检索 + 封面下载）
    # 共用一个 45 秒的截止时间，谁先用完谁少拿，不再给每一小步都发满额。
    _SEARCH_PAGE_BUDGET = 45.0

    async def _resolve_search_pages(
        self, candidates: list[Candidate], target_hash: int | None
    ) -> tuple[list[Candidate], str, str]:
        """候选落在平台搜索页时，用页面上的关键词反解出具体账号，再用指纹实锤。

        Lens 常把候选落在 `search.bilibili.com/all?keyword=…` 上：那是 JS 渲染的
        搜索结果列表，解析不出发布者，视觉复核只能判「疑似同一个人」（本单
        `s-20261121-1955-bae5`，距离 27、判 suspect、整单 not_found）。可它 query
        里那个关键词是有用的：拿去 B 站检索接口反查能拿到几个 UP 主，再拿这些号
        自己的投稿封面比指纹，对上了才换成账号页当结论。

        返回 `(新候选, trace 状态, trace 说明)`。反解到号但图对不上时不改结论，
        只在那条搜索页候选上挂一行 `account_note`，写明「找到了疑似账号、图对不上」
        —— 同名不同人只靠指纹区分，粉丝数不作门槛也不作否决条件。
        """
        if target_hash is None or not candidates:
            return [], "skip", "没有可比对的指纹"
        section = self.config.plugin
        if not bool(getattr(section, "search_page_resolve", True)):
            return [], "skip", "搜索页反解关掉了"
        threshold = int(getattr(section, "hash_threshold", 8) or 8)
        pages: list[tuple[Candidate, str, str]] = []
        for item in candidates:
            matched = searchpage.search_page_query(item.url or "", item.title or item.name or "")
            if matched:
                pages.append((item, matched[0], matched[1]))
        if not pages:
            return [], "skip", "没有候选落在平台搜索页上"
        found: list[Candidate] = []
        notes: list[str] = []
        hit_text = ""
        loop = asyncio.get_running_loop()
        deadline = time.monotonic() + self._SEARCH_PAGE_BUDGET
        for page, platform, keyword in pages[: self._SEARCH_PAGE_LIMIT]:
            if platform != "B站":
                notes.append(f"{platform} 的搜索页还没做反解")
                continue
            remaining = deadline - time.monotonic()
            if remaining < 6:
                notes.append("反解的时间用完了")
                break
            accounts = await self._bilibili_accounts(keyword, remaining)
            if not accounts:
                notes.append(f"关键词「{keyword}」在 B 站查不到账号")
                self._vlog("搜索页反解：关键词「%s」没查到账号", keyword)
                continue
            remaining = deadline - time.monotonic()
            if remaining < 4:
                notes.append(f"「{keyword}」反解到 {len(accounts)} 个号，来不及核指纹")
                break
            distances = await self._search_page_distances(accounts, target_hash, remaining)
            hit = searchpage.pick_hit(accounts, distances, threshold)
            measured = [value for value in distances.values() if value is not None]
            if hit is None:
                has_images = any(item.get("images") for item in accounts)
                # 「图对不上」和「图没下下来」是两回事：以前只要账号带封面地址就算
                # 图对不上，其实可能一张都没下成功。
                if measured:
                    reason = "投稿封面都对不上"
                elif has_images:
                    reason = "投稿封面没下下来"
                else:
                    reason = "这些号没有公开的投稿封面"
                nearest = min(measured) if measured else None
                page.account_note = searchpage.miss_note(
                    [str(item.get("name") or "") for item in accounts], reason
                )
                notes.append(f"「{keyword}」反解到 {len(accounts)} 个号，{reason}")
                self._vlog(
                    "搜索页反解：关键词「%s」反解到 %s，%s（最近距离 %s）",
                    keyword,
                    "、".join(str(item.get("name") or item.get("mid")) for item in accounts),
                    reason,
                    nearest,
                )
                continue
            account, image, _distance = hit
            name = str(account.get("name") or "")
            candidate = Candidate(
                engine=page.engine,
                url=str(account.get("url") or ""),
                title=f"{name} 的B站空间" if name else "B站空间",
                image_url=image,
            )
            candidate.account_page = True
            candidate.finalize()
            try:
                candidate = await loop.run_in_executor(
                    self._verify_pool,
                    lambda item=candidate: verify_candidate(
                        item,
                        target_hash,
                        threshold,
                        self._proxy(),
                        section.download_timeout,
                        self._fake_ip_nets(),
                    ),
                )
            except Exception as error:  # noqa: BLE001 - 核不过就当没反解到，不影响整单
                self._vlog("搜索页反解到号但核指纹失败：%s（%s）", candidate.url, str(error)[:60])
                continue
            if candidate.status != "match" or candidate.distance is None:
                self._vlog(
                    "搜索页反解到号但对不上：%s（距离 %s）", candidate.url, candidate.distance
                )
                continue
            candidate.note = searchpage.hit_note(keyword, name, int(candidate.distance))
            # 结论落到的是 UP 主主页、不是那一篇投稿：写明白，免得点进去找不着这张图。
            candidate.account_note = (
                f"🏠 这是 UP 主主页，不是那一篇投稿；主页上的投稿封面里有这张图"
                f"（指纹距离 {candidate.distance}）"
            )
            found.append(candidate)
            notes.append(f"「{keyword}」反解到 {candidate.url}（距离 {candidate.distance}）")
            hit_text = hit_text or notes[-1]
            self._vlog(
                "搜索页反解命中：关键词「%s」→ %s %s（距离 %s）",
                keyword,
                candidate.url,
                name,
                candidate.distance,
            )
        if found:
            return found, "hit", hit_text or "反解到账号并对上了指纹"
        return [], "none", "；".join(notes) or "反解没有结果"

    async def _bilibili_accounts(self, keyword: str, budget: float | None = None) -> list[dict[str, Any]]:
        """关键词 → B 站账号候选（带投稿封面）。失败一律当空。"""
        if not keyword:
            return []
        section = self.config.plugin
        per_call = max(6.0, min(10.0, float(section.download_timeout)))
        limit = self._SEARCH_PAGE_BUDGET if budget is None else max(3.0, float(budget))
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(
                    bilibili.resolve_accounts,
                    keyword,
                    self._proxy(),
                    per_call,
                    self._SEARCH_PAGE_ACCOUNTS,
                    self._SEARCH_PAGE_IMAGES,
                ),
                timeout=limit,
            )
        except Exception as error:  # noqa: BLE001 - 反解是加分项，失败静默降级
            self._vlog("搜索页反解（B站）失败：%s", str(error)[:80])
            return []

    async def _search_page_distances(
        self, accounts: list[dict[str, Any]], target_hash: int, budget: float | None = None
    ) -> dict[str, int | None]:
        """把每个号公开图与目标图的指纹距离都算出来（并发下载，整体限时）。

        超时不等于全丢：已经下完的那几张照样算进结果，剩下的取消——以前超时直接
        `return {}`，注释写着「用手上已经拿到的」，行为却相反，于是「封面还在下」
        被写成「投稿封面都对不上」。
        """

        async def one(image: str) -> tuple[str, int | None]:
            raw = await self._download(image)
            if not raw:
                return image, None
            other = dhash(raw)
            if other is None:
                return image, None
            return image, hamming(target_hash, other)

        images = [str(link) for account in accounts for link in (account.get("images") or [])]
        if not images:
            return {}
        limit = self._SEARCH_PAGE_BUDGET if budget is None else max(1.0, float(budget))
        tasks = [asyncio.ensure_future(one(image)) for image in images]
        done, pending = await asyncio.wait(tasks, timeout=limit)
        for task in pending:
            task.cancel()
        distances: dict[str, int | None] = {}
        for task in done:
            if task.cancelled() or task.exception() is not None:
                continue
            try:
                image, distance = task.result()
            except Exception:  # noqa: BLE001 - 单张失败不影响其它图
                continue
            distances[image] = distance
        return distances

    # 其它平台反查：候选 URL 里写明的非 X 账号最多收这么多个，能核图的平台最多
    # 核两条；跨平台同名另算一路。整段共用一个截止时间，谁也不许单独吃满。
    _OTHER_ACCOUNT_LIMIT = 8
    _OTHER_ACCOUNT_PROBES = 2
    _OTHER_ACCOUNT_BUDGET = 36.0
    # 标题里的名字去 B 站检索：一次检索加一轮封面下载十几秒，两个名字封顶。
    _BILIBILI_NAME_BUDGET = 22.0
    _BILIBILI_NAME_LIMIT = 2

    async def _bilibili_probe(
        self,
        keyword: str,
        target_hash: int | None,
        budget: float | None = None,
        mid: str = "",
    ) -> dict[str, Any]:
        """关键词 → B 站检索 → 投稿封面比指纹。

        返回 ``{"accounts": [...], "hit": (账号, 封面地址, 距离) | None}``。

        非 X 平台里只有这条路能真正核图：``x/web-interface/view`` 实测一律回 412，
        公开图只剩检索结果里的投稿封面。``mid`` 有值时只认那一个号——候选链接里
        已经写明了 mid，别的同名号没必要比。
        """
        if not keyword:
            return {"accounts": [], "hit": None}
        limit = self._BILIBILI_NAME_BUDGET if budget is None else max(3.0, float(budget))
        accounts = await self._bilibili_accounts(keyword, limit)
        if mid:
            accounts = [item for item in accounts if str(item.get("mid") or "") == str(mid)]
        if not accounts or target_hash is None:
            return {"accounts": accounts, "hit": None}
        threshold = int(getattr(self.config.plugin, "hash_threshold", 8) or 8)
        distances = await self._search_page_distances(accounts, target_hash, limit)
        return {
            "accounts": accounts,
            "hit": searchpage.pick_hit(accounts, distances, threshold),
        }

    async def _hunt_bilibili_name(
        self, names: list[str], target_hash: int | None
    ) -> dict[str, Any] | None:
        """标题里的名字 → B 站检索 → 投稿封面比指纹。

        和搜索页反解的区别只在入口：那条是候选落在 ``search.bilibili.com`` 上、
        页面自带关键词；这条是候选落在图集站／搬运站、名字只写在标题里
        （c077 那单就是这样，页面一个账号都不挂）。两条最后都落到同一件事上——
        拿名字找出 UP 主，再用他自己的投稿封面认人。
        """
        if target_hash is None or not names:
            return None
        if not bool(getattr(self.config.plugin, "other_platforms", True)):
            return None
        deadline = time.monotonic() + self._BILIBILI_NAME_BUDGET
        tried: list[str] = []
        found: list[dict[str, Any]] = []
        for name in names[: self._BILIBILI_NAME_LIMIT]:
            remaining = deadline - time.monotonic()
            if remaining < 8:
                break
            tried.append(name)
            probe = await self._bilibili_probe(name, target_hash, remaining)
            for account in probe.get("accounts") or []:
                found.append(
                    {
                        "mid": str(account.get("mid") or ""),
                        "name": str(account.get("name") or ""),
                        "url": str(account.get("url") or ""),
                        "keyword": name,
                    }
                )
            hit = probe.get("hit")
            if hit is None:
                self._vlog("B 站按名字反查：%s 没对上指纹", name)
                continue
            account, image, distance = hit
            row = {
                "status": "hit",
                "name": name,
                "account": str(account.get("name") or ""),
                "mid": str(account.get("mid") or ""),
                "url": str(account.get("url") or ""),
                "image": image,
                "distance": int(distance),
                "accounts": found,
                "names": tried,
            }
            self._vlog(
                "B 站按名字反查命中：%s → %s（mid %s，距离 %s）",
                name,
                row["account"],
                row["mid"],
                row["distance"],
            )
            return row
        if not tried:
            return None
        return {"status": "none", "accounts": found, "names": tried}

    async def _hunt_other_accounts(
        self,
        ordered: list[Candidate],
        best: Candidate | None,
        target_hash: int | None,
    ) -> dict[str, Any] | None:
        """候选里写明的非 X 账号：能核图的核图，核不了的留成线索。

        以前这些号只写进 ``Candidate.handle``，没有任何反查用它——838e 那条
        ``tiktok.com/@starrychibun/video/…`` 就是这样被丢掉的。现在分两路：

        1. 候选 URL 里的账号（B站 / 微博 / TikTok / IG）全部收进线索。B 站拿这个号
           的名字再检索一次、投稿封面比指纹；微博拿 UID 换昵称粉丝，有登录 cookie
           才翻得了带图帖。
        2. 结论已经落到 X 号上时，拿这个 handle 去微博和 B 站各搜一次同名。同一个
           人常几个平台用一个昵称，但同名只是线索——核不上图就不改结论。

        TikTok 和 Instagram 没有可用的公开封面（2026-11-23 实测 oembed 只回 iframe），
        只能当账号线索，这一点写在返回值的 note 里，别让人以为核过了。
        """
        if not bool(getattr(self.config.plugin, "other_platforms", True)):
            return None
        accounts = platforms.collect(ordered, self._OTHER_ACCOUNT_LIMIT)
        if not accounts and best is None:
            return None
        deadline = time.monotonic() + self._OTHER_ACCOUNT_BUDGET
        hit: dict[str, Any] | None = None
        probes = 0
        for row in accounts:
            if probes >= self._OTHER_ACCOUNT_PROBES:
                break
            remaining = deadline - time.monotonic()
            if remaining < 8:
                break
            kind = str(row.get("kind") or "")
            if kind == "weibo" and str(row.get("uid") or ""):
                probes += 1
                info = await self._weibo_profile(str(row["uid"]))
                if not info:
                    continue
                row["name"] = str(info.get("name") or row.get("name") or "")
                row["followers"] = int(info.get("followers") or 0)
                row["url"] = str(info.get("url") or row.get("url") or "")
                row["checked"] = True
                if target_hash is None or not self._weibo_cookie():
                    continue
                remaining = deadline - time.monotonic()
                if remaining < 6:
                    break
                probed = await self._with_weibo_post(
                    {
                        "uid": str(row["uid"]),
                        "name": str(row.get("name") or ""),
                        "followers": int(row.get("followers") or 0),
                        "url": str(row.get("url") or ""),
                    },
                    target_hash,
                )
                post = str(probed.get("post") or "")
                if post:
                    row["post"] = post
                    row["verified"] = True
                    hit = {
                        "platform": "微博",
                        "handle": str(row["uid"]),
                        "name": str(row.get("name") or ""),
                        "url": post,
                        "image": str(probed.get("image") or ""),
                        "distance": int(probed.get("distance") or 0),
                        "source": "候选链接里的微博号",
                    }
                    break
                continue
            if kind == "bilibili" and target_hash is not None:
                keyword = str(row.get("name") or "")
                if not self._searchable_name(keyword):
                    continue
                remaining = deadline - time.monotonic()
                if remaining < 8:
                    break
                probes += 1
                probe = await self._bilibili_probe(
                    keyword, target_hash, remaining, mid=str(row.get("handle") or "")
                )
                if probe.get("accounts"):
                    row["checked"] = True
                picked = probe.get("hit")
                if picked is None:
                    continue
                account, image, distance = picked
                hit = {
                    "platform": "B站",
                    "handle": str(account.get("mid") or row.get("handle") or ""),
                    "name": str(account.get("name") or row.get("name") or ""),
                    "url": str(account.get("url") or row.get("url") or ""),
                    "image": image,
                    "distance": int(distance),
                    "source": "候选链接里的 B 站号",
                }
                break
        # 结论落在 X 号上时，拿 handle 去微博、B 站各搜一次同名。
        if hit is None and best is not None and best.status == "match" and self._is_x(best):
            handle = str(best.handle or "").strip().lstrip("@")
            if handle and self._searchable_name(handle):
                remaining = deadline - time.monotonic()
                if remaining >= 8:
                    try:
                        rows = await asyncio.wait_for(
                            self._search_weibo_users(handle), timeout=remaining
                        )
                    except Exception as error:  # noqa: BLE001 - 查不到只少一条线索
                        self._vlog("跨平台同名（微博 %s）失败：%s", handle, str(error)[:60])
                        rows = []
                    for same in rows[:2]:
                        account = str(same.get("name") or "")
                        if not identity.weibo_name_matches(handle, account):
                            continue
                        accounts.append(
                            {
                                **platforms.from_handle(
                                    "微博",
                                    str(same.get("uid") or ""),
                                    str(same.get("url") or ""),
                                ),
                                "name": account,
                                "followers": int(same.get("followers") or 0),
                                "source": f"按 X @{handle} 搜到的同名号",
                            }
                        )
                remaining = deadline - time.monotonic()
                if remaining >= 10:
                    probe = await self._bilibili_probe(handle, target_hash, remaining)
                    for account in probe.get("accounts") or []:
                        accounts.append(
                            {
                                **platforms.from_handle(
                                    "B站",
                                    str(account.get("mid") or ""),
                                    str(account.get("url") or ""),
                                ),
                                "name": str(account.get("name") or ""),
                                "source": f"按 X @{handle} 找的同名 UP 主",
                            }
                        )
                    picked = probe.get("hit")
                    if picked is not None:
                        account, image, distance = picked
                        hit = {
                            "platform": "B站",
                            "handle": str(account.get("mid") or ""),
                            "name": str(account.get("name") or ""),
                            "url": str(account.get("url") or ""),
                            "image": image,
                            "distance": int(distance),
                            "source": f"按 X @{handle} 找的同名 UP 主",
                        }
        # 去重：候选里的号、跨平台搜到的号可能撞在一起。
        seen: set[tuple[str, str]] = set()
        kept: list[dict[str, Any]] = []
        for row in accounts:
            key = (str(row.get("platform") or ""), str(row.get("handle") or "").lower())
            if not key[1] or key in seen:
                continue
            seen.add(key)
            kept.append(row)
        if hit is None and not kept:
            return None
        return {
            "status": "hit" if hit else "seen",
            "hit": hit,
            "accounts": kept,
        }

    def _promote_other_account(
        self, best: Candidate | None, info: dict[str, Any]
    ) -> Candidate | None:
        """其它平台核到这张图时换成那条：微博换帖子，B 站换 UP 主主页。

        只有指纹对上才走这里（``_hunt_other_accounts`` 里 pick_hit 判过），
        所以这里不必再比一遍。
        """
        hit = info.get("hit") or {}
        platform = str(hit.get("platform") or "").strip()
        distance = int(hit.get("distance") or 0)
        if platform == "微博":
            return self._promote_weibo_post(
                best,
                {
                    "distance": distance,
                    "name": str(hit.get("name") or ""),
                    "uid": str(hit.get("handle") or ""),
                    "url": str(hit.get("url") or ""),
                    "image": str(hit.get("image") or ""),
                    "note": str(hit.get("source") or "候选链接里的微博号"),
                },
            )
        if platform != "B站":
            return best
        mid = str(hit.get("handle") or "").strip()
        url = str(hit.get("url") or "").strip() or (searchpage.space_url(mid) if mid else "")
        if not url:
            return best
        candidate = Candidate(
            engine="other_account",
            url=url,
            image_url=str(hit.get("image") or ""),
            distance=distance,
            status="match",
            platform="B站",
            name=str(hit.get("name") or ""),
            note=(
                f"{str(hit.get('source') or '候选里的 B 站号')}，"
                f"她自己的投稿封面里有这张图（指纹距离 {distance}）"
            ),
        ).finalize()
        # 结论是 UP 主主页、不是那一篇投稿：和搜索页反解保持同一口径。
        candidate.account_page = True
        candidate.account_note = (
            "🏠 这是 UP 主主页，不是那一篇投稿；主页上的投稿封面里有这张图"
        )
        if not candidate.title:
            candidate.title = f"{candidate.name} 的B站空间".strip()
        self._vlog("其它平台命中：B站 %s（距离 %s）", url, distance)
        return candidate

    @staticmethod
    def _other_account_note(info: dict | None) -> str:
        """trace 里那一句：核到了谁，或者只收着哪几个号。"""
        if not info:
            return "候选里没有其它平台的账号"
        hit = (info or {}).get("hit") or {}
        if hit:
            return (
                f"核到 {hit.get('platform')} {hit.get('name') or hit.get('handle')}"
                f"（指纹距离 {hit.get('distance')}）"
            )
        rows = [item for item in (info or {}).get("accounts") or [] if isinstance(item, dict)]
        if not rows:
            return "候选里没有其它平台的账号"
        return "；".join(
            f"{platforms.display(item)}{'（已核图对不上）' if item.get('checked') else ''}"
            for item in rows[:4]
        )

    def _more_limit(self) -> int:
        """回复「更多」时每个引擎列几条：深度检索取到的多，展示上限也跟着放开。"""
        section = self.config.plugin
        return max(1, section.deep_more_limit if section.deep_scan else section.more_limit)

    async def _replay_task(self, current: str, user_id: str, wanted: str) -> tuple[bool, str, int]:
        """按任务编号重发完整结果。

        带编号却找不到时要说清楚：这是缓存过期，不是指令写错了，所以主动回一句。
        裸「更多」找不到就悄悄返回，交给正常聊天，免得群里每次说「更多」都蹦提示。

        发送目标是**发指令的这个会话**，不是当初搜图的会话：在私聊里发「更多
        s-2026…」时，结果要回到私聊，回群里等于把私聊里的动作广播出去
        （实测 s-20261109-0835-c0d9 就是群里搜、私聊要更多、结果又落到群里）。
        """
        if self._tasks is None:
            return False, "插件尚未初始化", 0
        keep = formatting.ttl_text(self.config.plugin.task_ttl_minutes)
        if wanted:
            task = self._tasks.get(wanted)
            if task is None:
                await self.ctx.send.text(
                    f"任务 {wanted} 的结果已经清掉了（只保留 {keep}）。重新发图搜一次吧。",
                    current,
                )
                return True, "任务缓存已过期", 2
            if task.status != "done" or task.outcome is None:
                await self.ctx.send.text(f"任务 {wanted} 还没搜完，稍等一下再发。", current)
                return True, "任务未完成", 2
            await self._send_forward(task, self._more_limit(), replay=True, target=current)
            return True, "已重发指定任务", 2
        task = self._latest_task(current, user_id)
        if task is None or task.status != "done" or task.outcome is None:
            return False, "", 0
        await self._send_forward(task, self._more_limit(), replay=True, target=current)
        return True, "已重发上次结果", 2

    def _eta_text(self) -> str:
        """给用户看的耗时提示。

        实测：指纹直接命中的单中位数 31 秒，要跑视觉/Grok 兜底的中位数 86 秒、
        最慢 373 秒。所以先说 1 分钟，再交代难找时的上限，别按乐观值写死。
        """
        return formatting.ETA_FAST

    def _sauce_route(self, candidates: list[Candidate]) -> Candidate | None:
        """SauceNAO 高相似度命中即认为这张是插画。"""
        if not self._enabled("saucenao"):
            return None
        threshold = self.config.saucenao.route_similarity
        for item in candidates:
            if item.engine != "saucenao" or item.status != "match":
                continue
            if (item.similarity or 0) >= threshold:
                return item
        return None

    @staticmethod
    def _is_x(item: Candidate) -> bool:
        """X / Twitter 平台的结果在指纹距离相同时优先采用。"""
        return is_x_platform(item.platform)

    @staticmethod
    def _is_x_post(item: Candidate) -> bool:
        """只有具体的帖子页能当原帖；主页、转发/回复列表页不算。"""
        return is_x_platform(item.platform) and is_x_post_page(item.url)

    @staticmethod
    def _pick_best(ordered: list[Candidate], prefer_pixiv: bool = False) -> Candidate | None:
        matches = [
            item
            for item in ordered
            if item.status == "match"
            and not dead_site(item.url, item.platform)
            # 双保险：主流程已经把求图帖降成 suspect，这里再挡一次，免得以后
            # 有别的路径把 status 又提回 match。
            and not is_request_post(item.title)
        ]
        if not matches:
            return None
        if prefer_pixiv:
            # 插画路线：SauceNAO 常同时给出转载站和 pixiv 原图，优先当作画师原帖。
            # 但只在相似度接近时才让 pixiv 插队，避免低分 pixiv 命中盖掉高分结果。
            best_score = max(item.similarity or 0 for item in matches)
            pixiv = [
                item
                for item in matches
                if is_pixiv_platform(item.platform, item.url) and (item.similarity or 0) >= best_score - 5
            ]
            if pixiv:
                matches = pixiv
        hashed = [item for item in matches if item.distance is not None]
        if hashed:
            # 账号主页（目前只有搜索页反解出来的 B 站空间页）点进去看不到那张图：
            # 真实帖子距离不比它差太多时优先选帖子，别让「距离 2 的主页」顶掉
            # 「距离 3 的那一篇投稿」。
            posts = [item for item in hashed if not item.account_page]
            page_nearest = min(
                (
                    item.distance
                    for item in hashed
                    if item.account_page and item.distance is not None
                ),
                default=None,
            )
            if posts and page_nearest is not None:
                close_posts = [
                    item
                    for item in posts
                    if item.distance is not None and item.distance <= page_nearest + 2
                ]
                if close_posts:
                    hashed = close_posts
            closest = min(item.distance for item in hashed if item.distance is not None)
            # X 优先从「同距离」放宽到「差 1」：搬运站存的是原图，距离往往比 X 上
            # 被压过的那条低 1~2，死守同距离等于永远轮不到作者本人。差得更多就
            # 不是同一张图了，不能为了平台把它顶上来。
            x_first = [
                item
                for item in hashed
                if ImageSearcherPlugin._is_x_post(item)
                and (item.distance if item.distance is not None else 999) <= closest + 1
            ]
            pool = x_first or hashed
            return min(
                pool,
                key=lambda item: (
                    item.distance if item.distance is not None else 999,
                    0 if ImageSearcherPlugin._is_x_post(item) else 1,
                ),
            )
        return max(matches, key=lambda item: item.similarity or 0)

    # -- 视觉兜底 ---------------------------------------------------------

    _VISION_DOWNLOAD_CONCURRENCY = 4
    # Grok 一次列出的候选帖子上限。每条都要拉 fxtwitter 再下载配图，核多了会拖
    # 慢整单，8 条够用又不至于卡住；命中就提前停，全不中才会跑满。
    _GROK_MAX_POSTS = 8


    async def _vision_review(self, upload: bytes | None, ordered: list[Candidate]) -> dict[str, Any]:
        """指纹一条都没通过时，拼一张对照图交给视觉模型找「同一个人」。

        dHash 比的是整图灰度结构，拼图、裁剪、加字、换姿势都会把它打乱；视觉
        模型看的是人本身，能补上这一类。命中项标 ``status="vision"``，备注里写
        明是视觉复核、没有指纹依据，不能当成「同一张图」发出去。
        """
        section = self.config.vision
        started = time.monotonic()
        info: dict[str, Any] = {
            "ok": False,
            "checked": 0,
            "picked": [],
            # 视觉挑中的全部图片地址（含后来降级的）。挑参考图时优先用它们：
            # 模型说「同一个人」的那张，比指纹更近、模型却没认过的图更有对照价值。
            "picked_images": [],
            "downgraded": 0,
            "reason": "",
            "model": "",
            "error": "",
            "elapsed": 0.0,
            # 图集站页面上写的来源账号（站方标注，未必是作者本人）。
            "sources": [],
        }
        if upload is None:
            info["error"] = "没有原图，无法复核"
            return info

        limit = max(4, min(25, int(section.top_n)))
        # 按指纹距离取全局最接近的几张，不按引擎分组取：同一个引擎的前 N 条
        # 常是同一批相似图，跨引擎各拿几条更容易凑出真正的候选。
        ranked = sorted(
            (item for item in ordered if item.image_url),
            key=lambda item: (item.distance if item.distance is not None else 999, -(item.similarity or 0)),
        )
        pool: list[Candidate] = []
        seen: set[str] = set()
        for item in ranked:
            if item.image_url in seen:
                continue
            seen.add(item.image_url)
            pool.append(item)
            if len(pool) >= limit:
                break
        if not pool:
            info["error"] = "候选都没有图片地址"
            return info

        semaphore = asyncio.Semaphore(self._VISION_DOWNLOAD_CONCURRENCY)

        async def fetch(url: str) -> bytes | None:
            async with semaphore:
                return await self._download(url)

        raws = await asyncio.gather(*(fetch(item.image_url) for item in pool), return_exceptions=True)
        used: list[Candidate] = []
        images: list[bytes] = []
        for item, raw in zip(pool, raws):
            if isinstance(raw, BaseException) or not raw:
                continue
            used.append(item)
            images.append(raw)
        if not images:
            info["error"] = "候选图一张都没下载下来"
            return info

        try:
            grid = await asyncio.to_thread(vision.build_grid, upload, images)
        except Exception as error:  # noqa: BLE001 - 拼图失败不该影响已经搜到的文字结果
            info["error"] = f"拼图失败：{str(error)[:60]}"
            return info

        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": vision.build_prompt(len(images))},
                    {"type": "image_url", "image_url": {"url": grid.data_url}},
                ],
            }
        ]
        timeout = max(10, int(section.timeout))
        options: dict[str, Any] = {
            "prompt": messages,
            "task_name": section.task or "vlm",
            "temperature": 0.1,
            "max_tokens": 600,
        }
        if section.model:
            # 默认留空、按任务组顺序挑；点名是为了绕开组里靠前的推理型模型，
            # 它们会把 max_tokens 花在思考上、正文变空。
            options["model_name"] = section.model
        try:
            result = await asyncio.wait_for(
                self.ctx.call_capability("llm.generate", timeout_ms=timeout * 1000, **options),
                timeout=timeout + 5,
            )
        except asyncio.TimeoutError:
            info["error"] = f"视觉模型 {timeout} 秒内没返回"
            info["elapsed"] = round(time.monotonic() - started, 1)
            # 这个超时多半是「排队」而不是模型慢：宿主同时跑记忆整理之类的重任务时，
            # 视觉请求要等几十秒才轮到。日志里留下等待时长，方便判断该调 timeout 还是查宿主。
            self.ctx.logger.warning(
                "视觉复核超时：等待=%.1fs 超时=%ss 候选=%s 模型=%s",
                info["elapsed"],
                timeout,
                len(images),
                section.model or section.task,
            )
            return info
        except Exception as error:  # noqa: BLE001
            info["error"] = f"视觉模型调用失败：{str(error)[:80]}"
            info["elapsed"] = round(time.monotonic() - started, 1)
            self.ctx.logger.warning("视觉复核调用失败：耗时=%.1fs err=%s", info["elapsed"], error)
            return info

        # call_capability 可能已经把结果解包成字符串，两种形态都要接住。
        payload = result if isinstance(result, dict) else {"success": True, "response": str(result or "")}
        info["elapsed"] = round(time.monotonic() - started, 1)
        if not payload.get("success", True):
            info["error"] = f"视觉模型返回失败：{str(payload.get('error'))[:80]}"
            self.ctx.logger.warning(
                "视觉复核返回失败：耗时=%.1fs err=%s", info["elapsed"], payload.get("error")
            )
            return info
        info["ok"] = True
        info["checked"] = len(images)
        info["model"] = str(payload.get("model_name") or payload.get("model") or "")
        # 推理型模型可能把结论写在 reasoning 里、content 反而是空的，两处都找。
        text = str(payload.get("response") or payload.get("reasoning") or "")
        if not text:
            info["error"] = "视觉模型返回空内容"
            return info

        info["reason"] = vision.reason_of(text)
        info["watermark"] = vision.watermark_of(text)
        degraded: list[Candidate] = []
        for number in vision.parse_picks(text, len(images)):
            candidate = used[number - 1]
            if is_avatar_hit(candidate):
                # 头像是账号头像，跟原帖没关系：视觉说「同一个人」也确认不了什么，
                # 保持头像说明，也不并进视觉降级的统计。
                continue
            distance = candidate.distance
            if candidate.image_url:
                info["picked_images"].append(candidate.image_url)
            threshold = self.config.plugin.hash_threshold
            if distance is None:
                gap = "未做指纹比对"
            elif distance <= threshold:
                # 距离本来就够近：降级是因为落点不对（转发页、商品页），
                # 不能还写「超过阈值」，那会跟同一行里的距离自相矛盾。
                gap = f"指纹距离 {distance}，指纹对得上"
            else:
                gap = f"指纹距离 {distance}，超过阈值 {threshold}"
            # 视觉说「同一个人」还不够，得能落到人身上：电商商品图只是同款衣服，
            # 只有平台页面没账号的结果也确认不了是谁，都降级成疑似留在参考区。
            verdict = vision.verdict_of(candidate.handle, candidate.platform, candidate.url, candidate.name)
            if verdict != vision.VERDICT_VISION:
                candidate.status = "suspect"
                candidate.downgrade = "vision"
                candidate.note = vision.DOWNGRADE_NOTES[verdict].format(
                    gap=gap,
                    platform=candidate.platform or "这个站点",
                    handle=candidate.handle or "空白",
                )
                info["downgraded"] += 1
                degraded.append(candidate)
                continue
            candidate.status = "vision"
            candidate.note = f"视觉复核：同一个人（{gap}）"
            info["picked"].append(candidate.image_url or candidate.url)

        if degraded:
            info["sources"] = await self._gallery_sources(degraded)

        self.ctx.logger.info(
            "视觉兜底：候选=%s 传入=%s 命中=%s 降级=%s 模型=%s 理由=%s%s",
            len(pool),
            len(images),
            len(info["picked"]),
            info["downgraded"],
            info["model"] or "未返回",
            info["reason"] or "无",
            f" 错误={info['error']}" if info["error"] else "",
        )
        return info

    async def _read_watermark(
        self,
        upload: bytes | None,
        *,
        data_url: str = "",
        label: str = "",
    ) -> dict[str, Any]:
        """只读原图上的水印：命中搬运站时，这一步比再拼一次对照图更值得。

        拼图里 TARGET 格只有 320px，手写体小字经常被压没——实测同一张纳西妲
        cos，拼图那次模型回空串，原图直接发过去就能读出「魚蛋 Sakana /
        @Sakana_Yudan」。返回结构跟视觉复核一致，后面那条水印核对链路原样接。
        """
        section = self.config.vision
        info: dict[str, Any] = {
            "ok": False,
            "checked": 0,
            "picked": [],
            "picked_images": [],
            "downgraded": 0,
            "reason": "",
            "model": "",
            "error": "",
            "elapsed": 0.0,
            "sources": [],
            "watermark": "",
        }
        if not upload:
            info["error"] = "没有原图，读不了水印"
            return info

        started = time.monotonic()
        width = height = 0
        if data_url:
            # 分块读：切图和缩放已经由 vision.tile_data_urls 做过，这里直接用。
            pass
        else:
            try:
                data_url, width, height = await asyncio.to_thread(vision.data_url_for, upload)
            except Exception as error:  # noqa: BLE001 - 编码失败只影响这一步
                info["error"] = f"原图编码失败：{str(error)[:60]}"
                return info

        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": vision.build_watermark_prompt(label=label)},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ]
        timeout = max(10, int(section.timeout))
        options: dict[str, Any] = {
            "prompt": messages,
            "task_name": section.task or "vlm",
            "temperature": 0.1,
            # 只抄文字，不需要长回答；给太多额度反而容易开始描述画面。
            "max_tokens": 300,
        }
        if section.model:
            options["model_name"] = section.model
        try:
            result = await asyncio.wait_for(
                self.ctx.call_capability("llm.generate", timeout_ms=timeout * 1000, **options),
                timeout=timeout + 5,
            )
        except asyncio.TimeoutError:
            info["error"] = f"读水印 {timeout} 秒内没返回"
            info["elapsed"] = round(time.monotonic() - started, 1)
            self.ctx.logger.warning("读水印超时：等待=%.1fs 超时=%ss", info["elapsed"], timeout)
            return info
        except Exception as error:  # noqa: BLE001
            info["error"] = f"读水印调用失败：{str(error)[:80]}"
            info["elapsed"] = round(time.monotonic() - started, 1)
            self.ctx.logger.warning("读水印失败：%s", error)
            return info

        payload = result if isinstance(result, dict) else {"success": True, "response": str(result or "")}
        info["elapsed"] = round(time.monotonic() - started, 1)
        if not payload.get("success", True):
            info["error"] = f"读水印返回失败：{str(payload.get('error'))[:80]}"
            return info
        info["ok"] = True
        info["model"] = str(payload.get("model_name") or payload.get("model") or "")
        text = str(payload.get("response") or payload.get("reasoning") or "")
        if not text:
            info["error"] = "读水印返回空内容"
            return info
        parsed = vision.parse_watermark_reply(text)
        info["watermark"] = parsed["account"] or parsed["text"]
        info["watermark_text"] = parsed["text"]
        self.ctx.logger.info(
            "读水印%s：%s 模型=%s 文字=%s 账号=%s 耗时=%.1fs",
            f"[{label}]" if label else "",
            f"{width}x{height}" if width and height else "分块",
            info["model"] or "未返回",
            parsed["text"] or "无",
            parsed["account"] or "无",
            info["elapsed"],
        )
        return info

    _GALLERY_MAX_PAGES = 2

    async def _gallery_sources(self, items: list[Candidate], *, author_pages: bool = False) -> list[dict[str, Any]]:
        """读图集站页面上自己标的来源账号。

        视觉挑中的图常常落在 4play、v2ph 这类搬运站上：页面本身不是作者主页，
        所以结论会把它降级。但站方通常写着来源账号——实测 4play 的 meta 直接给
        了 ``instagram.com/menruinyanko``，那是整单唯一指向本人的线索。这里只
        抓降级候选里的图集站页面，最多两页，读不到就当没有。

        ``author_pages=True`` 时多认一类站（`AUTHOR_PAGE_HOSTS`，如 picazor）：
        这类站的图确实是原图，只是页面不是作者主页，所以不降级，单独翻一层
        「帖子页 → 发布者主页」拿作者账号。
        """
        pool: list[Candidate] = []
        seen: set[str] = set()
        reused: list[dict[str, Any]] = []
        for item in items:
            url = (item.url or "").strip()
            if not url or url in seen:
                continue
            # 列表页挖出来的那篇帖子已经顺手抽过账号了，直接拿来用，别再抓一次。
            known = [
                account
                for account in (getattr(item, "source_accounts", []) or [])
                if account.get("handle")
            ]
            if known:
                seen.add(url)
                site = url.split("://", 1)[-1].split("/", 1)[0].removeprefix("www.")
                for account in known[:2]:
                    reused.append({**account, "site": site, "page": url})
                continue
            if not is_gallery_site(url, item.platform):
                if not (author_pages and is_author_page_site(url, item.platform)):
                    continue
            seen.add(url)
            pool.append(item)
            if len(pool) >= self._GALLERY_MAX_PAGES:
                break
        if not pool:
            return reused

        results = await asyncio.gather(
            *(self._page_source(item.url) for item in pool), return_exceptions=True
        )
        sources: list[dict[str, Any]] = []
        for item, result in zip(pool, results):
            if isinstance(result, BaseException) or not result:
                continue
            site = (item.url or "").split("://", 1)[-1].split("/", 1)[0].removeprefix("www.")
            for account in result[:2]:
                sources.append({**account, "site": site, "page": item.url})
            self._vlog(
                "图集站来源：%s → %s",
                site,
                "、".join(f"{a['platform']}@{a['handle']}" for a in result[:2]),
            )
        return reused + sources

    async def _page_source(self, url: str) -> list[dict[str, Any]]:
        """抓一页 HTML，抽里面的来源账号；页面上没有就再翻一层发布者主页。

        picazor 那类站把账号挂在发布者主页上，帖子页只有一条 ``/en/<名字>`` 的
        内部链接。所以第一页抽不到账号时，用 `author_page_url()` 找出这个上级
        链接再抓一次——只多一次请求，读不到就当没有。
        """

        def fetch(target: str) -> str:
            session = make_session(self._proxy())
            try:
                response = session.get(
                    target,
                    timeout=10,
                    headers={
                        "User-Agent": (
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/131.0 Safari/537.36"
                        ),
                        "Accept": "text/html,application/xhtml+xml",
                    },
                )
                if response.status_code != 200:
                    return ""
                # 这类站的长尾推荐能到几百 KB，截断后仍然够统计账号出现次数。
                return response.text[:400000]
            finally:
                session.close()

        async def read(target: str) -> str:
            try:
                return await asyncio.wait_for(asyncio.to_thread(fetch, target), timeout=14)
            except Exception:  # noqa: BLE001 - 页面读不到只少一条线索
                return ""

        html = await read(url)
        if not html:
            return []
        accounts = parse_source_accounts(html, url)
        if accounts:
            return accounts
        parent = author_page_url(html, url)
        if not parent or parent == url:
            return []
        page = await read(parent)
        if not page:
            return []
        self._vlog("图集站翻发布者主页：%s → %s", url, parent)
        accounts = author_from_author_page(page, parent)
        for account in accounts:
            account["author_page"] = parent
        return accounts

    async def _grok_lookup(
        self, upload: bytes | None, candidates: list[Candidate] | None = None, reply_hint: str = ""
    ) -> dict[str, Any]:
        """把原图和已有候选一起交给 Grok，让它在 X 上核实到底是谁发的。

        实测过：单纯让它读水印会把 ``@ste11ar021`` 读成 ``@butter021``；让它凭空
        看图猜关键词去搜，也基本搜不到（实测那轮用的是 ``x_keyword_search``，全
        军覆没）。所以改成把图片反搜已经拿到的候选账号、标题一起递过去，让它做
        「核实这个人是不是发过这张图」，只在候选都对不上时才自己另找。
        """
        section = self.config.grok
        info: dict[str, Any] = {
            "ok": False,
            "tier": "none",
            "handle": "",
            # 作者昵称：核对命中时从帖子作者身上回填，首次回复要写「名字」，
            # 只给一条链接的话用户还得点进去才认得出是谁。
            "name": "",
            "post_url": "",
            "reference_url": "",
            "reason": "",
            "model": "",
            # 图上读到的 ID（水印、签名、帖号）。只是线索：实测读水印会把
            # @ste11ar021 认成 @butter021，所以它不参与结论，只进日志。
            "watermark": "",
            "error": "",
            "tools": [],
            "hints": 0,
            "verified": "unknown",
            # 核对时跟原图最接近的那张配图的指纹距离，参考图文案里要用。
            "distance": -1,
        }
        if upload is None:
            info["error"] = "没有原图，无法搜人"
            return info
        key = (section.api_key or "").strip()
        if not key:
            info["error"] = "没有配置 API Key"
            return info
        base = (section.base_url or "").strip().rstrip("/")
        if not base:
            info["error"] = "没有配置 Base URL"
            return info
        # 中转站会轮流挂 4.7 和 4.6：主模型失败就换下一个，别在同一棵树上等死。
        models = grok.model_chain(section.model, getattr(section, "models", None))
        model = models[0]
        try:
            compact, _ = prepare_upload(upload, limit=900_000, as_jpeg=True)
        except Exception as error:  # noqa: BLE001 - 图片处理失败不该影响其他兜底
            info["error"] = f"原图处理失败：{str(error)[:60]}"
            return info
        data_url = "data:image/jpeg;base64," + base64.b64encode(compact).decode("ascii")
        hints = grok.build_hints(grok.social_first(candidates or []))
        info["hints"] = len(hints)
        # 候选里出现 /reposts、/likes 这类页面时，必须提前告诉模型「图不是她发的」。
        # 不说的话它会认下转发者、答「没有原帖」（实测如此）；说清之后它会转去读
        # 图上的水印，找到真正发帖的号。
        reposts: list[str] = []
        for item in candidates or []:
            url = getattr(item, "url", "") or ""
            if is_repost_page(url) and url not in reposts:
                reposts.append(url)
        repost_note = "、".join(reposts[:3])
        if reposts:
            self._vlog("Grok 提示：候选里有转发页 %s", repost_note)
        trace = " ".join((reply_hint or "").split())
        if trace:
            # 程序已经把「她的回复」翻过一遍还没找着，这条必须写进 prompt：
            # 不然模型会以为没人试过，绕回同一条路，或者干脆说「没有原帖」。
            self._vlog("Grok 提示：回复页已翻过但没成 %s", trace)
        timeout = max(15, int(section.timeout))
        proxy = self._proxy()

        def call(payload_body: dict, seconds: int) -> dict:
            session = make_session(proxy, headers={"Authorization": f"Bearer {key}"})
            try:
                response = session.post(f"{base}/responses", json=payload_body, timeout=seconds)
                if response.status_code != 200:
                    raise HttpError(f"HTTP {response.status_code}：{response.text[:160]}")
                return response.json()
            finally:
                session.close()

        def quick_probe(name: str) -> bool:
            """换备用模型之前先花几秒探一下它是不是也挂了。

            整家中转站上游故障时两个模型会一起回 502，而每次失败要等 100 秒上下：
            实测 22:23 与 22:41 两单各白等 3~4 分钟，整单拖到 325 秒。这里用 8 个
            token 的极小请求探活，几秒就能判定；探不通就直接放弃，不再排第二次长等。
            探得通说明只是主模型单独挂了，照旧换过去跑完整检索。
            """
            body = {
                "model": name,
                "input": [{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}],
                "max_output_tokens": 8,
            }
            try:
                call(body, 20)
                return True
            except Exception:  # noqa: BLE001 - 探活失败只意味着放弃备选
                return False

        # 每个模型各等一次完整超时。中转站现在跑一轮带图的 X 检索要 100 秒上下
        # （实测 grok-4.6 118.8 秒才回、grok-4.7 超 120），超时给到 210 秒才稳；
        # 原来还有一次「短超时补一次」，实测没救回过任何一单，只是又白占 60 秒，删了。
        attempts: list[tuple[str, int]] = [(name, timeout) for name in models]
        started = time.time()
        payload: dict | None = None
        for index, (name, seconds) in enumerate(attempts):
            body = grok.build_payload(
                name, data_url, hints=hints, repost_note=repost_note, reply_hint=trace
            )
            try:
                payload = await asyncio.wait_for(
                    asyncio.to_thread(call, body, seconds), timeout=seconds + 5
                )
                model = name
                break
            except asyncio.TimeoutError:
                info["error"] = f"中转站 {seconds} 秒内没响应"
            except Exception as error:  # noqa: BLE001
                if isinstance(error, requests.exceptions.Timeout):
                    info["error"] = f"中转站 {seconds} 秒内没响应"
                elif isinstance(error, requests.exceptions.ConnectionError):
                    info["error"] = "中转站连不上"
                else:
                    info["error"] = f"调用 {name} 失败：{str(error)[:100]}"
                message = str(error)
                # key 失效换模型也没用；超时、连不上、5xx、模型不存在都换下一个。
                if isinstance(error, HttpError) and ("HTTP 401" in message or "HTTP 403" in message):
                    break
                if not isinstance(
                    error,
                    (requests.exceptions.Timeout, requests.exceptions.ConnectionError, HttpError),
                ):
                    break
                # 5xx 是上游整家挂了：两个模型走同一家中转站，多半一起 502。换之前
                # 先花几秒探一下下一个模型的死活，探不通就直接放弃，别再排一次上百秒
                # 的长等——实测那样把单次检索从 2 分钟拖到 325 秒。
                if grok.is_upstream_down(message) and index + 1 < len(attempts):
                    following = attempts[index + 1][0]
                    if await asyncio.to_thread(quick_probe, following):
                        self._vlog("Grok %s 上游 5xx，但 %s 探活正常，继续换过去", name, following)
                    else:
                        info["error"] = f"{name} 与 {following} 都不可用（上游 5xx）"
                        self._vlog("Grok 上游 5xx，探活 %s 也不通，放弃换模型", following)
                        break
            if index + 1 < len(attempts):
                next_name = attempts[index + 1][0]
                if next_name == name:
                    self._vlog("Grok %s 这次没成（%s），2 秒后重试一次", name, info["error"])
                else:
                    self._vlog("Grok %s 没成（%s），换 %s", name, info["error"], next_name)
                await asyncio.sleep(2)
        if payload is None and len(models) > 1:
            info["error"] = f"{info['error']}（试过 {'、'.join(models)}）"
        if payload is not None:
            result = grok.parse_output(payload, model=model)
            info.update(
                {
                    "ok": result.ok,
                    "tier": result.tier,
                    "handle": result.handle,
                    "post_url": result.post_url,
                    "reason": result.reason,
                    "model": result.model or model,
                    "tools": result.tools,
                    "watermark": result.watermark,
                    "error": result.error,
                }
            )
            if result.watermark:
                # 读错的 ID 一旦被当成结论会直接报错人，所以在日志里单独留一行，
                # 出问题能立刻看出它是「读错了」还是「读对了但没核到帖子」。
                self._vlog("Grok 图上读到 ID：%s", result.watermark)
            if result.tier == "account" and result.handle:
                # 只给主页时先确认这个号真的存在；查不到就整条丢掉，宁可报没找到，
                # 也不要把模型编出来的名字当成线索发出去。被封的号单独说明：
                # 人是真的，只是内容已经看不到。
                profile = await self._account_profile(result.handle)
                if profile.state == fxtwitter.PROFILE_MISSING:
                    # 明确查无此号才丢；suspended 只代表第三方读不到，号可能还在。
                    info["tier"] = "none"
                    info["handle"] = ""
                    info["error"] = f"@{result.handle} 在 X 上查不到，已丢弃"
                    info["reason"] = info["error"]
                elif profile.state == fxtwitter.PROFILE_SUSPENDED:
                    suffix = f"（用户 ID {profile.user_id}）" if profile.user_id else ""
                    if profile.confirmed:
                        # 主页 HTML 里真的吐出「Account suspended」才算封号。
                        info["tier"] = "none"
                        info["handle"] = ""
                        info["error"] = f"@{result.handle} 已被 X 封禁{suffix}，看不到内容，已丢弃"
                        info["reason"] = info["error"]
                    else:
                        info["reason"] = (
                            f"{info.get('reason') or ''}｜注意：X 接口读不到 @{result.handle}{suffix}，"
                            "可能是受限账号，主页要自己点开确认"
                        ).lstrip("｜")

            if info["tier"] == "post" and info["post_url"]:
                # 帖子里到底有没有这张图，不能只看模型给的链接：拉回来比指纹。
                # Grok 可能给出好几条看着像的帖子，逐条核，谁对上算谁——只看
                # 第一条的话，它挑错一条整单就废了。
                refs = [info["post_url"]]
                if result.posts:
                    refs = [url for _handle, url in result.posts[: self._GROK_MAX_POSTS]]
                checks: list[dict[str, Any]] = []
                for url in refs:
                    current = await self._verify_tweet(url, upload)
                    if current["verdict"] == "unknown":
                        # 图床一次 403、代理抖一下，就会把真原帖判成 unknown。
                        # 多条核对时漏掉一条就前功尽弃，这里补核一次。
                        self._vlog("帖子核对没下到图，重试一次：%s", url)
                        current = await self._verify_tweet(url, upload)
                    checks.append(current)
                    if current["verdict"] == "hit":
                        break
                info["checked_posts"] = len(checks)
                check = grok.pick_best_check(checks)
                # x.com/i/status/<id> 这种内部链接没有作者名，核对时 fxtwitter 会
                # 解析出真实作者，回填进去，后面 miss 分支才认得出这是个活账号。
                tweet = (check or {}).get("tweet")
                if tweet is not None and getattr(tweet, "ok", False) and getattr(tweet, "handle", ""):
                    if not info["handle"]:
                        info["handle"] = tweet.handle
                        info["post_url"] = f"https://x.com/{tweet.handle}/status/{tweet.tweet_id}"
                    if getattr(tweet, "name", "") and (
                        not info["handle"] or tweet.handle.lower() == str(info["handle"]).lower()
                    ):
                        info["name"] = tweet.name
                info["verified"] = check["verdict"] if check else "unknown"
                if check is not None:
                    info["distance"] = check["distance"]
                if check is not None and check["verdict"] == "hit":
                    note = f"帖子配图对得上（距离 {check['distance']}）"
                    info["reason"] = f"{note}；{info['reason']}" if info["reason"] else note
                    # 命中的那张配图顺手当参考图。以前只有核对失败才记
                    # reference_url，于是「找到原帖」的单子转发里反而没图，只剩
                    # 一句「没有可用的候选图，转发里是你发的原图」。
                    info["reference_url"] = check.get("closest_url") or ""
                elif check is not None and check["verdict"] == "miss":
                    note = f"帖子配图对不上（最近距离 {check['distance']}）"
                    if info["handle"]:
                        # 帖子留着当「存疑参考」：账号多半是对的，配图可能是同一套
                        # 的另一张。但不能说成原帖——同景同角色换个人拍，指纹也这样。
                        info["tier"] = "account"
                        info["reason"] = f"{note}，帖子只当参考"
                        # 核对时最接近的那张，拿来当参考图，用户可以直接肉眼比。
                        info["reference_url"] = check.get("closest_url") or ""
                    else:
                        info["tier"] = "none"
                        info["post_url"] = ""
                        info["reason"] = f"{note}，已丢弃"

        if info["tier"] == "account" and info["handle"] and upload:
            # 只知道账号、或者它给的帖子对不上时，去翻这个号自己的带图帖子：
            # 转发不进那个列表，命中的一定是它亲手发的，而且直接给出帖子地址。
            found = await self._media_lookup(info["handle"], upload)
            if found and found.get("url"):
                info["tier"] = "post"
                info["post_url"] = found["url"]
                info["verified"] = "hit"
                info["distance"] = found["distance"]
                info["reference_url"] = str(found.get("image") or "")
                note = (
                    f"翻了 @{info['handle']} 自己的带图帖子，这张图就在里面"
                    f"（距离 {found['distance']}）"
                )
                info["reason"] = f"{note}；{info['reason']}" if info["reason"] else note

        self.ctx.logger.info(
            "Grok 兜底：档位=%s 图上ID=%s 账号=%s 帖子=%s 核对=%s 候选=%s 工具=%s 耗时=%.1fs 理由=%s%s",
            info["tier"],
            info["watermark"] or "无",
            info["handle"] or "无",
            info["post_url"] or "无",
            info.get("checked_posts", 0),
            info["hints"],
            ",".join(info["tools"]) or "无",
            time.time() - started,
            info["reason"] or "无",
            f" 错误={info['error']}" if info["error"] else "",
        )
        return info

    async def _fetch_tweet(self, ref: str) -> fxtwitter.Tweet:
        """按链接或 ID 拉一条推文的元数据（fxtwitter，不需要登录）。"""
        tweet_id = fxtwitter.parse_tweet_id(ref)
        if not tweet_id:
            return fxtwitter.Tweet(error="没看出推文 ID")
        url = fxtwitter.build_api_url(tweet_id)
        proxy = self._proxy()
        timeout = min(20, max(8, int(self.config.plugin.download_timeout) + 6))

        def call() -> dict:
            session = make_session(proxy, headers={"Accept": "application/json"})
            try:
                response = session.get(url, timeout=timeout)
                if response.status_code != 200:
                    raise HttpError(f"HTTP {response.status_code}")
                return response.json()
            finally:
                session.close()

        try:
            payload = await asyncio.wait_for(asyncio.to_thread(call), timeout=timeout + 4)
        except Exception as error:  # noqa: BLE001 - 拉不到就当核对失败，不影响主流程
            return fxtwitter.Tweet(error=f"拉推文失败：{str(error)[:60]}", tweet_id=tweet_id)
        return fxtwitter.parse_tweet(payload, tweet_id=tweet_id)

    def _apply_tweet_identity(self, candidate: Candidate, tweet) -> bool:
        """把候选的地址/作者换成真正的原帖。换了返回 True。

        fxtwitter 的 ``quote`` 字段直接给出原帖地址与作者。引用帖自己没配图
        （``media`` 为空）时，候选图只能来自被引用那条——YuMIkAnime 那单就是
        引用了 @qyuuuz 的图吐槽，结果被当成作者发了出去。自己配了图的不动，
        那种情况图确实是它发的。

        转发（RT）走另一条：候选链接常常是 ``x.com/<转发者>/status/<id>``，
        fxtwitter 返回的 ``url`` 才是原帖、``author`` 才是原作者，``reposted_by``
        写明是谁转的。不换的话结论会挂到转发的人头上——实测 s-20261109-2222-b5e6
        把 @sssmnms 的图判给了转发者 @redwine278，图上的水印就写着 @sssmnms。
        """
        if not tweet.ok:
            return False
        if tweet.reposted_by and tweet.url and tweet.handle:
            former = f"@{tweet.reposted_by}"
            candidate.url = tweet.url
            candidate.handle = f"@{tweet.handle}"
            candidate.name = tweet.name or candidate.name
            candidate.platform = "X (Twitter)"
            candidate.source = "X"
            note = f"这条是 {former} 转的帖子，原文在 @{tweet.handle}"
            candidate.note = f"{candidate.note}；{note}" if candidate.note else note
            self._vlog("转发换成原帖：%s → %s（原作者 @%s）", former, candidate.url, tweet.handle)
            return True
        if not tweet.quote_url or not tweet.quote_handle:
            return False
        if tweet.media:
            return False
        former = f"@{tweet.handle}" if tweet.handle else "这条帖子"
        candidate.url = tweet.quote_url
        candidate.handle = f"@{tweet.quote_handle}"
        candidate.name = tweet.quote_name or candidate.name
        candidate.platform = "X (Twitter)"
        candidate.source = "X"
        note = f"原文是 {former} 的引用帖，图来自被引用的 @{tweet.quote_handle}"
        candidate.note = f"{candidate.note}；{note}" if candidate.note else note
        self._vlog("引用帖换成原帖：%s → %s", former, candidate.url)
        return True

    # 候选列表里的转发纠正：只为「已经对上图」的那批多花请求。mismatch 的图本来
    # 就不是这张，把作者名改对也没有意义。实测每单这类候选 0～3 条，8 路并发下
    # 增量不到 1 秒，所以预算给 12 秒就够，超时直接放弃、不影响结论。
    _FOLLOW_CONCURRENCY = 6
    _FOLLOW_BUDGET = 12.0

    async def _follow_quotes(
        self, candidates: list[Candidate], best: Candidate | None
    ) -> Candidate | None:
        """把指纹已经对上的 X 候选逐条换回原帖与原作者。

        去重是按「平台 + 账号」做的，所以这一步放在去重之后：先纠正再合并会把
        同一个原作者的几条不同帖子压成一条。改的是同一批对象，结论里的 best
        也跟着更新，不需要重新挑。
        """
        targets: list[Candidate] = []
        seen: set[str] = set()
        for item in candidates:
            if item is not best and item.status not in {"match", "suspect"}:
                continue
            url = item.url or ""
            if not is_x_post_page(url) or url in seen:
                continue
            seen.add(url)
            targets.append(item)
        if not targets:
            return best

        semaphore = asyncio.Semaphore(self._FOLLOW_CONCURRENCY)

        async def one(candidate: Candidate) -> None:
            async with semaphore:
                tweet = await self._fetch_tweet(candidate.url)
                self._apply_tweet_identity(candidate, tweet)

        try:
            await asyncio.wait_for(
                asyncio.gather(*(one(item) for item in targets), return_exceptions=True),
                timeout=self._FOLLOW_BUDGET,
            )
        except asyncio.TimeoutError:
            self._vlog("候选转发纠正超时：候选=%s 上限=%ss", len(targets), self._FOLLOW_BUDGET)
        return best

    async def _verify_tweet(self, ref: str, upload: bytes | None) -> dict[str, Any]:
        """把推文里的配图和用户原图做指纹比对。

        返回 verdict：hit（同一张图）/ miss（不是这张）/ unknown（没下到图或解不出指纹）。
        unknown 不能当成 miss——图床一次 403 就把真结果判死，比不核对更糟。
        """
        info: dict[str, Any] = {"verdict": "unknown", "distance": -1, "tweet": None, "error": ""}
        if not upload:
            info["error"] = "没有原图"
            return info
        tweet = await self._fetch_tweet(ref)
        info["tweet"] = tweet
        if not tweet.ok:
            info["error"] = tweet.error
            return info
        if not tweet.media:
            info["error"] = "这条帖子没有配图"
            return info
        mine = dhash(upload)
        if mine is None:
            info["error"] = "原图解不出指纹"
            return info
        distances: list[int] = []
        closest_url = ""
        closest_distance = 999
        for url in tweet.media[:4]:
            raw = await self._download(url)
            if not raw:
                continue
            other = dhash(raw)
            if other is None:
                continue
            distance = hamming(mine, other)
            distances.append(distance)
            if distance < closest_distance:
                closest_distance = distance
                closest_url = url
        info["verdict"] = fxtwitter.judge(distances, self.config.plugin.hash_threshold)
        info["distance"] = min(distances) if distances else -1
        info["closest_url"] = closest_url
        if info["verdict"] == "unknown" and not info["error"]:
            info["error"] = "配图一张都没下到"
        self._vlog(
            "推文核对：id=%s 作者=@%s 配图=%s 距离=%s 结论=%s",
            tweet.tweet_id,
            tweet.handle or "无",
            len(tweet.media),
            info["distance"],
            info["verdict"],
        )
        return info

    def _xid_reply(self, check: dict[str, Any]) -> str:
        tweet: fxtwitter.Tweet | None = check.get("tweet")
        threshold = self.config.plugin.hash_threshold
        if tweet is None or not tweet.ok:
            reason = check.get("error") or "拉不到这条帖子"
            return f"❌ 核对失败：{reason}。链接或 ID 对不对？"
        who = f"@{tweet.handle}" if tweet.handle else "未知账号"
        if tweet.name:
            who += f"（{tweet.name}）"
        link = tweet.url or f"https://x.com/{tweet.handle}/status/{tweet.tweet_id}"
        if check["verdict"] == "hit":
            return (
                f"✅ 对上了：这张图就是 {who} 这条帖子里的图，指纹距离 {check['distance']}。\n{link}"
            )
        if check["verdict"] == "miss":
            return (
                f"❌ 对不上：{who} 这条帖子的配图和你的图差得远，"
                f"最近距离 {check['distance']}（阈值 {threshold}）。\n{link}"
            )
        return f"⚠️ 没法比对：{who} 这条帖子拉到了，但配图没取下来（{check.get('error') or '下载失败'}）。\n{link}"

    def _drop_site_name_watermarks(
        self, vision_info: dict[str, Any] | None, ordered: list[Candidate]
    ) -> None:
        """水印读出来的词如果就是候选站点自己的名字，那不是账号，是站名。

        实测 `s-20261120-0004-158d`：图上印着 PixiBB，那是图集站
        `cosplay.pixibb.com` 的站名，`x.com/pixiBB` 恰好有人注册（brandy french），
        于是被挂成一条线索，还顺着它去查了微博。站名是网站二次上传时压上去的，
        跟作者无关。
        """
        if not vision_info:
            return
        urls = [item.url for item in ordered if item.url]
        if not urls:
            return
        readings = [
            item
            for item in (vision_info.get("watermark_readings") or [])
            if isinstance(item, dict)
        ]
        kept = [
            item for item in readings if not is_site_name(str(item.get("handle") or ""), urls)
        ]
        if len(kept) != len(readings):
            dropped = [
                str(item.get("handle") or "")
                for item in readings
                if item not in kept
            ]
            vision_info["watermark_readings"] = kept
            self._vlog("水印读到的是站点名，不当账号：%s", "、".join(dropped))
        handle = str(vision_info.get("watermark_handle") or "").strip().lstrip("@")
        if handle and is_site_name(handle, urls):
            for key in (
                "watermark_handle",
                "watermark_url",
                "watermark_state",
                "watermark_user_id",
                "watermark_name",
                "watermark_note",
            ):
                vision_info[key] = ""
            self._vlog("水印主号是站点名，丢掉：@%s", handle)

    def _promote_watermark_hit(
        self, best: Candidate | None, vision_info: dict[str, Any] | None
    ) -> Candidate | None:
        """水印核对到本人发过这张图时，把它当成结论。

        这条证据是「作者的带图帖里指纹对上」，比视觉认人硬。所以只有一种情况不
        顶掉：现有结论本身也是指纹对上的帖、又落到了具体账号上（那是另一条硬证据，
        谁更对留给用户点开看）。

        原来的写法是「已经有结论就不顶」，结果 s-20261112-0950-af18 里视觉挑了
        @ayawan1012（距离 22，只是模型觉得像），而水印那一步已经核到
        @idtNothinglose 自己发过这张图（距离 0），榜首却还是 ayawan——一条回复
        里两个账号，说的还不是同一个人。
        """
        if not vision_info:
            return best
        if (
            best is not None
            and best.status == "match"
            and (best.handle or "").strip()
            and not self._is_repost_result(best)
        ):
            return best
        post = str(vision_info.get("watermark_post") or "").strip()
        handle = str(vision_info.get("watermark_handle") or "").strip()
        if not post or not handle:
            return best
        account = "@" + handle.lstrip("@")
        distance = int(vision_info.get("watermark_distance") or 0)
        self._vlog("水印核对命中，升成结论：%s %s（距离 %s）", account, post, distance)
        note = f"图上水印核对：{account} 自己的带图帖里有这张图（距离 {distance}）"
        alt_from = str(vision_info.get("watermark_alt_from") or "").strip()
        if alt_from:
            # 水印那个号本人没发，是同一个人另一个号发的：这句要说清楚，
            # 不然用户会以为图上写的就是结论这个号。
            note = (
                f"图上水印写着 @{alt_from.lstrip('@')}，"
                f"同一人的另一个号 {account} 发过这张（距离 {distance}）"
            )
        return Candidate(
            engine="watermark",
            url=post,
            platform="X (Twitter)",
            handle=account,
            name=str(vision_info.get("watermark_name") or ""),
            distance=distance,
            status="match",
            note=note,
        )

    def _is_repost_result(self, item: Candidate | None) -> bool:
        """这条结论是不是「别人转发的页面」：落点是搬运站/社区站且没给出账号。"""
        if item is None:
            return False
        if (item.handle or "").strip():
            return False
        return is_repost_site(item.url or "", item.platform or "")

    async def _check_watermark_post(
        self, handle: str, upload: bytes | None, vision_info: dict[str, Any]
    ) -> None:
        """水印号存在还不够：翻它自己的带图帖，看有没有这张图。

        模型读小字会出错，读到一个真实存在、却跟这张图毫无关系的号也见过（实测
        ``X@Miharu`` 是个 2010 年的无关老号，点进去只会浪费时间）。所以存在的号
        再核一道：最近十几条带图帖里有没有这张。命中就把帖子地址和距离写下来，
        调用方会把它当成结论；没命中只写「最近没有」——老图翻不到很正常，不能
        据此断言「不是她」。
        """
        if not upload:
            vision_info["watermark_url"] = f"https://x.com/{handle}"
            vision_info["watermark_note"] = "这个号存在，但没有原图，没法核对它发没发过"
            return
        vision_info["watermark_handle"] = handle
        vision_info["watermark_url"] = f"https://x.com/{handle}"
        hit = await self._media_lookup(handle, upload)
        if hit and hit.get("error"):
            # 一次 5xx／超时可能只是接口抽风，先原样再试一次。
            self._vlog("翻 @%s 的带图帖子失败，重试一次：%s", handle, hit["error"])
            hit = await self._media_lookup(handle, upload)
        media_error = str((hit or {}).get("error") or "")
        scan: dict[str, Any] | None = None
        if hit and "scanned_posts" in hit:
            scan = {
                "total_posts": int(hit.get("total_posts") or 0),
                "scanned_posts": int(hit.get("scanned_posts") or 0),
                "scanned_images": int(hit.get("scanned_images") or 0),
                "truncated": bool(hit.get("truncated")),
            }
            vision_info["watermark_scan"] = scan
        search_error = ""
        if not (hit and hit.get("url")):
            # /media 只看得到最近若干条，老图翻不到；搜索那边有时还留着一条，
            # 用 ``from:<号> filter:images`` 再翻一页做兜底。
            fallback = await self._search_media_lookup(handle, upload)
            if fallback and fallback.get("url"):
                hit = fallback
            elif fallback and fallback.get("error"):
                # 搜索只是兜底：/media 已经实实在在翻过一遍时，不能因为它挂了就
                # 把「翻过、没有这张」写成「这次没翻成」——8bc9 那单就是这么把
                # 17 条带图帖的核对结果丢掉的。
                search_error = str(fallback["error"])
                if media_error:
                    hit = fallback
        if hit and hit.get("error"):
            # 拉取失败和「翻到了但没有」是两件事：接口 404/超时的时候不能写成
            # 「最近没有这张」，那等于把一次没查成当成了否定证据。
            vision_info["watermark_note"] = (
                f"这次没翻成它的帖子（{hit['error']}），不能据此说没有"
            )
            self._vlog("水印核对没翻成：@%s（%s）", handle, hit["error"])
            return
        if hit and hit.get("url"):
            distance = int(hit.get("distance") or 0)
            post = str(hit.get("url") or "")
            vision_info["watermark_post"] = post
            vision_info["watermark_distance"] = distance
            vision_info["watermark_note"] = (
                f"自己的带图帖里有这张图，指纹距离 {distance}"
            )
            self._vlog("水印核对命中：@%s → %s（距离 %s）", handle, post, distance)
            return
        # 「可能读错」这句由渲染那边统一写在括号里，这里再说一次就重复了。
        if scan and scan.get("scanned_posts"):
            note = (
                f"翻了她最近 {scan['scanned_posts']} 条带图帖"
                f"（{scan['scanned_images']} 张图）"
            )
            if scan.get("truncated"):
                note += "，更早的还没翻完"
            if search_error:
                note += f"；X 搜索这条兜底这次没成（{search_error}）"
            vision_info["watermark_note"] = note + "，没有这张"
        else:
            vision_info["watermark_note"] = "最近十几条带图帖和搜索结果里都没有这张"
        self._vlog("水印核对没命中：@%s", handle)

    # 关注列表最多看多少个号：接口一页就是 50 个，再多也拿不到。
    _FOLLOWING_SCAN_LIMIT = 50
    # 最多翻几个「同一个人」的带图帖。每翻一个都要下载十几张图，别贪多：
    # 实测一个号约 8～12 秒，三个就是半分钟，再多这一单就要等太久。
    # 176c 那单筛出 4 个同外链的号，答案排在第二个，三个能盖住。
    _FOLLOWING_TARGET_LIMIT = 3

    @staticmethod
    def _name_core(value: Any) -> str:
        """昵称压成小写字母数字，用来判断两个号是不是同一个词根。"""
        return re.sub(r"[^0-9a-z]+", "", str(value or "").lower())

    async def _following_accounts(self, handle: str) -> list[dict[str, Any]]:
        """拉一个号关注的人，只取一页。"""
        payload = await self._fetch_json(
            f"https://api.fxtwitter.com/2/profile/{handle}/following", timeout=15
        )
        rows = (payload or {}).get("results") if isinstance(payload, dict) else None
        return [row for row in (rows or []) if isinstance(row, dict)][: self._FOLLOWING_SCAN_LIMIT]

    async def _hunt_following_account(
        self, handle: str, upload: bytes | None
    ) -> dict[str, Any] | None:
        """水印核到的号本人没发这张图时，去她关注的人里找同一个人。

        实测 s-20261112-1030-176c：水印读成 ``neruchi890``，核到真号
        ``@NERUchi890``（28.7 万粉），最近 14 条带图帖里没有这张；她只关注 5 个
        人，其中一个 ``@oharapekopeko`` 发的正是这张（距离 0），两个号挂的是同
        一条 ``patreon.com/NENE318``。小号藏在自己的关注里是常见做法，这条路比
        「按名字搜 X」准——那边搜出来的是同名的陌生人。
        """
        if not upload or not handle:
            return None
        rows = await self._following_accounts(handle)
        if not rows:
            return None
        mine = await self._profile(handle) or {}
        my_links = set(same_person.external_links(mine))
        my_name = self._name_core(mine.get("name"))
        picked: list[str] = []
        for row in rows:
            screen = str(row.get("screen_name") or "").strip()
            if not screen or screen.lower() == handle.lower():
                continue
            links = set(same_person.external_links(row))
            if my_links and links & my_links:
                picked.append(screen)
                continue
            # 外链对不上时才看昵称：同一个词根的两个号（NENE / ne）也算。
            other = self._name_core(row.get("name"))
            if len(my_name) >= 2 and len(other) >= 2 and (my_name in other or other in my_name):
                picked.append(screen)
        if not picked:
            return None
        targets = picked[: self._FOLLOWING_TARGET_LIMIT]
        self._vlog(
            "水印号 @%s 关注里的疑似同一个人：%s",
            handle,
            "、".join("@" + item for item in targets),
        )
        for candidate in targets:
            hit = await self._media_lookup(candidate, upload)
            if not (hit and hit.get("url")):
                continue
            distance = int(hit.get("distance") or 0)
            self._vlog(
                "水印号 @%s 关注里的 @%s 发了这张图（距离 %s），升成结论",
                handle,
                candidate,
                distance,
            )
            return {
                "handle": candidate,
                "post": str(hit["url"]),
                "distance": distance,
                "name": "",
            }
        return None

    async def _follow_up_following(
        self,
        vision_info: dict[str, Any],
        results: list[dict[str, Any]],
        upload: bytes | None,
        ordered: list[Candidate] | None = None,
    ) -> None:
        """水印的号本人都没发这张图时，去她关注的人里找同一个人。

        只在「号存在、本人最近十几条里没有这张」时跑一次，每单最多一次；命中就
        把结论换成那个人那条帖子，没命中什么也不写——本来就已经是「最近没有这
        张」了，别再堆一句。

        这一步会有好几个候选号，先试「名字在候选里出现过」的那个：176c 那单视觉
        读到两个真号（PINKYKISS 和 neruchi890），后者才是图和搬运站标题里那个人，
        前者跟这张图没关系，先翻她把时间花光就白跑了。
        """
        if not upload or not vision_info or not results:
            return
        if not bool(getattr(self.config.vision, "watermark_following", True)):
            return
        pool = [
            item
            for item in results
            if str(item.get("state") or "") == fxtwitter.PROFILE_OK
            and str(item.get("handle") or "").strip()
            and not str(item.get("post") or "").strip()
        ]
        if not pool:
            return
        if ordered:
            pool.sort(
                key=lambda item: 0
                if self._account_mentioned(str(item.get("handle") or ""), ordered)
                else 1
            )
        handle = str(pool[0]["handle"])
        alt = await self._hunt_following_account(handle, upload)
        if not alt:
            return
        vision_info["watermark_post"] = alt["post"]
        vision_info["watermark_handle"] = alt["handle"]
        vision_info["watermark_url"] = f"https://x.com/{alt['handle']}"
        vision_info["watermark_distance"] = int(alt["distance"])
        vision_info["watermark_alt_from"] = handle
        if alt.get("name"):
            vision_info["watermark_name"] = alt["name"]
        vision_info["watermark_note"] = (
            f"水印写着 @{handle}，这张图是同一人的另一个号 @{alt['handle']} 发的，"
            f"指纹距离 {alt['distance']}"
        )

    # 近似写法最多试这么多个：每个都是一次 profile 请求，试太多会把兜底拖成
    # 几十秒，而真能对上的写法通常就在前几个（w 读散、少一个字母这类）。
    _VARIANT_LIMIT = 18

    def _account_mentioned(self, handle: str, ordered: list[Candidate]) -> bool:
        """这个号的名字有没有在候选的链接或标题里出现过。

        搬运站的路径和标题常写着当事人名（``fapello.com/.../nenechi890-neruchi890/``），
        出现在那里的号才是跟这张图有关的人；水印偶尔读出来的无关号不会出现在那里。
        """
        key = str(handle or "").strip().lstrip("@").lower()
        if len(key) < 4:
            return False
        for item in ordered:
            text = f"{item.url or ''} {item.title or ''}".lower()
            if key in text:
                return True
        return False

    async def _verify_watermarks(
        self,
        vision_info: dict[str, Any],
        grok_info: dict[str, Any] | None,
        upload: bytes | None,
        *,
        allow_variants: bool,
        ordered: list[Candidate] | None = None,
    ) -> None:
        """把两个模型读到的水印都核一遍，谁翻到这张图谁就是结论。

        只核「号存不存在」是不够的：``@Miharu`` 确实存在，可是个 2010 年的无关
        老号；``@Moliuwy`` 查无此人，真正的 ``@Moliww`` 就是这么漏掉的。所以这里
        对每个读法都走一遍「核账号 → 翻它自己的带图帖比指纹」，查无此人时还可以
        按近似拼写再找（仍要指纹对得上）。
        """
        readings: list[dict[str, Any]] = []
        seen: set[str] = set()

        def collect(raw_text: str, source: str) -> None:
            for index, handle in enumerate(vision.handles_from_watermark(raw_text)):
                if handle.lower() in seen:
                    continue
                seen.add(handle.lower())
                label = source if index == 0 else f"{source}(候选{index + 1})"
                readings.append({"source": label, "raw": raw_text, "handle": handle})

        collect(str(vision_info.get("watermark") or "").strip(), "vision")
        collect(str((grok_info or {}).get("watermark") or "").strip(), "grok")
        if not readings:
            return

        checked = await asyncio.gather(
            *(self._verify_watermark_reading(item, upload, allow_variants) for item in readings),
            return_exceptions=True,
        )
        results: list[dict[str, Any]] = []
        for item, result in zip(readings, checked):
            if isinstance(result, BaseException):
                self.ctx.logger.warning("水印核对异常：@%s %s", item["handle"], result)
                continue
            results.append(result)
        if not results:
            return
        vision_info["watermark_readings"] = results
        self._apply_watermark_primary(vision_info, results)
        if not str(vision_info.get("watermark_post") or "").strip():
            # 主读法没落地，才去关注列表里找同一个人的小号；命中由它自己把主字段改掉。
            await self._follow_up_following(vision_info, results, upload, ordered)

    def _apply_watermark_primary(
        self, vision_info: dict[str, Any], results: list[dict[str, Any]]
    ) -> None:
        """把「翻到这张图」的那条读法写成主结论字段，其余留作线索。"""
        if not results:
            return
        primary = next((item for item in results if item.get("post")), None)
        if primary is None:
            primary = next(
                (item for item in results if item.get("source") == "vision"), results[0]
            )
        vision_info["watermark"] = primary.get("raw") or ""
        vision_info["watermark_handle"] = primary.get("handle") or ""
        vision_info["watermark_url"] = primary.get("url") or ""
        vision_info["watermark_state"] = primary.get("state") or ""
        vision_info["watermark_user_id"] = primary.get("user_id") or ""
        if primary.get("name"):
            vision_info["watermark_name"] = primary["name"]
        if primary.get("note"):
            vision_info["watermark_note"] = primary["note"]
        if primary.get("post"):
            vision_info["watermark_post"] = primary["post"]
            vision_info["watermark_distance"] = primary.get("distance") or 0
        if primary.get("fixed"):
            # 记下「读成什么、最后核到哪个」，文案里要讲清楚这次是读偏了。
            vision_info["watermark_fixed_from"] = primary.get("from") or ""
        else:
            vision_info.pop("watermark_fixed_from", None)
        if not primary.get("post"):
            # 换了主读法但没命中时，别把上一条读法留下的帖子地址继续挂着。
            vision_info.pop("watermark_post", None)
            vision_info.pop("watermark_distance", None)
        for item in results:
            if item is primary:
                continue
            vision_info["watermark_other"] = {
                "raw": item.get("raw") or "",
                "handle": item.get("handle") or "",
                "state": item.get("state") or "",
                "source": item.get("source") or "",
            }
            break
        self._vlog(
            "水印核对：%s",
            "；".join(
                f"{item.get('source')}=@{(item.get('fixed') or item.get('handle'))}"
                f"({item.get('state')}{'，命中' if item.get('post') else ''})"
                for item in results
            ),
        )

    @staticmethod
    def _trace(trace: list[dict], step: str, state: str, note: str = "") -> None:
        """记一条「这一步跑没跑、为什么」。

        ``state``：``ran`` 跑了、``skip`` 没跑、``hit`` 跑出了结论、``none`` 跑了没结果。
        只用于「更多」里的过程说明，缺了不影响任何判断。
        """
        trace.append({"step": step, "state": state, "note": str(note or "")})

    @staticmethod
    def _name_hunt_trace_note(info: dict | None, names: list[str] | None = None) -> str:
        """把「按名字找号」这一步压成一句话，给「更多」里的过程说明用。"""
        if not info:
            return "这次没搜成（超时或接口失败）"
        status = str(info.get("status") or "")
        name = str(info.get("name") or "") or "、".join(list(names or [])[:2])
        handle = str(info.get("handle") or "").strip().lstrip("@")
        if status == "hit":
            where = "微博" if str(info.get("platform") or "") == "微博" else "X"
            who = f"微博 {info.get('account') or handle}" if where == "微博" else f"@{handle}"
            return f"按「{name}」找到 {where} {who}，她自己的帖里有这张图（距离 {info.get('distance')}）"
        profiles = [item for item in (info.get("profiles") or []) if isinstance(item, dict)]
        if profiles:
            found = "、".join(
                str(item.get("platform") or "") + "/" + str(item.get("handle") or "")
                for item in profiles[:3]
            )
            return f"按「{name}」搜到 {len(profiles)} 个主页（{found}），没核到这张图"
        return f"按「{name}」搜过，没找到对得上的主页"

    @staticmethod
    def _author_trace_note(info: dict | None, empty: str) -> str:
        """把一道「按名字找号」的结果压成一句话，给「更多」里的过程说明用。"""
        if not info:
            return empty
        status = str(info.get("status") or "")
        author = str(info.get("author") or "")
        handle = str(info.get("handle") or "").strip().lstrip("@")
        site = str(info.get("site") or "")
        if status == "hit":
            tail = f"（站点 {site}）" if site else ""
            return f"按「{author}」找到 @{handle}，她自己的帖子里有这张图{tail}"
        if status == "named":
            return f"「{author}」对上 @{handle}，但她最近的带图帖里没这张"
        if status == "author":
            return f"按「{author}」认到人，但翻不到这张图"
        if status == "seen":
            if info.get("explicit"):
                return f"页面标题里写着 @{handle}，翻她最近的带图帖没找到这张"
            return f"@{handle} 名字对得上，最近的带图帖里没有这张"
        if status == "none":
            variants = info.get("variants") or []
            return f"按「{author or '-'}」试了 {len(variants)} 个变体，没有显示名对得上的号"
        return f"跑完了：{status or '没有结果'}"

    def _watermark_plan(
        self,
        *,
        vision_enabled: bool,
        on_repost: bool,
        has_best: bool,
        has_handle: bool,
        is_drawing: bool,
        has_upload: bool,
    ) -> dict[str, Any]:
        """这一轮水印怎么读：单独读、跟着拼图读、还是不读。

        以前三个入口各判各的（兜底那段的 ``needs_watermark``、视觉复核内部的拼图、
        ``_should_reread_watermark`` 的重读），结果页上「没读」「读了没读到」长得
        一模一样，用户没法判断是没触发还是白跑。这里把第一次读水的决策收拢成一个
        纯函数，返回 ``{"mode": alone/puzzle/none, "read": bool, "reason": 一句话}``，
        日志和「更多」里的过程说明都直接用它。**行为跟以前完全一致**，只是理由能说清了。
        """
        if is_drawing:
            return {"mode": "none", "read": False, "reason": "插画路线不做这项兜底"}
        if not has_upload:
            return {"mode": "none", "read": False, "reason": "没有拿到原图，读不了水印"}
        if not vision_enabled:
            return {"mode": "none", "read": False, "reason": "视觉开关关着，水印这条线不跑"}
        if has_best and has_handle:
            return {
                "mode": "none",
                "read": False,
                "reason": "落点本来就有账号，不用再读水印",
            }
        if has_best:
            if not on_repost:
                return {
                    "mode": "none",
                    "read": False,
                    "reason": "落点没有账号，但 watermark_on_repost 关着",
                }
            return {
                "mode": "alone",
                "read": True,
                "reason": "落点没有账号（搬运页／图集页），原图单独读一次",
            }
        return {
            "mode": "puzzle",
            "read": True,
            "reason": "整轮没有指纹命中，拼图认人时顺带读一次",
        }

    @staticmethod
    def _should_reread_watermark(
        *,
        vision_enabled: bool,
        reread_enabled: bool,
        had_best: bool,
        repost_unresolved: bool = False,
        watermark_post: str,
        grok_post: str,
    ) -> bool:
        """要不要跑「原图再读一次水印」这道兜底。

        两种情况要跑：整轮没有结论；或者结论落在搬运站/社区站上又没给出账号
        （``repost_unresolved``）——那种页面只是别人转的，图上水印往往是唯一还能
        指到本人的线索。s-20261112-0752-4fdf 就是第二种：命中的是 reddit 转载帖，
        整图读水印回了个「无」，重读这道因为 ``had_best`` 为真被直接跳过，
        而图上其实写着 @kuru_xox。

        前提仍是两条水印线都没落地（``watermark_post`` 和 ``grok_post`` 都为空）。

        特意**不看拼图有没有读到水印文本**：读到但没核到图，同样算没落地；
        一个字都没读到，更该再试一次。s-20261111-1403-e15a 就是这么翻车的——
        图右下角写着 ``KWATERIN``（对应 @kwaterin），拼图一个字没读出来，
        而旧代码把重读挂在了「拼图读到东西」的分支里，于是连着被跳过，
        最后报了个「未找到」，图上写着答案插件却说没有。
        """
        if not (vision_enabled and reread_enabled):
            return False
        if not (not had_best or repost_unresolved):
            return False
        return not (watermark_post or "").strip() and not (grok_post or "").strip()

    def _reread_watermark_note(
        self,
        *,
        vision_enabled: bool,
        reread_enabled: bool,
        had_best: bool,
        repost_unresolved: bool,
        watermark_post: str,
        grok_post: str,
    ) -> str:
        """重读水印没跑时给一句人话；跑的时候返回空串。"""
        if not vision_enabled or not reread_enabled:
            return "重读开关关着（vision.enabled / watermark_reread）"
        if not (not had_best or repost_unresolved):
            return "已经有结论，且落点带着账号，不用重读"
        if (watermark_post or "").strip():
            return f"水印已经核到帖子 {watermark_post}，不用重读"
        if (grok_post or "").strip():
            return f"Grok 已经给出帖子 {grok_post}，不用重读"
        return ""

    async def _reread_watermark(
        self,
        vision_info: dict[str, Any],
        grok_info: dict[str, Any] | None,
        upload: bytes | None,
        ordered: list[Candidate] | None = None,
        *,
        prefer_candidates: bool = False,
    ) -> None:
        """原图读不出水印时，换看法、再换图各找一遍。

        三道，读到账号就停：
        1. 整图读——命中搬运页时先读候选图，那才是水印通常所在的那张；
        2. 切块读——一行小字在整幅画面里占比太小，切成六块它就大了一个数量级；
        3. 换图读——水印常常只留在转载链的那张图上，用户发来的原图是干净的。

        s-20261112-0752-4fdf 正是第 3 道：群里发的原图没水印，reddit 转载的那张
        手机边写着 @kuru_xox，同一套图的不同张，光读原图永远读不到。所以命中
        搬运页时先读候选图，兜底顺序反过来。

        同一道里的几张图一起读（候选图先并发下好）：一路读到就收工，其余的不再
        进下一道。s-20261123-0034-19ab 单是三个距离 0 的候选，串行读完花了七分钟，
        并行之后同样的活在一轮视觉调用的时间里就回。
        """
        if not upload or not bool(getattr(self.config.vision, "enabled", True)):
            return
        section = self.config.vision
        readings = list(vision_info.get("watermark_readings") or [])
        known = {
            str(item.get("handle") or "").lower() for item in readings if isinstance(item, dict)
        }
        tiles_on = bool(getattr(section, "watermark_tiles", True))
        trace_on = bool(getattr(section, "deep_trace", True))
        candidates = self._watermark_candidates(ordered) if trace_on else []
        # 几路读同时开跑，所以额度得锁着记：谁先拿到谁读，总数照样不超预算。
        budget_lock = asyncio.Lock()
        calls = 0

        async def take_call() -> bool:
            nonlocal calls
            async with budget_lock:
                if calls >= self._WATERMARK_REREAD_BUDGET:
                    return False
                calls += 1
                return True

        def new_slot() -> dict[str, Any]:
            """一路读自己的账本：读到什么先记在自己身上，回来再按优先级并账。"""
            return {"info": {}, "readings": [], "known": set(known), "hit": False}

        async def read_into(
            slot: dict[str, Any], source: bytes, label: str, data_url: str = ""
        ) -> bool:
            if not await take_call():
                return False
            return await self._reread_one(
                slot["info"],
                slot["readings"],
                slot["known"],
                source,
                data_url=data_url,
                label=label,
                verify=upload,
            )

        def absorb(slot: dict[str, Any]) -> None:
            """把一路读的账并回主线：读法按优先级追加，主线字段由合并后的读法决定。"""
            for item in slot.get("readings") or []:
                if item not in readings:
                    readings.append(item)
            known.update(slot.get("known") or set())
            info = slot.get("info") or {}
            for key in ("watermark", "watermark_text"):
                if info.get(key) and not str(vision_info.get(key) or "").strip():
                    vision_info[key] = info[key]
            vision_info["watermark_readings"] = readings
            if readings:
                self._apply_watermark_primary(vision_info, readings)

        async def read_whole(source: bytes, label: str) -> dict[str, Any]:
            slot = new_slot()
            slot["hit"] = await read_into(
                slot, source, label if label != "原图" else ""
            )
            return slot

        async def read_located(source: bytes, label: str) -> dict[str, Any]:
            slot = new_slot()
            # 定位这一次调用也要记账，剩下的「读放大图」由 read_into 自己记。
            if not await take_call():
                return slot
            slot["hit"] = await self._locate_and_read(
                source,
                label,
                lambda src, name, data_url="": read_into(slot, src, name, data_url),
            )
            return slot

        async def read_tile(
            source: bytes, label: str, data_url: str
        ) -> dict[str, Any]:
            slot = new_slot()
            slot["hit"] = await read_into(slot, source, label, data_url=data_url)
            return slot

        async def read_tiles(source: bytes, prefix: str) -> list[dict[str, Any]]:
            try:
                tiles = await asyncio.to_thread(vision.tile_data_urls, source)
            except Exception as error:  # noqa: BLE001 - 切块失败只是少一道兜底
                self.ctx.logger.warning("%s 切块失败：%s", prefix, error)
                return []
            return list(
                await asyncio.gather(
                    *[
                        read_tile(source, f"{prefix}-{label}", data_url)
                        for label, data_url in tiles[: self._WATERMARK_TILE_LIMIT]
                    ],
                    return_exceptions=True,
                )
            )

        async def run_batch(jobs: list[Any]) -> list[dict[str, Any]]:
            """等一批读回来，异常当成没读到；结果顺序与 jobs 一致。"""
            done = await asyncio.gather(*jobs, return_exceptions=True)
            slots: list[dict[str, Any]] = []
            for item in done:
                if isinstance(item, BaseException):
                    self.ctx.logger.warning("并行读水印有一路失败：%s", str(item)[:60])
                    continue
                if isinstance(item, list):
                    slots.extend(item)
                else:
                    slots.append(item)
            return slots

        loaded: list[tuple[str, bytes]] = []
        if candidates:
            # 三张候选图一起下：串行下完再开始读，光这一步就白等十几秒。
            images = await asyncio.gather(
                *[self._download(item.image_url) for item in candidates],
                return_exceptions=True,
            )
            for index, raw in enumerate(images):
                if isinstance(raw, BaseException):
                    self.ctx.logger.warning("候选图下载失败：%s", str(raw)[:60])
                    continue
                if raw:
                    loaded.append((f"候选{index + 1}", raw))
        if loaded:
            self._vlog(
                "追查模式：候选图备好 %s 张（%s），距离 %s",
                len(loaded),
                "、".join(label for label, _ in loaded),
                "、".join(str(item.distance) for item in candidates[: len(loaded)]),
            )

        # 整图一起读，按优先级并账：命中那一路的主线字段落地，其余只留读法记录。
        # 切块仍然排在整图之后：切块一块一调用，慢而且最容易读出假号
        # （s-20261112-0858-c44f 六块读出 MURUA、MIRUI、idcNothinglose 三个号，
        # 一个都没核上）。整图只要有一路读到，就轮不到切块。
        whole = list(loaded)
        if not prefer_candidates:
            whole = [("原图", upload)] + whole
        else:
            whole = whole + [("原图", upload)]
        slots = await run_batch([read_whole(raw, label) for label, raw in whole])
        for slot in slots:
            absorb(slot)
        if any(slot.get("hit") for slot in slots):
            self._vlog("整图读命中，定位与切块不再跑")
            return
        # 整图都读空了才轮到「两步读」：先只问水印在哪（不抄字），再按坐标从原图
        # 裁一块放大读。比切六块便宜一半以上，而且裁的是原图，小字不会被缩放吃掉。
        # 用户 11-13 点名要的就是这个顺序：定位 → 放大 → 兜底。
        if bool(getattr(section, "watermark_locate", True)):
            targets = whole[: self._WATERMARK_LOCATE_LIMIT]
            slots = await run_batch([read_located(raw, label) for label, raw in targets])
            for slot in slots:
                absorb(slot)
            if any(slot.get("hit") for slot in slots):
                return
        if not tiles_on:
            return
        for label, raw in whole:
            slots = await run_batch([read_tiles(raw, label)])
            for slot in slots:
                absorb(slot)
            if any(slot.get("hit") for slot in slots):
                self._vlog("分块读在 %s 命中，剩下的图不再切", label)
                return

    # 分块最多读这么多块。2×3 正好六块，再多就说明前面几块都没戏，继续读只是烧额度。
    _WATERMARK_TILE_LIMIT = 6

    # 追查模式最多读几张候选图。候选图算指纹时都下载过，重取一张不贵，但视觉调用
    # 一次两三秒，三张够了——真带水印的那张通常就是距离最近的那条。
    _CANDIDATE_WATERMARK_LIMIT = 3

    # 两步读最多对几张图跑（原图一张、最像的候选图一张）。每张最多两次视觉调用：
    # 一次定位、一次读放大后的那一块。
    _WATERMARK_LOCATE_LIMIT = 2

    # 这一轮兜底最多花多少次视觉调用。三张候选图 + 整图 + 六块，正好一轮；超了
    # 就按顺序砍掉后面的，兜底不能把整单拖成几分钟。
    _WATERMARK_REREAD_BUDGET = 12

    @classmethod
    def _watermark_candidates(cls, ordered: list[Candidate] | None) -> list[Candidate]:
        """挑出值得读水印的候选图：按指纹距离排，越像越先读。"""
        rows = [
            item
            for item in (ordered or [])
            if (item.image_url or "").strip()
            and item.status in {"match", "suspect", "vision"}
        ]
        rows.sort(
            key=lambda item: (item.distance if item.distance is not None else 999, item.rank)
        )
        return rows[: cls._CANDIDATE_WATERMARK_LIMIT]

    async def _locate_watermark(self, data_url: str) -> dict[str, Any] | None:
        """只问「水印在哪」，不抄字。一次视觉调用换一个坐标框。"""
        section = self.config.vision
        timeout = max(10, int(section.timeout))
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": vision.build_locate_prompt()},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ]
        options: dict[str, Any] = {
            "prompt": messages,
            "task_name": section.task or "vlm",
            "temperature": 0.0,
            # 只要一个框，给多了模型会开始描述画面。
            "max_tokens": 120,
        }
        if section.model:
            options["model_name"] = section.model
        try:
            result = await asyncio.wait_for(
                self.ctx.call_capability("llm.generate", timeout_ms=timeout * 1000, **options),
                timeout=timeout + 5,
            )
        except Exception as error:  # noqa: BLE001 - 定位失败就退回切块
            self._vlog("水印定位调用失败：%s", str(error)[:60])
            return None
        payload = result if isinstance(result, dict) else {"success": True, "response": str(result or "")}
        if not payload.get("success", True):
            self._vlog("水印定位返回失败：%s", str(payload.get("error"))[:60])
            return None
        text = str(payload.get("response") or payload.get("reasoning") or "")
        return vision.parse_locate_reply(text)

    async def _locate_and_read(self, source: bytes, label: str, read: Any) -> bool:
        """两步读：定位 → 从原图裁下来放大 → 读字。返回 True 表示这一步就读到了。"""
        where = f"[{label}]" if label and label != "原图" else ""
        try:
            data_url, _width, _height = await asyncio.to_thread(vision.data_url_for, source)
        except Exception as error:  # noqa: BLE001 - 编码失败只影响这一步
            self._vlog("水印定位跳过%s：原图编码失败 %s", where, str(error)[:40])
            return False
        located = await self._locate_watermark(data_url)
        if not located or not located.get("box"):
            self._vlog("水印定位%s：图上没找到小字", where)
            return False
        try:
            crop_url, box = await asyncio.to_thread(vision.crop_data_url, source, located["box"])
        except Exception as error:  # noqa: BLE001 - 裁不出来就退回切块
            self._vlog("水印定位%s 裁剪失败：%s", where, str(error)[:40])
            return False
        spot = str(located.get("where") or "").strip() or "定位点"
        self.ctx.logger.info(
            "水印定位%s：%s，裁剪框=%s，放大后送读", where, spot, box
        )
        return await read(source, f"{label or '原图'}-{spot}", data_url=crop_url)

    async def _reread_one(
        self,
        vision_info: dict[str, Any],
        readings: list[dict[str, Any]],
        known: set[str],
        source: bytes | None,
        *,
        data_url: str,
        label: str,
        verify: bytes | None = None,
    ) -> bool:
        """读一次（整图或某一块），读到没见过的号就核它自己的带图帖。

        ``source`` 是要读的那张图，``verify`` 是拿来比指纹的原图——候选图上的
        水印要拿用户发的那张去核，才算「她发过跟你这张一样的图」。
        返回 True 表示这个号自己的帖子里真有这张图，整条兜底就此收工。
        """
        where = f"[{label}]" if label else ""
        try:
            info = await self._read_watermark(source, data_url=data_url, label=label)
        except Exception as error:  # noqa: BLE001 - 重读只是加分项
            self.ctx.logger.warning("重读水印失败%s：%s", where, error)
            return False
        if not info.get("ok"):
            self._vlog("重读水印没成%s：%s", where, info.get("error") or "-")
            return False
        raw = str(info.get("watermark") or "").strip()
        found_handles = vision.handles_from_watermark(raw)
        if not found_handles:
            # 中文昵称不是 X 号，取号函数一个字符都抽不出来，但它是微博那类平台的
            # 唯一线索。s-20261113-1402-bd67 就是这样：图上明明写着 @兔子兔子兔w，
            # 这里回一句「没读出账号」把它丢了，最后报未找到，而正确答案就是她。
            # 所以留下来交给微博反查，核不核得上是那一步的事。
            names = [name for name in handles.at_names([raw], limit=2) if handles.has_cjk(name)]
            if not names:
                self._vlog("重读水印%s：文字=%s，没读出账号", where, raw or "-")
                return False
            if not str(vision_info.get("watermark") or "").strip():
                vision_info["watermark"] = raw
            if not str(vision_info.get("watermark_text") or "").strip():
                vision_info["watermark_text"] = raw
            self._vlog(
                "重读水印%s：文字=%s 是中文昵称（X 上没有），留给微博核",
                where,
                "、".join(names),
            )
            return False
        fresh = [handle for handle in found_handles if handle.lower() not in known]
        if not fresh:
            self._vlog("重读水印%s：还是 @%s，不重复核", where, "、@".join(found_handles))
            return False
        landed = False
        for index, handle in enumerate(fresh):
            reading = await self._verify_watermark_reading(
                {
                    "source": f"vision-{label}" if label else "vision-original",
                    "raw": raw,
                    "handle": handle,
                },
                verify if verify is not None else source,
                True,
            )
            readings.append(reading)
            for key in ("handle", "fixed"):
                # 核出来的真实写法也算见过，免得下一块读到同一个号又核一遍。
                value = str(reading.get(key) or "").strip().lower()
                if value:
                    known.add(value)
            vision_info["watermark_readings"] = readings
            self._apply_watermark_primary(vision_info, readings)
            if str(reading.get("post") or "").strip():
                landed = True
                break
            if index + 1 < len(fresh):
                self._vlog("重读水印%s：@%s 没对上，接着试同一条水印里的下一个写法", where, handle)
        return landed

    async def _verify_watermark_reading(
        self,
        reading: dict[str, Any],
        upload: bytes | None,
        allow_variants: bool,
    ) -> dict[str, Any]:
        """核一个水印读法：先看号在不在，在就翻它的带图帖比这张图。"""
        handle = str(reading.get("handle") or "")
        info: dict[str, Any] = {
            "source": reading.get("source") or "",
            "raw": reading.get("raw") or "",
            "handle": handle,
            "url": f"https://x.com/{handle}",
            "state": "unknown",
            "user_id": "",
            "name": "",
            "post": "",
            "distance": None,
            "note": "",
            "fixed": "",
            "from": "",
        }
        try:
            profile = await self._account_profile(handle)
        except Exception as error:  # noqa: BLE001 - 核不到只影响这一条线索
            self.ctx.logger.warning("水印账号核对失败：@%s %s", handle, error)
            info["note"] = "这个号这次没核成"
            return info

        info["state"] = profile.state
        info["user_id"] = profile.user_id or ""
        info["name"] = profile.name or ""
        if profile.handle:
            # 用 X 上真实的书写形式（Moliww 而不是读出来的 moliwy），显示时别让
            # 用户以为拼错了。
            info["handle"] = profile.handle
            info["url"] = f"https://x.com/{profile.handle}"
        if profile.state == fxtwitter.PROFILE_OK:
            holder: dict[str, Any] = {}
            await self._check_watermark_post(handle, upload, holder)
            info["note"] = str(holder.get("watermark_note") or "")
            info["post"] = str(holder.get("watermark_post") or "")
            if holder.get("watermark_distance") is not None:
                info["distance"] = int(holder["watermark_distance"])
            self._vlog(
                "水印号核对：@%s（%s）状态=%s id=%s 命中=%s",
                handle,
                info["source"],
                profile.state,
                profile.user_id or "-",
                info["post"] or "无",
            )
            return info

        if profile.state == fxtwitter.PROFILE_MISSING and allow_variants:
            found = await self._variant_watermark(handle, upload)
            if found:
                info["from"] = handle
                info["fixed"] = found["handle"]
                info["handle"] = found["handle"]
                info["url"] = f"https://x.com/{found['handle']}"
                info["state"] = fxtwitter.PROFILE_OK
                info["user_id"] = found.get("user_id") or ""
                info["name"] = found.get("name") or ""
                info["post"] = found.get("post") or ""
                info["distance"] = found.get("distance")
                info["note"] = str(found.get("note") or "")
                return info

        if profile.state == fxtwitter.PROFILE_SUSPENDED:
            suffix = f"（用户 ID {profile.user_id}）" if profile.user_id else ""
            if profile.confirmed:
                info["note"] = f"该账号已被 X 封禁{suffix}，主页已经打不开了"
            else:
                info["note"] = f"X 接口读不到这个号{suffix}，主页能不能打开要自己点一下"
        elif profile.state == fxtwitter.PROFILE_MISSING:
            info["note"] = "查无此号"
        else:
            info["note"] = "这个号这次没核成"
        self._vlog(
            "水印号核对：@%s（%s）状态=%s id=%s",
            handle,
            info["source"],
            profile.state,
            profile.user_id or "-",
        )
        return info

    async def _variant_watermark(
        self, handle: str, upload: bytes | None
    ) -> dict[str, Any] | None:
        """号名被读散时按近似写法再找一遍，只认「它自己的帖子里有这张图」。

        猜写法本身不安全，所以门槛只有一道、而且很硬：候选号的带图帖里必须
        真有这张图。这样就算猜错，也不会把别人的主页摆成作者。
        """
        if not upload or not handle:
            return None
        if not bool(getattr(self.config.vision, "watermark_variants", True)):
            return None
        variants = handles.typo_variants(handle, limit=self._VARIANT_LIMIT)
        if not variants:
            return None

        semaphore = asyncio.Semaphore(6)

        async def probe(name: str) -> tuple[str, fxtwitter.Profile] | None:
            async with semaphore:
                try:
                    return name, await asyncio.wait_for(self._account_profile(name), timeout=12)
                except Exception:  # noqa: BLE001 - 单个写法超时不影响其他
                    return None

        results = await asyncio.gather(*(probe(name) for name in variants), return_exceptions=True)
        alive = [
            item
            for item in results
            if isinstance(item, tuple) and item[1].state == fxtwitter.PROFILE_OK
        ]
        if not alive:
            self._vlog("水印近似写法：试了 %s 个，没有对上的号", len(variants))
            return None
        for name, profile in alive[:3]:
            holder: dict[str, Any] = {}
            await self._check_watermark_post(name, upload, holder)
            if holder.get("watermark_post"):
                self._vlog(
                    "水印近似写法命中：%s → @%s %s", handle, name, holder["watermark_post"]
                )
                return {
                    "handle": profile.handle or name,
                    "user_id": profile.user_id or "",
                    "name": profile.name or "",
                    "post": holder["watermark_post"],
                    "distance": holder.get("watermark_distance"),
                    "note": holder.get("watermark_note") or "",
                }
        self._vlog(
            "水印近似写法：试了 %s 个，对上 %s 个，都没翻到这张图", len(variants), len(alive)
        )
        return None

    async def _account_profile(self, handle: str) -> fxtwitter.Profile:
        """查账号在 X 上的状态与数字 ID：ok / missing / suspended / unknown。

        原来用 ``cdn.syndication.twimg.com/widgets/followbutton/info.json``，2026-11
        实测对任何号都返回空（连肯定存在的 @wn40536699 也是空），确认能力等于没有，
        换成 fxtwitter 的 ``/2/profile/<号>``：它能区分「查无此号」和「被 X 封禁」，
        被封时还会带回数字 ID，那是唯一改不掉的身份标识。
        """
        url = fxtwitter.build_profile_url(handle)
        if not url:
            return fxtwitter.Profile()

        def fetch() -> fxtwitter.Profile:
            session = make_session(self._proxy())
            try:
                response = session.get(url, timeout=15, headers={"Accept": "application/json"})
                if response.status_code not in {200, 404}:
                    return fxtwitter.Profile()
                try:
                    return fxtwitter.parse_profile_info(response.json())
                except Exception:  # noqa: BLE001 - 不是 JSON 就当作没查成
                    return fxtwitter.Profile()
            finally:
                session.close()

        try:
            profile = await asyncio.wait_for(asyncio.to_thread(fetch), timeout=20)
        except Exception:  # noqa: BLE001 - 查不了就当没查成，不影响已经拿到的结论
            return fxtwitter.Profile()
        if profile.state == fxtwitter.PROFILE_SUSPENDED:
            # fxtwitter 一句 suspended 不够硬：受限账号也可能这样回。再抓一次
            # 网页主页，服务端真的吐出「Account suspended」才敢说是封号。
            profile.confirmed = bool(await self._homepage_suspended(handle))
            self._vlog("账号封禁复核：@%s 主页确认=%s", handle, profile.confirmed)
        return profile

    async def _homepage_suspended(self, handle: str) -> bool | None:
        """抓 x.com/<号> 的 HTML，看服务端有没有下发「Account suspended」。

        返回 None 表示没查成（网络问题、被墙、页面结构变了），调用方不能把它
        当成「没封」。实测封号号返回 44KB 且带 ``Account suspended`` 与
        ``UserUnavailable``/``unavailable_reason: Suspended``；正常号 162KB，
        带的是简介 meta。
        """
        user = (handle or "").strip().lstrip("@")
        if not user:
            return None

        def fetch() -> bool | None:
            session = make_session(self._proxy())
            try:
                response = session.get(
                    f"https://x.com/{user}",
                    timeout=15,
                    headers={
                        "User-Agent": (
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/131.0 Safari/537.36"
                        )
                    },
                )
                if response.status_code != 200:
                    return None
                return "account suspended" in response.text.lower()
            finally:
                session.close()

        try:
            return await asyncio.wait_for(asyncio.to_thread(fetch), timeout=20)
        except Exception:  # noqa: BLE001 - 查不了就老实说没查成
            return None

    async def _check_best_account(
        self, best: Candidate | None, vision_info: dict[str, Any] | None
    ) -> Candidate | None:
        """结论里挂着的账号如果已被 X 封禁，把封前的数字 ID 标出来。

        封号之后 handle 会被释放、甚至被别人抢注，唯一还能对上人的就是数字 ID
        （``https://x.com/i/user/<id>``）。每单最多多一次 profile 请求，只在结论
        带 handle 时发；水印那条刚刚核过同一个号就不重复查了。纯数字 handle
        多半是微博 UID / B 站 UID，不去 X 上白查一趟。
        """
        if best is None or not best.handle:
            return best
        handle = best.handle.strip().lstrip("@")
        if not handle or handle.isdigit():
            return best
        seen = str((vision_info or {}).get("watermark_handle") or "")
        if seen and seen.lower() == handle.lower():
            return best
        profile = await self._account_profile(handle)
        if profile.state == fxtwitter.PROFILE_SUSPENDED:
            suffix = f"（用户 ID {profile.user_id}）" if profile.user_id else ""
            if profile.confirmed:
                best.account_note = f"⚠️ 该账号已被 X 封禁{suffix}，主页已经打不开了。"
            else:
                # 没确认到主页文案就只能说「读不到」：受限账号也会回 suspended。
                best.account_note = f"⚠️ X 接口读不到这个号{suffix}，可能受限或已封，主页要自己点开确认。"
            self._vlog(
                "结论账号状态：@%s id=%s 封禁确认=%s",
                handle,
                profile.user_id or "-",
                profile.confirmed,
            )
        return best

    # 翻账号自己的带图帖子时最多看这么多条。接口默认一页只给十几条，所以要带
    # ``count`` 一次多要一点（2026-11 实测同一个号：默认 14 条／15 张图，
    # ``count=50`` 给到 39 条／42 张图）。条数够了还得按时间预算扫：一单的总时长
    # 比「一条不漏」重要，扫到预算就把「更早的还没翻」如实记下来。
    _MEDIA_SCAN_POSTS = 60
    _MEDIA_SCAN_IMAGES = 4
    _MEDIA_PAGE_COUNT = 100
    _MEDIA_SCAN_BUDGET = 18.0
    # 一张图下载走代理约 1 秒（2026-11 实测 30 张 32 秒），一张一张比的话 18 秒
    # 只盖得住十来张，一条 34P 的套图根本翻不完；并发几条就能把 60 条帖子扫完。
    _MEDIA_SCAN_BATCH = 6
    _MEDIA_SCAN_CONCURRENCY = 6
    # 缩略图比原图的指纹差 0～1，判定阈值不变；落在「阈值 + 这个数」以内的
    # 再拿原图复核一次，宁可多下几张也不要把真命中挡在门外。
    _MEDIA_NEAR_SLACK = 3

    async def _media_lookup(self, handle: str, upload: bytes | None) -> dict[str, Any] | None:
        """翻账号自己的带图帖子（不含转发），用指纹确认这张图是不是它发的。

        比「让模型给链接」硬在两点：列表里只有本人发的东西，转发进不来；命中时
        直接拿到那条帖子的地址，不是「疑似这个人」。没有命中时会带上「翻了几条
        帖、几张图、有没有被预算截断」，上层才写得清「翻过但没有」，而不是把
        「没翻完」说成「没有」。
        """
        if not upload or not handle:
            return None
        url = fxtwitter.build_media_url(handle, count=self._MEDIA_PAGE_COUNT)
        if not url:
            return None
        timeout = min(20, max(8, int(self.config.plugin.download_timeout) + 6))

        def fetch() -> dict:
            session = make_session(self._proxy(), headers={"Accept": "application/json"})
            try:
                response = session.get(url, timeout=timeout)
                if response.status_code != 200:
                    raise HttpError(f"HTTP {response.status_code}")
                return response.json()
            finally:
                session.close()

        try:
            payload = await asyncio.wait_for(asyncio.to_thread(fetch), timeout=timeout + 4)
        except Exception as error:  # noqa: BLE001 - 翻不到就不翻，别影响已经拿到的结论
            self._vlog("翻 @%s 的带图帖子失败：%s", handle, str(error)[:80])
            # 分清「没翻到」和「没翻成」：上层据此决定写「最近没有」还是
            # 「这次没查成」，后者不能当成否定证据。
            return {"error": str(error)[:80]}
        posts = fxtwitter.parse_media_list(payload)
        if not posts:
            self._vlog("翻 @%s 的带图帖子：一条都没拿到", handle)
            return None
        mine = dhash(upload)
        if mine is None:
            return None
        threshold = self.config.plugin.hash_threshold
        best: dict[str, Any] | None = None
        deadline = time.monotonic() + self._MEDIA_SCAN_BUDGET
        scanned_posts = 0
        scanned_images = 0
        truncated = False
        semaphore = asyncio.Semaphore(self._MEDIA_SCAN_CONCURRENCY)

        async def fingerprint(link: str) -> int | None:
            async with semaphore:
                raw = await self._download(link)
            if not raw:
                return None
            other = dhash(raw)
            if other is None:
                return None
            return hamming(mine, other)

        pool = posts[: self._MEDIA_SCAN_POSTS]
        for start in range(0, len(pool), self._MEDIA_SCAN_BATCH):
            # 至少扫一批，免得预算极小时一张都没比就说「翻过了」。
            if scanned_posts and time.monotonic() >= deadline:
                truncated = True
                break
            chunk = pool[start : start + self._MEDIA_SCAN_BATCH]
            pairs = [
                (post, link)
                for post in chunk
                for link in post.media[: self._MEDIA_SCAN_IMAGES]
            ]
            # 先拿缩略图扫：同一条线路实测快一倍半，判定的 dHash 只差 0～1。
            distances = await asyncio.gather(
                *(fingerprint(fxtwitter.thumb_url(link)) for _, link in pairs)
            )
            scanned_posts += len(chunk)
            borderline: list[tuple[Any, str]] = []
            for (post, link), distance in zip(pairs, distances):
                if distance is None:
                    continue
                scanned_images += 1
                if distance > threshold:
                    # 缩略图重压过，落在阈值边上的拿原图再确认，别为省时间漏掉真命中。
                    if distance <= threshold + self._MEDIA_NEAR_SLACK:
                        borderline.append((post, link))
                    continue
                if best is None or distance < best["distance"]:
                    # image 留着当参考图：命中帖子的那张配图，转发里可以直接摆出来。
                    best = {
                        "url": post.url,
                        "distance": distance,
                        "created_at": post.created_at,
                        "image": link,
                    }
            if best is None and borderline:
                originals = await asyncio.gather(
                    *(fingerprint(link) for _, link in borderline)
                )
                for (post, link), distance in zip(borderline, originals):
                    if distance is None or distance > threshold:
                        continue
                    if best is None or distance < best["distance"]:
                        best = {
                            "url": post.url,
                            "distance": distance,
                            "created_at": post.created_at,
                            "image": link,
                        }
            if best is not None:
                break
        if best is None and scanned_posts < len(pool):
            truncated = True
        counts = {
            "total_posts": len(posts),
            "scanned_posts": scanned_posts,
            "scanned_images": scanned_images,
            "truncated": truncated,
        }
        self._vlog(
            "翻 @%s 的带图帖子：拿到 %s 条，扫了 %s 条／%s 张图%s，结论=%s",
            handle,
            len(posts),
            scanned_posts,
            scanned_images,
            "（预算用完，更早的没翻）" if truncated and best is None else "",
            f"命中 distance={best['distance']}" if best else "没有这张图",
        )
        if best is not None:
            best.update(counts)
            return best
        return counts

    # 搜索兜底：/media 只给最近十几条，老图翻不到时用搜索再捞一遍。
    _SEARCH_SCAN_PAGES = 2
    _SEARCH_SCAN_POSTS = 20

    async def _search_media_lookup(self, handle: str, upload: bytes | None) -> dict[str, Any] | None:
        """用 ``from:<号> filter:images`` 搜这个号发过的图，逐张比指纹。

        ``/2/profile/<号>/media`` 只回最近十几条，水印读到的号要是很久以前发的，
        那条路永远翻不到；X 的搜索索引有时还留着，命中同样给出帖子地址。
        """
        mine = dhash(upload) if upload else None
        if mine is None or not handle:
            return None
        query = f"from:{handle} filter:images"
        state: dict[str, Any] = {}
        hits: list[fxtwitter.SearchHit] = []
        cursor = ""
        for _ in range(self._SEARCH_SCAN_PAGES):
            page = await asyncio.to_thread(self._search_page, query, cursor, state)
            if not page:
                if not hits:
                    return {"error": "搜索没返回结果"}
                break
            items, cursor = page
            hits.extend(items)
            if state.get("throttled") or not cursor:
                break
        if not hits:
            return None
        threshold = self.config.plugin.hash_threshold
        best: dict[str, Any] | None = None
        for hit in hits[: self._SEARCH_SCAN_POSTS]:
            for link in hit.media[: self._MEDIA_SCAN_IMAGES]:
                raw = await self._download(link)
                if not raw:
                    continue
                other = dhash(raw)
                if other is None:
                    continue
                distance = hamming(mine, other)
                if distance <= threshold and (best is None or distance < best["distance"]):
                    best = {"url": hit.url, "distance": distance, "created_at": hit.created_at}
        self._vlog(
            "搜索翻 @%s：%s 条，%s",
            handle,
            len(hits),
            f"命中 distance={best['distance']}" if best else "没有这张图",
        )
        return best

    # 回复页反查的预算：翻 15 页回复、回查前 240 个父帖，总耗时卡在 60 秒内。
    # 节奏上刻意放慢：每批 8 个请求、批间停 0.5 秒，约 5 个/秒——实测一口气
    # 打 138 个没有触发限流，但没必要贴着上限跑，翻不完就放弃（宁可保持疑似）。
    _REPLY_SCAN_PAGES = 15
    _REPLY_SCAN_PARENTS = 240
    _REPLY_SCAN_BATCH = 8
    _REPLY_SCAN_GAP = 0.5
    _REPLY_SCAN_BUDGET = 60.0
    _REPLY_SCAN_ATTEMPTS = 2
    # 这些状态码说明对方已经在拦了，继续发只会更糟，立刻收手。
    _REPLY_THROTTLE_CODES = (403, 429)

    def _search_page(
        self, query: str, cursor: str, state: dict[str, Any]
    ) -> tuple[list[fxtwitter.SearchHit], str] | None:
        """取一页搜索结果；翻不动就返回 None（当「没更多」处理）。"""
        url = fxtwitter.build_search_url(query, cursor)
        if not url:
            return None
        session = make_session(self._proxy(), headers={"Accept": "application/json"})
        try:
            response = session.get(url, timeout=15)
            if response.status_code in self._REPLY_THROTTLE_CODES:
                state["throttled"] = True
                return None
            if response.status_code != 200:
                raise HttpError(f"HTTP {response.status_code}")
            payload = response.json()
            return fxtwitter.parse_search(payload)
        except Exception as error:  # noqa: BLE001 - 翻不到就不翻，别影响已拿到的结论
            self._vlog("翻 X 搜索失败（%s）：%s", query, str(error)[:80])
            return None
        finally:
            session.close()

    def _parent_hit(self, status_id: str, key: str, state: dict[str, Any]) -> dict[str, Any] | None:
        """回查一条父帖：里面有没有媒体键为 key 的图。有就返回作者与帖子地址。"""
        session = make_session(self._proxy(), headers={"Accept": "application/json"})
        try:
            response = session.get(f"{fxtwitter.API_BASE}/2/status/{status_id}", timeout=15)
            if response.status_code in self._REPLY_THROTTLE_CODES:
                state["throttled"] = True
                return None
            if response.status_code != 200:
                return None
            data = (response.json() or {}).get("status") or {}
        except Exception:  # noqa: BLE001 - 单条拉不到就当这条没有
            return None
        finally:
            session.close()
        media = data.get("media") if isinstance(data.get("media"), dict) else {}
        for entry in media.get("all") or []:
            if not isinstance(entry, dict):
                continue
            if fxtwitter.media_key(str(entry.get("url") or "")) != key:
                continue
            author = data.get("author") if isinstance(data.get("author"), dict) else {}
            handle = str(author.get("screen_name") or "").strip()
            return {
                "url": str(data.get("url") or "") or (
                    f"https://x.com/{handle}/status/{status_id}" if handle else ""
                ),
                "handle": handle,
                "name": str(author.get("name") or "").strip(),
                "created_at": str(data.get("created_at") or ""),
            }
        return None

    async def _reply_page_lookup(self, handle: str, key: str) -> tuple[dict[str, Any] | None, str]:
        """顺着 @handle 的回复记录，找出那张图真正出自哪条帖子。

        X 的回复页（``/<号>/with_replies``）免登录读不到，但搜索接口能按账号翻
        回复，每条都带 ``replying_to.status``。把父帖挨个取回来比媒体键，命中
        的那条就是原帖，作者也是真正发图的人。

        返回（命中的帖子, 结果码）。结果码是 ``hit`` / ``throttled``（被限流）/
        ``empty``（一条回复都没翻到）/ ``miss``（翻完了没这张图）/ ``timeout``，
        失败时调用方要把它写进给 Grok 的提示里。
        """
        query = fxtwitter.build_replies_query(handle)
        if not query or not key:
            return None, "empty"
        state: dict[str, Any] = {"throttled": False}
        deadline = time.monotonic() + self._REPLY_SCAN_BUDGET
        parents: list[str] = []
        cursor = ""
        for _ in range(self._REPLY_SCAN_PAGES):
            if time.monotonic() > deadline or state["throttled"]:
                break
            page = await asyncio.to_thread(self._search_page, query, cursor, state)
            if not page:
                break
            hits, cursor = page
            if not hits:
                break
            for hit in hits:
                if hit.parent_id and hit.parent_id not in parents:
                    parents.append(hit.parent_id)
            if not cursor or len(parents) >= self._REPLY_SCAN_PARENTS:
                break
        parents = parents[: self._REPLY_SCAN_PARENTS]
        if not parents:
            self._vlog("回复页反查：@%s 一条回复都没翻到%s", handle, "（被限流）" if state["throttled"] else "")
            return None, "throttled" if state["throttled"] else "empty"
        checked = 0
        for start in range(0, len(parents), self._REPLY_SCAN_BATCH):
            if state["throttled"]:
                break
            if time.monotonic() > deadline:
                self._vlog("回复页反查：@%s 到预算上限，已查 %s 个父帖", handle, checked)
                return None, "timeout"
            batch = parents[start : start + self._REPLY_SCAN_BATCH]
            results = await asyncio.gather(
                *(asyncio.to_thread(self._parent_hit, pid, key, state) for pid in batch)
            )
            checked += len(batch)
            for found in results:
                if found:
                    self._vlog(
                        "回复页反查：@%s 翻了 %s 条、查了 %s 个父帖，命中 %s（@%s）",
                        handle,
                        len(parents),
                        checked,
                        found["url"],
                        found["handle"] or "未知",
                    )
                    return found, "hit"
            # 每批之间停一下：一次性打完 200 多个请求才容易招来限流。
            await asyncio.sleep(self._REPLY_SCAN_GAP)
        if state["throttled"]:
            self._vlog("回复页反查：@%s 被限流，已查 %s 个父帖就收手", handle, checked)
            return None, "throttled"
        self._vlog("回复页反查：@%s 查了 %s 个父帖，没有这张图", handle, checked)
        return None, "miss"

    async def _retweet_lookup(self, handle: str, key: str) -> tuple[dict[str, Any] | None, str]:
        """转发页：搜这个号的转发，结果本身就是她转的那条原帖，直接比媒体键。

        比回复页省一层：不用再逐条回查父帖，搜索结果里的 ``author`` 就是原作者。
        代价是 X 对「转发搜索」的覆盖很浅（实测一个号只回两三条），翻不到就交给
        Grok。
        """
        query = fxtwitter.build_reposts_query(handle)
        if not query or not key:
            return None, "empty"
        state: dict[str, Any] = {"throttled": False}
        deadline = time.monotonic() + self._REPLY_SCAN_BUDGET
        cursor = ""
        seen = 0
        for _ in range(self._REPLY_SCAN_PAGES):
            if time.monotonic() > deadline or state["throttled"]:
                break
            page = await asyncio.to_thread(self._search_page, query, cursor, state)
            if not page:
                break
            hits, cursor = page
            if not hits:
                break
            for hit in hits:
                seen += 1
                if not any(fxtwitter.media_key(link) == key for link in hit.media):
                    continue
                self._vlog(
                    "转发页反查：@%s 搜了 %s 条转发，命中 %s（@%s）",
                    handle,
                    seen,
                    hit.url,
                    hit.author_handle or "未知",
                )
                return {
                    "url": hit.url,
                    "handle": hit.author_handle,
                    "name": hit.author_name,
                    "created_at": hit.created_at,
                }, "hit"
            if not cursor:
                break
            await asyncio.sleep(self._REPLY_SCAN_GAP)
        if state["throttled"]:
            self._vlog("转发页反查：@%s 被限流，搜了 %s 条就收手", handle, seen)
            return None, "throttled"
        if time.monotonic() > deadline:
            self._vlog("转发页反查：@%s 到预算上限，搜了 %s 条", handle, seen)
            return None, "timeout"
        self._vlog("转发页反查：@%s 搜了 %s 条转发，没有这张图", handle, seen)
        return None, "empty" if not seen else "miss"

    async def _resolve_x_list_page(self, candidates: list[Candidate], stream_id: str = "") -> str:
        """把「X 回复页 / 转发页」上对得上的图，倒推回它真正的那条原帖。

        这两种页面上的图都是别人的：回复页里是她回复的那条帖子，转发页里是她转的
        那条帖子——实测 s-20261109-2118-5137：图在 @wn40536699 的回复页里，指纹
        距离 0，实际是 @hinatahimarii 发的。只在指纹已经对上（距离 ≤ 阈值）时
        反查，翻不到就保持疑似；认错人比没找到更糟。

        回复页走 `_reply_page_lookup`（翻回复 → 父帖），转发页走 `_retweet_lookup`
        （搜 `filter:retweets`，结果本身就是原帖）。点赞页没有对应的搜索语法，
        只能保持疑似。

        返回一句「已经翻过但没搞定」的说明（成功时返回空串）。调用方把它交给
        Grok：翻不到多半是接口不给力，不是这张图没有出处，得让它换个路子查。
        """
        threshold = self.config.plugin.hash_threshold
        pending = [
            item
            for item in candidates
            if item.status == "suspect"
            and item.distance is not None
            and item.distance <= threshold
            and x_page_kind(item.url or "") in {"with_replies", "reposts"}
        ]
        if not pending:
            return ""
        # 这一段要花几十秒，先说一声——不然聊天里会以为已经搜完了。
        if stream_id:
            try:
                await self.ctx.send.text(formatting.TRACE_PING_TEXT, stream_id)
            except Exception as error:  # noqa: BLE001 - 提示发不出去不影响反查
                self._vlog("顺藤摸瓜提示发送失败：%s", str(error)[:80])
        self._vlog("列表页反查开始：候选=%s 提示=%s", len(pending), formatting.TRACE_PING_TEXT)
        failed: list[str] = []
        attempts = 0
        for item in pending:
            if attempts >= self._REPLY_SCAN_ATTEMPTS:
                break
            handle = x_page_handle(item.url or "")
            key = fxtwitter.media_key(item.image_url or "")
            if not handle or not key:
                continue
            kind = x_page_kind(item.url or "")
            attempts += 1
            if kind == "reposts":
                found, reason = await self._retweet_lookup(handle, key)
            else:
                found, reason = await self._reply_page_lookup(handle, key)
            if not found:
                why = {
                    "throttled": "被接口限流、没翻完",
                    "empty": "一条回复都没翻到" if kind == "with_replies" else "一条转发都没搜到",
                    "timeout": "到时间上限、没翻完",
                    "miss": "翻完了没命中",
                }.get(reason, "没翻到")
                failed.append(f"@{handle}（{why}）")
                continue
            who = f"@{found['handle']}" if found["handle"] else "某个账号"
            item.url = found["url"] or item.url
            if found["handle"]:
                item.handle = who
            if found["name"]:
                item.name = found["name"]
            item.platform = "X (Twitter)"
            item.source = "X"
            item.status = "match"
            if kind == "reposts":
                item.note = (
                    f"指纹一致（距离 {item.distance}）：这是 @{handle} 的转发页，"
                    f"她转的是 {who} 的这条帖子"
                )
            else:
                item.note = (
                    f"指纹一致（距离 {item.distance}）：这是 @{handle} 的回复页，"
                    f"顺着她的回复找到原帖在 {who} 名下"
                )
            self._vlog("列表页换成原帖：@%s（%s）→ %s（距离 %s）", handle, kind, item.url, item.distance)
            return ""
        return "、".join(failed)

    async def _settle_vision_handle(self, best: Candidate | None, upload: bytes | None) -> Candidate | None:
        """视觉给的是 X 账号主页时，翻这个号自己的帖子核实，翻不到就降级。

        视觉常凭「同款发饰、发尾蓝白渐变、白色长袜」就把人认下来，落地却是
        ``x.com/<号>`` 这种主页——主页上什么图都有，点进去根本对不上是哪张。
        实测 s-20261105-1537 那单挑中 `x.com/realpakbupati`，理由是「同款发饰、
        发尾蓝白渐变」，可那张图是粉发白裙、跟原图的银紫发护士服毫无关系，
        模型把拼图里别的格的特征套上去了。

        翻到她自己的带图帖且指纹命中，就换成那条帖子；一条都没命中，这条结论
        就撑不起来，降成疑似。
        """
        if best is None or best.status != "vision" or not is_x_platform(best.platform):
            return best
        if x_page_kind(best.url or "") != "profile":
            return best
        handle = (best.handle or "").strip().lstrip("@")
        if not handle:
            return best
        hit = await self._media_lookup(handle, upload)
        if hit and hit.get("url"):
            # 翻到的那条是拿原图逐张比过指纹才命中的（_media_lookup 只在
            # distance <= hash_threshold 时返回），这里已经是「同一张图」而不是
            # 「同一个人」的猜测：状态必须跟着升成 match，否则外面还会按视觉结论
            # 写「没找到同一张图」「不是指纹比对」，跟这条标注的指纹距离自相矛盾。
            distance = int(hit.get("distance") or 0)
            best.url = str(hit.get("url") or best.url)
            best.distance = distance
            best.status = "match"
            best.note = (
                f"指纹一致（距离 {distance}）：视觉认人后翻 @{handle} 的带图帖子，"
                "找到这张"
            )
            self._vlog("视觉主页换成原帖：%s → %s（距离 %s）", handle, best.url, distance)
            return best
        best.status = "suspect"
        best.note = f"视觉复核：疑似同一个人，但没在 @{handle} 最近的带图帖里翻到这张，仅供参考"
        self._vlog("视觉主页没翻到原帖，降级：%s", handle)
        return None

    async def _require_vision_proof(
        self,
        best: Candidate | None,
        upload: bytes | None,
        route: str,
    ) -> Candidate | None:
        """视觉命中、指纹又对不上时，必须有「她自己发过这张」作证，否则只算线索。

        `_settle_vision_handle()` 只管落点是 X 主页的那一类。落在一条具体帖子、
        或者微博／Instagram 这类翻不了帖的站点上时，之前是直接当结论发的：
        s-20261112-1435-9aed 视觉凭「粉色双马尾、同款粉色系服装」挑了一条，距离 28，
        落在一个微博 UID 上，整单写成找到——而正确答案在 X 上，压根没进候选。

        门槛沿用用户 11-12 定下的口径：距离在 ``hash_threshold`` 内说明图就是同一张，
        只是落点不好，照旧当视觉结论；超阈值（或没算出距离）就去翻那个号自己的
        带图帖，翻到才升成 match，翻不到／翻不成／翻不了都降成疑似，只留在参考区。
        代价是这类单更容易落成「未找到」，换来的是不再把别人当成答案。
        """
        if best is None or best.status != "vision" or not upload or route != "photo":
            return best
        threshold = self.config.plugin.hash_threshold
        if not vision.needs_post_proof(best.distance, threshold):
            return best
        gap = (
            f"指纹距离 {best.distance}"
            if best.distance is not None
            else "没算出指纹距离"
        )
        handle = (best.handle or "").strip().lstrip("@")
        if is_x_platform(best.platform) and handle:
            hit = await self._media_lookup(handle, upload)
            if hit and hit.get("url"):
                distance = int(hit.get("distance") or 0)
                best.url = str(hit.get("url") or best.url)
                best.distance = distance
                best.status = "match"
                best.note = (
                    f"指纹一致（距离 {distance}）：视觉认人后翻 @{handle} 的带图帖子，"
                    "找到这张"
                )
                self._vlog(
                    "视觉命中补证：@%s → %s（距离 %s）", handle, best.url, distance
                )
                return best
            note = vision.PROOF_NOTE_BLOCKED if (hit or {}).get("error") else vision.PROOF_NOTE_MISS
            self._vlog(
                "视觉命中没有帖子作证（%s）：@%s，%s",
                gap,
                handle,
                str((hit or {}).get("error") or "带图帖里没有这张")[:80],
            )
        else:
            note = vision.PROOF_NOTE_OFFLINE
            self._vlog("视觉命中没有帖子作证（%s）：%s 翻不了帖", gap, handle or best.url)
        best.status = "suspect"
        best.downgrade = "vision"
        best.note = note.format(gap=gap)
        return None

    # 按名字换号时最多核这么多个候选。检索结果里第一个通常就是本人，多试只是
    # 给同名的人留一次机会，翻帖那一步一样要指纹对上才作数。
    _X_FROM_NAME_LIMIT = 2

    # 命中搬运站后翻候选里的 X 号：最多两个，整段卡 90 秒。这两个号常常是
    # 「同一篇报道里带出来的账号」，未必是作者；所以门槛还是指纹，翻不到就老实写。
    _REPOST_AUTHOR_LIMIT = 2
    _REPOST_AUTHOR_BUDGET = 90.0

    def _repost_x_handles(self, ordered: list[Candidate], best: Candidate | None) -> list[str]:
        """候选里出现过的 X 号，按出现顺序去重——不要求那条帖子本身就是这张图。

        搬运站的转贴页没有账号，唯一还能通向本人的就是同批候选里那几个 X 账号：
        实测 jh361 那单的 67 条候选里有 ``@oioioi525``（Lens 认为画风像，指纹距离 30），
        这种「图不对、人可能是」的号，正式结论用不上，但翻一遍它的带图帖很便宜。
        """
        seen: set[str] = set()
        blocked = (best.handle or "").strip().lstrip("@").lower() if best is not None else ""
        found: list[str] = []
        # 列表页挖出来的那篇帖子，页面自己标着 X 号的，先拿它去翻——这比「同批候选
        # 里谁可能是」硬得多。挖到帖子就把账号丢掉，等于白挖一层。
        for account in list(getattr(best, "source_accounts", []) or []) if best is not None else []:
            if str(account.get("platform") or "") != "X":
                continue
            handle = str(account.get("handle") or "").strip().lstrip("@")
            if not handle or not _X_HANDLE_RE.match(handle):
                continue
            lower = handle.lower()
            if lower in seen or lower == blocked:
                continue
            seen.add(lower)
            found.append(handle)
        for item in ordered:
            handle = (item.handle or "").strip().lstrip("@")
            if not handle or handle.isdigit():
                continue
            # 图集页的编号会挂成 handle（"UID 1856233540"），翻它纯粹浪费预算。
            if not _X_HANDLE_RE.match(handle):
                continue
            lower = handle.lower()
            if lower in seen or lower == blocked:
                continue
            # 以前只收落在 X 上的候选；实测 s-20261111-1028-1063 那个号挂在图集站
            # 的标题里（cosplayrule34「Model Misa Daidai (@xidaidai)」），整条
            # 线因此没跑，结论停在 galleryepic 上。号出现在哪一页不重要，能不能
            # 在它的带图帖里翻到这张才是判据。
            seen.add(lower)
            found.append(handle)
            if len(found) >= self._REPOST_AUTHOR_LIMIT:
                break
        return found

    _TITLE_AUTHOR_LIMIT = 5
    # 查这五个变体的总预算：正常一个号一两秒，失败一次要等满 20 秒，五六个加起来
    # 能把整单拖长两三分钟。超预算就停止试后面的变体，已经认出的号照样往下翻。
    _TITLE_AUTHOR_BUDGET = 70
    # 一次搜索里常常同时命中好几个搬运站／图集站：45de 是 nudebird 的两页，
    # 158d 是 xx.knit.bid 与 pixibb。以前只追 best 那一条链，别的站上白纸黑字
    # 写着的作者名没人看。这里把它们一起捞回来追，最多追这么多个站。
    _LEAD_SITE_LIMIT = 5
    # 只追核过图、且距离够近的落点：距离 20 以上的基本是「同一个人/同一个词」撞出来的。
    _LEAD_SITE_MAX_DISTANCE = 12
    # 多站一起追的总预算：每个名字都要查号、翻帖，全放开能把整单拖上三分钟。
    _SITE_AUTHOR_BUDGET = 55
    # 查资料失败后的重试间隔。测试里会改成 0。
    _PROFILE_RETRY_DELAY = 1.2
    # 抓候选页面取正文喂模型：超时别太久，正文也别太长（只为抽个名字，不值得几千 token）。
    _PAGE_EXCERPT_TIMEOUT = 10
    _PAGE_EXCERPT_CHARS = 1500

    @staticmethod
    def _written_handle(item: Candidate) -> tuple[str, str]:
        """候选里直接写着的 X 号，``name`` 和 ``title`` 都要看。

        站方给的 ``name`` 常是洗过的模特名（cosplayrule34 的 name 是
        「Model Kokura Chiyo in cosplay W from Arknights ...」），``title`` 里才留着
        「(@yummychiyo)」。只看 name 就把真号漏了（2c76 那单）。
        """
        for text in (item.name, item.title):
            handle, written = handles.title_handle(str(text or ""))
            if handle:
                return handle, written
        return "", ""

    def _lead_pages(self, ordered: list[Candidate], best: Candidate | None) -> list[Candidate]:
        """挑出「落在网页上、能指向作者」的落点，当成要找作者的地方。

        一次搜索经常同时命中好几个搬运站／图集站，只追 best 那条会漏掉别的站上写着的
        作者名。这里按距离从近到远收，同一个域名只留一条（同站的多篇转载，作者名
        通常一样，重复追只是多花时间）。页面上没账号的收；标题里明写着账号的也收
        （那个号还要核一遍它自己的带图帖），URL 里带出来的账号页不收。
        """
        pool: list[Candidate] = []
        if best is not None:
            pool.append(best)
        pool.extend(ordered)

        def rank(item: Candidate) -> int:
            """标题里直接写着 (@handle) 的页面排最前。

            这种页面是唯一「账号白纸黑字写在上面」的线索，可名额只有 5 个：2c76 那单
            的 cosplayrule34（标题「Model Kokura Chiyo (@yummychiyo)」）排在第六，
            正好被挤掉，真号连试都没试。
            """
            return 0 if self._written_handle(item)[0] else 1

        found: list[Candidate] = []
        hosts: set[str] = set()
        for item in sorted(pool, key=rank):
            if item is None or any(item is seen for seen in found):
                continue
            if item.status not in {"match", "suspect", "vision"}:
                continue
            if self._is_x(item):
                continue
            written_handle = self._written_handle(item)[0]
            if (item.handle or "").strip():
                # 页面自己写着账号的候选也要追：2c76 的 cosplayrule34 标题里就是
                # 「(@yummychiyo)」，extract 已经把这个号填进 handle，可先前的
                # 「只追页面上没有账号的落点」把它整条排除了，真号连核都没核。
                # 只放行「标题里真写着这个 handle」的，URL 里带出来的账号页不算。
                current = (item.handle or "").strip().lstrip("@").lower()
                if not written_handle or written_handle.lower() != current:
                    continue
            distance = item.distance
            if distance is None or int(distance) > self._LEAD_SITE_MAX_DISTANCE:
                continue
            host = (
                str(item.url or "").split("://", 1)[-1].split("/", 1)[0].split("?", 1)[0].lower()
            )
            if not host or host in hosts:
                continue
            hosts.add(host)
            found.append(item)
            if len(found) >= self._LEAD_SITE_LIMIT:
                break
        return found

    async def _title_author_leads(
        self, pages: list[Candidate]
    ) -> list[tuple[str, list[str], str, str, str]]:
        """把多个落点上的作者名一次收齐：``(author, variants, via, name, site)``。

        先用规则从每个标题里剥（「染三ran3 - 露露卡」这种标题本身就写着名字），再问
        一次模型把所有页面的标题和正文一起看。**一次搜索最多问两次模型**：批量一次，
        批量什么都没给出时才退回 best 单页问一次——一站一问能把整单拖上几分钟。

        标题里直接写着 ``(@handle)`` 的那批排最前面，``via`` 记作 ``title``：那是
        页面自己写的账号，不需要再猜。
        """
        if not pages:
            return []

        def build(
            author: str, handle: str, name: str, via: str, site: str
        ) -> tuple[str, list[str], str, str, str] | None:
            author = str(author or "").strip()
            if not author:
                return None
            # 「[X] Maru on the road」这种是搬运工自己起的一句话，不是名字：规则从标题
            # 里剥出来的东西常在三个词以上，拿去展开变体只会查一堆不存在的号。
            if len([word for word in re.split(r"\s+", author) if word]) > 3:
                return None
            split_name, split_handle = handles.split_author_name(author)
            variants: list[str] = []
            for item in [handle, split_handle, *handles.x_variants(author)]:
                value = str(item or "").strip().lstrip("@")
                if value and value.lower() not in {seen.lower() for seen in variants}:
                    variants.append(value)
            # 纯中文名（「诗诗爱生活」）展开不出任何 X 变体：x_variants 只认 ASCII。
            # 但这个名字还要交给「按名字找号」去微博等平台查一轮，不能在这里就
            # 把 lead 丢掉——b3b4 百度动态那单的账号名只在页面标题里，丢了就整条断。
            return (
                author,
                variants[: self._TITLE_AUTHOR_LIMIT],
                via,
                name or split_name,
                site,
            )

        rules: list[tuple[str, list[str], str, str, str]] = []
        for page in pages:
            text = str(page.name or page.title or "")
            author = handles.title_author(text) or handles.title_at_author(text)
            if not author and is_content_site(str(page.url or ""), str(page.platform or "")):
                # 内容号落地页的候选标题只是正文首句，账号写在页面 <title> 的
                # 「@xxx的动态」里（b3b4 落在 mbd.baidu.com，整页没有 author 字段），
                # 得抓一次页面标题才看得到。只在内容站上抓，别给每个落点都多花一次请求。
                page_title, _ = await self._page_excerpt_for_author(str(page.url or ""))
                author = handles.title_at_author(page_title)
            lead = build(author, "", "", "rule", str(page.url or ""))
            if lead is not None:
                rules.append(lead)

        # 页面标题里直接写着 X 号的，比任何推测都硬，排在前面先试：
        # 「Model Kokura Chiyo (@yummychiyo) in cosplay W from Ark」这种写法，
        # 规则抽出来是 `CoserwW21photos` 之类的垃圾变体，真号就在括号里没人看
        # （2c76 那单）。这批不走 build()：括号前面那串常常是整句话，名字对不上
        # 也照样去翻带图帖，能不能核到图才是判据。
        direct: list[tuple[str, list[str], str, str, str]] = []
        seen_handles: set[str] = set()
        for page in pages:
            handle, written = self._written_handle(page)
            if not handle or handle.lower() in seen_handles:
                continue
            seen_handles.add(handle.lower())
            split_name, split_handle = handles.split_author_name(written)
            variants: list[str] = [handle]
            for item in [split_handle, *handles.x_variants(written)]:
                value = str(item or "").strip().lstrip("@")
                if value and value.lower() not in {seen.lower() for seen in variants}:
                    variants.append(value)
            direct.append(
                (
                    written or handle,
                    variants[: self._TITLE_AUTHOR_LIMIT],
                    "title",
                    split_name or written or handle,
                    str(page.url or ""),
                )
            )
            self._vlog(
                "标题作者：页面里直接写着 @%s（名字「%s」），先核这个号",
                handle,
                written or "-",
            )

        models: list[tuple[str, list[str], str, str, str]] = []
        section = getattr(self.config, "title_author", None)
        if section is not None and section.enabled and len(rules) < 2:
            answers = await self._ask_site_authors(pages)
            if answers is None and len(pages) == 1:
                single = await self._ask_title_author(pages[0])
                if single is not None:
                    author, handle, name = single
                    answers = [{"author": author or name, "handle": handle, "site": pages[0].url}]
            for answer in answers or []:
                lead = build(
                    str(answer.get("author") or answer.get("name") or ""),
                    str(answer.get("handle") or ""),
                    str(answer.get("name") or ""),
                    "llm",
                    str(answer.get("site") or ""),
                )
                if lead is not None:
                    models.append(lead)

        leads: list[tuple[str, list[str], str, str, str]] = []
        seen: set[str] = set()
        for lead in [*direct, *models, *rules]:
            key = lead[0].lower()
            if key in seen:
                continue
            seen.add(key)
            leads.append(lead)
        return leads

    async def _ask_site_authors(self, pages: list[Candidate]) -> list[dict[str, str]] | None:
        """问一次模型：这几页里各写着谁，一次看完。

        返回 ``[{"author", "name", "handle", "site"}]``；模型明确说都没写作者时返回空
        列表，调用失败／答复解析不了时返回 ``None``（调用方退回规则，不再追问）。
        """
        section = self.config.title_author
        blocks: list[str] = []
        for index, page in enumerate(pages, start=1):
            title = str(page.name or page.title or "").strip()
            page_title, excerpt = await self._page_excerpt_for_author(page.url)
            block = [
                f"【第 {index} 页】站点：{page.source or page.platform or '未知'}",
                f"网址：{page.url}",
                f"标题：{title}",
            ]
            if page_title and page_title != title:
                block.append(f"页面里的标题：{page_title}")
            if excerpt:
                block.append("正文：")
                block.append(excerpt)
            blocks.append("\n".join(block))
        if not blocks:
            return None
        lines = [
            "下面是一次图搜落到的几个网页（都是搬运站／图集站，页面上可能写着原作者）。",
            "请逐页判断这张图真正的作者是谁，只输出一个 JSON 数组，不要解释：",
            '[{"site": "这一页的网址", "author": "页面里原样的完整名字",'
            ' "name": "其中的人名（中文/日文那半截）", "handle": "其中的英文或数字 id",'
            ' "x": "页面里写明的 X 账号"}]',
            "",
            "规则：",
            "- 只写页面里真有的名字，没写就整页跳过，不要猜、不要拿站名当作者；",
            "- **很多人把中文名和英文 id 写在一起**，必须拆成两截：「染三ran3」→ name=染三、handle=ran3；",
            "- author 填页面里原样的那一整串，不要自己拼、不要翻译；",
            "- 正文里写明的署名（作者、Photo by、credit、© 后面那个名字）比标题推测可靠；",
            "- 标题里的分类标签（[Cosplay]、【写真】）、作品名、图片数量、体积都不是名字；",
            "- 页面写了 X 链接或 @账号时填在 x 里，没写就留空。",
            "",
            "\n\n".join(blocks),
        ]
        prompt = "\n".join(lines)
        timeout = max(20, int(section.timeout))
        options: dict[str, Any] = {
            "prompt": prompt,
            "task_name": section.task or "utils",
            "temperature": 0,
            "max_tokens": 500,
        }
        if section.model:
            options["model_name"] = section.model
        started = time.monotonic()
        try:
            result = await asyncio.wait_for(
                self.ctx.call_capability("llm.generate", timeout_ms=timeout * 1000, **options),
                timeout=timeout + 5,
            )
        except asyncio.TimeoutError:
            self.ctx.logger.warning(
                "多站作者抽取超时：等待=%.1fs 超时=%ss 页面=%s",
                time.monotonic() - started,
                timeout,
                len(pages),
            )
            return None
        except Exception as error:  # noqa: BLE001 - 抽不动就退回规则，别拖住整单
            self.ctx.logger.warning(
                "多站作者抽取失败：耗时=%.1fs err=%s",
                time.monotonic() - started,
                str(error)[:80],
            )
            return None
        payload = result if isinstance(result, dict) else {"success": True, "response": str(result or "")}
        inner = payload.get("result")
        if isinstance(inner, dict) and ("response" in inner or "text" in inner):
            payload = {**payload, **inner}
        if not payload.get("success", True):
            self.ctx.logger.warning("多站作者抽取返回失败：%s", str(payload.get("error"))[:80])
            return None
        text = str(payload.get("response") or payload.get("text") or "")
        found = handles.llm_author_list(text)
        if found is None:
            self.ctx.logger.warning(
                "多站作者抽取空手：耗时=%.1fs 答复=%s",
                time.monotonic() - started,
                text.strip().replace("\n", " ")[:60] or "（空）",
            )
            return None
        self._vlog(
            "多站作者：模型 %.1f 秒后给出 %s 条（%s）",
            time.monotonic() - started,
            len(found),
            "、".join(str(item.get("author") or "-") for item in found)[:80],
        )
        return found

    async def _scan_author_lead(
        self,
        lead: tuple[str, list[str], str, str, str],
        upload: bytes | None,
    ) -> dict[str, Any] | None:
        """拿一个名字去 X 上把号找回来，并翻她的带图帖比指纹。

        ``via == "title"`` 是页面标题自己写明的账号（``(@yummychiyo)``）：这种号
        显示名对不上也照翻一次——页面写的是账号、不是名字，名字那截常是站方抄的
        整句话。翻不到就只当线索（status=seen），不拿它改结论。
        """
        author, variants, via, name, site = lead
        explicit = via == "title"
        scanned: list[str] = []
        started = time.monotonic()
        for handle in variants[: self._TITLE_AUTHOR_LIMIT]:
            if scanned and time.monotonic() - started > self._TITLE_AUTHOR_BUDGET:
                self._vlog(
                    "标题作者：认号花了 %.0f 秒，剩下的变体不再试",
                    time.monotonic() - started,
                )
                break
            if not scanned and time.monotonic() - started > self._TITLE_AUTHOR_BUDGET * 2:
                # 一个都没认出来、时间又烧光了：再试下去只会把整单拖得更久。
                self._vlog(
                    "标题作者：查号超过 %.0f 秒仍没对上任何一个，放弃",
                    time.monotonic() - started,
                )
                break
            profile = await self._profile(handle)
            if not profile:
                continue
            display = str(profile.get("name") or "")
            matched = handles.name_matches(author, display)
            if not matched and not explicit:
                self._vlog(
                    "标题作者：@%s 显示名「%s」对不上「%s」，跳过", handle, display, author
                )
                continue
            if not matched:
                # 页面写着「(@yummychiyo)」，显示名却是另一套写法：名字未必抄错，
                # 但这里认不了。照样翻一次她的带图帖，对上图才算数。
                self._vlog(
                    "标题作者：页面写着 @%s（显示名「%s」），名字对不上也翻一次",
                    handle,
                    display or "-",
                )
            scanned.append(handle)
            hit = await self._media_lookup(handle, upload)
            if not (hit and hit.get("url")):
                hit = await self._search_media_lookup(handle, upload)
            base = {
                "author": author,
                "name": name,
                "variants": variants[: self._TITLE_AUTHOR_LIMIT],
                "via": via,
                "site": site,
                "scanned": scanned,
                "handle": handle,
                "x_name": display,
                "post": "",
                "distance": None,
                "explicit": explicit,
            }
            if hit and hit.get("url"):
                distance = int(hit.get("distance") or 0)
                self._vlog(
                    "标题作者：@%s 命中 %s（距离 %s，作者「%s」）",
                    handle,
                    hit.get("url"),
                    distance,
                    author,
                )
                return {**base, "status": "hit", "post": str(hit.get("url") or ""), "distance": distance}
            if matched:
                self._vlog("标题作者：@%s 名字对上了，但没翻到这张图", handle)
                return {**base, "status": "named"}
            # 页面写的号、名字没对上、帖子里也没有这张：只当线索，别拿它改结论。
            self._vlog("标题作者：页面写着的 @%s 最近没发这张，只当线索", handle)
            return {**base, "status": "seen"}
        return {
            "status": "none",
            "author": author,
            "name": name,
            "variants": variants[: self._TITLE_AUTHOR_LIMIT],
            "via": via,
            "site": site,
            "scanned": [],
            "explicit": explicit,
        }

    async def _hunt_title_author(
        self,
        best: Candidate | None,
        upload: bytes | None,
        ordered: list[Candidate] | None = None,
    ) -> dict[str, Any] | None:
        """转载站标题里写着的作者名，拿去 X 上把号找回来。

        s-20261119-1703-0c5b 的 best 标题是「染三ran3 - 露露卡[108P7V-1.23GB]」，
        插件翻了候选里两个不相干的号（aisaki_cos、oioioi525）就收工；可正主
        ``ran3ran3ran3`` 的显示名就是「染三 ran3」——名字对得上，她的带图帖里
        也确实有这张图。

        判据只有一条：**显示名必须和标题里的名字对得上**。光「这个号存在」不算，
        `ran3` 这种 1 粉的空号存在也不代表是她。名字对上但没翻到这张图时只当
        线索（status=named），不改结论。
        """
        if best is None or not upload:
            return None
        pages = self._lead_pages(ordered or [], best)
        leads = await self._title_author_leads(pages)
        if not leads:
            return None
        started = time.monotonic()
        scanned: list[str] = []
        named: dict[str, Any] | None = None
        first: dict[str, Any] | None = None
        for lead in leads:
            spent = time.monotonic() - started
            if named is not None and (spent > self._SITE_AUTHOR_BUDGET or len(scanned) >= 2):
                # 已经认出一个名字对得上的人了，再试别的名字收益很低。
                break
            if spent > self._SITE_AUTHOR_BUDGET * 2:
                self._vlog("多站追作者：%.0f 秒用完，剩下的名字不再试", spent)
                break
            result = await self._scan_author_lead(lead, upload)
            if result is None:
                continue
            scanned.extend(result.get("scanned") or [])
            if result.get("status") == "hit":
                return result
            if result.get("status") == "named" and named is None:
                named = result
            if first is None:
                first = result
        if named is not None:
            return {**named, "scanned": scanned}
        if first is not None:
            return first
        return {
            "status": "none",
            "author": "",
            "name": "",
            "variants": [],
            "via": "",
            "site": "",
            "scanned": scanned,
        }

    async def _title_author_candidates(self, best: Candidate) -> tuple[str, list[str], str, str]:
        """从标题里认出作者名，再展开成候选 X 账号。

        默认交给 MaiBot 的文本模型（``[title_author]`` 段，任务组默认 utils）：资源站
        标题写法太杂，固定规则总在漏（`s-20261119-1722-fe9b` 的「Cosplay] ran3 - …」
        被抽成 `Cosplay] ran3`，展开成 `Cosplayran3`，一个都不存在）。关掉开关、或者
        模型没回／答复解析不了时退回规则抽取。

        返回值 ``(author, variants, via, name)``：``author`` 是原样的整串（比对显示名
        用它），``name`` 是拆出来的名字那半截（「染三ran3」→ 染三），``via`` 写清这次
        走的是 llm 还是 rule，展示时据此说明名字是哪来的。
        """
        section = getattr(self.config, "title_author", None)
        if section is not None and section.enabled:
            answer = await self._ask_title_author(best)
            if answer is not None:
                author, handle, name = answer
                if not author:
                    author = name or handle
                variants: list[str] = []
                if handle:
                    variants.append(handle)
                for item in handles.x_variants(author):
                    if item.lower() not in {value.lower() for value in variants}:
                        variants.append(item)
                if variants:
                    self._vlog(
                        "标题作者：模型给出「%s」（名字 %s / 账号 %s），展开 %s",
                        author,
                        name or "-",
                        handle or "无",
                        variants[: self._TITLE_AUTHOR_LIMIT],
                    )
                return author, variants[: self._TITLE_AUTHOR_LIMIT], "llm", name
            self._vlog("标题作者：模型没给出可用结果，退回规则抽取")
        author = handles.title_author(best.name)
        name, _ = handles.split_author_name(author)
        return author, handles.x_variants(author), "rule", name

    async def _page_excerpt_for_author(self, url: str) -> tuple[str, str]:
        """抓候选页面，取一小段正文给模型看。

        模型不联网，只给一个 URL 等于没给；而资源站的署名、原帖外链往往只在正文里
        （标题那行经常只有分类和作品名）。抓不到（403、超时、不是 HTML）就只凭标题问，
        不额外拖时间。
        """
        if not url:
            return "", ""
        try:
            html = await asyncio.wait_for(
                self._fetch_page_html(url), timeout=self._PAGE_EXCERPT_TIMEOUT
            )
        except Exception:  # noqa: BLE001 - 正文只是加分项，拿不到照样能问
            return "", ""
        if not html:
            return "", ""
        title, body = weibopost.page_text(html)
        return str(title or "").strip(), weibopost.page_excerpt(body, limit=self._PAGE_EXCERPT_CHARS)

    async def _ask_title_author(self, best: Candidate) -> tuple[str, str, str] | None:
        """问一次文本模型：这个标题里的作者是谁、名字和账号各是哪截。

        返回 ``(author, handle, name)``；都空表示模型明确说页面里没有作者名，调用方
        据此收工（不再拿规则硬抽）。超时、报错、答复解析不了时返回 ``None``，调用方
        退回规则。

        ``author`` 是页面里原样的整串（比对 X 显示名要用它），``name`` / ``handle``
        是把「中文名 + 英文 id」拆开的结果——很多人两截写在一起（「染三ran3」），
        不拆开展示起来像一个人名，实际上半截是账号。
        """
        section = self.config.title_author
        title = str(best.name or best.title or "").strip()
        if not title:
            return None
        page_title, excerpt = await self._page_excerpt_for_author(best.url)
        lines = [
            "下面是一条图搜结果落到的网页，标题是资源站/搬运站自己写的。",
            f"站点：{best.source or best.platform or '未知'}",
            f"标题：{title}",
        ]
        if page_title and page_title != title:
            lines.append(f"页面里的标题：{page_title}")
        if best.url:
            lines.append(f"页面：{best.url}")
        if excerpt:
            lines.extend(
                [
                    "",
                    "页面正文（去掉标签后的纯文字，开头往往混着导航，可能不完整）：",
                    excerpt,
                ]
            )
        lines.extend(
            [
                "",
                "请判断这张图真正的作者叫什么，只输出一个 JSON，不要解释：",
                '{"author": "页面里原样的完整名字", "name": "其中的人名（中文/日文那半截）",'
                ' "handle": "其中的英文或数字 id（账号那半截）", "x": "页面里写明的 X 账号，没有就留空"}',
                "",
                "规则：",
                "- **很多人把中文名和英文 id 写在一起**，必须拆成两截分别填："
                "「染三ran3」→ name=染三、handle=ran3；「渔蛋Sakana」→ name=渔蛋、handle=Sakana；"
                "「Vivian」只有一截时，name 留空、handle=Vivian；「染三」只有中文时反过来；",
                "- author 填页面里原样的那一整串（上面两个例子分别是「染三ran3」「渔蛋Sakana」），"
                "不要自己拼，也不要翻译；",
                "- 优先看页面正文里写明的署名（作者、Photo by、credit、© 后面那个名字），"
                "正文写明的比标题里推测的可靠；",
                "- 标题里的名字去掉开头的分类标签（[Cosplay]、【写真】这类）、作品名、"
                "图片数量、体积、语言标签，也不要翻译或补全；分隔符（- 、| 、/ 、方括号）"
                "后面的一般是作品名，不是名字；页面标题末尾那个「- 站名」也要去掉；",
                "- 如果页面里直接写了她的 X 链接或 @账号（x.com/xxx、twitter.com/xxx、@xxx），"
                "填在 x 里——这比从名字里拆出来的可靠；没写到就留空，别拿名字去猜；",
                "- 页面和标题里都没有作者名（比如只是一句描述、或者写的是搬运站自己的名字），"
                '就回 {"author": "", "name": "", "handle": "", "x": ""}。',
            ]
        )
        prompt = "\n".join(lines)
        timeout = max(10, int(section.timeout))
        options: dict[str, Any] = {
            "prompt": prompt,
            "task_name": section.task or "utils",
            "temperature": 0,
            "max_tokens": 200,
        }
        if section.model:
            options["model_name"] = section.model
        started = time.monotonic()
        try:
            result = await asyncio.wait_for(
                self.ctx.call_capability("llm.generate", timeout_ms=timeout * 1000, **options),
                timeout=timeout + 5,
            )
        except asyncio.TimeoutError:
            self.ctx.logger.warning(
                "标题作者抽取超时：等待=%.1fs 超时=%ss 标题=%s",
                time.monotonic() - started,
                timeout,
                title[:60],
            )
            return None
        except Exception as error:  # noqa: BLE001 - 抽不动就退回规则，别拖住整单
            self.ctx.logger.warning(
                "标题作者抽取失败：耗时=%.1fs err=%s", time.monotonic() - started, str(error)[:80]
            )
            return None
        payload = result if isinstance(result, dict) else {"success": True, "response": str(result or "")}
        # 新版 Runner 会把结果再包一层 {"success": true, "result": {...}}，两种形态都要接。
        inner = payload.get("result")
        if isinstance(inner, dict) and ("response" in inner or "text" in inner):
            payload = {**payload, **inner}
        if not payload.get("success", True):
            self.ctx.logger.warning("标题作者抽取返回失败：%s", str(payload.get("error"))[:80])
            return None
        text = payload.get("response") or payload.get("text") or ""
        author, handle, name = handles.llm_author_reply(str(text))
        if not (author or handle or name) and not re.search(r"\{.*\}", str(text), re.S):
            # 模型这轮什么都没说（空字符串、只肯解释、输出被截断），当成失败退回规则。
            # 只有它真的回了 JSON 且三个字段都空，才算「页面里确实没写作者名」。
            # 实测 s-20261119-2329-0fd8：模型 2.0 秒回了空串，被当成「没作者」，
            # 整条反查就此收工，标题里明明写着「染三ran3」。
            self.ctx.logger.warning(
                "标题作者抽取空手：耗时=%.1fs 答复=%s 标题=%s",
                time.monotonic() - started,
                str(text).strip().replace("\n", " ")[:40] or "（空）",
                title[:60],
            )
            return None
        self._vlog(
            "标题作者：模型 %.1f 秒后答复「%s」→ 「%s」（名字 %s / 账号 %s）",
            time.monotonic() - started,
            str(text).strip().replace("\n", " ")[:80],
            author,
            name or "-",
            handle or "无",
        )
        return author, handle, name

    async def _hunt_repost_author(
        self, ordered: list[Candidate], best: Candidate | None, upload: bytes | None
    ) -> dict[str, Any] | None:
        """指纹命中落在普通网页上（没有账号）时，去翻候选里那几个 X 号自己的帖。

        实测这一类落点几乎都是搬运页：jh361 那篇「推特高颜值萝莉 染三COS…」跟原图
        距离 2，文章里却没有一个账号，标题里的「推特」只是站点模板。以前命中就收工，
        看起来像「找到搬运站就算找到了」。现在把候选里的 X 号挨个翻一遍带图帖
        （``/media`` 不够再翻 ``from:<号> filter:images``），谁真有这张图谁当结论。
        """
        handles = self._repost_x_handles(ordered, best)
        if not handles or not upload:
            return None
        started = time.time()
        scanned: list[str] = []
        failed: list[str] = []
        for handle in handles:
            left = self._REPOST_AUTHOR_BUDGET - (time.time() - started)
            if left <= 5:
                self._vlog("翻候选 X 号：预算用完，剩下 %s 没翻", "、".join(handles[len(scanned):]))
                break
            hit = await self._media_lookup(handle, upload)
            if not (hit and hit.get("url")):
                fallback = await self._search_media_lookup(handle, upload)
                if fallback and fallback.get("url"):
                    hit = fallback
            if hit and hit.get("url"):
                distance = int(hit.get("distance") or 0)
                profile = await self._profile(handle)
                self._vlog(
                    "翻候选 X 号：@%s 命中 %s（距离 %s，%ss）",
                    handle,
                    hit.get("url"),
                    distance,
                    int(time.time() - started),
                )
                return {
                    "status": "hit",
                    "scanned": [*scanned, handle],
                    "failed": failed,
                    "handle": handle,
                    "post": str(hit.get("url") or ""),
                    "distance": distance,
                    "x_name": str((profile or {}).get("name") or ""),
                }
            if hit and hit.get("error"):
                failed.append(handle)
            scanned.append(handle)
        self._vlog(
            "翻候选 X 号：%s 没翻到（失败 %s，%ss）",
            "、".join(scanned) or "无",
            "、".join(failed) or "无",
            int(time.time() - started),
        )
        if not scanned:
            return None
        return {"status": "none", "scanned": scanned, "failed": failed}

    def _promote_repost_author(
        self, best: Candidate | None, info: dict[str, Any]
    ) -> Candidate | None:
        """候选里的某个 X 号自己发过这张图时，那条帖子才是结论。

        搬运页/图集页只是别人转的；这一条的证据是「作者自己的带图帖里指纹对上」，
        跟水印核对同一档，所以直接升成 match。
        """
        post = str(info.get("post") or "")
        handle = str(info.get("handle") or "").strip().lstrip("@")
        if not post or not handle:
            return best
        distance = int(info.get("distance") or 0)
        self._vlog("搬运站转贴反查命中：@%s %s（距离 %s）", handle, post, distance)
        return Candidate(
            engine="repost_author",
            url=post,
            platform="X (Twitter)",
            handle="@" + handle,
            name=str(info.get("x_name") or ""),
            distance=distance,
            status="match",
            note=f"搬运站那条只是转贴：@{handle} 自己的带图帖里有这张图（距离 {distance}）",
        )

    @staticmethod
    def _weibo_uid(item: Candidate | None) -> str:
        """落点是微博、账号字段是纯数字时，那个数字就是微博 UID。

        微博候选的 handle 只有 UID（``7652767178``），没有昵称；UID 本身搜不出人，
        要先换成昵称。
        """
        if item is None:
            return ""
        handle = (item.handle or "").strip().lstrip("@")
        if len(handle) < 5 or not handle.isdigit():
            return ""
        url = (item.url or "").lower()
        if "weibo.com" in url or "weibo.cn" in url:
            return handle
        return ""

    def _weibo_cookie(self) -> str:
        """微博登录 cookie：SUB / SUBP 两格优先，老配置里那整串兜底。

        拆成两格是用户 2026-11-13 的要求——整串一百多字符，粘贴时最容易漏一半或
        多带个引号，而漏了另一半的 cookie 依然能让请求发出去，只是微博会回 401/432，
        看起来像「登录填了却没用」。
        """
        section = self.config.weibo
        return identity.weibo_cookie_header(
            str(section.sub or ""),
            str(section.subp or ""),
            str(section.cookie or ""),
        )

    async def _weibo_profile(self, uid: str) -> dict[str, Any] | None:
        """微博 UID → 昵称/粉丝/认证。查不到就当没有，不编名字。"""

        def fetch() -> dict[str, Any] | None:
            return identity.fetch_weibo_profile(
                uid,
                self._proxy(),
                timeout=15,
                cookie=self._weibo_cookie(),
            )

        try:
            info = await asyncio.wait_for(asyncio.to_thread(fetch), timeout=28)
        except Exception as error:  # noqa: BLE001 - 换不到昵称就停下，不影响已有结论
            self._vlog("微博 UID %s 换成昵称失败：%s", uid, str(error)[:80])
            return None
        if info:
            self._vlog(
                "微博 UID %s → 昵称 %s（粉丝 %s）",
                uid,
                info.get("name"),
                info.get("followers"),
            )
        return info

    async def _search_weibo_users(self, name: str) -> list[dict[str, Any]]:
        """按微博昵称搜人，按粉丝数从多到少。搜不到返回空列表，接口挂了抛异常。"""
        name = (name or "").strip()
        if len(name) < 2:
            return []

        def fetch() -> list[dict[str, Any]]:
            return identity.search_weibo_users(
                name,
                self._proxy(),
                timeout=20,
                cookie=self._weibo_cookie(),
                min_fans=0,
                limit=5,
            )

        return await asyncio.wait_for(asyncio.to_thread(fetch), timeout=32)

    async def _with_weibo_post(
        self, row: dict[str, Any], target_hash: int | None
    ) -> dict[str, Any]:
        """微博核到人之后再往前一步：翻她的带图帖比指纹，对上就把结论换成那条帖。

        只报「主页 + 粉丝数」对用户没什么用——他还得自己点进去翻。
        s-20261113-1402-bd67 的正确答案就是水印上那个 @兔子兔子兔w 的帖子，
        翻到第几条都行，只要指纹对得上。翻不到就退回原来的线索。
        """
        if target_hash is None or not self._weibo_cookie():
            # 没登录态翻不了帖（帖搜索、时间线都要 cookie），只报主页就行。
            return row
        hit = await self._weibo_uid_post(
            str(row.get("uid") or ""),
            str(row.get("name") or ""),
            int(row.get("followers") or 0),
            target_hash,
        )
        if not hit:
            return row
        merged = dict(row)
        merged["post"] = str(hit.get("url") or "")
        merged["distance"] = int(hit.get("distance") or 0)
        merged["image"] = str(hit.get("image") or "")
        return merged

    # 名字抽取交给模型时最多看这么多条标题、最多等这么多秒。只在落点是图集站/搬运站
    # 时才问：那类站点的标题是搬运工自己写的，固定分隔符切不准是常态。
    _TITLE_NAMES_ROWS = 8
    _TITLE_NAMES_TIMEOUT = 8

    @staticmethod
    def _name_key(text: str) -> str:
        """比「这个名字在标题里出现过没有」：折平花体/全角，去掉空白与标点。"""
        value = handles.fold(str(text or "")).lower()
        return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", value)

    async def _llm_title_names(self, ordered: list[Candidate], known: str = "") -> list[str]:
        """搬运站/图集站标题里的名字交给模型抽，规则只当兜底。

        649c、9b22、411f 三单的错法各不相同——把 pixiv 作品名当人名、把半句攻略
        标题当名字、把「漫画预告」这种分类词当名字——共同点都是拿固定分隔符硬切。
        资源站的标题是搬运工自己写的（「Cosplay] ran3 - …」「永恒的新生(1) - lol」
        「纸悦Etsu_ko 蔚蓝档案 鬼方佳代子 [34P-182MB]」），切准是偶然，切不准是常态。

        只用一次模型调用看完前几条标题，不逐条问。模型给的每个名字都必须能在某条
        标题里原样找到（`_name_key` 折平后比较），找不到的当幻觉丢掉——这样它最多
        抽漏，不会凭空造出一个人去各平台乱搜。
        """
        section = getattr(self.config, "title_author", None)
        if section is None or not getattr(section, "enabled", False):
            return []
        rows = [
            item
            for item in ordered[: self._TITLE_NAMES_ROWS]
            if str(item.title or "").strip()
        ]
        if not rows:
            return []
        # 「标题作者」那条腿刚刚已经问过模型、并给出了一个能在标题里找到的名字时，
        # 不再问第二遍——同一次搜索里模型调用已经三次（多站作者、标题作者、这里），
        # 能省一次就省一次。
        known_key = self._name_key(known)
        if known_key and any(known_key in self._name_key(str(item.title or "")) for item in rows):
            return [str(known).strip()]
        # 只在命中页本身就是图集站/搬运站时问：正经帖子、X 原帖的标题不需要模型。
        if not any(
            is_repost_site(str(item.url or ""), str(item.platform or ""))
            for item in ordered[:3]
        ):
            return []
        lines = [
            "下面是一条以图搜图结果里的候选页面标题，站点多是把别人的图成套搬过来的"
            "图集站、资源站或内容站。",
            "请判断这些标题里，哪些是这张图的作者／模特（coser）的名字，哪些只是作品名、"
            "角色名、游戏名、分类标签、站名、图片数量或体积。",
            "",
        ]
        for index, item in enumerate(rows, start=1):
            site = str(item.platform or item.source or "").strip() or "未知站点"
            lines.append(f"{index}. [{site}] {item.title}")
        lines.extend(
            [
                "",
                "只输出一个 JSON，不要解释：",
                '{"names": ["名字1", "名字2"]}',
                "",
                "规则：",
                "- 名字是「人」的部分，原样照抄标题里的写法，不要翻译、不要补全、不要展开成账号；",
                "- 名字后面紧跟的作品名、角色名、游戏名、括号里的张数体积都不算名字"
                "（「纸悦Etsu_ko 蔚蓝档案 鬼方佳代子 [34P-182MB]」里只有纸悦Etsu_ko 是名字）；",
                "- 「Cosplay」「写真」「漫画预告」「Cos新图」这类分类词、「- COS团」「| MissKon」"
                "这类站名都不是名字；",
                "- 标题里没有人名（比如只有作品名或一句描述）就回 {\"names\": []}。",
            ]
        )
        prompt = "\n".join(lines)
        options: dict[str, Any] = {
            "prompt": prompt,
            "task_name": section.task or "utils",
            "temperature": 0,
            "max_tokens": 200,
        }
        if section.model:
            options["model_name"] = section.model
        started = time.monotonic()
        try:
            result = await asyncio.wait_for(
                self.ctx.call_capability(
                    "llm.generate", timeout_ms=self._TITLE_NAMES_TIMEOUT * 1000, **options
                ),
                timeout=self._TITLE_NAMES_TIMEOUT + 5,
            )
        except asyncio.TimeoutError:
            self.ctx.logger.warning(
                "标题名字抽取超时：%.1fs，退回规则", time.monotonic() - started
            )
            return []
        except Exception as error:  # noqa: BLE001 - 抽不动就退回规则，别拖住整单
            self.ctx.logger.warning(
                "标题名字抽取失败：耗时=%.1fs err=%s",
                time.monotonic() - started,
                str(error)[:80],
            )
            return []
        payload = result if isinstance(result, dict) else {"success": True, "response": str(result or "")}
        inner = payload.get("result")
        if isinstance(inner, dict) and ("response" in inner or "text" in inner):
            payload = {**payload, **inner}
        if not payload.get("success", True):
            self.ctx.logger.warning("标题名字抽取返回失败：%s", str(payload.get("error"))[:80])
            return []
        raw = str(payload.get("response") or payload.get("text") or "")
        wanted = self._parse_title_names(raw)
        titles = [self._name_key(str(item.title or "")) for item in rows]
        titles.extend(self._name_key(str(item.name or "")) for item in rows)
        picked: list[str] = []
        for name in wanted:
            text = str(name or "").strip()
            key = self._name_key(text)
            if not (2 <= len(text) <= 24) or not key:
                continue
            if handles.is_stop_word(text) or is_site_name(text, [str(item.url or "") for item in rows]):
                continue
            if not any(key and key in title for title in titles):
                self._vlog("标题名字：模型给的「%s」在标题里找不到，当幻觉丢掉", text)
                continue
            if text.lower() in {item.lower() for item in picked}:
                continue
            picked.append(text)
        if picked:
            self._vlog("标题名字：模型读了 %s 条标题，取到「%s」", len(rows), "、".join(picked))
        return picked

    @staticmethod
    def _parse_title_names(raw: str) -> list[str]:
        """解析模型那条答复：优先 JSON 的 names，其次逐行短名字。"""
        body = re.sub(r"^```[a-zA-Z]*|```$", "", str(raw or "").strip()).strip()
        names: list[str] = []
        match = re.search(r"\{.*\}", body, re.S)
        if match:
            try:
                data = json.loads(match.group(0))
            except (ValueError, TypeError):
                data = None
            if isinstance(data, dict):
                rows = data.get("names") if isinstance(data.get("names"), list) else []
                for item in rows:
                    if isinstance(item, str) and item.strip():
                        names.append(item.strip())
                if names or "names" in data:
                    return names
        for line in body.splitlines():
            item = line.strip().lstrip("-*•0123456789.、 ").strip()
            if item and not re.search(r"[:：{}\[\]\"]", item) and 2 <= len(item) <= 24:
                names.append(item)
        return names

    def _title_name_candidates(
        self,
        ordered: list[Candidate],
        extra_names: list[str] | None = None,
        blocked: set[str] | None = None,
        trusted: list[str] | None = None,
    ) -> list[str]:
        """候选标题里读出来的名字，给「按名字找号」那两条腿共用。

        这里只用规则，不问模型（模型那条腿是 `_llm_title_names()`，它的结果从
        ``extra_names`` 汇进来，排在规则抽到的名字前面）：`handles.title_author()`
        剥掉开头的分类标签后取第一个分隔符之前那段（「小仓千代w - W 泳装」→
        「小仓千代w」），候选自己的 ``name`` 字段也常常就是站方抄下来的作者名。
        图集站的分类词、站名、纯数字都在这里挡掉，免得拿「Cosplay」「hotgirlpix」
        去搜人。

        为什么这么判：c077 那单的图上没人、页面上也没账号，唯一还能用的事实是标题里
        那个名字。这一步只负责「有哪些名字可以查」，含不含中文、值不值得花检索额度
        交给两条腿各自判——微博只吃中文名，站外那条还认够长的英文名。
        """
        urls = [str(item.url or "") for item in ordered[:8]]
        found: list[str] = []
        seen: set[str] = set()
        # 已经核实过的 pixiv 作品名：它是作品标题，不是人名。9b22 那单把
        # 「永恒的新生(1)」当成人名去各平台搜，搜回来一个不相干的 Facebook 视频页；
        # 真正的作者是 pixiv 上的 `lol`，早就写在出处那一行里了。
        refused: set[str] = set()
        for item in blocked or set():
            text = str(item or "").strip()
            if not text:
                continue
            refused.add(text.lower())
            refused.add(
                re.sub(
                    r"\s*[(（]\s*\d{1,3}\s*(?:photos?|张|枚)?\s*[)）]\s*$",
                    "",
                    text,
                    flags=re.I,
                ).strip().lower()
            )

        def clean(value: str) -> str:
            """「Coser@小仓千代w: W泳装(21 photos)」→「小仓千代w」。

            这类标题拿冒号把人和作品名连起来，`handles.title_author()` 只认「 - 」
            这类分隔符，切完仍是一整句，上面「带空白就是句子」那道校验会把它丢掉。
            实测 `s-20261123-0034-19ab`（跟 c077 同一套图）：「小仓千代w」就在标题
            里，结果一个名字都没抽出来，微博那条腿白跑。
            """
            text = str(value or "").strip()
            text = re.split(r"[：:]", text, maxsplit=1)[0].strip()
            text = re.sub(
                r"^(?:coser|model|photographer)\s*@?\s*", "", text, flags=re.I
            )
            text = text.strip("@ ").strip()
            # 作品页标题尾部常挂着页码／张数（pixiv「永恒的新生(1)」、「(21 photos)」），
            # 那不是名字的一部分，留着会让搜出来的东西更离谱。
            text = re.sub(
                r"\s*[(（]\s*\d{1,3}\s*(?:photos?|张|枚)?\s*[)）]\s*$", "", text, flags=re.I
            ).strip()
            # 内容号／新闻标题常把名字挂在书名号里：「穿衣v.s脫衣哪個好？貧乳細腰
            # 《Mio Kuuko》白皙纖瘦體態別有一番…」。按「取开头那一截」切出来的是
            # 「穿衣v.s脫衣哪個好？貧乳細腰《Mio」——半句标题（411f 拿它去搜，一条
            # 都对不上），而真名就摆在书名号里。有引号就先取引号里的。
            quoted = _QUOTED_NAME.search(text)
            if quoted:
                inner = quoted.group(1).strip(" @")
                if 2 <= len(inner) <= 24 and not handles.is_stop_word(inner):
                    return inner
            # 只剩右半边括号的是被切掉的半句（「明日方舟》卫界大战缪缪分队超详细攻略」
            # 这种攻略标题），不是名字；9b22 的候选里就混进过这么一条。
            if re.search(r"[》」』】〉]", text) and not re.search(r"[《「『【〈]", text):
                return ""
            # 中文标题常把作品名直接跟在后头（「小仓千代w 明日方舟W泳装」「诗诗爱生活
            # 的动态」）：名字就是开头那一截。整条带去微博搜，一条都对不上（5832
            # 那单就是这样白跑一次）。
            if handles.has_cjk(text):
                head = re.split(r"\s+", text, maxsplit=1)[0]
                # 开头那一截里带问号、句号、逗号的是整句话，不是名字。
                if _SENTENCE_PUNCT.search(head):
                    return ""
                if 2 <= len(head) <= 24:
                    text = head
            # 「Cosplay]」「Photo]」这类是站点分类标签的残渣，不是名字（5832 那单
            # 就从某条候选的 name 字段里抽出过一个「Cosplay]」）。
            if handles.is_stop_word(text.strip("[]()【】（）〔〕 ")):
                return ""
            return text

        def add(value: str, *, exact: bool = False) -> None:
            text = clean(value)
            low = text.lower()
            if not (2 <= len(text) <= 24) or low in seen:
                return
            if low in refused or str(value or "").strip().lower() in refused:
                return
            if text.isdigit() or is_site_name(text, urls):
                return
            # 一整个句子不是名字：带空白或标点的多半是搬运工起的标题。从书名号里
            # 取出来的「Mio Kuuko」是例外——那本来就是两段的名字，不是一个句子。
            if any(char.isspace() for char in text) and not (
                exact or _quoted_exact(value, text)
            ):
                return
            seen.add(low)
            found.append(text)

        for item in ordered[:8]:
            # 只从核过图的候选里抽名字。SauceNAO 那些「相似度 44%」的插画页跟这张图
            # 没关系，它们只有标题、没有距离：19ab 那单就从一条 deviantart 候选里
            # 抽出「girlmachine」，白花一次检索额度，还翻了一个不相干的号 19 条帖
            # （同一条口径见 `_hunt_x_handle`：没量过距离的标题不算数）。
            if item.distance is None and item.status not in {"match", "vision"}:
                continue
            # pixiv 作品页的标题是作品名，不是人名（作者写在页面里、由出处那行给出）。
            # 9b22 就是从这里把「永恒的新生(1)」当成人名拿去各平台搜的。
            if _is_pixiv_artwork(str(item.url or "")):
                continue
            add(handles.title_author(str(item.title or "")))
            # 书名号里的名字单独捞一次：`title_author()` 见到前半句是整句话就整条
            # 丢掉，真名往往正好藏在那对书名号里（411f 的《Mio Kuuko》）。
            quoted = _QUOTED_NAME.search(str(item.title or ""))
            if quoted:
                add(quoted.group(1), exact=True)
            # 候选的 name 字段是站方填的「作者」，但整句标题也会被塞进来，
            # 所以同样要过上面那道长度与空白校验。
            add(str(item.name or ""))
        for name in trusted or []:
            # 模型给的名字是整串（「Mio Kuuko」这种两段名字带空格），走 exact 绕开
            # 「带空白就是句子」那道校验；它已经在 `_llm_title_names` 里核过「能在
            # 标题里原样找到」了。
            add(str(name or ""), exact=True)
        for name in extra_names or []:
            add(str(name or ""))
        return found

    # 同名号最多往下翻三个：微博按相关度排，正主常常不在第一个（第一个常是
    # 「只包含关键词」的重名号），只看 rows[0] 等于白搜。翻一个号十几秒，
    # 再往后翻收益很小。
    _WEIBO_SAME_LIMIT = 3

    async def _weibo_pick_same_name(
        self,
        name: str,
        rows: list[dict[str, Any]],
        *,
        floor: int,
        by_title: bool,
        source: str,
        target_hash: int | None,
    ) -> dict[str, Any]:
        """同名号里挑一个：前面几个都翻带图帖，谁自己的帖子里有这张图就是谁。

        以前只看 `rows[0]`：微博搜索结果按相关度排，正主未必排第一。c077 那种
        情况第一个多半是「小仓千代空水」这类只包含关键词的路人号，正主排在第二
        就永远翻不到。现在前三个都过一遍，判据还是图——昵称对不上、粉丝不够的
        也照样翻帖，图对上了才算她。

        返回的 dict 直接进 `outcome.weibo`：`status=hit` 是找到，`none` 是搜过
        但没有像样的号（带上最多粉那个，好让文案说清「同名的都是路人号」）。
        """
        weak: dict[str, Any] | None = None

        def remember(followers: int, account: str, url: str) -> None:
            nonlocal weak
            if weak is not None and (weak.get("max_fans") is None or followers <= int(weak["max_fans"])):
                return
            weak = {
                "status": "none",
                "kind": "name",
                "query": name,
                "max_fans": followers,
                "top_name": account,
                "url": url,
                "source": source,
                "min_fans": floor,
            }

        for top in rows[: self._WEIBO_SAME_LIMIT]:
            account = str(top.get("name") or "")
            followers = int(top.get("followers") or 0)
            url = str(top.get("url") or "")
            # 从标题里读出来的名字必须昵称也对得上才认。微博搜索按相关度排，
            # 「小仓千代w」能搜出「小仓千代空水」这种只是包含关系的重名号，
            # 粉丝够门槛时只报粉丝数就成了误报（c077 那单就靠这一条把路人号挡掉）。
            # 水印里写着的号不走这道：那是用户自己看到的写法，照旧只比粉丝数。
            if by_title and not identity.weibo_name_matches(name, account):
                self._vlog("微博反查：%s 搜到的「%s」昵称对不上，只当路人号", name, account)
                remember(followers, account, url)
                continue
            row = {
                "status": "hit",
                "kind": "name",
                "query": name,
                "uid": str(top.get("uid") or ""),
                "name": account,
                "followers": followers,
                "url": url,
                "source": source,
                "min_fans": floor,
            }
            if followers >= floor:
                self._vlog(
                    "微博反查：%s → %s（粉丝 %s，来源 %s）", name, account, followers, source
                )
                return await self._with_weibo_post(row, target_hash)
            # 粉丝少不等于不是本人：素人号、小号几十粉很常见。这里让图说话——
            # 翻她最近的带图帖比指纹，对上了就是实锤，落到那条微博上；对不上
            # 才按同名路人号处理（s-20261117-0741-4631 就是 98 粉被门槛挡回去，
            # 结果只留了一句「低于门槛」，人其实还在那儿）。
            proven = await self._with_weibo_post(row, target_hash)
            if proven.get("post"):
                self._vlog(
                    "微博反查：%s 的「%s」只有 %s 粉，但她自己的帖子里有这张图（距离 %s）",
                    name,
                    account,
                    followers,
                    proven.get("distance"),
                )
                return proven
            self._vlog(
                "微博反查：%s 的「%s」只有 %s 粉（门槛 %s），翻了她的带图帖也没这张",
                name,
                account,
                followers,
                floor,
            )
            remember(followers, account, url)
        return weak or {
            "status": "none",
            "kind": "name",
            "query": name,
            "max_fans": None,
            "source": source,
            "min_fans": floor,
        }

    async def _hunt_weibo(
        self,
        ordered: list[Candidate],
        vision_info: dict[str, Any] | None,
        grok_info: dict[str, Any] | None,
        target_hash: int | None = None,
        *,
        best: Candidate | None = None,
        extra_names: list[str] | None = None,
    ) -> dict[str, Any] | None:
        """读到微博号时去微博站内搜一遍，把主页和粉丝数当线索留下来。

        以前只核 X：水印读成「@青生w」这种中文昵称，抽取函数直接丢掉；Grok 读成
        「微博@萌生w」也只留在文案里，没往下查。这里补的就是这一步——微博号落地
        成「昵称 + 粉丝数 + 主页链接」，让用户能自己点开看。

        只当线索：不改 best、不参与匹配判定。粉丝数低于门槛的同名号不写进结果，
        否则搜「青生w」能捞出 15 个「阮青生w*」。
        """
        section = self.config.weibo
        if not section.enabled:
            return None
        vision_info = vision_info or {}
        grok_info = grok_info or {}
        seal = str(vision_info.get("watermark") or "").strip()
        seal_text = str(vision_info.get("watermark_text") or "").strip()
        grok_seal = str(grok_info.get("watermark") or "").strip()
        seal_texts = [text for text in (seal, seal_text, grok_seal) if text]
        title_texts = [
            self._name_text(item) for item in ordered[:8] if self._name_text(item)
        ]
        targets = handles.weibo_targets(seal_texts + title_texts, limit=3)
        uids = [value for kind, value in targets if kind == "uid"]
        names = [value for kind, value in targets if kind == "name"]
        # 水印里含中文的 @昵称不可能是 X 号（X 只认 ASCII），只可能是微博这类
        # 中文平台的昵称，所以直接排进微博候选。
        for name in handles.at_names(seal_texts, limit=4):
            if handles.has_cjk(name) and name not in names:
                names.append(name)
        # 水印那个号在 X 上查无此人时，同样去微博试一次：同一个人常常两个平台
        # 用一个昵称，X 上没这个名字不代表微博上也没有。
        for item in vision_info.get("watermark_readings") or []:
            if not isinstance(item, dict):
                continue
            if str(item.get("state") or "") != fxtwitter.PROFILE_MISSING:
                continue
            handle = str(item.get("handle") or "").strip()
            if handle and handle not in names:
                names.append(handle)
        if not uids and not names:
            # 水印上写着「© Ryomky」这种署名时，既没有 @、也不是微博写法，上面两步
            # 都抽不到东西。可那常常就是本人的昵称——s-20261113-1402-6a76 的正主
            # 就是 Ryomky 的微博。只在别的线索一个都没有时才拿它去试，免得平白
            # 多搜一遍、多出一堆同名的路人号。
            for text in seal_texts:
                for handle in vision.handles_from_watermark(text, limit=2):
                    if handle not in names:
                        names.append(handle)
        # 标题里只写着名字、没写「微博」时，这个名字一样值得去微博搜一次。
        # c077（s-20261122-2219-c077）的图只落在 hotgirlpix / misskon 这类图集站，
        # 页面一个社媒账号都不挂；标题里明明写着「小仓千代w」，旧逻辑只拿它去 X
        # 按显示名核对（她的 X 显示名不是这串中文），于是整单收在「只找到别的网站
        # 的转发」。中文昵称在 X 上本来就查无此号，微博才是它的主场。
        #
        # 只在「结论还没落到具体账号上」时跑：已经有指纹对上的账号结论，说明人
        # 已经知道了，再搜一遍只是白花三十秒。
        by_title: list[str] = []
        posed_account = bool(
            best is not None and best.status == "match" and (best.handle or "").strip()
        )
        allow_by_name = bool(getattr(section, "by_name", True)) and not posed_account and (
            best is None
            or best.status != "match"
            # 指纹对上了、可落点是搬运站 / 图集站：那页只是转贴，人还没找到。
            # c077 的结论就是 `hotgirlpix.com/collection/coser` 这种站点合集页。
            or is_repost_site(best.url or "", best.platform or "")
        )
        if allow_by_name:
            for name in self._title_name_candidates(ordered, extra_names or []):
                # 微博昵称就是账号名，纯 ASCII 的名字 X 那边已经按显示名核过，
                # 这里只补中文名——那才是 X 搜不到的写法。
                if not handles.has_cjk(name) or name in names:
                    continue
                names.append(name)
                by_title.append(name)
        # 站点名不是人名：水印写着 `PixiBB.COM`、`MissKON.com` 时，上面那道兜底会
        # 把站名当昵称递进来，拿去微博搜纯属白跑（19ab 那单就搜了一次「PixiBB」）。
        site_urls = [str(item.url or "") for item in ordered[:8]]
        names = [name for name in names if not is_site_name(name, site_urls)]
        if not uids and not names:
            return None

        def source_of(name: str) -> str:
            if name and name in seal:
                return "水印"
            if name and name in grok_seal:
                return "Grok 读数"
            if name and name in seal_text:
                return "水印（候选图）"
            if name and name in by_title:
                return "候选标题里的名字"
            return "候选标题"

        floor = int(section.min_fans or 0)
        weaker: dict[str, Any] | None = None
        for uid in uids[:2]:
            info = await self._weibo_profile(uid)
            if not info:
                continue
            followers = int(info.get("followers") or 0)
            row = {
                "status": "hit",
                "kind": "uid",
                "query": uid,
                "uid": uid,
                "name": str(info.get("name") or ""),
                "followers": followers,
                "url": f"https://weibo.com/u/{uid}",
                "verified": str(info.get("verified") or ""),
                "source": "候选链接",
                "min_fans": floor,
            }
            self._vlog("微博反查：UID %s → %s（粉丝 %s）", uid, row["name"], followers)
            return await self._with_weibo_post(row, target_hash)
        for name in names[:2]:
            try:
                rows = await self._search_weibo_users(name)
            except Exception as error:  # noqa: BLE001 - 查不到不影响已有结论
                text = str(error)
                self._vlog("微博反查失败（%s）：%s", name, text[:80])
                # 填了登录 cookie 却收到 401/403/432，多半是那串 cookie 过期（约 7 天）。
                # 这时要明说一句，否则整条微博反查会静默变成「什么都没搜到」。
                if self._weibo_cookie() and any(
                    code in text for code in ("401", "403", "432")
                ):
                    return {"status": "error", "reason": "cookie", "query": name}
                continue
            source = source_of(name)
            if rows:
                candidate = await self._weibo_pick_same_name(
                    name,
                    rows,
                    floor=floor,
                    by_title=name in by_title,
                    source=source,
                    target_hash=target_hash,
                )
                if candidate.get("status") == "hit":
                    return candidate
            else:
                candidate = {
                    "status": "none",
                    "kind": "name",
                    "query": name,
                    "max_fans": None,
                    "source": source,
                    "min_fans": floor,
                }
                self._vlog("微博反查：%s 微博上一个同名的都没有", name)
            if weaker is None or (
                candidate.get("max_fans") is not None
                and (weaker.get("max_fans") is None or candidate["max_fans"] > weaker["max_fans"])
            ):
                weaker = candidate
        return weaker

    # 转载页反查的上限：抓两页、每页最多核 3 条帖、每条最多比 3 张配图。
    # 这一路只在「还没找到可靠出处」时才跑，一次几十秒，不能拖成几分钟。
    _WEIBO_REPOST_MAX_PAGES = 2
    _WEIBO_REPOST_MAX_POSTS = 3
    _WEIBO_REPOST_MAX_PICS = 3
    _WEIBO_TIMELINE_POSTS = 20
    # 水印昵称/署名那一路最多扫多少条帖：一条帖最多比 3 张图，扫 12 条就是上限 36 次
    # 下载。不能再多——这一步在「还没结论」的单子上每次都要跑。
    _WEIBO_CLUE_MAX_POSTS = 12

    async def _fetch_page_html(self, url: str) -> str:
        """抓一页 HTML（给转载页反查用）。抓不到返回空串，不影响已有结论。"""

        def fetch() -> str:
            session = make_session(self._proxy())
            try:
                response = session.get(
                    url,
                    timeout=12,
                    headers={
                        "User-Agent": (
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/131.0 Safari/537.36"
                        ),
                        "Accept": "text/html,application/xhtml+xml",
                    },
                )
                if response.status_code != 200:
                    return ""
                # 不能直接用 response.text：响应头没声明 charset 时 requests 按
                # latin-1 解，中文标题会变成 ``Cosæ­£ç``，拿这串当搜索词等于白搜。
                return decode_body(
                    response.content, response.headers.get("Content-Type", "")
                )[:400000]
            finally:
                session.close()

        try:
            return await asyncio.wait_for(asyncio.to_thread(fetch), timeout=16)
        except Exception:  # noqa: BLE001 - 页面读不到只少一条线索
            return ""

    async def _weibo_pic_hit(
        self,
        pic_ids: object,
        url: str,
        target_hash: int | None,
    ) -> int | None:
        """拿一条微博帖的配图比指纹，命中返回距离，没命中返回 None。

        只比前几张：微博一条帖常带九宫格，水印和构图最靠前的通常就是主图。
        """
        if target_hash is None:
            return None
        threshold = int(self.config.plugin.hash_threshold)
        section = self.config.plugin
        for pic_url in weibopost.large_pic_urls(pic_ids, limit=self._WEIBO_REPOST_MAX_PICS):  # type: ignore[arg-type]
            candidate = Candidate(engine="weibo", url=url, image_url=pic_url)
            try:
                checked = await asyncio.to_thread(
                    verify_candidate,
                    candidate,
                    target_hash,
                    threshold,
                    self._proxy(),
                    section.download_timeout,
                    self._fake_ip_nets(),
                )
            except Exception:  # noqa: BLE001 - 单张取不到就试下一张
                continue
            if checked.distance is not None and checked.distance <= threshold:
                return int(checked.distance)
        return None

    async def _weibo_author_post(
        self,
        author: str,
        target_hash: int | None,
    ) -> dict[str, Any] | None:
        """转载页上写着微博作者名：核到这个号，翻她最近的带图帖比指纹。

        比搜帖更便宜也更准——名字是页面自己标的，不依赖微博有没有把那条帖索引到
        检索结果里。翻不到不返回 hit，交给搜帖那条路。
        """
        try:
            rows = await self._search_weibo_users(author)
        except Exception as error:  # noqa: BLE001 - 查不到就换搜帖那条路
            self._vlog("转载页作者 %s 搜号失败：%s", author, str(error)[:60])
            return None
        if not rows:
            return None
        top = rows[0]
        if not identity.weibo_name_matches(author, str(top.get("name") or "")):
            return None
        uid = str(top.get("uid") or "")
        if not uid:
            return None
        hit = await self._weibo_uid_post(
            uid, str(top.get("name") or ""), int(top.get("followers") or 0), target_hash
        )
        if not hit:
            return None
        hit["kind"] = "author"
        return hit

    async def _weibo_uid_post(
        self,
        uid: str,
        name: str,
        followers: int,
        target_hash: int | None,
    ) -> dict[str, Any] | None:
        """翻某个微博号最近的带图帖，找指纹对得上的那张。

        名字只负责找到人，认人仍然靠图：微博同名的号太多，昵称对得上不等于就是她。
        翻到就返回命中信息（帖地址 + 距离 + 配图），翻不到返回 None。
        """
        if not uid or target_hash is None:
            return None
        try:
            posts = await asyncio.to_thread(
                identity.fetch_weibo_timeline,
                uid,
                self._proxy(),
                18,
                self._weibo_cookie(),
                1,
                self._WEIBO_TIMELINE_POSTS,
            )
        except Exception as error:  # noqa: BLE001 - 翻不到帖就换别的路
            self._vlog("微博 UID %s 翻帖失败：%s", uid, str(error)[:60])
            return None
        for post in posts[: self._WEIBO_CLUE_MAX_POSTS]:
            if not post.get("pic_ids"):
                continue
            distance = await self._weibo_pic_hit(
                post.get("pic_ids"), str(post.get("url") or ""), target_hash
            )
            if distance is None:
                continue
            self._vlog(
                "微博帖核对：%s（UID %s）的帖子 %s 指纹距离 %s",
                name or uid,
                uid,
                post.get("url"),
                distance,
            )
            pictures = weibopost.large_pic_urls(post.get("pic_ids"), limit=1)
            return {
                "status": "hit",
                "uid": uid,
                "name": name,
                "followers": followers,
                "url": str(post.get("url") or ""),
                "distance": distance,
                "image": pictures[0] if pictures else "",
            }
        return None

    async def _hunt_role_hint(
        self, upload: bytes, candidates: list[Candidate] | None = None
    ) -> dict[str, Any] | None:
        """图搜图没结论时，按「这是哪个角色」去 X 上找同角色的 coser。

        只给线索，不判定谁发过这张图：角色对得上不等于人是同一个，认人仍然靠指纹。
        一次调用、一条硬预算，超时或失败都只是少一行线索。
        """
        section = self.config.grok
        if not bool(getattr(section, "role_hint", True)):
            return None
        if not bool(getattr(section, "enabled", False)):
            return None
        key = str(getattr(section, "api_key", "") or "").strip()
        base = str(getattr(section, "base_url", "") or "").strip().rstrip("/")
        if not key or not base:
            return None
        try:
            compact, _ = prepare_upload(upload, limit=900_000, as_jpeg=True)
        except Exception as error:  # noqa: BLE001 - 图处理失败就少这一路
            self._vlog("角色线索：原图处理失败 %s", str(error)[:60])
            return None
        data_url = "data:image/jpeg;base64," + base64.b64encode(compact).decode("ascii")
        # 候选标题里常写着角色名（「死芒 Necrass」「遐蝶 Castorice」），一起喂给它，
        # 免得它裸看图认成别的角色——认错一次，后面 8 个账号全是别人。
        hints = grok.build_hints(grok.social_first(candidates or []))
        models = grok.model_chain(section.model, getattr(section, "models", None))
        seconds = max(15, int(getattr(section, "role_timeout", 60)))
        # 整条线的硬预算：中转站经常第一个模型就挂，逐个等满会把整单拖到十几分钟。
        deadline = time.monotonic() + seconds + 30
        proxy = self._proxy()
        started = time.monotonic()

        def call(body: dict, timeout: int) -> dict:
            session = make_session(proxy, headers={"Authorization": f"Bearer {key}"})
            try:
                response = session.post(f"{base}/responses", json=body, timeout=timeout)
                if response.status_code != 200:
                    raise HttpError(f"HTTP {response.status_code}：{response.text[:120]}")
                return response.json()
            finally:
                session.close()

        payload: dict | None = None
        model = models[0]
        for name in models:
            left = deadline - time.monotonic()
            if left < 10:
                self._vlog("角色线索：预算用完，剩下的模型不再试")
                break
            wait = min(seconds, int(left))
            try:
                payload = await asyncio.wait_for(
                    asyncio.to_thread(
                        call,
                        grok.build_role_payload(name, data_url, hints=hints),
                        wait,
                    ),
                    timeout=wait + 5,
                )
                model = name
                break
            except asyncio.TimeoutError:
                self._vlog("角色线索：%s 等满 %s 秒没回，换下一个", name, wait)
            except Exception as error:  # noqa: BLE001 - 一个模型失败不代表这条线没救
                self._vlog("角色线索：%s 没成（%s）", name, str(error)[:60])
        if payload is None:
            self._vlog("角色线索：这次没问到（试过 %s）", "、".join(models))
            return None
        hint = grok.parse_role_output(payload, model=model)
        if hint.error:
            self._vlog("角色线索：解析失败 %s", hint.error)
            return None
        self._vlog(
            "角色线索：角色=%s 账号=%s 耗时=%.1fs %s",
            "、".join(hint.roles) or "未知",
            [item.get("handle") for item in hint.accounts][:6],
            time.monotonic() - started,
            grok.dumps_role(hint),
        )
        if not hint.ok:
            return None
        return {
            "roles": hint.roles,
            "accounts": hint.accounts,
            "keywords": hint.keywords,
            "model": hint.model,
            "reason": hint.reason,
        }

    async def _hunt_weibo_repost(
        self,
        ordered: list[Candidate],
        target_hash: int | None,
    ) -> dict[str, Any] | None:
        """转载页 → 微博原帖：页面标了作者就核她，没标就拿正文去搜帖，最后比指纹。

        `s-20261113-1250-26a8` 的正确答案是 G44不会受伤 的微博，而候选里只有一条
        `sina.cn` 的转载页（距离 11）。那页正文与微博原文一字不差，页头还写着
        「G44不会受伤 / 微博认证：动漫博主」——两条路都能通，命中与否只看配图
        指纹对不对得上。

        只当结论用硬证据：搜帖只负责找人，认人仍然靠图。对不上就什么都不改。
        """
        section = self.config.weibo
        if not bool(getattr(section, "enabled", True)):
            return None
        if not bool(getattr(section, "repost_search", True)):
            return None
        if not self._weibo_cookie():
            return None
        pages = [
            item
            for item in ordered
            if weibopost.is_weibo_repost_page(item.url or "", item.platform or "")
        ]
        if not pages:
            return None
        pages.sort(key=lambda item: (item.distance if item.distance is not None else 999, item.rank))
        for page in pages[: self._WEIBO_REPOST_MAX_PAGES]:
            html = await self._fetch_page_html(page.url or "")
            if not html:
                continue
            title, body = weibopost.page_text(html)
            author = weibopost.page_author(body)
            self._vlog(
                "转载页反查：%s 标题=%s 作者=%s",
                weibopost.host_of(page.url or ""),
                (title or "无")[:40],
                author or "无",
            )
            if author:
                hit = await self._weibo_author_post(author, target_hash)
                if hit:
                    return hit
            for query in weibopost.post_queries(title, body, author):
                try:
                    posts = await asyncio.to_thread(
                        identity.search_weibo_posts,
                        query,
                        self._proxy(),
                        20,
                        self._weibo_cookie(),
                        self._WEIBO_REPOST_MAX_POSTS,
                    )
                except Exception as error:  # noqa: BLE001 - 一次搜不到就试下一条检索词
                    self._vlog("转载页搜帖失败（%s）：%s", query, str(error)[:60])
                    continue
                self._vlog("转载页搜帖：q=%s → %s 条", query, len(posts))
                for post in posts:
                    distance = await self._weibo_pic_hit(
                        post.get("pic_ids"), str(post.get("url") or ""), target_hash
                    )
                    if distance is None:
                        continue
                    self.ctx.logger.info(
                        "转载页反查命中：%s（%s）指纹距离 %s",
                        post.get("name"),
                        post.get("url"),
                        distance,
                    )
                    return {
                        "status": "hit",
                        "kind": "post",
                        "uid": str(post.get("uid") or ""),
                        "name": str(post.get("name") or ""),
                        "url": str(post.get("url") or ""),
                        "distance": distance,
                        "query": query,
                        "image": weibopost.large_pic_urls(post.get("pic_ids"), limit=1)[0],
                    }
        return {"status": "miss"}

    def _promote_weibo_post(self, best: Candidate | None, info: dict[str, Any]) -> Candidate:
        """把微博那条帖变成结论（转载页反查、水印昵称反查都走这里）。"""
        distance = int(info.get("distance") or 0)
        name = str(info.get("name") or "")
        uid = str(info.get("uid") or "")
        lead = str(info.get("note") or "").strip()
        note = f"{lead}，配图指纹距离 {distance}" if lead else f"配图指纹距离 {distance}，本人发过这张"
        post = Candidate(
            engine="weibo_post",
            url=str(info.get("url") or ""),
            image_url=str(info.get("image") or ""),
            distance=distance,
            status="match",
            note=note,
            platform="微博",
            name=name,
            handle=uid,
        ).finalize()
        if not post.title:
            post.title = f"微博 {name}".strip()
        self._vlog("微博结论：换成 %s 的帖 %s（距离 %s）", name or uid, post.url, distance)
        return post

    async def _search_x_handles(self, name: str, limit: int = 3) -> list[str]:
        """昵称 → 疑似 X 号，跑一次网页检索。

        先用 SerpAPI 那几把 key（lens 池）；它那边全都不可用时，如果 Serper 配了
        key 就换 Serper 顶上。两家都是 Google 后端，搜到的结果一样，所以只要有一家
        跑通就收工——返回空也认，不拿另一家再花一次额度。``limit`` 放宽之后，
        handle 里带着这个名字的号会排在最前面（不再被同名的无关大号挤掉）。
        """
        found: list[str] = []
        for pool_name in ("lens", "serper"):
            pool = self._pools.get(pool_name)
            if not pool:
                continue
            ran = False
            for _ in range(2):
                key = pool.acquire()
                if not key:
                    break

                def fetch(api_key: str = key) -> list[str]:
                    return identity.search_x_handles(
                        name, api_key, self._proxy(), limit=limit, prefer=name
                    )

                try:
                    found = await asyncio.wait_for(asyncio.to_thread(fetch), timeout=40)
                except Exception as error:  # noqa: BLE001 - 搜不到就当没有这一步
                    text = str(error)
                    if "429" in text or "401" in text or "403" in text:
                        # 一个 key 的额度用完就换下一个，不然这一步天天静默失败。
                        pool.mark_failure(key, 3600 if "429" in text else None)
                        self._vlog(
                            "按名字搜 X（%s）：key 额度不可用（%s），换下一个",
                            pool_name,
                            text[:40],
                        )
                        continue
                    self._vlog("按名字搜 X（%s）失败：%s", pool_name, text[:80])
                    found = []
                    break
                pool.mark_success(key)
                ran = True
                break
            if ran:
                break
        self._vlog("按名字搜 X：%s → %s", name, "、".join(found) or "没给出号")
        return found

    async def _hunt_x_from_name(
        self, best: Candidate | None, upload: bytes | None
    ) -> dict[str, Any] | None:
        """结论只到「同一个人」、落点又认不了图时，顺着名字去 X 上核这个号。

        微博这类站给不了原帖：候选只有 UID，视觉翻来覆去也只能说「同一个人」。
        这时把 UID 换成昵称，拿昵称检索一次 X 账号，逐个翻它们自己的带图帖比
        指纹——命中就把结论换成那条帖子，没命中就只留一句「找到号了、最近没发
        这张」，绝不因为名字像就改结论。
        """
        uid = self._weibo_uid(best)
        if not uid or not upload:
            return None
        weibo = await self._weibo_profile(uid)
        if not weibo:
            return {"status": "none", "name": "", "uid": uid, "reason": "微博资料读不到"}
        name = str(weibo.get("name") or "")
        handles_found = await self._search_x_handles(name)
        if not handles_found:
            return {"status": "none", "name": name, "weibo": weibo, "reason": "检索没给出 X 号"}
        seen: dict[str, Any] | None = None
        for handle in handles_found[: self._X_FROM_NAME_LIMIT]:
            info = await self._profile(handle)
            if not info:
                continue
            x_name = str(info.get("name") or "")
            followers = int(info.get("followers") or 0)
            if not (
                identity.name_matches(name, x_name)
                or identity.name_matches(name, handle)
            ):
                self._vlog("按名字搜到的 @%s（%s）跟微博昵称对不上，丢掉", handle, x_name or "-")
                continue
            resolved = str(info.get("screen_name") or handle)
            self._vlog(
                "按名字对上 X：@%s（%s，粉丝 %s），翻带图帖比指纹",
                resolved,
                x_name or "-",
                followers,
            )
            hit = await self._media_lookup(resolved, upload)
            if hit and hit.get("url"):
                return {
                    "status": "hit",
                    "name": name,
                    "weibo": weibo,
                    "handle": resolved,
                    "x_name": x_name,
                    "followers": followers,
                    "post": str(hit.get("url") or ""),
                    "distance": int(hit.get("distance") or 0),
                }
            seen = {
                "status": "seen",
                "name": name,
                "weibo": weibo,
                "handle": resolved,
                "x_name": x_name,
                "followers": followers,
                "reason": "最近十几条带图帖里没这张"
                if not (hit and hit.get("error"))
                else "这次没翻成它的带图帖",
            }
        return seen or {
            "status": "none",
            "name": name,
            "weibo": weibo,
            "reason": "搜到的号跟昵称对不上",
        }

    def _promote_x_identity(self, best: Candidate | None, info: dict[str, Any]) -> Candidate | None:
        """按名字换到的号自己发过这张图时，那条帖子就是结论。

        证据是「这个号自己的带图帖里指纹对上」，比「视觉觉得是同一个人」硬，
        所以直接升成 match；换不到就原样退回，不碰已有结论。
        """
        post = str(info.get("post") or "")
        handle = str(info.get("handle") or "").strip().lstrip("@")
        if not post or not handle:
            return best
        distance = int(info.get("distance") or 0)
        self._vlog("按名字换号命中，升成结论：@%s %s（距离 %s）", handle, post, distance)
        return Candidate(
            engine="identity",
            url=post,
            platform="X (Twitter)",
            handle="@" + handle,
            name=str(info.get("x_name") or ""),
            distance=distance,
            status="match",
            note=f"按微博昵称找到 X @{handle}：它自己的带图帖里有这张图（距离 {distance}）",
        )

    # 标题里最多认三个署名、最多翻三个号的带图帖。每次翻帖都是十来条候选图
    # 下载比指纹，成本不低，而这些标题里真正写着作者名的通常只有一条。
    _SIGNATURE_NAME_LIMIT = 3
    _SIGNATURE_PROBE_LIMIT = 3
    # 按名字搜 X 一次最多取几个候选：以前只取 3 个，7496 那单真号 `kuru_xox`
    # 就被三个同名的无关大号挤掉了。放宽到 8，靠 handle 命中优先排序顶着，
    # 真正翻帖的仍然只有 _SIGNATURE_PROBE_LIMIT 个，不额外多花时间。
    _SIGNATURE_SEARCH_LIMIT = 8

    @staticmethod
    def _watermark_hints(vision_info: dict[str, Any] | None) -> list[dict[str, Any]]:
        """图上读出来、并且已经核到存在的号，按读出顺序去重。

        给两处用：标题署名那条线拿它纠写法，以及「水印 + 署名」的交叉验证。
        只收 `state == "ok"` 的——查无此号的写法拿去做交叉验证只会把错号钉死。
        """
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in (vision_info or {}).get("watermark_readings") or []:
            if not isinstance(item, dict):
                continue
            handle = str(item.get("handle") or "").strip().lstrip("@")
            if not handle or handle.lower() in seen:
                continue
            seen.add(handle.lower())
            out.append(
                {
                    "handle": handle,
                    "state": str(item.get("state") or ""),
                    "user_id": str(item.get("user_id") or ""),
                    "name": str(item.get("name") or ""),
                    "post": str(item.get("post") or ""),
                }
            )
        primary = str((vision_info or {}).get("watermark_handle") or "").strip().lstrip("@")
        if primary and primary.lower() not in seen:
            out.insert(
                0,
                {
                    "handle": primary,
                    "state": str((vision_info or {}).get("watermark_state") or ""),
                    "user_id": str((vision_info or {}).get("watermark_user_id") or ""),
                    "name": str((vision_info or {}).get("watermark_name") or ""),
                    "post": str((vision_info or {}).get("watermark_post") or ""),
                },
            )
        return out

    async def _hunt_signature_author(
        self,
        ordered: list[Candidate],
        upload: bytes | None,
        hints: list[str] | None = None,
    ) -> dict[str, Any] | None:
        """顺着标题里的署名（``by XXX``、``coser: XXX``、``@XXX``）去 X 上找本人。

        搬运帖常把原作者名连动词一起抄进标题，那是整条标题里唯一指向本人的部分。
        插件原来只从候选 URL 里抽 handle：s-20261112-0752-4fdf 抽到 miya1st 和
        _ray_wing_ 两个都不对，而标题写着「Vivian cosplay by KURU」，KURU 完全没
        用上，于是停在 reddit 的转载页上。

        ``hints`` 是图上读出来的号（水印）：它跟署名只差一个字母时（7496 的
        ``kufu_xox`` 与 ``KURU``），直接按署名改写回来先试——比按名字重新搜一遍
        准，也比盲改字母省事。

        找到号还不够：必须那个号自己的带图帖里真有这张图才算数，翻不到就当没有。
        reddit 这类搬运页也走这条路——它的标题是别人在说话，不能拿来自由抽词，但
        署名声明确实常写在那儿；翻不到那张图就什么都不提，不留「疑似」。
        """
        if not upload:
            return None
        rows = sorted(
            (
                item
                for item in ordered[:12]
                if (item.title or item.name)
                and item.status != "mismatch"
                # 这里只排页面主人型站点（skeb 的接稿页、电商页），reddit 这类
                # 搬运页反而要留着：正经转载常把原作者署名抄在标题里。
                and not handles.is_signature_blocked(item.source)
            ),
            key=self._x_hunt_priority,
        )
        if not rows:
            return None
        # 只要有候选真的比过图，署名就只从那几条里抽。没量过距离的页面（unverified、
        # 拿不到主图的 suspect）根本没确认过「这页上有这张图」，它的标题属于另一张
        # 图或同一题材的另一篇——411f 那单就是这样从四条 danbooru 插画页上抽出
        # 「nemo」，一路查到 @NemoNevermore_ 摆到首屏，跟本图半点关系没有。
        compared = [
            item
            for item in rows
            if item.status in {"match", "vision"} or item.distance is not None
        ]
        if compared:
            rows = compared
        texts = [self._name_text(item) for item in rows]
        names = handles.signature_names(texts, limit=self._SIGNATURE_NAME_LIMIT)
        # 标题里直接贴了原帖链接的，比「cosplay by KURU」还明确：链接指着谁就是谁，
        # 不用再猜名字。正文抓不到（reddit 403），标题里的链接是唯一能拿到的那份。
        linked = handles.link_handles(texts, limit=self._SIGNATURE_SEARCH_LIMIT)
        if linked:
            self._vlog("标题里的原帖链接：%s", "、".join("@" + item for item in linked))
            known = {name.lower() for name in names}
            names = [*names, *(item for item in linked if item.lower() not in known)]
        if not names:
            return None
        self._vlog("标题署名：%s", "、".join(names))
        seen_handles = {
            str(item.handle or "").strip().lstrip("@").lower()
            for item in ordered
            if (item.handle or "").strip()
        }
        probes = 0
        seen_note = ""
        seen_info: dict[str, Any] | None = None
        anchors = [
            item
            for item in (hints or [])
            if isinstance(item, dict) and str(item.get("handle") or "").strip()
        ]
        typed = [str(item.get("handle")).strip().lstrip("@") for item in anchors]
        for name in names:
            if probes >= self._SIGNATURE_PROBE_LIMIT:
                break
            # 署名优先：先拿水印号按署名纠一遍（kufu_xox → kuru_xox），再按名字搜。
            corrected = [
                fixed
                for handle in typed
                for fixed in handles.signature_fix(handle, [name])
            ]
            if corrected:
                self._vlog(
                    "署名纠写法：%s → %s",
                    "、".join("@" + item for item in typed[:2]),
                    "、".join("@" + item for item in corrected[:3]),
                )
            searched = await self._search_x_handles(
                name, limit=self._SIGNATURE_SEARCH_LIMIT
            )
            candidates = corrected + [item for item in searched if item]
            # 署名本身也可能是 handle（标题里直接写 @kuru_xox 的那种）。
            candidates.append(name)
            tried: set[str] = set()
            for handle in candidates:
                key = str(handle or "").strip().lstrip("@")
                low = key.lower()
                if not key or low in tried or low in seen_handles:
                    continue
                tried.add(low)
                info = await self._profile(key)
                if not info:
                    continue
                x_name = str(info.get("name") or "")
                resolved = str(info.get("screen_name") or key)
                if not (
                    identity.name_matches(name, x_name)
                    or identity.name_matches(name, resolved)
                ):
                    self._vlog("署名 %s 搜到的 @%s（%s）对不上，丢掉", name, resolved, x_name or "-")
                    continue
                if probes >= self._SIGNATURE_PROBE_LIMIT:
                    break
                probes += 1
                self._vlog(
                    "署名对上 X：%s → @%s（%s，粉丝 %s），翻带图帖比指纹",
                    name,
                    resolved,
                    x_name or "-",
                    info.get("followers") or 0,
                )
                holder: dict[str, Any] = {}
                await self._check_watermark_post(resolved, upload, holder)
                post = str(holder.get("watermark_post") or "")
                if post:
                    source = next(
                        (item for item in rows if name.lower() in self._name_text(item).lower()),
                        None,
                    )
                    return {
                        "status": "hit",
                        "name": name,
                        "handle": resolved,
                        "x_name": x_name,
                        "followers": int(info.get("followers") or 0),
                        "post": post,
                        "distance": int(holder.get("watermark_distance") or 0),
                        "source_url": source.url if source is not None else "",
                        "source_title": " ".join(
                            (source.title or source.name or "").split()
                        )[:60]
                        if source is not None
                        else "",
                    }
                matched_rows = [
                    item for item in rows if name.lower() in self._name_text(item).lower()
                ]
                # 同一名字可能既出现在 reddit 标题里、也出现在图集站标题里：只要后者
                # 在，就按后者算来源，别因为 reddit 那条把整条线索丢掉。
                source = next(
                    (
                        item
                        for item in matched_rows
                        if not handles.is_signature_only(item.source)
                    ),
                    None,
                )
                # reddit 这类搬运页的署名只当「找号的入口」：她自己帖子里没有这张图，
                # 就什么都不提。否则又变成「给个名字让你自己去猜」——那正是这单里
                # @Choose、OnlyFans「Looking」两条噪声的来路。
                if matched_rows and source is None:
                    self._vlog(
                        "署名 %s 只出现在 %s，自己的帖子里没有这张图，不当线索",
                        name,
                        matched_rows[0].source or "搬运页",
                    )
                    continue
                seen_note = str(holder.get("watermark_note") or "")
                seen_info = {
                    "status": "seen",
                    "name": name,
                    "handle": resolved,
                    "x_name": x_name,
                    "followers": int(info.get("followers") or 0),
                    "reason": seen_note,
                    "platform": str(source.platform or "") if source is not None else "",
                    "source_url": source.url if source is not None else "",
                    "source_title": " ".join((source.title or source.name or "").split())[:60]
                    if source is not None
                    else "",
                }
        # 交叉验证：图上水印读到、并且核到存在的号，如果跟标题署名是同一个名字，
        # 两个独立来源就指向同一个人。她自己的帖子里翻不到这张（删过帖、或者本来
        # 就在付费图包里）不影响「作者是谁」——1710 那单水印明明读出 @kuru_xox，
        # 却因为翻不到那张判成「没找到」，只在下面挂了一句「可能是模型读错了」。
        for anchor in anchors:
            if str(anchor.get("state") or "") != fxtwitter.PROFILE_OK:
                continue
            handle = str(anchor.get("handle") or "").strip().lstrip("@")
            for name in names:
                if not handles.name_matches_handle(name, handle):
                    continue
                # 水印带中文名时，中文名才是本人的标识：@Natsuko_夏夏子 的 handle
                # 只剩 natsuko_，而 X 上的 @natsuko_ 是另一个号（显示名对不上中文
                # 名），不能因为拉丁那段重名就认成作者（s-20261117-0741-4631）。
                if handles.watermark_name_conflict(str(anchor.get("raw") or ""), str(anchor.get("name") or "")):
                    self._vlog(
                        "水印「%s」带中文名，X 上 @%s 的显示名是「%s」，对不上，不认定作者",
                        str(anchor.get("raw") or "")[:40],
                        handle,
                        str(anchor.get("name") or "") or "-",
                    )
                    continue
                self._vlog(
                    "交叉验证成立：水印 @%s + 标题署名 %s，认定作者（原帖翻不到）",
                    handle,
                    name,
                )
                return {
                    "status": "author",
                    "name": name,
                    "handle": handle,
                    "x_name": str(anchor.get("name") or ""),
                    "user_id": str(anchor.get("user_id") or ""),
                }
        if seen_info:
            return seen_info
        if seen_note:
            return {"status": "seen", "name": names[0], "reason": seen_note}
        return None

    def _promote_cross_author(
        self, best: Candidate | None, info: dict[str, Any]
    ) -> Candidate | None:
        """图上水印和标题署名指向同一个号时，把「作者是谁」定下来。

        这不算放宽指纹：指纹管的是「这张图是不是它发的」，这里回答的是「这是谁」。
        证据是两条独立的线索对上同一个号（图上写着、搬运帖标题也署这个名），而
        原帖翻不到有正当理由（她自己删过帖、或者图只在付费包里）。

        已经有落到人身上的结论（指纹命中的帖子、核过的账号）时不动它——那些更硬。
        """
        handle = str(info.get("handle") or "").strip().lstrip("@")
        if not handle:
            return best
        if best is not None and (best.handle or "").strip() and best.status == "match":
            return best
        # 水印是「那张图上的」，只有当这张图确实出现在那个页面上（指纹命中）时才算
        # 作者证据。只是「疑似同一个人」的候选不行：s-20261117-0741-4631 拿一条距离
        # 24 的 reddit 帖——那是别人另一套图，白毛带角拿镰刀——上面的微博水印去定
        # 作者，结果既认错了人，还把微博号当成 X 号去查。
        if best is None or best.status != "match":
            self._vlog(
                "水印 @%s 和署名 %s 对上了，但手上那条候选不是这张图（%s），不据此认定作者",
                handle,
                str(info.get("name") or ""),
                getattr(best, "status", "") or "无候选",
            )
            return best
        self._vlog(
            "认定作者：@%s（图上水印 + 标题署名 %s）", handle, str(info.get("name") or "")
        )
        return Candidate(
            engine="author",
            url="",
            platform="X (Twitter)",
            handle="@" + handle,
            name=str(info.get("x_name") or ""),
            distance=None,
            status="match",
            note=(
                "作者是她，但这张的帖子已经翻不到：图上水印和标题署名都指向这个号，"
                "可她自己最近的带图帖里没有这张（可能删了，或者只在付费图包里）"
            ),
        )

    def _promote_signature_author(
        self, best: Candidate | None, info: dict[str, Any]
    ) -> Candidate | None:
        """署名那个号自己发过这张图时，那条帖子就是结论。

        证据是「这个号自己的带图帖里指纹对上」，比搬运页那点相似度硬得多。
        """
        post = str(info.get("post") or "")
        handle = str(info.get("handle") or "").strip().lstrip("@")
        if not post or not handle:
            return best
        name = str(info.get("name") or "")
        distance = int(info.get("distance") or 0)
        self._vlog("标题署名反查命中，升成结论：%s → @%s %s（距离 %s）", name, handle, post, distance)
        return Candidate(
            engine="signature",
            url=post,
            platform="X (Twitter)",
            handle="@" + handle,
            name=str(info.get("x_name") or ""),
            distance=distance,
            status="match",
            note=f"标题里署名 {name}，@{handle} 自己的带图帖里有这张图（距离 {distance}）",
        )

    # 按 id= 的名字查各平台时，X 上最多核这么多个号（每次都要翻它自己的带图帖，
    # 一个号几秒，多了会把单子拖长）。
    _ID_NAME_PROBE_LIMIT = 2
    # 一次最多拿几个「id=」读法去问，按水印里出现的先后取。
    _ID_NAME_LIMIT = 2

    @classmethod
    def _should_search_id_name(
        cls,
        *,
        enabled: bool,
        has_upload: bool,
        names: list[str],
        watermark_post: str,
        best_status: str,
        best_is_x: bool,
    ) -> bool:
        """水印里那个 ``id=`` 名字该不该拿去各平台查。

        只要水印还没核到帖子就值得查：视觉命中「同一个人」、微博只给 UID、落在
        搬运页上都算。s-20261112-0925-cad4 就是视觉先给了个命中，这一步整条被
        跳过，图上写着 ``id=NaohingLace`` 却没人去各平台找。
        只有「结论已经是指纹对上的 X 帖」才不必再查。
        """
        if not enabled or not has_upload or not names:
            return False
        if str(watermark_post or "").strip():
            return False
        if str(best_status or "") == "match" and best_is_x:
            return False
        return True


    async def _hunt_from_id_name(
        self, names: list[str], upload: bytes | None
    ) -> dict[str, Any] | None:
        """图上写着 ``id=某某``：拿这个名字去各平台找主页，X 上的再翻号核图。

        s-20261112-0858-c44f 这单整图读出 ``Reng♡ @id.NothingLose``，机器人自己的
        「理解图片」把同一处读成 ``@id=NothingLee``——``id=`` 是中文平台的写法，X 上
        查 nothinglose／NothingLee 全是 404。所以这一步不只搜 X：一次网页检索把
        X、Instagram、TikTok、微博、Patreon、Reddit、Bilibili、Facebook、YouTube、
        小红书的主页都捞出来；能核图验真的只有 X（翻号自己的带图帖比指纹），其余
        平台只当线索写一行，绝不因为名字像就改结论。

        ``names`` 是同一处水印的几种读法（整图、各块、Grok 可能读得不一样），
        按顺序问，问到有主页就停；一个都没有就返回空 profiles。
        """
        wanted = [str(name or "").strip() for name in names if str(name or "").strip()]
        if not upload or not wanted:
            return None
        if not self._pools.get("lens") and not self._pools.get("serper"):
            self._vlog("按 id= 的名字查各平台：没有可用的网页检索 key，跳过")
            return None
        profiles: list[dict[str, str]] = []
        used = ""
        for name in wanted:
            rows = await self._search_profiles_with_rotation(name)
            if rows:
                profiles = rows
                used = name
                break
        if not profiles:
            used = wanted[0]
            self._vlog("按 id= 的名字查各平台：%s 都没搜到主页", "、".join(wanted))
        else:
            self._vlog(
                "按 id= 的名字查各平台：%s → %s",
                used,
                "、".join(f"{row.get('platform')}/{row.get('handle')}" for row in profiles),
            )
        result: dict[str, Any] = {
            "name": used,
            "profiles": profiles,
            "status": "seen" if profiles else "",
        }
        x_rows = [row for row in profiles if row.get("platform") == "X"]
        for row in x_rows[: self._ID_NAME_PROBE_LIMIT]:
            probe = str(row.get("handle") or "")
            info = await self._profile(probe)
            if not info:
                continue
            x_name = str(info.get("name") or "")
            resolved = str(info.get("screen_name") or probe)
            if not (
                identity.name_matches(used, x_name)
                or identity.name_matches(used, resolved)
            ):
                self._vlog("id= %s 搜到的 @%s（%s）对不上，丢掉", used, resolved, x_name or "-")
                continue
            self._vlog("id= %s 对上 X：@%s（%s），翻带图帖比指纹", used, resolved, x_name or "-")
            holder: dict[str, Any] = {}
            await self._check_watermark_post(resolved, upload, holder)
            post = str(holder.get("watermark_post") or "")
            if not post:
                continue
            result.update(
                {
                    "status": "hit",
                    "handle": resolved,
                    "x_name": x_name,
                    "followers": int(info.get("followers") or 0),
                    "post": post,
                    "distance": int(holder.get("watermark_distance") or 0),
                }
            )
            return result
        return result

    # 按名字去各平台找号：整段预算之外，X 上最多核这么多个号（每核一个都要翻它
    # 自己的带图帖，一个号几秒；两个足够盖住同一处名字的两种写法）。
    _NAME_HUNT_PROBE_LIMIT = 2
    # 纯 ASCII 的名字短到这个长度以下就不值一次检索额度（多半是关键词碎片），
    # 那种写法本来就该由「X 显示名对得上」那条线去认。
    _NAME_HUNT_MIN_ASCII = 4

    @classmethod
    def _searchable_name(cls, name: str, urls: list[str] | None = None) -> bool:
        """这个名字值不值得花一次网页检索额度。"""
        text = str(name or "").strip()
        if not (2 <= len(text) <= 24):
            return False
        if is_site_name(text, urls):
            return False
        if handles.has_cjk(text):
            return True
        core = re.sub(r"[^A-Za-z0-9_]+", "", text)
        return len(core) >= cls._NAME_HUNT_MIN_ASCII

    @classmethod
    def _should_search_name(
        cls,
        *,
        enabled: bool,
        has_upload: bool,
        names: list[str],
        best_status: str = "",
        best_handle: str = "",
        searched: list[str] | None = None,
        urls: list[str] | None = None,
    ) -> bool:
        """标题里那个名字该不该拿去各平台查。

        只有「结论已经是指纹对上的具体账号帖」才不必再查：那种情况人已经找到了，
        再搜一遍只是白花一次检索额度。结论为空、只到「同一个人」、停在图集站/搬运
        站上，都值得查——c077 就卡在「名字只拿去 X 按显示名核过，对不上就收工」。

        名字本身还要过了才会花额度：站名（`is_site_name`）不查，纯 ASCII 低于四个
        字符的不查，标题作者那条线已经拿同一个名字搜过的不查（一次检索结果复用，
        别把额度花两遍）。
        """
        if not enabled or not has_upload:
            return False
        # match + 有 handle 就是「指纹对上的具体帖」：X 是 @号，微博是 UID。
        if str(best_status or "") == "match" and str(best_handle or "").strip():
            return False
        done = {str(item or "").strip().lower() for item in (searched or [])}
        for name in names or []:
            text = str(name or "").strip()
            if not text or text.lower() in done:
                continue
            if cls._searchable_name(text, urls):
                return True
        return False

    @staticmethod
    def _title_author_searched(title_author: dict | None) -> str:
        """标题作者那条线真正认到号时，返回它搜过的那个名字（小写）；否则空串。

        `_hunt_title_author` 查不到时也会把 `author` 带回来（`status="none"`），
        拿它当「已经搜过」会把「按名字找号」这条腿整条挡掉——c077 那单就是中文名
        在 X 上按显示名对不上，恰恰是最需要换平台再试一次的时候。
        """
        info = title_author or {}
        if str(info.get("status") or "") not in {"hit", "named"}:
            return ""
        return str(info.get("author") or "").strip().lower()

    async def _hunt_name_profiles(
        self,
        names: list[str],
        upload: bytes | None,
        target_hash: int | None = None,
        source: str = "",
        skip_weibo: set[str] | None = None,
    ) -> dict[str, Any] | None:
        """标题里的名字 → 各平台主页；X 与微博的再翻本人带图帖比指纹。

        c077（`s-20261122-2219-c077`）那单：图只落在 hotgirlpix / misskon 这类
        图集站，页面一个社媒账号都不挂，标题里却写着 coser 的名字。旧逻辑只拿它去
        X 按显示名核对，对不上就收工；可中文名在 X 上本来就查无此号，她本人常
        两个平台用同一个昵称。所以这一步把名字铺到各平台去：一次网页检索捞出
        X / 微博 / Instagram / TikTok 的主页。

        **只有 X 与微博能往下核图**：X 的核显示名再翻带图帖，微博的读主页核昵称
        与粉丝门槛，有登录 cookie 才翻得了她的帖。其余平台（IG / TikTok / B 站…）
        只进线索列表——名字像不等于人就是同一个，改结论必须有指纹。
        """
        if not upload:
            return None
        wanted: list[str] = []
        for name in names:
            text = str(name or "").strip()
            if not text or text in wanted:
                continue
            if not self._searchable_name(text):
                continue
            wanted.append(text)
        if not wanted:
            return None
        if not self._pools.get("lens") and not self._pools.get("serper"):
            self._vlog("按名字找号：没有可用的网页检索 key，跳过")
            return None
        section = self.config.name_hunt
        deadline = time.monotonic() + float(getattr(section, "budget", 45.0) or 45.0)
        limit = max(1, int(getattr(section, "max_names", 2) or 1))
        floor = int(getattr(self.config.weibo, "min_fans", 0) or 0)
        profiles: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        used = ""
        verified_x: dict[str, Any] | None = None
        for name in wanted[:limit]:
            remaining = deadline - time.monotonic()
            if remaining < 8:
                self._vlog("按名字找号：预算用完了，%s 不再查", name)
                break
            try:
                rows = await asyncio.wait_for(
                    self._search_profiles_with_rotation(name), timeout=remaining
                )
            except Exception as error:  # noqa: BLE001 - 查不到不影响已有结论
                self._vlog("按名字找号：%s 检索失败（%s）", name, str(error)[:80])
                continue
            if not used and rows:
                used = name
            copies: dict[tuple[str, str], dict[str, Any]] = {}
            for row in rows or []:
                key = (
                    str(row.get("platform") or ""),
                    str(row.get("handle") or "").lower(),
                )
                if key in seen:
                    continue
                seen.add(key)
                item = dict(row)
                copies[key] = item
                profiles.append(item)
            if rows:
                self._vlog(
                    "按名字找号：%s → %s",
                    name,
                    "、".join(
                        f"{row.get('platform')}/{row.get('handle')}" for row in rows
                    ),
                )
            # 微博：UID 换昵称，昵称与粉丝门槛都过了才认；有 cookie 才翻得了帖。
            # 微博昵称那条腿已经拿这个名字搜过一次时，这里只留主页当线索，不再翻一遍。
            weibo_already = str(name).strip() in {
                str(item).strip() for item in (skip_weibo or set())
            }
            for row in (
                []
                if weibo_already
                else [item for item in (rows or []) if item.get("platform") == "微博"][:2]
            ):
                remaining = deadline - time.monotonic()
                if remaining < 6:
                    break
                uid = str(row.get("handle") or "")
                if not uid:
                    continue
                try:
                    info = await asyncio.wait_for(self._weibo_profile(uid), timeout=remaining)
                except Exception:  # noqa: BLE001 - 换不到昵称就只留主页这条线索
                    info = None
                if not info:
                    continue
                account = str(info.get("name") or "")
                followers = int(info.get("followers") or 0)
                copy = copies.get(("微博", uid.lower()))
                if copy is not None:
                    copy.update(
                        {
                            "name": account,
                            "followers": followers,
                            "url": str(info.get("url") or copy.get("url") or ""),
                        }
                    )
                if not identity.weibo_name_matches(name, account):
                    self._vlog(
                        "按名字找号：微博 %s 的昵称叫「%s」，对不上「%s」，只当线索",
                        uid,
                        account,
                        name,
                    )
                    continue
                if followers < floor:
                    # 重名太多，粉丝数不够的号只留在主页列表里，不往下核图。
                    self._vlog(
                        "按名字找号：微博「%s」只有 %s 粉（门槛 %s），只当线索",
                        account,
                        followers,
                        floor,
                    )
                    continue
                remaining = deadline - time.monotonic()
                if remaining < 6:
                    break
                try:
                    probed = await asyncio.wait_for(
                        self._with_weibo_post(
                            {
                                "uid": uid,
                                "name": account,
                                "followers": followers,
                                "url": str(info.get("url") or ""),
                            },
                            target_hash,
                        ),
                        timeout=remaining,
                    )
                except asyncio.TimeoutError:
                    # 翻帖本身有 18 秒、最多比 36 张图，整段还有别的名字要查；
                    # 超时就只留主页线索，别把预算吃光。
                    self._vlog("按名字找号：微博「%s」翻帖超时，只当线索", account)
                    continue
                post = str(probed.get("post") or "")
                if not post:
                    continue
                return {
                    "name": name,
                    "status": "hit",
                    "platform": "微博",
                    "handle": uid,
                    "account": account,
                    "followers": followers,
                    "post": post,
                    "distance": int(probed.get("distance") or 0),
                    "image": str(probed.get("image") or ""),
                    "profiles": profiles,
                    "source": source,
                }
            # X：显示名（或 handle 本身）对得上才值得翻它的带图帖。
            for row in [item for item in (rows or []) if item.get("platform") == "X"][
                : self._NAME_HUNT_PROBE_LIMIT
            ]:
                remaining = deadline - time.monotonic()
                if remaining < 6:
                    break
                probe = str(row.get("handle") or "")
                if not probe:
                    continue
                try:
                    info = await asyncio.wait_for(
                        self._profile(probe), timeout=max(6.0, remaining)
                    )
                except Exception:  # noqa: BLE001 - 查不到这个号只少一条线索
                    info = None
                if not info:
                    continue
                x_name = str(info.get("name") or "")
                resolved = str(info.get("screen_name") or probe)
                if not (
                    identity.name_matches(name, x_name)
                    or identity.name_matches(name, resolved)
                ):
                    self._vlog(
                        "按名字找号：@%s（%s）跟「%s」对不上，丢掉",
                        resolved,
                        x_name or "-",
                        name,
                    )
                    continue
                self._vlog("按名字找号：%s 对上 X @%s，翻带图帖比指纹", name, resolved)
                if verified_x is None:
                    # 显示名对得上 = 这个号已经被认出是她本人。翻不到图只能说明她
                    # 最近没发这张，号本身仍然要摆出去（还要拿去查付费页）。
                    verified_x = {
                        "handle": resolved,
                        "name": x_name,
                        "followers": int(info.get("followers") or 0),
                        "url": f"https://x.com/{resolved}",
                    }
                holder: dict[str, Any] = {}
                remaining = deadline - time.monotonic()
                if remaining < 6:
                    break
                try:
                    await asyncio.wait_for(
                        self._check_watermark_post(resolved, upload, holder),
                        timeout=remaining,
                    )
                except asyncio.TimeoutError:
                    self._vlog("按名字找号：翻 @%s 的带图帖超时，只当线索", resolved)
                    continue
                scan = holder.get("watermark_scan") or {}
                if scan and verified_x is not None:
                    # 把「翻了几条、几张图」带到结论里：只说「没核到」的话，用户
                    # 看到的就跟「没查」一样。
                    verified_x["scanned_posts"] = int(scan.get("scanned_posts") or 0)
                    verified_x["scanned_images"] = int(scan.get("scanned_images") or 0)
                    verified_x["scan_truncated"] = bool(scan.get("truncated"))
                post = str(holder.get("watermark_post") or "")
                if not post:
                    continue
                return {
                    "name": name,
                    "status": "hit",
                    "platform": "X",
                    "handle": resolved,
                    "account": x_name,
                    "followers": int(info.get("followers") or 0),
                    "post": post,
                    "distance": int(holder.get("watermark_distance") or 0),
                    "profiles": profiles,
                    "source": source,
                }
        if not profiles:
            self._vlog("按名字找号：%s 都没搜到主页", "、".join(wanted[:limit]))
        return {
            "name": used or wanted[0],
            "status": "seen" if profiles else "none",
            "profiles": profiles,
            "source": source,
            # 显示名对得上、但自己的帖子里没这张图的号：下游拿它查付费页、写进线索行。
            "x_handle": str((verified_x or {}).get("handle") or ""),
            "x_name": str((verified_x or {}).get("name") or ""),
            "x_followers": int((verified_x or {}).get("followers") or 0),
            "x_scanned_posts": int((verified_x or {}).get("scanned_posts") or 0),
            "x_scanned_images": int((verified_x or {}).get("scanned_images") or 0),
            "x_scan_truncated": bool((verified_x or {}).get("scan_truncated")),
        }

    def _promote_name_hit(
        self, best: Candidate | None, info: dict[str, Any]
    ) -> Candidate | None:
        """按名字找到的那个号自己发过这张图时，那条帖子就是结论。"""
        post = str(info.get("post") or "")
        name = str(info.get("name") or "")
        distance = int(info.get("distance") or 0)
        if not post:
            return best
        if str(info.get("platform") or "").strip() == "微博":
            return self._promote_weibo_post(
                best,
                {
                    "distance": distance,
                    "name": str(info.get("account") or ""),
                    "uid": str(info.get("handle") or ""),
                    "url": post,
                    "image": str(info.get("image") or ""),
                    "note": f"按标题里的名字「{name}」找到她的微博",
                },
            )
        handle = str(info.get("handle") or "").strip().lstrip("@")
        if not handle:
            return best
        self._vlog("按名字找号命中，升成结论：%s → @%s %s（距离 %s）", name, handle, post, distance)
        return Candidate(
            engine="namehunt",
            url=post,
            platform="X (Twitter)",
            handle="@" + handle,
            name=str(info.get("account") or ""),
            distance=distance,
            status="match",
            note=f"标题里写着「{name}」，@{handle} 自己的带图帖里有这张图（距离 {distance}）",
        )

    async def _search_profiles_with_rotation(self, name: str) -> list[dict[str, str]]:
        """一次网页检索，key 用完了就换下一个；SerpAPI 全不可用时换 Serper。

        现网三把 SerpAPI key，其中一把月额度已经用完（实测同一个名字一个回 429、
        另一个正常）。不换 key 的话这一步会天天静默失败，看着就跟没这功能一样。
        """
        for pool_name in ("lens", "serper"):
            pool = self._pools.get(pool_name)
            if not pool:
                continue
            for _ in range(2):
                key = pool.acquire()
                if not key:
                    break

                def fetch(api_key: str = key) -> list[dict[str, str]]:
                    # fallback=True：先搜「名字 + cosplay」，一条主页都没捞到再退回
                    # 裸名字。411f 的「Mio Kuuko」加 cosplay 之后回来的全是 pinterest
                    # 图板，一条主页都没有；裸名字才搜到 x.com/Kuukow002 与
                    # instagram.com/kuuko_w。这里以前写死 False，等于白搜一次。
                    return identity.search_profiles(name, api_key, self._proxy(), fallback=True)

                try:
                    rows = await asyncio.wait_for(asyncio.to_thread(fetch), timeout=40)
                except Exception as error:  # noqa: BLE001 - 查不到就当没这一步
                    text = str(error)
                    if "429" in text or "401" in text or "403" in text:
                        # 额度用完的 key 冷却久一点，别每单都拿它试一次。
                        pool.mark_failure(key, 3600 if "429" in text else None)
                        self._vlog(
                            "按 id= 的名字查各平台（%s）：key 额度不可用（%s），换下一个",
                            pool_name,
                            text[:40],
                        )
                        continue
                    self._vlog("按 id= 的名字查各平台（%s）失败：%s", pool_name, text[:80])
                    return []
                pool.mark_success(key)
                return rows
        return []

    def _promote_id_name(self, best: Candidate | None, info: dict[str, Any]) -> Candidate | None:
        """``id=`` 那个名字在 X 上核到了这张图，那条帖子就是结论。"""
        post = str(info.get("post") or "")
        handle = str(info.get("handle") or "").strip().lstrip("@")
        if not post or not handle:
            return best
        name = str(info.get("name") or "")
        distance = int(info.get("distance") or 0)
        self._vlog("id= 名字反查命中，升成结论：%s → @%s %s（距离 %s）", name, handle, post, distance)
        return Candidate(
            engine="idname",
            url=post,
            platform="X (Twitter)",
            handle="@" + handle,
            name=str(info.get("x_name") or ""),
            distance=distance,
            status="match",
            note=f"图上写着 id={name}，@{handle} 自己的带图帖里有这张图（距离 {distance}）",
        )

    # 从候选标题猜账号时最多验这么多个名字。别名列表常常挤在某一条搬运站
    # 标题里，前面几条候选的站点名会把名额吃光，所以名额放宽到 20，并按状态
    # 排序：先看指纹或视觉确实对上的，再看下不到图的社交页（作者本人常在那里），
    # 视觉觉得像但指纹差得远的排最后。
    _X_HUNT_LIMIT = 20
    _X_HUNT_STATUS_ORDER = {"match": 0, "vision": 0, "unverified": 1, "suspect": 2}

    @classmethod
    def _x_hunt_priority(cls, item: Candidate) -> int:
        return cls._X_HUNT_STATUS_ORDER.get(item.status, 3)

    # 粉丝数门槛。实测 500 会把几百粉的同名路人（kuuko_w、Medieval）放进来，
    # 5000 以下即使是真的也很难和同名号区分，宁可漏报。
    _X_GUESS_MIN_FOLLOWERS = 5000

    async def _profile(self, handle: str) -> dict[str, Any] | None:
        """查一个 X 账号的公开资料（fxtwitter，免登录）。

        失败要能看见原因：`s-20261119-2011-9aa1` 那单查 `ran3ran3ran3` 时四个变体
        全部静默返回空，结果里只留「没找到作者」，日志里一个字都没有，根本不知道
        是查无此号还是接口挂了。现在 404（查无此号）只记一行；其余失败（429、5xx、
        超时、连不上）**重试一次**再放弃。
        """
        url = f"https://api.fxtwitter.com/2/profile/{handle}"
        proxy = self._proxy()

        def fetch() -> tuple[int, dict[str, Any] | None, str]:
            session = make_session(proxy)
            try:
                response = session.get(url, timeout=15)
                if response.status_code != 200:
                    return response.status_code, None, ""
                payload = response.json()
                user = payload.get("user") if isinstance(payload, dict) else None
                return 200, (user if isinstance(user, dict) else None), ""
            except Exception as error:  # noqa: BLE001 - 网络问题也算一次「没查到」
                return 0, None, str(error)[:80]
            finally:
                session.close()

        for attempt in (1, 2):
            try:
                code, user, error = await asyncio.wait_for(asyncio.to_thread(fetch), timeout=20)
            except Exception as exc:  # noqa: BLE001
                code, user, error = 0, None, str(exc)[:80]
            if user:
                return user
            if code == 404:
                self._vlog("查 @%s：查无此号", handle)
                return None
            self._vlog(
                "查 @%s 失败：HTTP %s %s，%s",
                handle,
                code or "-",
                error or "",
                "重试一次" if attempt == 1 else "放弃",
            )
            if attempt == 1:
                await asyncio.sleep(self._PROFILE_RETRY_DELAY)
        return None

    async def _fetch_json(
        self, url: str, headers: dict[str, str] | None = None, timeout: int = 15
    ) -> dict[str, Any] | None:
        """读一个公开 JSON 接口。失败一律当没有，别让付费页把整单拖住。"""
        proxy = self._proxy()

        def fetch() -> dict[str, Any] | None:
            session = make_session(proxy, headers=headers)
            try:
                response = session.get(url, timeout=timeout)
                if response.status_code != 200:
                    return None
                payload = response.json()
                return payload if isinstance(payload, dict) else None
            finally:
                session.close()

        try:
            data = await asyncio.wait_for(asyncio.to_thread(fetch), timeout=timeout + 4)
        except Exception:  # noqa: BLE001 - 查不到就当没有
            return None
        return data if isinstance(data, dict) else None

    async def _track_paywall(
        self, handle: str, texts: list[str], post_url: str = ""
    ) -> dict[str, Any] | None:
        """确认作者之后，去她挂的付费页找这一期卖在哪。

        图包里的图公开渠道永远搜不到：作者只在 Patreon／FANBOX 上卖。X 资料里
        通常就挂着这两个链接，付费页那边有免登录的公开列表，能读到标题、发布
        时间、最低订阅额与档位，公开帖还能当免费预览。付费帖正文读不到，所以
        这里只给「卖在哪、多少钱、有没有免费预览」。
        """
        handle = (handle or "").strip().lstrip("@")
        if not handle or not handle.isascii():
            return None
        user = await self._profile(handle)
        if not user:
            return None
        website = user.get("website") or {}
        website_url = str(website.get("url") or "") if isinstance(website, dict) else str(website or "")
        links = paywall.links_of(str(user.get("description") or ""), website_url)
        if not links:
            return None
        sources = list(texts)
        if post_url:
            tweet = await self._fetch_tweet(post_url)
            if tweet is not None and getattr(tweet, "ok", False) and tweet.text:
                # 结论帖的正文和话题标签最说明是哪个角色（``#RemielleDan``），排最前。
                sources.insert(0, tweet.text)
        words = paywall.keywords(*sources)
        for kind in ("patreon", "fanbox"):
            name = links.get(kind)
            if not name:
                continue
            try:
                found = (
                    await self._patreon_track(name, words)
                    if kind == "patreon"
                    else await self._fanbox_track(name, words)
                )
            except Exception as error:  # noqa: BLE001 - 付费页查不到不影响结论
                self._vlog("付费页追踪失败：%s %s", kind, str(error)[:80])
                continue
            if found:
                found["handle"] = handle
                post = found.get("post") or {}
                # 没对上这一期就没有「这一期」可对，翻了也只会添乱：78c8 那单
                # 售卖页没对上任何一期，硬翻出来的那条恰好就是结论帖本身，还被
                # 写成「她 X 上提到过这个角色，不是这一期」。
                if post.get("url"):
                    try:
                        teaser = await self._hunt_teaser(handle, words, post, exclude=post_url)
                    except Exception as error:  # noqa: BLE001 - 翻不到预告不影响售卖页
                        self._vlog("免费预告查找失败：%s", str(error)[:80])
                        teaser = None
                    if teaser:
                        found["teaser"] = teaser
                self._vlog(
                    "付费页命中：%s %s → %s",
                    kind,
                    name,
                    str((found.get("post") or {}).get("url") or found.get("shop") or "")[:80],
                )
                return found
        return None

    async def _track_pixiv_source(
        self,
        candidates: list[Candidate],
        upload: bytes | None = None,
        trace: list[dict] | None = None,
    ) -> tuple[dict[str, Any] | None, Candidate | None]:
        """从搬运站的图床文件名里读 pixiv 作品号，回流 pixiv 核对，能比指纹就比。

        `cdfa` 那单：用户把 pixiv 上的图存下来又发进群，图片地址是
        `cdn.honkaihaven.com/art/…/沒有名字-by-lol-…-152342429.webp`——文件名里
        就写着 pixiv 作品号 152342429 和作者名 lol，搜索引擎一个都没对上。这类
        搬运站按 pixiv 作品扒图，图床文件名是它自己拼的 SEO 串，等于把出处抄在
        文件名上。作品号必须回 pixiv 核对：`ajax/illust/<id>` 匿名能读到标题、
        作者名、页数（图片地址字段一律 null），作者名还要能在文件名里找到——否则
        `photo-20261123.jpg` 这种流水号会撞上旧作品。

        图片本身走 `pixiv.re` 反代取（12MB 原图，R-18 也给，不用 cookie），取到
        就跟原图比指纹：距离过阈值就造一条 `match` 候选交出去，让结论真的落在
        pixiv 上；取不到或对不上就只留「出处是哪个作品」，文案写明没比过指纹。

        返回 ``(出处信息, 可比对就算出来的候选)``。
        """
        if not candidates:
            return None, None
        seen: list[str] = []
        hints: list[tuple[str, str]] = []
        for item in candidates:
            for url in (item.image_url, item.fallback_image, item.url):
                illust_id, filename = pixiv_filename_hint(str(url or ""))
                if not illust_id or illust_id in seen:
                    continue
                seen.append(illust_id)
                hints.append((illust_id, filename))
            if len(hints) >= 3:
                break
        if not hints:
            self._trace(trace, "pixiv 出处", "skip", "候选图地址里没有 pixiv 作品号")
            return None, None
        for illust_id, filename in hints:
            body = await self._pixiv_illust(illust_id)
            if not body:
                self._vlog("pixiv 作品号 %s 查不到（文件名 %s）", illust_id, filename[:60])
                continue
            user_name = str(body.get("userName") or "").strip()
            flat_name = handles.fold(filename).lower()
            flat_user = handles.fold(user_name).lower()
            matched = bool(flat_user) and len(flat_user) >= 2 and flat_user in flat_name
            if not matched:
                self._vlog(
                    "pixiv %s 的作者是「%s」，文件名里没有这个名字，不采信：%s",
                    illust_id,
                    user_name,
                    filename[:60],
                )
                continue
            artwork_id = str(body.get("illustId") or illust_id)
            info: dict[str, Any] = {
                "illust_id": artwork_id,
                "title": str(body.get("illustTitle") or "").strip(),
                "user_name": user_name,
                "user_id": str(body.get("userId") or ""),
                "x_restrict": int(body.get("xRestrict") or 0),
                "create_date": str(body.get("createDate") or ""),
                "page_count": int(body.get("pageCount") or 0),
                "url": pixiv_artwork_url(artwork_id),
                "read_from": filename,
                "author_matched": True,
            }
            hit = await self._pixiv_mirror_match(artwork_id, upload, info)
            self._vlog(
                "从文件名读到 pixiv 作品：%s《%s》by %s（指纹距离 %s）",
                artwork_id,
                info["title"][:40],
                user_name,
                info.get("distance"),
            )
            self._trace(
                trace,
                "pixiv 出处",
                "hit",
                "文件名里的作品号对上了 pixiv：%s《%s》by %s，指纹距离 %s"
                % (artwork_id, info["title"][:30], user_name, info.get("distance") if info.get("distance") is not None else "没比成"),
            )
            return info, hit
        self._trace(
            trace,
            "pixiv 出处",
            "none",
            "读到 %d 个作品号，pixiv 上没对上（作品号不存在或作者名不符）" % len(hints),
        )
        return None, None

    async def _pixiv_mirror_match(
        self, illust_id: str, upload: bytes | None, info: dict[str, Any]
    ) -> Candidate | None:
        """用 pixiv.re 反代取原图，跟用户这张比指纹；过了阈值就造一条 pixiv 候选。

        pixiv 的图片地址字段对未登录一律 null（连非 R-18 也是），但 pixiv.re 这类
        反代能吃下 `/<作品号>.jpg` 并把原图吐出来——实测 152342429 拿到 3328×4864、
        12MB，跟 honkaihaven 那张指纹距离 0。作品有多页时反代默认给第一页，对不上
        就当没比成，不硬猜。
        """
        if not upload:
            return None
        upload_hash = dhash(upload)
        if upload_hash is None:
            return None
        raw: bytes | None = None
        used = ""
        for suffix in ("jpg", "png"):
            url = f"https://pixiv.re/{illust_id}.{suffix}"
            data = await self._download(url)
            if data:
                raw, used = data, url
                break
        if not raw:
            self._vlog("pixiv.re 取不到 %s 的图，只能留作品信息", illust_id)
            return None
        found_hash = dhash(raw)
        if found_hash is None:
            return None
        distance = hamming(found_hash, upload_hash)
        threshold = int(getattr(self.config.plugin, "hash_threshold", 8) or 8)
        info["image_url"] = used
        info["distance"] = distance
        info["verified"] = distance <= threshold
        if distance > threshold:
            self._vlog("pixiv.re 那张跟原图距离 %s，超过阈值 %s，不换结论", distance, threshold)
            return None
        self._vlog("pixiv.re 那张跟原图距离 %s，确认为原帖", distance)
        user_name = str(info.get("user_name") or "").strip()
        user_id = str(info.get("user_id") or "").strip()
        account_note = f"作者：{user_name}" if user_name else ""
        if user_id:
            account_note += f"（pixiv.net/users/{user_id}）" if account_note else f"pixiv.net/users/{user_id}"
        return Candidate(
            engine="pixiv",
            url=str(info.get("url") or ""),
            title=f"{info.get('title') or ''} - {user_name}".strip(" -"),
            image_url=used,
            distance=distance,
            status="match",
            platform="pixiv",
            name=str(info.get("title") or "").strip(),
            account_note=account_note,
            note="指纹一致（距离 %s），pixiv 原帖" % distance,
        )

    async def _pixiv_illust(self, illust_id: str) -> dict[str, Any] | None:
        """匿名读 pixiv 作品信息。未登录只给标题、作者、页数，给不了图片地址。"""
        value = str(illust_id or "").strip()
        if not value.isdigit():
            return None
        payload = await self._fetch_json(
            f"https://www.pixiv.net/ajax/illust/{value}",
            headers={"Referer": "https://www.pixiv.net/"},
            timeout=8,
        )
        if not payload or payload.get("error"):
            return None
        body = payload.get("body")
        if not isinstance(body, dict) or not str(body.get("illustTitle") or "").strip():
            return None
        return body

    async def _patreon_track(self, vanity: str, words: list[str]) -> dict[str, Any] | None:
        """Patreon：名字换 campaign id，再读档位与最近二十期。"""
        base = "https://www.patreon.com/api"
        listing = await self._fetch_json(f"{base}/campaigns?filter[vanity]={vanity}")
        items = (listing or {}).get("data") or []
        campaign_id = str((items[0] or {}).get("id") or "") if items else ""
        if not campaign_id:
            return None
        detail = await self._fetch_json(f"{base}/campaigns/{campaign_id}?include=rewards")
        posts_payload = await self._fetch_json(
            f"{base}/posts?filter[campaign_id]={campaign_id}&page[count]=20&sort=-published_at"
        )
        posts = paywall.patreon_posts(posts_payload)
        tiers = paywall.patreon_rewards(detail)
        currency = next((tier["currency"] for tier in tiers if tier.get("currency")), "")
        post = paywall.pick_post(posts, words)
        info: dict[str, Any] = {
            "platform": "patreon",
            "shop": f"https://www.patreon.com/{vanity}",
            "post": post,
            "tiers": tiers,
            "preview": None,
        }
        if post is not None:
            if not post.get("currency"):
                post["currency"] = currency
            preview = paywall.free_post(posts, paywall.keywords(post.get("title") or ""))
            if preview is not None and preview.get("url") != post.get("url"):
                info["preview"] = {"title": preview.get("title"), "url": preview.get("url")}
        if post is None and not tiers:
            return None
        return info

    async def _fanbox_track(self, creator: str, words: list[str]) -> dict[str, Any] | None:
        """FANBOX：公开列表带 ``feeRequired``，能直接看出哪期要付费。"""
        headers = {
            "Origin": "https://www.fanbox.cc",
            "Referer": f"https://{creator}.fanbox.cc/",
        }
        payload = await self._fetch_json(
            f"https://api.fanbox.cc/post.listCreator?creatorId={creator}&limit=20", headers=headers
        )
        posts = paywall.fanbox_posts(payload, creator)
        if not posts:
            return None
        plans = await self._fetch_json(
            f"https://api.fanbox.cc/plan.listCreator?creatorId={creator}", headers=headers
        )
        tiers = paywall.fanbox_plans(plans)
        post = paywall.pick_post(posts, words)
        info: dict[str, Any] = {
            "platform": "fanbox",
            "shop": f"https://{creator}.fanbox.cc",
            "post": post,
            "tiers": tiers,
            "preview": None,
        }
        if post is not None:
            preview = paywall.free_post(posts, paywall.keywords(post.get("title") or ""))
            if preview is not None and preview.get("url") != post.get("url"):
                info["preview"] = {"title": preview.get("title"), "url": preview.get("url")}
        return info

    async def _track_paywall_direct(
        self, url: str, texts: list[str], target: dict[str, str]
    ) -> dict[str, Any] | None:
        """结论本身就是付费页帖子时，按这条链接反推「卖在哪、多少钱」。

        图搜命中的页面常常就是作者的售卖帖：链接里带着作者名与帖子 id，页面
        本身却不告诉你她的 X 号。所以这里不猜账号，直接拿链接换 campaign
        （档位、作者挂的 X 号）与那一期，再按作品名找免费预览。
        """
        platform = str(target.get("platform") or "")
        name = str(target.get("name") or "")
        post_id = str(target.get("post_id") or "")
        if not post_id:
            return None
        words = paywall.keywords(*texts)
        try:
            if platform == "patreon":
                found = await self._patreon_post_track(name, post_id)
            else:
                found = await self._fanbox_post_track(name, post_id)
        except Exception as error:  # noqa: BLE001 - 付费页查不到不影响结论
            self._vlog("付费页直查失败：%s %s", platform, str(error)[:80])
            return None
        if not found:
            return None
        handle = str(found.pop("handle", "") or "")
        if handle and found.get("post"):
            try:
                teaser = await self._hunt_teaser(handle, words, found["post"], exclude=url)
            except Exception as error:  # noqa: BLE001 - 翻不到预告不影响售卖页
                self._vlog("免费预告查找失败：%s", str(error)[:80])
                teaser = None
            if teaser:
                found["teaser"] = teaser
        self._vlog(
            "付费页命中（直查）：%s %s → %s",
            platform,
            name,
            str((found.get("post") or {}).get("url") or found.get("shop") or "")[:80],
        )
        return found

    async def _patreon_post_track(self, vanity: str, post_id: str) -> dict[str, Any] | None:
        """Patreon：作者名换 campaign，按帖子 id 取这一期（顺带档位与作者 X 号）。"""
        if not vanity:
            return None
        base = "https://www.patreon.com/api"
        listing = await self._fetch_json(f"{base}/campaigns?filter[vanity]={vanity}")
        items = (listing or {}).get("data") or []
        campaign_id = str((items[0] or {}).get("id") or "") if items else ""
        if not campaign_id:
            return None
        # 单帖接口一次就带回这一期的标题、锁没锁、档位，以及 included 里作者的
        # social_connections（X 号就在那儿），比先读 campaign 再列帖更省一步。
        single = await self._fetch_json(f"{base}/posts/{post_id}")
        found = paywall.patreon_posts({"data": [(single or {}).get("data") or {}]})
        post = found[0] if found else None
        tiers = paywall.patreon_rewards(single)
        if not tiers:
            # 单帖没带档位时（旧帖或接口变动）退回 campaign 详情。
            tiers = paywall.patreon_rewards(
                await self._fetch_json(f"{base}/campaigns/{campaign_id}?include=rewards")
            )
        posts = paywall.patreon_posts(
            await self._fetch_json(
                f"{base}/posts?filter[campaign_id]={campaign_id}&page[count]=20&sort=-published_at"
            )
        )
        if post is None:
            post = next(
                (item for item in posts if paywall.post_id_of(item.get("url") or "") == post_id),
                None,
            )
        currency = next((tier["currency"] for tier in tiers if tier.get("currency")), "")
        if post is not None and not post.get("currency"):
            post["currency"] = currency
        x_url = paywall.social_url(single)
        info: dict[str, Any] = {
            "platform": "patreon",
            "shop": f"https://www.patreon.com/{vanity}",
            "post": post,
            "tiers": tiers,
            "preview": None,
            "x_url": x_url,
            "handle": paywall.handle_of(x_url),
        }
        if post is not None:
            preview = paywall.free_post(posts, paywall.keywords(post.get("title") or ""))
            if preview is not None and preview.get("url") != post.get("url"):
                info["preview"] = {"title": preview.get("title"), "url": preview.get("url")}
        if post is None and not tiers:
            return None
        return info

    async def _fanbox_post_track(self, creator: str, post_id: str) -> dict[str, Any] | None:
        """FANBOX：作者名列公开帖，按帖子 id 认这一期，档位另取。"""
        if not creator:
            return None
        headers = {
            "Origin": "https://www.fanbox.cc",
            "Referer": f"https://{creator}.fanbox.cc/",
        }
        posts = paywall.fanbox_posts(
            await self._fetch_json(
                f"https://api.fanbox.cc/post.listCreator?creatorId={creator}&limit=20",
                headers=headers,
            ),
            creator,
        )
        post = next(
            (item for item in posts if paywall.post_id_of(item.get("url") or "") == post_id),
            None,
        )
        if post is None:
            single = await self._fetch_json(
                f"https://api.fanbox.cc/post.info?postId={post_id}", headers=headers
            )
            body = (single or {}).get("body") or {}
            found = paywall.fanbox_posts({"body": {"posts": [body]}}, creator) if body else []
            post = found[0] if found else None
        tiers = paywall.fanbox_plans(
            await self._fetch_json(
                f"https://api.fanbox.cc/plan.listCreator?creatorId={creator}", headers=headers
            )
        )
        if post is None and not tiers:
            return None
        return {
            "platform": "fanbox",
            "shop": f"https://{creator}.fanbox.cc",
            "post": post,
            "tiers": tiers,
            "preview": None,
        }

    async def _hunt_teaser(
        self, handle: str, words: list[str], paid_post: dict, exclude: str = ""
    ) -> dict[str, Any] | None:
        """去她自己的 X 上翻这一期的公开帖。

        付费页那边读不到正文和封面，能先看的就是她自己发的公开帖。搜索
        ``from:<号> <作品名>`` 比翻最近的带图帖强：既能捞到窗口外的老帖，也能
        捞到「我在 FANBOX 发表了 …」这种上架通知（实测 ran3 的妃咲那期就有）。

        但同角色的帖会撞到别的期：妃咲这期的关键词只能搜到上一期「妃咲(兔女郎)
        44p」的上架通知，指着 10-03 那期。所以命中之后还要按发布时间跟这一期比，
        隔得远的就降级成「顺带看见」，文案里写清是哪天的，不能当这一期的预告。
        """
        handle = (handle or "").strip().lstrip("@")
        if not handle or not words:
            return None
        post_url = str((paid_post or {}).get("url") or "").strip()
        # 结论帖自己不能被当成「预告」：78c8 那单售卖页没对上期，翻出来的
        # 恰好就是结论那条帖子，摆出来像在自说自话。
        avoid = {
            value.split("?", 1)[0].rstrip("/").lower()
            for value in (post_url, exclude)
            if value
        }
        queries = [word for word in words if len(word) >= 3 or not word.isascii()][:2]
        seen: set[str] = set()
        items: list[dict[str, Any]] = []
        for word in queries:
            url = fxtwitter.build_search_url(f"from:{handle} {word}")
            if not url:
                continue
            hits, _ = fxtwitter.parse_search(await self._fetch_json(url))
            for hit in hits:
                text = hit.text or ""
                if word.lower() not in text.lower() or not hit.url:
                    continue
                if post_url and post_url in text:
                    continue
                if hit.url.split("?", 1)[0].rstrip("/").lower() in avoid:
                    continue
                if hit.url in seen:
                    continue
                seen.add(hit.url)
                shop_link = "fanbox.cc" in text or "patreon.com" in text
                items.append(
                    {
                        "url": hit.url,
                        "text": " ".join(text.split())[:60],
                        "published_raw": str(hit.created_at or ""),
                        "kind": "announcement" if shop_link else "teaser",
                        "has_media": bool(hit.media),
                    }
                )
        return paywall.pick_teaser(items, str((paid_post or {}).get("published") or ""))

    def _identity_texts(self, ordered: list[Candidate]) -> list[str]:
        """已经靠图像对上的那几条候选的名字，用来卡住张冠李戴。

        只取 match / vision 两种状态：它们的图跟原图确实像，标题里的名字才可能
        是作者。一条都对不上时返回空列表，调用方按「不做这道校验」处理，不能
        因为没对上就什么线索都不给。
        """
        picked = [item for item in ordered if item.status in ("match", "vision")]
        return [
            f"{item.title} {item.name}"
            for item in picked[:3]
            if item.title or item.name
        ]

    @staticmethod
    def _name_text(item: Candidate) -> str:
        """抽名字用的文本。

        X 帖子的标题是「名字 on X: "正文"」，正文里什么词都有——「Happy
        Halloween!」那单就抽出了 @halloween，还顺手推了 leakgallery.com/Halloween。
        只留 on X: 前面那截。
        """
        title = item.title or ""
        if is_x_post_page(item.url):
            title = x_title_name(title)
        return f"{title} {item.name}"

    async def _hunt_x_handle(self, ordered: list[Candidate]) -> dict[str, Any] | None:
        """从候选标题里认出疑似 X 账号，验证存在后取粉丝最多的那个。

        搬运站的标题常把作者别名一起抄过来（「Kuuko / kuukow002 / kuuko_w」），
        这是不调用 Grok 时唯一能拿到作者账号的线索。只认「名字原样出现在标题
        里」的名字，再要求账号真实存在且粉丝量够；两条都不满足就宁可不给。
        """
        # 只看「图确实像」的那几类候选：mismatch 的标题跟这张图无关，里面
        # 的 Gundam、Azur Lane 之类抽出来全是噪声，还会把名额占满，让真正
        # 写着作者名的那条（fapello 的「NENEchi890, NERUchi890」）挤不进来。
        rows = sorted(
            (
                item
                for item in ordered[:12]
                if (item.title or item.name)
                # 只认真核过图的候选。`unverified` 是「图根本没下载比对过」——它的
                # 标题跟这张图有没有关系完全未知，从这里抽名字纯属撞运气：0fd8 那单
                # 一条 facebook 帖子（unverified）标题里写着「…for Twitch Con Europe…」，
                # 就抽出了 944 万粉的 @Twitch，跟图毫无关系。
                and item.status in {"match", "vision", "suspect"}
                # suspect 里也混着「压根没比过图」的（distance 是空的）：158d 那单一条
                # danbooru 候选的「标题」其实是图片地址（…/strike-the-blood/images/…），
                # 从里面抽出了 16 万粉的 @Strike。没量过距离就不算数。
                and (item.status == "vision" or item.distance is not None)
                and not handles.is_blocked_source(item.source)
            ),
            key=self._x_hunt_priority,
        )
        if not rows:
            return None
        # 一条都没对上图（这一单就是「未找到」）时，这些名字只是从噪声标题里抽出
        # 来的单词，抽中哪个真实存在的大号纯属巧合：实测「Nothing To Hide」抽出
        # 106 万粉的 @nothing、「Design」抽出 @Design，全跟图无关。没锚点就不猜。
        anchors = self._identity_texts(ordered)
        if not anchors:
            return None
        texts = [self._name_text(item) for item in rows]
        names = handles.extract(texts, limit=self._X_HUNT_LIMIT)
        if not names:
            return None
        # 站名不是账号：实测 2c76 的候选标题写着「Telegraph」（telegra.ph 那页），
        # 抽出来正好撞上 X 上的 @Telegraph。用**全部**候选的地址判定，别只跟前几行
        # 比——写着站名的那一页未必排在前面。
        site_urls = [str(item.url or "") for item in ordered]
        kept = [name for name in names if not is_site_name(name, site_urls)]
        if len(kept) != len(names):
            dropped = [name for name in names if name not in kept]
            self._vlog("疑似 X 账号：%s 是候选站点名，不当账号", "、".join(dropped))
        names = kept
        if not names:
            return None
        profiles = await asyncio.gather(*(self._profile(name) for name in names))
        known = {str(item.handle or "").lower() for item in ordered if item.handle}
        # 地址推断只认 X 上的候选、和已经对上图的候选：普通网页的地址里什么词都有，
        # `…for-twitch-con-europe…` 这种从小作文里拼出来的 slug 抽出 @Twitch 也不奇怪。
        urls = [
            item.url
            for item in rows
            if item.url and (is_x_platform(item.url) or item.status in {"match", "vision"})
        ]
        found: list[dict[str, Any]] = []
        for name, info in zip(names, profiles):
            if not info:
                continue
            followers = int(info.get("followers") or 0)
            if followers < self._X_GUESS_MIN_FOLLOWERS:
                continue
            score = handles.match_score(name, texts)
            if score <= 0:
                continue
            # 候选本身就是这个号时再补一句「疑似同一个人的 X」等于说废话，还会
            # 让人以为另有一个号。X 候选的 handle 直接从名单里排掉。
            if name.lower() in known:
                continue
            if anchors and not (
                handles.shares_anchor(name, anchors)
                or handles.supported_by_context(name, texts, anchors)
            ):
                self._vlog("疑似 X 账号：@%s 跟已对上的名字对不上，丢掉", name)
                continue
            lower_name = name.lower()
            source = next(
                (
                    item
                    for item in rows
                    if lower_name in self._name_text(item).lower()
                ),
                None,
            ) or next(
                (
                    item
                    for item in rows
                    if handles.stem(name) in self._name_text(item).lower()
                ),
                None,
            )
            found.append(
                {
                    "handle": str(info.get("screen_name") or name),
                    "followers": followers,
                    "name": str(info.get("name") or "")[:40],
                    "source_url": source.url if source is not None else "",
                    "source_platform": source.platform if source is not None else "",
                    "source_title": " ".join((source.title or source.name or "").split())[:60]
                    if source is not None
                    else "",
                    "score": score,
                    "guess": name,
                    "slug": handles.in_url_slug(name, urls),
                }
            )
        if not found:
            return None
        # 搬运站的标题常并列好几个别名（「cw / heyimbee / heyymimimi」）。只按粉丝
        # 挑会选中最出名的那个，而它可能只是顺带抄进来的无关大号——实测 fapeza 这
        # 单就挑了 56 万粉的 @heyimbee，实际是路径里的 @heyymimimi。站点自己的路径
        # 更接近「这套图是谁」，所以路径里出现过的名字优先，其次才比粉丝。
        best = handles.pick_by_slug(found, urls)
        self._vlog(
            "疑似 X 账号：@%s 粉丝=%s 路径命中=%s 来源=%s 标题=%s",
            best["handle"],
            best["followers"],
            "是" if best["slug"] else "否",
            best["source_platform"] or "-",
            best["source_title"] or "-",
        )
        return best

    # 泄露站按名字建收录页：有页说明这个人确实在做 OnlyFans，没页返回 404。
    _PAID_PROBE = "https://leakgallery.com"

    async def _paid_probe(self, name: str) -> dict[str, Any] | None:
        """查这个名字在泄露站有没有收录页，有就把页面上读到的称呼带回来。"""
        url = f"{self._PAID_PROBE}/{name}"
        proxy = self._proxy()

        def fetch() -> tuple[int, str]:
            session = make_session(proxy)
            try:
                response = session.get(url, timeout=15)
                return response.status_code, response.text or ""
            finally:
                session.close()

        try:
            status, html = await asyncio.wait_for(asyncio.to_thread(fetch), timeout=20)
        except Exception:  # noqa: BLE001 - 查不了就当没有，不影响已经搜到的结果
            return None
        if status != 200:
            return None
        info = paid.parse_page(html, name)
        if not info:
            return None
        info["page_url"] = url
        return info

    async def _hunt_paid(self, ordered: list[Candidate]) -> dict[str, Any] | None:
        """从候选标题里认出名字，去泄露站确认这个人有 OnlyFans 付费内容。

        名字必须原样出现在候选标题里，再看站上有没有收录页，两道都过才采信。
        命中后顺带补上 X 和 IG，结果里给的是「同一个人可能在卖的号」，不是原帖。
        """
        # 只看「图确实像」的那几类候选：mismatch 的标题跟这张图无关，里面
        # 的 Gundam、Azur Lane 之类抽出来全是噪声，还会把名额占满，让真正
        # 写着作者名的那条（fapello 的「NENEchi890, NERUchi890」）挤不进来。
        rows = sorted(
            (
                item
                for item in ordered[:12]
                if (item.title or item.name)
                and item.status != "mismatch"
                and not handles.is_blocked_source(item.source)
            ),
            key=self._x_hunt_priority,
        )
        if not rows:
            return None
        # 跟疑似 X 账号同一条道理：一条都对不上图时，名字只是从噪声标题里抽出来的
        # 单词，命中的泄露站页面也是别人的。
        anchors = self._identity_texts(ordered)
        if not anchors:
            return None
        texts = [self._name_text(item) for item in rows]
        names = handles.extract(texts, limit=self._X_HUNT_LIMIT)
        if not names:
            return None
        pages = await asyncio.gather(*(self._paid_probe(name) for name in names))
        known = {str(item.handle or "").lower() for item in ordered if item.handle}
        urls = [item.url for item in rows if item.url]
        found: list[dict[str, Any]] = []
        for name, info in zip(names, pages):
            if not info:
                continue
            score = handles.match_score(name, texts)
            if score <= 0:
                continue
            if name.lower() in known:
                continue
            if anchors and not (
                handles.shares_anchor(name, anchors)
                or handles.supported_by_context(name, texts, anchors)
            ):
                self._vlog("付费线索：%s 跟已对上的名字对不上，丢掉", name)
                continue
            lower = name.lower()
            stem = handles.stem(name)
            source = next(
                (item for item in rows if lower in self._name_text(item).lower()),
                None,
            ) or next(
                (item for item in rows if stem in self._name_text(item).lower()),
                None,
            )
            found.append(
                {
                    "guess": name,
                    "handle": name,
                    "name": str(info.get("name") or name)[:40],
                    "page_url": str(info.get("page_url") or ""),
                    "source_url": source.url if source is not None else "",
                    "source_platform": source.platform if source is not None else "",
                    "source_title": " ".join((source.title or source.name or "").split())[:60]
                    if source is not None
                    else "",
                    "score": score,
                    "x_handle": "",
                    "x_followers": 0,
                    "instagram": "",
                }
            )
        if not found:
            return None
        # 名字选法跟疑似 X 账号一致：站点路径里的名字比标题里并列的别名可信。
        # 路径里的名字在泄露站没有收录页时宁可不说——把别人的付费号安上来，
        # 比少一行提示严重得多。
        slugged = [item for item in found if handles.in_url_slug(item["guess"], urls)]
        if slugged:
            best = max(slugged, key=lambda item: item["score"])
        elif any(handles.in_url_slug(name, urls) for name in names):
            self._vlog("付费线索：路径里的名字在泄露站没有收录页，不再拿并列别名顶上")
            return None
        else:
            # 名字越长越具体，同一批里取最具体的那个，避免短词命中别人的页。
            best = max(found, key=lambda item: item["score"])
        if not best:
            return None
        profile = await self._profile(best["handle"])
        if profile:
            best["x_handle"] = str(profile.get("screen_name") or "")
            best["x_followers"] = int(profile.get("followers") or 0)
            best["instagram"] = paid.instagram_of(profile)
        self._vlog(
            "疑似付费内容：%s 来源=%s X=%s IG=%s 标题=%s",
            best["handle"],
            best["source_platform"] or "-",
            best["x_handle"] or "-",
            best["instagram"] or "-",
            best["source_title"] or "-",
        )
        return best

    # 最多核对这么多个 X 号的外链。候选里同时出现好几个 X 号的情况不多，6 个够用。
    _SAME_PERSON_LIMIT = 6

    async def _hunt_same_person(self, ordered: list[Candidate]) -> dict[str, Any] | None:
        """候选里出现两个以上 X 号时，看它们是否挂着同一条外链。

        coser 常开好几个号，也常互相转发；靠转发关系分不清谁是谁，但同一个
        收款页（Patreon 之类）指向同一个人，这条不靠猜名字。

        但「谁跟谁是一个人」和「谁是这张图的作者」是两件事。所以只把跟这张图
        有关系的号拉进来比：指纹明确对不上的（mismatch）本来就不是这张图的
        结果，凑进来就是把无关的人挂在结论下面。
        """
        pairs: list[tuple[str, str]] = []
        anchors: set[str] = set()
        exact: set[str] = set()
        for item in ordered:
            if not self._is_x(item):
                continue
            handle = str(item.handle or "").strip().lstrip("@")
            if not handle:
                continue
            status = str(item.status or "")
            pairs.append((handle, status))
            if status == "match":
                exact.add(handle.lower())
            if status in same_person.TRUSTED:
                anchors.add(handle.lower())
        handles_seen = same_person.eligible(pairs)[: self._SAME_PERSON_LIMIT]
        if len(handles_seen) < 2:
            return None
        profiles = await asyncio.gather(*(self._profile(handle) for handle in handles_seen))
        table = {
            handle.lower(): profile
            for handle, profile in zip(handles_seen, profiles)
            if isinstance(profile, dict) and profile
        }
        groups = same_person.find_shared(table, anchors=anchors)
        if not groups:
            return None
        top = groups[0]
        members = []
        for handle in top["handles"]:
            info = table.get(handle) or {}
            members.append(
                {
                    "handle": str(info.get("screen_name") or handle),
                    "name": str(info.get("name") or "")[:40],
                    "followers": int(info.get("followers") or 0),
                    "tied": handle in anchors,
                    # 「跟这张图对上的那条」只给指纹真对上的号：视觉判的「同一个人」
                    # 不是同一张图，两个号都标一遍会看成有两个结论。
                    "exact": handle in exact,
                }
            )
        members.sort(key=lambda item: -item["followers"])
        self._vlog(
            "同一个人多号：%s 依据=%s",
            "、".join("@" + item["handle"] for item in members),
            top["link"],
        )
        return {"link": top["link"], "members": members}

    def _rank(
        self,
        candidates: list[Candidate],
        names: list[str],
        prefer_pixiv: bool = False,
    ) -> list[Candidate]:
        order = {name: index for index, name in enumerate(names)}
        status_weight = {"match": 0, "vision": 1, "suspect": 2, "unverified": 3, "mismatch": 4, "error": 5}

        def key(item: Candidate) -> tuple:
            distance = item.distance if item.distance is not None else 999
            similarity = -(item.similarity if item.similarity is not None else 0)
            pixiv_first = 0 if prefer_pixiv and is_pixiv_platform(item.platform, item.url) else 1
            # 引擎顺序放在质量之前：优先级就是展示顺序，照片 Lens 先、插画 SauceNAO 先。
            # X / Twitter 只在指纹距离完全相同时插队，不会盖过更接近的图。
            return (
                status_weight.get(item.status, 2),
                pixiv_first,
                order.get(item.engine, 99),
                item.rank,
                distance,
                0 if self._is_x_post(item) else 1,
                similarity,
            )

        return sorted(candidates, key=key)

    # -- 任务 -------------------------------------------------------------

    def _start_task(
        self,
        raw: bytes,
        image_url: str,
        stream_id: str,
        user_id: str,
        *,
        notify: bool,
        force: bool = False,
    ) -> SearchTask:
        if self._tasks is None:
            raise RuntimeError("插件尚未初始化")
        if not self.config.plugin.enabled:
            raise RuntimeError("插件已禁用")
        if self._tasks.running_count() >= max(1, self.config.plugin.max_concurrent_tasks):
            raise RuntimeError("当前搜索任务太多，请稍后再试")

        task = self._tasks.new_task(stream_id, user_id)
        self._live[task.task_id] = {
            "stream_id": stream_id,
            "user_id": user_id,
            "started_at": time.time(),
        }
        background = asyncio.create_task(self._run_task(task, raw, image_url, notify, force))
        self._background.add(background)
        background.add_done_callback(self._background.discard)
        self._schedule_slow_ping(task)
        return task

    def _schedule_slow_ping(self, task: SearchTask) -> None:
        """给一单挂上「3 分钟还没跑完就补句话」的定时器。

        实测兜底单中位数 86 秒、最慢 373 秒，中间那段静默最容易让人觉得卡死。
        指令触发和 Tool 触发都走 _start_task，所以两边都能吃到这条提示。
        """
        timer = asyncio.create_task(self._slow_ping(task))
        self._slow_pings[task.task_id] = timer

        def _forget(done: asyncio.Task, key: str = task.task_id) -> None:
            self._slow_pings.pop(key, None)
            self._background.discard(done)

        timer.add_done_callback(_forget)

    async def _slow_ping(self, task: SearchTask) -> None:
        try:
            await asyncio.sleep(formatting.SLOW_PING_SECONDS)
        except asyncio.CancelledError:
            return
        if task.status != "running":
            return
        sent = await self.ctx.send.text(formatting.SLOW_PING_TEXT, task.stream_id)
        if not _send_ok(sent):
            self.ctx.logger.warning("慢任务提示发送失败：task=%s result=%s", task.task_id, sent)
            return
        self._vlog(
            "慢任务提示已发送：task=%s 已等待=%ss 发往=%s",
            task.task_id,
            round(task.elapsed),
            task.stream_id,
        )

    def _cancel_slow_ping(self, task_id: str) -> None:
        timer = self._slow_pings.pop(task_id, None)
        if timer is not None:
            timer.cancel()

    async def _run_task(self, task: SearchTask, raw: bytes, image_url: str, notify: bool, force: bool = False) -> None:
        try:
            outcome = await self._search(raw, image_url, task.stream_id, force=force)
            if self._tasks:
                self._tasks.finish(task, outcome)
            self._vlog(
                "搜图完成：task=%s 状态=%s 路线=%s 候选=%s 引擎=%s 耗时=%sms 缓存=%s 错误=%s",
                task.task_id,
                outcome.status,
                outcome.route,
                outcome.total_candidates,
                outcome.engine_counts,
                outcome.elapsed_ms,
                "是" if outcome.cached else "否",
                outcome.errors or "无",
            )
            if outcome.best is not None:
                self._vlog(
                    "最佳匹配：引擎=%s 平台=%s 名字=%s 账号=%s 距离=%s 相似度=%s 图源=%s",
                    outcome.best.engine,
                    outcome.best.platform,
                    outcome.best.name,
                    outcome.best.handle,
                    outcome.best.distance,
                    outcome.best.similarity,
                    outcome.image_source or "无",
                )
            if notify:
                await self._send_forward(task, self.config.plugin.reference_limit)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001
            self.ctx.logger.error("搜图任务失败：%s", error, exc_info=True)
            if self._tasks:
                self._tasks.fail(task, str(error))
            if notify and task.stream_id:
                await self.ctx.send.text(f"搜图失败：{error}", task.stream_id)
        finally:
            # 任务收尾就掐掉补话定时器，别在结果已经发出后再补一句「还在找」。
            self._cancel_slow_ping(task.task_id)
            self._live.pop(task.task_id, None)

    # QQ 单条合并转发的图片总量有上限，超过后 send_group_forward_msg 会被对端拒绝。
    # 实测 6 张图能过、13 张失败，所以「更多」按这个预算拆成几条发。
    _MAX_FORWARD_IMAGES = 6
    # 首次结果的普通图文消息最多带几张图：结论配原图加一张最像的候选就够了。
    _MAX_FIRST_IMAGES = 3

    @staticmethod
    def _planned_images(outcome: SearchOutcome, groups: list[tuple[str, list[Candidate]]]) -> int:
        total = 1 if outcome.image_b64 else 0
        for _engine, items in groups:
            total += sum(1 for item in items if formatting.candidate_image(outcome, item))
        return total

    async def _send_engine_forwards(
        self,
        task: SearchTask,
        groups: list[tuple[str, list[Candidate]]],
        section: Any,
        target: str = "",
    ) -> None:
        """图太多时按引擎拆成几条合并转发，每条不超过 6 张图。"""
        outcome = task.outcome
        if outcome is None:
            return
        where = target or task.stream_id
        batches: list[tuple[str, list[Candidate]]] = []
        for engine, items in groups:
            chunk: list[Candidate] = []
            used = 0
            for item in items:
                cost = 1 if formatting.candidate_image(outcome, item) else 0
                if chunk and used + cost > self._MAX_FORWARD_IMAGES:
                    batches.append((engine, chunk))
                    chunk, used = [], 0
                chunk.append(item)
                used += cost
            if chunk:
                batches.append((engine, chunk))
        if not batches:
            return

        total = len(batches)
        delivered = 0
        for index, (engine, items) in enumerate(batches, start=1):
            nodes = formatting.build_engine_forward_nodes(
                task,
                engine,
                items,
                command=COMMAND,
                nickname=section.nickname,
                ttl_minutes=section.task_ttl_minutes,
                user_id=self._self_id or "0",
                part=(index, total),
                with_best=(index == 1),
                include_footer=(index == total),
            )
            try:
                sent = await self.ctx.send.forward(nodes, where)
            except Exception as error:  # noqa: BLE001 - 单批失败不影响后续批次
                self.ctx.logger.warning("分条转发异常：批次=%s/%s 引擎=%s err=%s", index, total, engine, error)
                continue
            self._vlog(
                "分条合并转发：task=%s 发往=%s 批次=%s/%s 引擎=%s 候选=%s 图=%s 返回类型=%s 成功=%s",
                task.task_id,
                where,
                index,
                total,
                engine,
                len(items),
                sum(1 for item in nodes for part in item.get("content") or [] if part.get("type") == "image"),
                type(sent).__name__,
                _send_ok(sent),
            )
            if _send_ok(sent):
                delivered += 1
            else:
                self.ctx.logger.warning("分条转发被拒绝：批次=%s/%s 引擎=%s", index, total, engine)
            await asyncio.sleep(1)
        if delivered == 0:
            text = formatting.build_text_summary(
                task,
                command=COMMAND,
                show_reference=section.show_reference,
                limit=self.config.plugin.reference_limit,
                eta_text=self._eta_text(),
            )
            fallback = await self.ctx.send.text(text, where)
            if not _send_ok(fallback):
                self.ctx.logger.warning("分条转发全部失败，文字退路也失败：task=%s", task.task_id)

    async def _send_forward(
        self,
        task: SearchTask,
        limit: int,
        *,
        replay: bool = False,
        target: str = "",
    ) -> None:
        if self._tasks is None or not task.stream_id:
            return
        # target 是「这条消息发到哪」：搜图完成时就是任务自己的会话，重发（更多）
        # 时是发指令的那个会话。两者可能不是同一个。
        where = target or task.stream_id
        await self._ensure_image(task)
        await self._ensure_engine_images(task, limit=limit, whole=replay)
        section = self.config.plugin
        outcome = task.outcome
        if not replay and section.plain_first_reply:
            # 开关打开：首次结果换成一条普通图文消息。合并转发卡片偶发拿不到新的
            # real_seq，客户端会整条不画；普通消息不受这个影响。
            await self._send_first_reply(task, where)
            return

        if replay:
            if outcome is not None:
                groups = formatting.engine_groups(outcome, outcome.best, limit)
                if len(groups) > 1 and self._planned_images(outcome, groups) > self._MAX_FORWARD_IMAGES:
                    await self._send_engine_forwards(task, groups, section, target=where)
                    return
            # 「更多」是重发全部候选，保持单条结构（图多时会走上面的按引擎拆条）。
            blocks = [
                formatting.build_forward_nodes(
                    task,
                    command=COMMAND,
                    show_reference=section.show_reference,
                    limit=limit,
                    nickname=section.nickname,
                    ttl_minutes=section.task_ttl_minutes,
                    replay=replay,
                    user_id=self._self_id or "0",
                )
            ]
        else:
            # 开关关着（默认）：首次结果还是合并转发。视觉、Grok、页脚各占一个节点，
            # 最后塞进同一条转发——两种来源的结论和配图挤在一个节点里，用户分不清哪张
            # 属于哪个判断。
            blocks = formatting.build_result_forwards(
                task,
                command=COMMAND,
                show_reference=section.show_reference,
                limit=limit,
                nickname=section.nickname,
                ttl_minutes=section.task_ttl_minutes,
                replay=replay,
                user_id=self._self_id or "0",
            )

        delivered = 0
        last_error: Exception | None = None
        merged = [node for block in blocks for node in block]
        image_count = formatting.count_forward_images(merged)
        # 视觉、Grok、页脚三条塞进同一条合并转发：群里只出一条消息，点开是三条。
        # 图多到会被 NapCat 拒收时（实测 6 张能过、13 张失败）还是逐条发，
        # 宁可多出几条消息，也别让整条转发失败。
        if len(blocks) > 1 and image_count <= self._MAX_FORWARD_IMAGES:
            self._vlog(
                "合并转发合并发送：task=%s 块=%s 节点=%s 图=%s",
                task.task_id,
                len(blocks),
                len(merged),
                image_count,
            )
            blocks = [merged]

        for index, nodes in enumerate(blocks, start=1):
            try:
                sent = await self.ctx.send.forward(nodes, where)
            except Exception as error:  # noqa: BLE001 - 一块失败不影响后面的块
                last_error = error
                self.ctx.logger.warning("合并转发失败：块=%s/%s err=%s", index, len(blocks), error)
                continue
            self._vlog(
                "合并转发已发送：task=%s replay=%s 发往=%s 块=%s/%s 节点=%s 图=%s 返回类型=%s 成功=%s",
                task.task_id,
                replay,
                where,
                index,
                len(blocks),
                len(nodes),
                sum(1 for item in nodes for part in item.get("content") or [] if part.get("type") == "image"),
                type(sent).__name__,
                _send_ok(sent),
            )
            if _send_ok(sent):
                delivered += 1
            else:
                self.ctx.logger.warning("合并转发被拒绝：块=%s/%s", index, len(blocks))
            if index < len(blocks):
                # 连着发多条容易被适配器判成刷屏，隔一秒。
                await asyncio.sleep(1)

        # 卡片发出去就结束，不再补「结果已发」那句文字：用户明确不要这条。
        # 卡片本身偶发不渲染仍然没解，要靠 plain_first_reply 开关换发法来解决。

        # 转发失败要留一句人话，否则群里只看到「正在搜图」就没了下文。
        if delivered == 0:
            self.ctx.logger.warning("合并转发全部失败，退回文字消息：%s", last_error or "被拒绝")
            text = formatting.build_text_summary(
                task,
                command=COMMAND,
                show_reference=section.show_reference,
                limit=limit,
                eta_text=self._eta_text(),
            )
            fallback = await self.ctx.send.text(text, where)
            if not _send_ok(fallback):
                self.ctx.logger.warning("转发退路也失败：task=%s 文本长度=%s", task.task_id, len(text))

    async def _send_first_reply(self, task: SearchTask, where: str) -> None:
        """首次结果：一条普通图文消息，文字结论在前，最多跟三张关键图。

        只在 `plain_first_reply` 打开时走这里。合并转发卡片偶发拿不到新的 real_seq
        （QQ 把它挂到上一条消息上），客户端按 seq 去重就整条不画，而插件这边只能拿到
        一个 bool，既预知不了也补救不了。普通消息不受这个影响，代价是群里多占一行；
        全部候选仍在「更多」里。
        """
        section = self.config.plugin
        text, images = formatting.build_first_reply(
            task,
            command=COMMAND,
            ttl_minutes=section.task_ttl_minutes,
            max_images=self._MAX_FIRST_IMAGES,
        )
        sent: Any = None
        if images:
            segments: list[dict] = [{"type": "text", "data": text}]
            segments.extend({"type": "image", "content": image} for image in images)
            try:
                sent = await self.ctx.send.hybrid(segments, where)
            except Exception as error:  # noqa: BLE001 - 图文失败就退回纯文字
                self.ctx.logger.warning("首次结果图文发送失败，改用纯文字：%s", error)
                sent = None
        if not _send_ok(sent):
            sent = await self.ctx.send.text(text, where)
        self._vlog(
            "首次结果已发送：task=%s 发往=%s 图=%s 文字=%s字 返回类型=%s 成功=%s",
            task.task_id,
            where,
            len(images),
            len(text),
            type(sent).__name__,
            _send_ok(sent),
        )
        if not _send_ok(sent):
            self.ctx.logger.warning("首次结果发送失败：task=%s", task.task_id)

    # -- 等图 -------------------------------------------------------------

    def _pending_key(self, kwargs: dict, stream_id: str) -> tuple[str, str]:
        return (self._stream_id(kwargs) or stream_id, self._user_id(kwargs))

    async def _wait_for_image(self, current: str, user_id: str, quoted: bool = False, force: bool = False) -> None:
        """指令没带图：提示一次，并在超时时间内接住用户补发的单图。"""
        key = (current, user_id)
        seconds = max(10, self.config.plugin.image_wait_seconds)
        self._pending[key] = time.time() + seconds
        self._pending_force[key] = bool(force)
        self._vlog("进入等待图片窗口：key=%s 秒数=%s 引用=%s 重搜=%s", key, seconds, quoted, force)
        if quoted:
            # 引用消息里可能确实有图，但机器人这边没收到过那条消息，取不到只能请用户直接发。
            text = (
                f"引用的那条消息我这边没有存底（大概是机器人不在线时发的，或者太早了），取不到图。"
                f"把图直接发出来再发「{COMMAND}」，我在 {seconds} 秒内等你。"
            )
        else:
            text = f"没看到图片。把图发出来，或者引用一条带图的消息再发「{COMMAND}」，我在 {seconds} 秒内等你。"
        sent = await self.ctx.send.text(text, current)
        if not _send_ok(sent):
            self.ctx.logger.warning("等待图片提示发送失败：key=%s result=%s", key, sent)
        old = self._pending_timers.pop(key, None)
        if old is not None:
            old.cancel()
        timer = asyncio.create_task(self._expire_pending(key, seconds))
        self._pending_timers[key] = timer
        self._background.add(timer)
        timer.add_done_callback(self._background.discard)

    async def _expire_pending(self, key: tuple[str, str], seconds: int) -> None:
        try:
            await asyncio.sleep(seconds + 1)
        except asyncio.CancelledError:
            return
        if self._pending.pop(key, None) is None:
            return
        self._pending_force.pop(key, None)
        self._pending_timers.pop(key, None)
        if not key[0]:
            return
        sent = await self.ctx.send.text("等图超时了，重新发图再发指令就行。", key[0], set_reply=True)
        if not _send_ok(sent):
            self.ctx.logger.warning("等待图片超时提示（引用回复）发送失败，改用普通消息重试：key=%s", key)
            await self.ctx.send.text("等图超时了，重新发图再发指令就行。", key[0])

    # -- Tool 组件 --------------------------------------------------------

    @Tool(
        "image_search",
        brief_description="用户发来图片、想查这张图的原始出处或作者社交账号时使用。",
        detailed_description=(
            "对聊天里的某张图片做以图搜图，反查原始出处与账号。"
            "上下文里每条消息都带 <message msg_id=\"...\">，想搜哪张图就把那条消息的 msg_id 传进来，"
            "几小时前的图也能取；不传时依次取「用户引用的图」和「当前会话最近 3 分钟内收到的图」。"
            "搜索通常要 1 分钟上下（开了深度检索更久），因此本工具立即返回 task_id，不等待结果。"
            "返回后请告诉用户「正在搜索，约 1 分钟」，稍后再调用 search_status 取结果。"
            "本工具只返回文字摘要，不会发图片。"
        ),
        parameters=[
            ToolParameterInfo(
                name="msg_id",
                param_type=ToolParamType.STRING,
                description=(
                    "要搜的那条带图消息的 msg_id，从上下文 <message msg_id=\"...\"> 里抄，只填数字。"
                    "不填就取用户引用的图或最近 3 分钟内的图。"
                ),
                required=False,
            ),
            ToolParameterInfo(
                name="note",
                param_type=ToolParamType.STRING,
                description="可选，本次搜索的备注，会写进日志",
                required=False,
            ),
            ToolParameterInfo(
                name="force",
                param_type=ToolParamType.BOOLEAN,
                description=(
                    "可选。为 true 时忽略按图缓存强制重搜；用户说「重搜」「重新查」「刚才那个再搜一遍」时用它。"
                    "默认 false，同一张图搜过会直接返回上次结果。"
                ),
                required=False,
            ),
        ],
    )
    async def image_search(self, note: str = "", msg_id: str = "", force: bool = False, **kwargs: Any) -> dict[str, Any]:
        stream_id = self._stream_id(kwargs)
        user_id = self._user_id(kwargs)
        forced = _as_bool(force)
        wanted = "".join(ch for ch in str(msg_id or "") if ch.isdigit())
        if wanted:
            # 模型点名了具体消息：直接按 ID 取，不受 3 分钟窗口限制。
            found_raw, found_url = await self._image_by_id(wanted, stream_id)
            if found_raw:
                raw, image_url, source, stale = found_raw, found_url, "指定的消息", 0
            else:
                message = (
                    f"按 msg_id={wanted} 没取到图片。请确认那是带图消息的 msg_id（上下文里 <message msg_id=\"...\"> 的数字），"
                    "或者让用户引用那张图再试。"
                )
                self.ctx.logger.warning("Tool 指定的消息没有图片：msg_id=%s", wanted)
                return {"success": False, "content": message, "message": message}
        else:
            raw, image_url, source, stale = await self._resolve_image(kwargs, allow_recent=True)
        self._vlog("Tool 取图：来源=%s 体积=%sKB 链接=%s", source or "无", len(raw) // 1024 if raw else 0, (image_url or "无")[:80])
        if not raw:
            if stale:
                message = formatting.stale_image_hint(stale)
            else:
                message = "没有找到可用的图片。请让用户先发一张图，或引用一条带图的消息再试。"
            return {"success": False, "content": message, "message": message}

        try:
            task = self._start_task(raw, image_url, stream_id, user_id, notify=True, force=forced)
        except Exception as error:  # noqa: BLE001
            return {"success": False, "content": f"启动搜图失败：{error}", "message": str(error)}

        # 工具调用同样是「说完就不管」，聊天里先落一句进度，机器人的思考模型
        # 下一轮才看得到当前到底在干什么。
        await self.ctx.send.text(
            f"🔍 正在搜图（图片来源：{source}{'，已忽略缓存' if forced else ''}），"
            f"{formatting.eta_full()}，结果稍后以合并转发发出。",
            stream_id,
        )

        message = (
            f"已开始搜索（task_id={task.task_id}，图片来源：{source}"
            f"{'，已忽略缓存' if forced else ''}），预计 {formatting.eta_full()}。"
            f"结果会以合并转发发到聊天里；也可以用 search_status 凭 task_id 取文字摘要。"
            f"如果超过 {formatting.SLOW_PING_SECONDS // 60} 分钟还没出结果，我会在聊天里回一句"
            f"「{formatting.SLOW_PING_TEXT}」，那说明确实在慢慢对比，不是卡死了。"
        )
        self.ctx.logger.info("Tool 启动搜图：task=%s user=%s note=%s", task.task_id, user_id, note)
        return {
            "success": True,
            "task_id": task.task_id,
            "content": message,
            "message": message,
        }

    @Tool(
        "search_status",
        brief_description="查询以图搜图任务的进度或结果。",
        detailed_description=(
            "凭 task_id 查询 image_search 启动的搜索任务；不传 task_id 时取当前会话最近一次任务。"
            "任务还在跑就返回进度提示；已完成则返回最佳匹配的平台、名字、账号、链接与 AI 检测结论。"
            "结果只包含文字，不含图片。"
        ),
        parameters=[
            ToolParameterInfo(
                name="task_id",
                param_type=ToolParamType.STRING,
                description="image_search 返回的任务编号；留空表示最近一次任务",
                required=False,
            ),
        ],
    )
    async def search_status(self, task_id: str = "", **kwargs: Any) -> dict[str, Any]:
        if self._tasks is None:
            return {"success": False, "content": "插件尚未初始化"}
        task = self._tasks.get(task_id) if task_id else None
        if task is None:
            task = self._latest_task(self._stream_id(kwargs), self._user_id(kwargs))
        if task is None:
            message = (
                f"没有找到对应的搜索任务，可能已经清掉了"
                f"（结果保留 {formatting.ttl_text(self.config.plugin.task_ttl_minutes)}）。"
            )
            return {"success": False, "content": message, "message": message}

        if task.status == "running":
            message = (
                f"任务 {task.task_id} 还在搜索中，已等待 {task.elapsed:.0f} 秒，请稍后再查。"
                f"（{formatting.ETA_SLOW_NOTE}；超过 {formatting.SLOW_PING_SECONDS // 60} 分钟"
                f"还不出结果时，聊天里会有一条「{formatting.SLOW_PING_TEXT}」）"
            )
            return {"success": True, "task_id": task.task_id, "status": "running", "content": message}

        section = self.config.plugin
        summary = formatting.build_text_summary(
            task,
            command=COMMAND,
            show_reference=section.show_reference,
            limit=section.reference_limit,
            eta_text=self._eta_text(),
        )
        return {
            "success": task.status == "done",
            "task_id": task.task_id,
            "status": task.status,
            "content": summary,
            "message": summary,
        }

    # -- Command 组件 -----------------------------------------------------

    @Command(
        "image_search_command",
        description="以图搜出处：群里发图后带上指令即可",
        pattern=_COMMAND_PATTERN,
    )
    async def image_search_command(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, int]:
        return await self._handle_command(stream_id, kwargs)

    @Command(
        "image_search_more",
        description="重发上次搜图结果",
        pattern=_MORE_PATTERN,
    )
    async def image_search_more(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, int]:
        if self._tasks is None:
            return False, "插件尚未初始化", 0
        current = self._stream_id(kwargs) or stream_id
        user_id = self._user_id(kwargs)
        matched = kwargs.get("matched_groups") or {}
        wanted = str(matched.get("task") or "").strip()
        return await self._replay_task(current, user_id, wanted)

    @Command(
        "image_search_capture",
        description="接住等待中的单张图片消息",
        pattern=_CAPTURE_PATTERN,
    )
    async def image_search_capture(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, int]:
        current = self._stream_id(kwargs) or stream_id
        user_id = self._user_id(kwargs)
        key = (current, user_id)
        deadline = self._pending.get(key)
        if not deadline or time.time() > deadline:
            return False, "", 0

        raw, image_url, _source, _stale = await self._resolve_image(kwargs, allow_recent=False)
        if not raw:
            self.ctx.logger.warning("等待中的图片消息没有取到图，继续等：key=%s", key)
            await self.ctx.send.text("这张图我这边没取到，再发一次试试。", current)
            return False, "", 0

        self._pending.pop(key, None)
        self._pending_force.pop(key, None)
        timer = self._pending_timers.pop(key, None)
        if timer is not None:
            timer.cancel()

        try:
            self._start_task(raw, image_url, current, user_id, notify=True)
        except Exception as error:  # noqa: BLE001
            await self.ctx.send.text(f"启动搜图失败：{error}", current)
            return False, str(error), 1

        await self.ctx.send.text(f"🔍 收到图片，开始搜图，{formatting.eta_full()}。", current)
        return True, "搜图中", 2

    async def _handle_command(self, stream_id: str, kwargs: dict) -> tuple[bool, str, int]:
        current = self._stream_id(kwargs) or stream_id
        user_id = self._user_id(kwargs)
        matched = kwargs.get("matched_groups") or {}
        arg = str(matched.get("arg") or "").strip()

        # 「重搜」= 无视按图缓存，强制重新跑一遍引擎。写在最前面，后面还能跟「更多」。
        force, arg = split_force(arg)
        head = (arg.split() or [""])[0]
        # 「xid」= 拿一条已知推文来核对：帖子里的图和用户发的图是不是同一张。
        xid_ref = ""
        if head.lower() in {"xid", "核对", "推文"}:
            parts = arg.split(maxsplit=1)
            xid_ref = parts[1].strip() if len(parts) > 1 else ""
        if force:
            self._vlog("指令要求强制重搜：忽略按图缓存")

        if head in {"更多", "more", "全部"}:
            # 支持「更多 s-20261103-0832-d7d9」：群里同时有好几次搜索时靠编号定位。
            parts = arg.split(maxsplit=1)
            wanted = parts[1].strip() if len(parts) > 1 else ""
            return await self._replay_task(current, user_id, wanted)

        if arg in {"状态", "status"}:
            if self._tasks is not None:
                task = self._latest_task(current, user_id)
                if task is None:
                    await self.ctx.send.text("当前没有进行中的搜索任务。", current)
                elif task.status == "running":
                    await self.ctx.send.text(f"任务 {task.task_id} 搜索中，已等待 {task.elapsed:.0f} 秒。", current)
                else:
                    await self.ctx.send.text(f"任务 {task.task_id} 状态：{task.status}。", current)
            return True, "状态已发送", 2

        if not self.config.plugin.enabled:
            return False, "插件已禁用", 0
        if not current:
            return False, "无法获取会话 ID", 1

        raw, image_url, source, _stale = await self._resolve_image(kwargs)
        self._vlog("指令取图：来源=%s 体积=%sKB 链接=%s", source or "无", len(raw) // 1024 if raw else 0, (image_url or "无")[:80])

        if head.lower() in {"xid", "核对", "推文"}:
            if not xid_ref:
                await self.ctx.send.text(
                    f"把推文链接或 ID 写在后面，例如「{COMMAND} xid 2105595040806707433」。",
                    current,
                )
                return True, "缺少推文 ID", 2
            if not raw:
                await self.ctx.send.text(
                    f"核对要把图和指令发在同一条消息里：「{COMMAND} xid <链接或 ID>」。",
                    current,
                )
                return True, "缺少图片", 2
            check = await self._verify_tweet(xid_ref, raw)
            await self.ctx.send.text(self._xid_reply(check), current)
            return True, "核对完成", 2

        if not raw:
            message = kwargs.get("message")
            quoted = bool(_reply_target(_segments_of(message), message if isinstance(message, dict) else {}))
            await self._wait_for_image(current, user_id, quoted=quoted, force=force)
            return True, "等待图片", 2

        try:
            task = self._start_task(raw, image_url, current, user_id, notify=True, force=force)
        except Exception as error:  # noqa: BLE001
            await self.ctx.send.text(f"启动搜图失败：{error}", current)
            return False, str(error), 1

        await self.ctx.send.text(
            f"🔍 正在搜图（{source}{'，已忽略缓存' if force else ''}），"
            f"{formatting.eta_full()}，结果稍后以合并转发发出。",
            current,
        )
        return True, "搜图中", 2


def create_plugin() -> ImageSearcherPlugin:
    """创建插件实例。"""
    return ImageSearcherPlugin()
