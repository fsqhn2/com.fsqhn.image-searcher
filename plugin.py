"""麦麦寻人：以图搜出处的 MaiBot 插件。

Tool 给 planner 用（只回纯文字），Command 给群友用（合并转发）。
两条路的搜索逻辑完全一致，区别只在呈现方式。
"""

from __future__ import annotations

import asyncio
import base64
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
    fxtwitter,
    formatting,
    grok,
    handles,
    identity,
    paid,
    paywall,
    resultcache,
    same_person,
    vision,
)
from .core.commands import build_command_pattern, build_more_pattern, split_force
from .core.config import migrate_legacy_sections
from .core.engines import (
    ENGINES_VERSION,
    BaiduEngine,
    Candidate,
    EngineError,
    LensEngine,
    SauceNaoEngine,
    YandexEngine,
    detect_ai,
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
    is_x_platform,
    is_x_post_page,
    x_page_handle,
    x_page_kind,
    x_title_name,
)
from .core.gallery import is_gallery_site, is_repost_site, parse_source_accounts
from .core.hashing import dhash, hamming, prepare_upload
from .core.httpclient import HttpError, download_image_multi, fake_ip_networks, make_session
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
            "搬运站命中时也读水印：指纹已经命中，但落点是 arca.live、dcinside、reddit、"
            "贴吧这类社区/搬运站、又没给出账号时，仍然把原图交给视觉模型读一遍水印。"
            "这类页面往往只是转贴，图上写的水印账号才是唯一还能指到本人的线索；"
            "只在落点没有账号时触发，读到的水印仍只当线索，不会因此改掉已经命中的那条。"
        ),
        json_schema_extra=_ui(
            "搬运站命中时也读水印",
            "默认开。命中搬运站时额外跑一次视觉读水印，命中本人的带图帖才会顶掉搬运站那条；"
            "关掉就沿用原来的行为——有命中就收工，不再花这一次调用。",
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
    watermark_tiles: bool = Field(
        default=True,
        description=(
            "水印分块重读：整图读不出水印时，把原图切成六块（上下三行×左右两列，"
            "带重叠），一块一块放大再读，读到账号就停。整图读不出不一定是分辨率不够，"
            "而是那行字在整幅画面里占比太小，模型注意力落不到它身上。"
        ),
        json_schema_extra=_ui(
            "水印分块重读（原图切块）",
            "默认开。只在整图读不出、或读到的号核不上时触发，最多多六次视觉调用；"
            "关掉就只读整图。",
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
    engine_timeout: int = Field(
        default=50,
        description="单个搜索引擎最多等多少秒，超时就跳过它、继续用其他引擎的结果。",
        json_schema_extra=_ui("单个引擎超时（秒）", "默认 50。Yandex 走代理容易抖，网络差可以再调到 60。"),
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
        description="唯一一个代理设置：所有搜索引擎、候选图下载、AI 检测都走它。留空表示直连。",
        json_schema_extra=_ui(
            "代理地址",
            "强烈建议填写。例如 http://127.0.0.1:6789。Lens、Yandex 对这些服务的国内直连基本不通，不填代理通常搜不出结果。",
            placeholder="http://127.0.0.1:6789",
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


class ImageSearcherConfig(PluginConfigBase):
    """插件总配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    engines: EnginesConfig = Field(default_factory=EnginesConfig)
    lens: LensConfig = Field(default_factory=LensConfig)
    yandex: YandexConfig = Field(default_factory=YandexConfig)
    baidu: BaiduConfig = Field(default_factory=BaiduConfig)
    saucenao: SauceNaoConfig = Field(default_factory=SauceNaoConfig)
    vision: VisionConfig = Field(default_factory=VisionConfig)
    grok: GrokConfig = Field(default_factory=GrokConfig)
    ai_detect: AiDetectConfig = Field(default_factory=AiDetectConfig)


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

    async def on_unload(self) -> None:
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

        global_proxy = _proxy_of(section.proxy)
        self._pools = {
            "lens": KeyPool(config.lens.keys),
            "saucenao": KeyPool(config.saucenao.keys),
            "ai": KeyPool(config.ai_detect.keys),
        }
        self._engines = {
            "lens": LensEngine(
                self._pools["lens"],
                global_proxy,
                section.engine_timeout,
                config.lens.retries,
                config.lens.retry_delay,
            ),
            "yandex": YandexEngine(global_proxy, section.engine_timeout),
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

    def _proxy(self) -> str:
        """引擎、候选图下载、AI 检测共用的那一个代理。"""
        return _proxy_of(self.config.plugin.proxy)

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

        best 为空（没找到可靠出处）时照样给一张参考图，方便用户核对是不是同一张：
        视觉方案挑中的那张排最前，其次才是指纹距离最近的。候选全失败时退回用户
        原图；没有原图（比如重放磁盘恢复的任务）就返回空。
        """
        pool = reference_candidates(best, ordered, picked)
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
        if outcome.status == "ok" and not whole:
            # 命中时首次回复只有结论，参考区要等「更多」，不必提前下载这些配图。
            return
        targets: list[str] = []
        for engine, items in formatting.engine_groups(outcome, outcome.best, limit):
            if not whole and outcome.best is not None and engine == outcome.best.engine:
                continue  # 首次回复只补其他引擎的图，命中那条所在引擎不重复给
            picked = items if whole else items[:1]
            targets.extend(item.image_url for item in picked if item.image_url)
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

    async def _search(self, raw: bytes, image_url: str = "", stream_id: str = "", force: bool = False) -> SearchOutcome:
        config = self.config
        section = config.plugin
        started = time.time()
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

        timeout = section.engine_timeout + 5
        loop = asyncio.get_running_loop()
        coros = [
            asyncio.wait_for(
                loop.run_in_executor(self._engine_pool, self._call_engine, name, upload, image_url, stream_id),
                timeout=timeout,
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
        needs_watermark = bool(
            had_best
            and config.vision.watermark_on_repost
            and best is not None
            and not (best.handle or "").strip()
            and is_repost_site(best.url or "", best.platform or "")
        )
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
                    )
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
                    await self._reread_watermark(vision_info, grok_info, upload)
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

        # 引用帖不是原帖，转发的原帖也不属于转发者：指纹已经对上的 X 候选都换回
        # 原作者与那条帖子。只挑 match/suspect，mismatch 的图本来就不是这张。
        best = await self._follow_quotes(ordered, best)
        # 图上水印核对到本人发过这张图时，这条就是结论：指纹对得上、又是本人
        # 亲手发的帖，比「视觉说是同一个人」硬得多。
        best = self._promote_watermark_hit(best, vision_info)
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
        if (
            upload
            and bool(getattr(config.vision, "signature_hunt", True))
            and (
                best is None
                or (
                    best.status == "match"
                    and not (best.handle or "").strip()
                    and not self._is_x(best)
                )
            )
        ):
            try:
                signature_author = await self._hunt_signature_author(ordered, upload)
            except Exception as error:  # noqa: BLE001 - 反查不到不影响已经命中的那条
                self.ctx.logger.warning("标题署名反查失败：%s", error)
                signature_author = None
            if signature_author and signature_author.get("status") == "hit":
                best = self._promote_signature_author(best, signature_author)
        # 同一张图落在普通网页上（搬运站/图集页，页面上没有账号）时别停在那儿：
        # 候选里出现过的 X 号挨个翻一遍，谁自己的帖子里有这张图谁就是结论。
        repost_author: dict[str, Any] | None = None
        if (
            upload
            and best is not None
            and best.status == "match"
            and not (best.handle or "").strip()
            and not self._is_x(best)
            and bool(getattr(config.vision, "repost_author", True))
        ):
            try:
                repost_author = await self._hunt_repost_author(ordered, best, upload)
            except Exception as error:  # noqa: BLE001 - 翻不到不影响已经命中的那条
                self.ctx.logger.warning("搬运站转贴反查失败：%s", error)
                repost_author = None
            if repost_author and repost_author.get("status") == "hit":
                best = self._promote_repost_author(best, repost_author)
        # 结论里的账号核一下死活：被封的号 handle 会被释放，只剩数字 ID 能对上人。
        best = await self._check_best_account(best, vision_info)

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
        if bool(getattr(config.vision, "paywall", True)):
            author = ""
            post_url = ""
            if best is not None and (best.handle or "").strip():
                author = best.handle.strip().lstrip("@")
                if self._is_x(best) and "/status/" in (best.url or ""):
                    post_url = best.url
            elif (grok_info or {}).get("tier") == "post":
                author = str((grok_info or {}).get("handle") or "").strip().lstrip("@")
                post_url = str((grok_info or {}).get("post_url") or "")
            if author:
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
                _proxy_of(section.proxy),
                section.engine_timeout,
            )

        # 指纹命中的常是搬运站，作者本人的 X 号不会出现在候选的账号字段里。
        # 从候选标题里认出疑似名字，去 X 验证存在性；它不改 best、也不覆盖 Grok
        # 已经核到的号，只作为「同一个人可能在这个号」的补充线索。
        x_guess: dict[str, Any] | None = None
        if (
            upload
            and ordered
            and not (grok_info or {}).get("handle")
            and not (best is not None and self._is_x(best))
        ):
            try:
                x_guess = await self._hunt_x_handle(ordered)
            except Exception as error:  # noqa: BLE001 - 猜号失败不影响已经搜到的结果
                self.ctx.logger.warning("疑似 X 账号推断失败：%s", error)

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
            paywall=paywall_info,
        )

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

    async def _gallery_sources(self, items: list[Candidate]) -> list[dict[str, Any]]:
        """读图集站页面上自己标的来源账号。

        视觉挑中的图常常落在 4play、v2ph 这类搬运站上：页面本身不是作者主页，
        所以结论会把它降级。但站方通常写着来源账号——实测 4play 的 meta 直接给
        了 ``instagram.com/menruinyanko``，那是整单唯一指向本人的线索。这里只
        抓降级候选里的图集站页面，最多两页，读不到就当没有。
        """
        pool: list[Candidate] = []
        seen: set[str] = set()
        for item in items:
            url = (item.url or "").strip()
            if not url or url in seen or not is_gallery_site(url, item.platform):
                continue
            seen.add(url)
            pool.append(item)
            if len(pool) >= self._GALLERY_MAX_PAGES:
                break
        if not pool:
            return []

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
        return sources

    async def _page_source(self, url: str) -> list[dict[str, Any]]:
        """抓一页 HTML，抽里面的来源账号。抓不到就返回空，不影响别的结论。"""

        def fetch() -> str:
            session = make_session(self._proxy())
            try:
                response = session.get(
                    url,
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

        try:
            html = await asyncio.wait_for(asyncio.to_thread(fetch), timeout=14)
        except Exception:  # noqa: BLE001 - 页面读不到只少一条线索
            return []
        return parse_source_accounts(html, url)

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
        proxy = self.config.plugin.proxy

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

    def _promote_watermark_hit(
        self, best: Candidate | None, vision_info: dict[str, Any] | None
    ) -> Candidate | None:
        """水印核对到本人发过这张图时，把它当成结论。

        这条证据是「作者的带图帖里指纹对上」，比视觉认人硬；已经有结论时不该被
        它顶掉。唯一的例外是结论本身落在搬运站/社区站又没有账号（只是别人转的
        图，例如 arca.live 那条距离 0 的帖子）——作者本人的带图帖显然更该当结论。
        """
        if not vision_info:
            return best
        if best is not None and not self._is_repost_result(best):
            return best
        post = str(vision_info.get("watermark_post") or "").strip()
        handle = str(vision_info.get("watermark_handle") or "").strip()
        if not post or not handle:
            return best
        account = "@" + handle.lstrip("@")
        distance = int(vision_info.get("watermark_distance") or 0)
        self._vlog("水印核对命中，升成结论：%s %s（距离 %s）", account, post, distance)
        return Candidate(
            engine="watermark",
            url=post,
            platform="X (Twitter)",
            handle=account,
            name=str(vision_info.get("watermark_name") or ""),
            distance=distance,
            status="match",
            note=f"图上水印核对：{account} 自己的带图帖里有这张图（距离 {distance}）",
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
        if not (hit and hit.get("url")):
            # /media 只看得到最近十几条，老图翻不到；搜索那边有时还留着一条，
            # 用 ``from:<号> filter:images`` 再翻一页做兜底。
            fallback = await self._search_media_lookup(handle, upload)
            if fallback and fallback.get("url"):
                hit = fallback
            elif fallback and fallback.get("error") and not (hit and hit.get("error")):
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
        vision_info["watermark_note"] = (
            "最近十几条带图帖和搜索结果里都没有这张，可能是模型读错了"
        )
        self._vlog("水印核对没命中：@%s", handle)

    # 近似写法最多试这么多个：每个都是一次 profile 请求，试太多会把兜底拖成
    # 几十秒，而真能对上的写法通常就在前几个（w 读散、少一个字母这类）。
    _VARIANT_LIMIT = 18

    async def _verify_watermarks(
        self,
        vision_info: dict[str, Any],
        grok_info: dict[str, Any] | None,
        upload: bytes | None,
        *,
        allow_variants: bool,
    ) -> None:
        """把两个模型读到的水印都核一遍，谁翻到这张图谁就是结论。

        只核「号存不存在」是不够的：``@Miharu`` 确实存在，可是个 2010 年的无关
        老号；``@Moliuwy`` 查无此人，真正的 ``@Moliww`` 就是这么漏掉的。所以这里
        对每个读法都走一遍「核账号 → 翻它自己的带图帖比指纹」，查无此人时还可以
        按近似拼写再找（仍要指纹对得上）。
        """
        readings: list[dict[str, Any]] = []
        raw = str(vision_info.get("watermark") or "").strip()
        handle = vision.handle_from_watermark(raw)
        if handle:
            readings.append({"source": "vision", "raw": raw, "handle": handle})
        other_raw = str((grok_info or {}).get("watermark") or "").strip()
        other_handle = vision.handle_from_watermark(other_raw)
        if other_handle and all(item["handle"].lower() != other_handle.lower() for item in readings):
            readings.append({"source": "grok", "raw": other_raw, "handle": other_handle})
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

    async def _reread_watermark(
        self,
        vision_info: dict[str, Any],
        grok_info: dict[str, Any] | None,
        upload: bytes | None,
    ) -> None:
        """整图读不出水印时再读一次，还读不出就切成分块逐块读。

        ac88 那单的水印是 4×4 拼图读的（TARGET 格只有 320px），把 ``Moliww``
        读成了 ``Miharu``；同一张图按原图读就清楚得多。

        整图也读不出来时（s-20261112-0752-4fdf：手机边写着 @kuru_xox，整图回
        「无」）再切块：那行字在整幅画面里占比太小，模型注意力落不到它身上，切成
        六块之后同一行字大了一个数量级。一块一块发，读到账号就停。
        """
        if not upload or not bool(getattr(self.config.vision, "enabled", True)):
            return
        readings = list(vision_info.get("watermark_readings") or [])
        known = {
            str(item.get("handle") or "").lower() for item in readings if isinstance(item, dict)
        }

        if await self._reread_one(
            vision_info, readings, known, upload, data_url="", label=""
        ):
            return
        if not bool(getattr(self.config.vision, "watermark_tiles", True)):
            return
        try:
            tiles = await asyncio.to_thread(vision.tile_data_urls, upload)
        except Exception as error:  # noqa: BLE001 - 切块失败只是少一道兜底
            self.ctx.logger.warning("原图切块失败：%s", error)
            return
        if not tiles:
            return
        self._vlog("整图没读出可用水印，改分块读：共 %s 块", len(tiles))
        for label, data_url in tiles[: self._WATERMARK_TILE_LIMIT]:
            if await self._reread_one(
                vision_info, readings, known, upload, data_url=data_url, label=label
            ):
                self._vlog("分块读在第 %s 块命中原作者，剩下的块不再读", label)
                return

    # 分块最多读这么多块。2×3 正好六块，再多就说明前面几块都没戏，继续读只是烧额度。
    _WATERMARK_TILE_LIMIT = 6

    async def _reread_one(
        self,
        vision_info: dict[str, Any],
        readings: list[dict[str, Any]],
        known: set[str],
        upload: bytes | None,
        *,
        data_url: str,
        label: str,
    ) -> bool:
        """读一次（整图或某一块），读到没见过的号就核它自己的带图帖。

        返回 True 表示这个号自己的帖子里真有这张图——整条分块循环就此收工。
        """
        where = f"[{label}]" if label else ""
        try:
            info = await self._read_watermark(upload, data_url=data_url, label=label)
        except Exception as error:  # noqa: BLE001 - 重读只是加分项
            self.ctx.logger.warning("重读水印失败%s：%s", where, error)
            return False
        if not info.get("ok"):
            self._vlog("重读水印没成%s：%s", where, info.get("error") or "-")
            return False
        raw = str(info.get("watermark") or "").strip()
        handle = vision.handle_from_watermark(raw)
        if not handle:
            self._vlog("重读水印%s：文字=%s，没读出账号", where, raw or "-")
            return False
        if handle.lower() in known:
            self._vlog("重读水印%s：还是 @%s，不重复核", where, handle)
            return False
        reading = await self._verify_watermark_reading(
            {
                "source": f"vision-tile-{label}" if label else "vision-original",
                "raw": raw,
                "handle": handle,
            },
            upload,
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
        return bool(str(reading.get("post") or "").strip())

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
            session = make_session(self.config.plugin.proxy)
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
            session = make_session(self.config.plugin.proxy)
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

    # 翻账号自己的带图帖子时最多看这么多条。这个接口一次只给最近十几条，
    # 多了请求不出去；够不够用取决于图新不新，翻不到就老实说没有。
    _MEDIA_SCAN_POSTS = 12
    _MEDIA_SCAN_IMAGES = 2

    async def _media_lookup(self, handle: str, upload: bytes | None) -> dict[str, Any] | None:
        """翻账号自己的带图帖子（不含转发），用指纹确认这张图是不是它发的。

        比「让模型给链接」硬在两点：列表里只有本人发的东西，转发进不来；命中时
        直接拿到那条帖子的地址，不是「疑似这个人」。代价是只看得到最近十几条。
        """
        if not upload or not handle:
            return None
        url = fxtwitter.build_media_url(handle)
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
        for post in posts[: self._MEDIA_SCAN_POSTS]:
            for link in post.media[: self._MEDIA_SCAN_IMAGES]:
                raw = await self._download(link)
                if not raw:
                    continue
                other = dhash(raw)
                if other is None:
                    continue
                distance = hamming(mine, other)
                if distance <= threshold and (best is None or distance < best["distance"]):
                    # image 留着当参考图：命中帖子的那张配图，转发里可以直接摆出来。
                    best = {
                        "url": post.url,
                        "distance": distance,
                        "created_at": post.created_at,
                        "image": link,
                    }
        self._vlog(
            "翻 @%s 的带图帖子：%s 条，结论=%s",
            handle,
            len(posts),
            f"命中 distance={best['distance']}" if best else "没有这张图",
        )
        return best

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

    async def _weibo_profile(self, uid: str) -> dict[str, Any] | None:
        """微博 UID → 昵称/粉丝/认证。查不到就当没有，不编名字。"""

        def fetch() -> dict[str, Any] | None:
            return identity.fetch_weibo_profile(uid, self._proxy(), timeout=15)

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

    async def _search_x_handles(self, name: str) -> list[str]:
        """昵称 → 疑似 X 号，借用 Lens 那套 SerpAPI key 跑一次网页检索。"""
        pool = self._pools.get("lens")
        key = pool.acquire() if pool else ""
        if not key:
            self._vlog("按名字搜 X：没有可用的 SerpAPI key，跳过")
            return []

        def fetch() -> list[str]:
            return identity.search_x_handles(name, key, self._proxy())

        try:
            found = await asyncio.wait_for(asyncio.to_thread(fetch), timeout=40)
        except Exception as error:  # noqa: BLE001 - 搜不到就当没有这一步
            self._vlog("按名字搜 X 失败：%s", str(error)[:80])
            return []
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

    async def _hunt_signature_author(
        self, ordered: list[Candidate], upload: bytes | None
    ) -> dict[str, Any] | None:
        """顺着标题里的署名（``by XXX``、``coser: XXX``、``@XXX``）去 X 上找本人。

        搬运帖常把原作者名连动词一起抄进标题，那是整条标题里唯一指向本人的部分。
        插件原来只从候选 URL 里抽 handle：s-20261112-0752-4fdf 抽到 miya1st 和
        _ray_wing_ 两个都不对，而标题写着「Vivian cosplay by KURU」，KURU 完全没
        用上，于是停在 reddit 的转载页上。

        找到号还不够：必须那个号自己的带图帖里真有这张图才算数，翻不到就当没有。
        """
        if not upload:
            return None
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
        texts = [self._name_text(item) for item in rows]
        names = handles.signature_names(texts, limit=self._SIGNATURE_NAME_LIMIT)
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
        for name in names:
            if probes >= self._SIGNATURE_PROBE_LIMIT:
                break
            candidates = [item for item in await self._search_x_handles(name) if item]
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
                seen_note = str(holder.get("watermark_note") or "")
        if seen_note:
            return {"status": "seen", "name": names[0], "reason": seen_note}
        return None

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
        """查一个 X 账号的公开资料（fxtwitter，免登录）。"""
        url = f"https://api.fxtwitter.com/2/profile/{handle}"
        proxy = self._proxy()

        def fetch() -> dict[str, Any] | None:
            session = make_session(proxy)
            try:
                response = session.get(url, timeout=15)
                if response.status_code != 200:
                    return None
                payload = response.json()
                user = payload.get("user") if isinstance(payload, dict) else None
                return user if isinstance(user, dict) else None
            finally:
                session.close()

        try:
            return await asyncio.wait_for(asyncio.to_thread(fetch), timeout=20)
        except Exception:  # noqa: BLE001 - 查不到就当没这个号
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
                and item.status != "mismatch"
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
        profiles = await asyncio.gather(*(self._profile(name) for name in names))
        known = {str(item.handle or "").lower() for item in ordered if item.handle}
        urls = [item.url for item in rows if item.url]
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
        for item in ordered:
            if not self._is_x(item):
                continue
            handle = str(item.handle or "").strip().lstrip("@")
            if not handle:
                continue
            status = str(item.status or "")
            pairs.append((handle, status))
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

    # QQ 单条合并转发的图片总量有上限，超过后 send_group_forward_msg 会被对端拒绝。
    # 实测 6 张图能过、13 张失败，所以「更多」按这个预算拆成几条发。
    _MAX_FORWARD_IMAGES = 6

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
        if replay and outcome is not None:
            groups = formatting.engine_groups(outcome, outcome.best, limit)
            if len(groups) > 1 and self._planned_images(outcome, groups) > self._MAX_FORWARD_IMAGES:
                await self._send_engine_forwards(task, groups, section, target=where)
                return
        if replay:
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
            # 视觉、Grok、页脚各占一个节点，最后塞进同一条合并转发：
            # 两种来源的结论和配图挤在一个节点里，用户分不清哪张属于哪个判断。
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
