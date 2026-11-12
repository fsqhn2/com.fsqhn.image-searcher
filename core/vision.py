"""视觉兜底：把目标图和候选图拼成一张网格，交给 MaiBot 的视觉模型挑「同一个人」。

dHash 比的是整图灰度结构，遇到拼接、裁剪、加字、换姿势就会失准：明明是同一
套衣服同一个人，距离也能到 20 以上。这里换一个判据——人物身份是否一致，不
要求同一张图。

只在整轮没有任何 ``match`` 时启用，dHash 的主路逻辑不变。所以这里全部是纯
函数：拼图、写 prompt、解析回复，方便单测，不碰网络也不碰插件上下文。
"""

from __future__ import annotations

import base64
import io
import json
import re
from dataclasses import dataclass
from urllib.parse import urlparse

from PIL import Image, ImageDraw, ImageFont, ImageOps

from .extract import is_repost_page, is_social_platform

GRID_COLS = 4
CELL_SIZE = 320
GAP = 4
BACKGROUND = (46, 46, 46)
PLACEHOLDER = (86, 86, 86)
JPEG_QUALITY = 82
LABEL_SIZE = 22
# 候选图可能是几千像素的原图，拼图时只留 224 的格子，没必要把整张大图留在内存里。
# 服务器空闲内存只有几百 MB，这里比 hashing.MAX_PIXELS 更保守。
MAX_SOURCE_PIXELS = 30_000_000

PROMPT_TEMPLATE = """你在帮我核对「同一个人」。

第 0 格是目标图，标着 TARGET；第 1 到第 {count} 格是搜索引擎返回的候选，每格左上角标着编号。

请找出候选里哪些和 TARGET 是同一个人。

图里经常看不到脸——只露腿、只有背影、只截了半身，这很正常，照样要判断。按图里能看到的部分找「属于这一个具体的人」的证据：
- 有脸时：脸型、五官形状、五官之间的比例；
- 没脸时：局部本身的可辨识细节，比如纹身、痣、疤、指甲与手型、腿型与皮肤特征、某一侧耳朵上的饰品、发长与发尾处理、衣物上特定图案的位置与磨损、鞋袜的穿法、拍摄现场里少见又重复出现的物件。

下面这些不算证据，只靠它们必须返回空数组：
- 角色名、作品名——同一个角色谁都能 cos；
- 同款服装、发色、体型、姿势、构图、背景墙这类描述——换个人穿同样成立。
判断标准就一句：把这条理由放到任何一张同角色同款的图上都成立，那它就是没证据。

另外要排除：电商商品图、模特展示图、商品详情页截图，即使衣服鞋子一模一样，也不算同一个人——那只是同款商品，不是同一个人。
同一个人换了姿势、换了角度、被裁掉一部分、被别人拼成对比图、加了水印或滤镜、换了背景，都算同一个人。
拿不准的不要选，宁缺毋滥。

还有一件顺手的事：第 0 格 TARGET 上要是有水印、签名、网址，或者角落里的小字账号，就把它逐字抄进 watermark（比如 "@abc123"、"微博@某某"）。只抄 TARGET 这一格：候选格上的水印是那个网站或别人上传时加的，抄进来会认错人。看不清、认不全、或者本来就没有，一律留空，别猜。

只输出一行 JSON，不要任何解释与代码块：
{{"same_person": [编号], "reason": "不超过 40 字，写你看到的那个具体特征", "watermark": "TARGET 格上的水印原文，没有就空串"}}
一个都不是就输出：
{{"same_person": [], "reason": "...", "watermark": ""}}
"""

_JSON_BLOCK = re.compile(r"\{.*\}", re.S)
_ARRAY_FIELD = re.compile(r"(?:same_person|picks|same|indices)['\"]?\s*[:：]\s*\[([^\]]*)\]")
_INT = re.compile(r"\d+")
_FENCE = re.compile(r"```[a-zA-Z]*")


@dataclass
class Grid:
    """拼好的对照图。

    格子编号从 0 开始：0 号是目标图，第 n 号对应候选列表的第 n-1 项。解析
    回复时按这个偏移反查是哪一条候选。
    """

    data: bytes
    width: int = 0
    height: int = 0

    @property
    def data_url(self) -> str:
        return "data:image/jpeg;base64," + base64.b64encode(self.data).decode("ascii")


def build_prompt(count: int) -> str:
    """count 是候选格数（不含目标图）。"""
    return PROMPT_TEMPLATE.format(count=count)


# 单独读水印用。拼图里 TARGET 格只有 320px，图上那种手写体小字常被压没——
# 实测同一个人物照，拼图版模型回空串，原图版能读出「魚蛋 Sakana / @Sakana_Yudan」。
WATERMARK_PROMPT = """请把这张图上出现的所有文字逐字抄下来。

重点看这些位置：四角、上下边缘、半透明的浅色小字、边框里的署名、贴纸与角标。
常见的形态是社交账号（``@xxx``、``x.com/xxx``、``instagram.com/xxx``）、摄影师或
画师的署名、网址、日期。

规则：
- 只抄图上真实存在的字，看不清、认不全就留空，不要猜，不要补全；
- 不要描述画面内容，不要判断这是什么角色、穿什么衣服；
- 如果同一个账号出现多次，只抄一次。

只输出一行 JSON，不要任何解释与代码块：
{{"text": "图上所有文字，没有就空串", "account": "看到的社交账号，没有就空串"}}
"""


# 整图缩小读不出来时的第二道：把原图切成几块，每块单独放大去读。整图读水印
# 失败不一定是分辨率不够——3073×4096 的图缩到 1600 长边，实测人眼仍能认出
# 水印，模型却回空串。真正的问题是那行字在整幅画面里只占几千分之一，模型的
# 注意力根本不会落到它身上。切成六块之后，同一行字在画面里大了一个数量级。
#
# 实测 s-20261112-0752-4fdf：图上手机边写着 @kuru_xox，整图读回来是「无」，
# 而这张图在 reddit 上只是别人转的，插件因此停在搬运页上找不到作者。
TILE_PROMPT_NOTE = """注意：这一张是从一张更大照片上裁下来的一块（{label}），只抄这一块里出现的文字。
裁切边缘可能只剩半个字，看不清就留空，不要猜。"""

TILE_ROWS = 3
TILE_COLS = 2
TILE_OVERLAP = 0.12
TILE_MAX_SIDE = 1500
TILE_MAX_UPSCALE = 2.5

_ROW_NAMES = ("上", "中", "下")
_COL_NAMES = ("左", "右")


def build_watermark_prompt(*, label: str = "") -> str:
    """读水印的 prompt。``label`` 非空表示这是分块读的某一块。"""
    if not label:
        return WATERMARK_PROMPT
    return WATERMARK_PROMPT + "\n" + TILE_PROMPT_NOTE.format(label=label)


def tile_boxes(
    width: int,
    height: int,
    *,
    rows: int = TILE_ROWS,
    cols: int = TILE_COLS,
    overlap: float = TILE_OVERLAP,
) -> list[tuple[str, tuple[int, int, int, int]]]:
    """把画布切成带重叠的小块，返回 [(标签, (left, top, right, bottom))]。

    重叠是为了不把一行字正好切在缝上。标签写成「中左」这种位置词，日志里一眼
    能看出是哪一块读出了东西。
    """
    rows = max(1, int(rows))
    cols = max(1, int(cols))
    width = max(1, int(width))
    height = max(1, int(height))
    box_w = width / cols
    box_h = height / rows
    pad_x = box_w * max(0.0, float(overlap))
    pad_y = box_h * max(0.0, float(overlap))
    boxes: list[tuple[str, tuple[int, int, int, int]]] = []
    for row in range(rows):
        for col in range(cols):
            left = max(0, int(round(col * box_w - pad_x)))
            top = max(0, int(round(row * box_h - pad_y)))
            right = min(width, int(round((col + 1) * box_w + pad_x)))
            bottom = min(height, int(round((row + 1) * box_h + pad_y)))
            if right - left < 8 or bottom - top < 8:
                continue
            boxes.append((_tile_label(row, col, rows, cols), (left, top, right, bottom)))
    return boxes


def _tile_label(row: int, col: int, rows: int, cols: int) -> str:
    if rows <= len(_ROW_NAMES) and cols <= len(_COL_NAMES):
        if cols == 1:
            return _ROW_NAMES[row]
        return _ROW_NAMES[row] + _COL_NAMES[col]
    return f"第{row * cols + col + 1}块"


def tile_data_urls(
    raw: bytes,
    *,
    rows: int = TILE_ROWS,
    cols: int = TILE_COLS,
    overlap: float = TILE_OVERLAP,
    max_side: int = TILE_MAX_SIDE,
) -> list[tuple[str, str]]:
    """把原图切成几块，各自放大到能看清小字，返回 [(标签, data_url)]。

    调用方一块一块发、读到账号就停，别一次把六块全送出去烧额度。
    """
    image = _decode(raw)
    if image is None:
        raise ValueError("图片解码失败")
    out: list[tuple[str, str]] = []
    for label, box in tile_boxes(image.width, image.height, rows=rows, cols=cols, overlap=overlap):
        tile = image.crop(box)
        width, height = tile.size
        scale = float(max_side) / max(1, max(width, height))
        scale = min(max(0.3, scale), TILE_MAX_UPSCALE)
        if abs(scale - 1.0) > 0.02:
            tile = tile.resize((max(1, int(width * scale)), max(1, int(height * scale))))
        buffer = io.BytesIO()
        tile.convert("RGB").save(buffer, format="JPEG", quality=JPEG_QUALITY, optimize=True)
        out.append((label, "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")))
    return out


def data_url_for(raw: bytes, *, max_side: int = 1600) -> tuple[str, int, int]:
    """把原图缩到能看清字的大小再编码成 data URL。

    太大会把 token 花在像素上；1600 的长边对小字水印够用了，实测 2048 的
    原图缩到这里仍能读出角落的账号。
    """
    image = _decode(raw)
    if image is None:
        raise ValueError("图片解码失败")
    width, height = image.size
    scale = min(1.0, float(max_side) / max(width, height))
    if scale < 1.0:
        image = image.resize((max(1, int(width * scale)), max(1, int(height * scale))))
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return (
        "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii"),
        image.width,
        image.height,
    )


def parse_watermark_reply(text: str) -> dict[str, str]:
    """读水印那次调用的返回：{'text': 原文, 'account': 账号}，读不到就是空串。"""
    match = _JSON_BLOCK.search(_FENCE.sub("", text or ""))
    if not match:
        return {"text": "", "account": ""}
    try:
        payload = json.loads(match.group(0))
    except Exception:  # noqa: BLE001
        return {"text": "", "account": ""}
    if not isinstance(payload, dict):
        return {"text": "", "account": ""}
    result = {}
    for key in ("text", "account"):
        value = str(payload.get(key) or "").strip().strip("\"'“”").strip()
        lowered = value.lower()
        if value and len(value) <= 8 and any(lowered.startswith(word) for word in _WATERMARK_EMPTY):
            value = ""
        result[key] = value[:120]
    return result


# 电商/商品页域名。视觉模型看「同款格裙、黑丝、小皮鞋」很容易把卖同款衣服的
# 商品图当成同一个人，实测一次命中 3 条全是速卖通的连裤袜商品页。这类结果只
# 能当参考，不能进结论。
COMMERCE_TOKENS = (
    "aliexpress",
    "alibaba",
    "taobao",
    "tmall",
    "jd.com",
    "1688",
    "pinduoduo",
    "amazon",
    "ebay",
    "etsy",
    "shein",
    "temu",
    "rakuten",
    "mercari",
    "shopee",
    "lazada",
    "walmart",
    "ozon",
    "wildberries",
    "dhgate",
    "banggood",
    "cjdropshipping",
    "redbubble",
    "teespring",
)


def _hosts_of(text: str) -> list[str]:
    value = str(text or "").strip().lower()
    if not value:
        return []
    host = urlparse(value).netloc if "://" in value else value
    host = host.rsplit("@", 1)[-1].split(":")[0]
    return [host] if host else []


def is_commerce(platform: str = "", url: str = "", name: str = "") -> bool:
    """是不是电商商品页。平台名、链接、标题任意一处命中就算。"""
    for raw in (url, platform):
        for host in _hosts_of(raw):
            if any(token in host for token in COMMERCE_TOKENS):
                return True
    title = str(name or "").lower()
    return bool(title) and any(token in title for token in COMMERCE_TOKENS)


def is_traceable(handle: str = "", platform: str = "", url: str = "") -> bool:
    """能不能顺着这条结果找到人：有账号名，或者落在社交/画师站上。"""
    if str(handle or "").strip():
        return True
    return is_social_platform(url=url, source=platform)


# 视觉说「同一个人」之后，这条结果还能到什么程度。顺序有意义：先排电商商品页，
# 再看落点是不是只是一个平台编号，最后确认有没有账号名。
VERDICT_VISION = "vision"
VERDICT_COMMERCE = "commerce"
VERDICT_REPOST = "repost"
VERDICT_PLATFORM_ID = "platform_id"
VERDICT_UNTRACEABLE = "untraceable"
VERDICT_NO_ACCOUNT = "no_account"

# 视觉命中但指纹对不上时，光靠「模型说像」撑不起结论。要看能不能翻到那人
# 自己发的这条帖子；翻不到就只留一句线索。
PROOF_NOTE_MISS = "视觉复核：疑似同一个人（{gap}），但翻了她最近的带图帖没找到这张，只算线索"
PROOF_NOTE_BLOCKED = "视觉复核：疑似同一个人（{gap}），但这次没翻成她的带图帖、核不了，只算线索"
PROOF_NOTE_OFFLINE = "视觉复核：疑似同一个人（{gap}），但这个落点翻不了帖、核不了，只算线索"


def needs_post_proof(distance: int | None, threshold: int) -> bool:
    """视觉命中要不要拿「她自己发过这张」来作证。

    距离够近时图本身就是同一张，只是落点不好（转发页、商品页），不必再翻帖；
    距离超过阈值、或者压根没算出距离，那这条结论就只剩「模型说像」一句话——
    必须翻到那个人自己的带图帖才算数，否则只算线索。

    s-20261112-1435-9aed 就是这么错的：视觉凭「粉色双马尾、同款粉色系服装」挑了
    一条，指纹距离 28，落在一个微博 UID 上，整单却写成了找到。
    """
    if distance is None:
        return True
    return int(distance) > int(threshold)

DOWNGRADE_NOTES = {
    VERDICT_COMMERCE: "视觉复核：疑似同一个人，但来自电商商品页（{gap}），仅供参考",
    VERDICT_REPOST: "视觉复核：疑似同一个人，但那是转发页、图不是她发的（{gap}），仅供参考",
    VERDICT_PLATFORM_ID: "视觉复核：疑似同一个人，但那条只有一个页面编号（{handle}）、不是作者账号（{gap}），仅供参考",
    VERDICT_UNTRACEABLE: "视觉复核：疑似同一个人，但那是{platform}上的页面、不是作者主页（{gap}），仅供参考",
    VERDICT_NO_ACCOUNT: "视觉复核：疑似同一个人，但只找到{platform}的平台页面、解析不出发布者（{gap}），仅供参考",
}

# 平台内部编号：B 站 BV 号、各类站点 UID。它们能定位到一条视频或一个人，
# 但不是账号名——拿它当结论就是「找到了一个 BV1xx」，对用户没有任何用。
_BV_ID = re.compile(r"^BV[A-Za-z0-9]+$", re.I)
_NUMERIC_ID = re.compile(r"^\d{5,}$")


def is_platform_id(handle: str = "") -> bool:
    """这个 handle 只是平台编号（BV 号、数字 UID），不是账号名。

    ``@`` 前缀照剥，标题里抽出来的 ``@12345678`` 也一样不是人名。
    """
    value = str(handle or "").strip().lstrip("@")
    if not value:
        return False
    return bool(_BV_ID.match(value) or _NUMERIC_ID.match(value))


def verdict_of(handle: str = "", platform: str = "", url: str = "", name: str = "") -> str:
    """视觉命中之后，这条候选能到什么档。

    只落在社交平台页面上、却没解析出账号名的（比如一条抖音 note 链接），点进去
    也确认不了是谁发的，只能算疑似——之前这类被当成结论发出去，用户看到的
    「找到了」其实只是同场景的另一条内容。

    B 站视频页尤其要小心：从 `bilibili.com/video/BV…` 抠出来的 BV 号不是账号，
    视频页也可能是搬运。实测 s-20261109-2342-2312 把一条 BV 号当成结论发给用户，
    距离还是 30，看着像找到了人，实际什么都没找到。
    """
    if is_commerce(platform, url, name):
        return VERDICT_COMMERCE
    if is_repost_page(url):
        return VERDICT_REPOST
    if is_platform_id(handle):
        return VERDICT_PLATFORM_ID
    if not is_traceable(handle, platform, url):
        return VERDICT_UNTRACEABLE
    if not str(handle or "").strip():
        return VERDICT_NO_ACCOUNT
    return VERDICT_VISION



def _font(size: int = LABEL_SIZE):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1 不支持 size 参数
        return ImageFont.load_default()


def _decode(raw: bytes | None) -> Image.Image | None:
    if not raw:
        return None
    try:
        image = Image.open(io.BytesIO(raw))
        if image.width * image.height > MAX_SOURCE_PIXELS:
            return None
        image = ImageOps.exif_transpose(image)
        return image.convert("RGB")
    except Exception:  # noqa: BLE001 - 解不开的图留灰块，不能中断整张拼图
        return None


def _fit(image: Image.Image, cell: int) -> Image.Image:
    """等比缩到格子里，剩下的边留背景色，避免人脸被拉扁。

    传进来的图是刚解码出来的独占对象，直接原地改，不额外复制一份。
    """
    image.thumbnail((cell, cell), Image.LANCZOS)
    canvas = Image.new("RGB", (cell, cell), BACKGROUND)
    canvas.paste(image, ((cell - image.width) // 2, (cell - image.height) // 2))
    return canvas


def _label(draw: ImageDraw.ImageDraw, x: int, y: int, text: str, font) -> None:
    """白底黑字，贴在同一位置，浅色图和深色图上都能看清。"""
    box = draw.textbbox((0, 0), text, font=font)
    width = box[2] - box[0] + 10
    height = box[3] - box[1] + 8
    draw.rectangle((x, y, x + width, y + height), fill=(255, 255, 255))
    draw.text((x + 5, y + 4 - box[1]), text, font=font, fill=(0, 0, 0))


def build_grid(
    target: bytes | None,
    candidates: list[bytes],
    *,
    cols: int = GRID_COLS,
    cell: int = CELL_SIZE,
) -> Grid:
    """目标图放 0 号格，候选从 1 号开始按传入顺序铺开。

    逐张解码、缩放、贴进画布，处理完就丢，不在内存里同时留几十张原图——服务器
    内存本来就紧。图解码失败时留灰块但编号照旧，这样格子编号和候选列表始终
    一一对应，模型报哪一格都能反查到具体候选。
    """
    sources: list[bytes | None] = [target, *candidates]
    if len(sources) < 2:
        raise ValueError("拼图至少需要目标图和一张候选")

    columns = max(2, min(int(cols), len(sources)))
    cell = max(96, int(cell))
    rows = (len(sources) + columns - 1) // columns
    width = columns * cell + (columns + 1) * GAP
    height = rows * cell + (rows + 1) * GAP

    canvas = Image.new("RGB", (width, height), BACKGROUND)
    draw = ImageDraw.Draw(canvas)
    font = _font()

    for index, raw in enumerate(sources):
        row, column = divmod(index, columns)
        x = GAP + column * (cell + GAP)
        y = GAP + row * (cell + GAP)
        image = _decode(raw)
        tile = _fit(image, cell) if image is not None else Image.new("RGB", (cell, cell), PLACEHOLDER)
        canvas.paste(tile, (x, y))
        _label(draw, x + 4, y + 4, "TARGET" if index == 0 else str(index), font)

    buffer = io.BytesIO()
    canvas.save(buffer, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return Grid(data=buffer.getvalue(), width=width, height=height)


def parse_picks(text: str, max_index: int) -> list[int]:
    """从模型回复里取出「同一个人」的格子编号，越界和 0 号一律丢掉。

    模型偶尔会套 markdown 代码块、用单引号、或者只回一个 ``[3]``，所以先按
    标准 JSON 解，失败再退回正则抠 ``same_person`` 后面的数组。
    """
    if not text or max_index < 1:
        return []

    cleaned = _FENCE.sub("", text).strip()
    payload = None
    match = _JSON_BLOCK.search(cleaned)
    if match:
        try:
            payload = json.loads(match.group(0))
        except Exception:  # noqa: BLE001 - 交给下面的正则兜底
            payload = None

    numbers: list[int] = []
    if isinstance(payload, dict):
        values = payload.get("same_person")
        if values is None:
            for key in ("picks", "same", "indices", "matches"):
                if key in payload:
                    values = payload[key]
                    break
        numbers = _to_numbers(values)
    elif isinstance(payload, list):
        numbers = _to_numbers(payload)

    if not numbers:
        loose = _ARRAY_FIELD.search(cleaned)
        if loose:
            numbers = [int(item) for item in _INT.findall(loose.group(1))]

    picked: list[int] = []
    for number in numbers:
        if 1 <= number <= max_index and number not in picked:
            picked.append(number)
    return picked


def _to_numbers(values: object) -> list[int]:
    if isinstance(values, (int, float)):
        return [int(values)]
    if isinstance(values, str):
        return [int(item) for item in _INT.findall(values)]
    if isinstance(values, (list, tuple, set)):
        numbers: list[int] = []
        for item in values:
            numbers.extend(_to_numbers(item))
        return numbers
    return []


def reason_of(text: str) -> str:
    """取模型给的一句理由，只用于日志和备注，失败就返回空串。"""
    match = _JSON_BLOCK.search(_FENCE.sub("", text or ""))
    if not match:
        return ""
    try:
        payload = json.loads(match.group(0))
    except Exception:  # noqa: BLE001
        return ""
    if isinstance(payload, dict):
        return str(payload.get("reason") or "").strip()[:60]
    return ""


# 水印里像 X 账号的部分：@abc123，或者整串就是个裸账号。
_WATERMARK_HANDLE = re.compile(r"@([A-Za-z0-9_]{2,15})")
_WATERMARK_BARE = re.compile(r"[A-Za-z0-9_]{3,15}")

# 中文平台习惯把 ID 写成 ``id=某某``、``id:某某``、``id.某某``，前面还可能带个 @。
# 直接拿 _WATERMARK_HANDLE 去匹配会先撞上 ``@id``，把 ``id`` 当成账号——实测
# s-20261112-0858-c44f 图上写着 ``Reng♡ @id.NothingLose``，取号取成 ``id``，
# 又因为 ``@id`` 前面刚被 Grok 读过、判了「还是它、不重复核」，整图那次结果就
# 这么丢了，最后报未找到。
_WATERMARK_ID_PREFIX = re.compile(
    r"@?\s*\bid\s*[=:.：]\s*([A-Za-z0-9_]{3,15})",
    re.I,
)
# 小字里 ``id=`` 的等号常被读成 c，``id=NothingLose`` 就成了 ``idcNothingLose``。
# 只在紧挨着的时候才认这个 c，免得 ``id cosplay`` 也被当成账号。
_WATERMARK_IDC = re.compile(r"@?\s*\bidc([A-Za-z0-9_]{4,15})", re.I)
# 模型说「没有」的各种写法。水印常常小到只有一个像素高，它照样会回一句「看不清」，
# 这种不能当成「读到了一个 ID」。
_WATERMARK_EMPTY = (
    "无",
    "没有",
    "未发现",
    "未见",
    "看不清",
    "不清晰",
    "不清楚",
    "空",
    "none",
    "null",
    "n/a",
    "no ",
)
# 水印里会出现的站名，不是账号。裸串命中这些词时不能当 handle。
_WATERMARK_PLATFORMS = frozenset(
    {
        "weibo",
        "twitter",
        "instagram",
        "tiktok",
        "douyin",
        "bilibili",
        "wechat",
        "xiaohongshu",
        "www",
        "http",
        "https",
        "com",
    }
)


def watermark_of(text: str) -> str:
    """取模型从 TARGET 格抄下来的水印原文。读不清或没读，一律返回空串。

    只做搬运，不做推断：中文、@句柄、网址都原样留着，交给展示层判断有没有用。
    模型读小字水印经常读错（实测 @ste11ar021 被读成 @butter021），所以这只能
    当线索，不能当结论，调用方要在文案里说明。
    """
    match = _JSON_BLOCK.search(_FENCE.sub("", text or ""))
    if not match:
        return ""
    try:
        payload = json.loads(match.group(0))
    except Exception:  # noqa: BLE001
        return ""
    if not isinstance(payload, dict):
        return ""
    value = str(payload.get("watermark") or "").strip().strip("\"'“”").strip()
    if not value:
        return ""
    lowered = value.lower()
    if len(value) <= 8 and any(lowered.startswith(word) for word in _WATERMARK_EMPTY):
        return ""
    return value[:40]


def id_name_from_watermark(text: str) -> str:
    """水印里 ``id=`` 后面那个名字，没有就返回空串。

    中文平台习惯这么标 ID（``@id=NothingLee``），这一截往往是本名而不是 X 的
    handle，得拿去别的平台找人对。
    """
    value = str(text or "").strip()
    if not value:
        return ""
    for pattern in (_WATERMARK_ID_PREFIX, _WATERMARK_IDC):
        match = pattern.search(value)
        if match:
            return match.group(1)
    return ""


def handle_from_watermark(text: str) -> str:
    """从水印文本里取 X 账号，取不到返回空串。

    只认两种：明写的 ``@xxx``，或者整串就是一个裸账号。中文名、夹杂描述的一律
    不猜——按拼音或近似拼写去试探，等于给错人。
    """
    found = handles_from_watermark(text)
    return found[0] if found else ""


def handles_from_watermark(text: str, limit: int = 3) -> list[str]:
    """从水印文本里取出所有像账号的候选，按可信度排序。

    一条水印里常常不止一个写法：``Reng♡ @id.NothingLose`` 既有 ``@id`` 这个
    半截，也有 ``id=`` 后面真正的名字。只取第一个会取到 ``@id``——它前面刚被
    另一个来源核过，于是整条被当成「还是它」丢掉，答案就在眼前却报未找到。
    所以这里把候选都列出来，由调用方逐个去核。

    ``id=`` / ``id:`` / ``id.`` 后面那截排最前：中文平台习惯这么写 ID，它比
    前面的 ``@id`` 半截更可能是真的。
    """
    value = str(text or "").strip()
    if not value:
        return []
    found: list[str] = []

    def add(candidate: str) -> None:
        name = (candidate or "").strip().strip(".").strip()
        low = name.lower()
        if len(name) < 3 or name.isdigit() or low in _WATERMARK_PLATFORMS:
            return
        if name not in found:
            found.append(name)

    for pattern in (_WATERMARK_ID_PREFIX, _WATERMARK_IDC):
        for match in pattern.finditer(value):
            add(match.group(1))
    for match in _WATERMARK_HANDLE.finditer(value):
        add(match.group(1))
    # 点号分隔的写法（@id.NothingLose）拆开再试一次尾巴，x 的 handle 不能带点。
    for chunk in re.split(r"[@\s]+", value):
        if "." in chunk:
            add(chunk.rsplit(".", 1)[-1])
    bare = value.strip().strip("@").strip()
    if _WATERMARK_BARE.fullmatch(bare):
        add(bare)
    return found[: max(1, limit)]
