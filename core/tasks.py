"""搜索任务表：Tool 建任务、Command 建任务、追问取结果。

任务按 task_id 索引，同时按 (stream_id, user_id) 记住最近一次，供「更多」直接
取用，不用重新搜索。主结果留在内存里（含配图），落盘只保存文字摘要与候选图
地址，重启后旧任务仍能回文字和参考图。
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .engines import Candidate


@dataclass
class SearchOutcome:
    """一次搜索的完整结果。"""

    status: str = "ok"  # ok / not_found / error
    best: Candidate | None = None
    candidates: list[Candidate] = field(default_factory=list)
    ai: dict | None = None
    route: str = "photo"  # photo / drawing
    route_reason: str = ""
    errors: dict[str, str] = field(default_factory=dict)
    engine_counts: dict[str, int] = field(default_factory=dict)
    elapsed_ms: int = 0
    total_candidates: int = 0
    image_b64: str = ""
    image_source: str = ""
    # 视觉和 Grok 同时命中时，best 那张是视觉挑的；Grok 帖子配图里最近的一张单独放这儿。
    # 只留在内存里，落盘不保存。
    grok_image_b64: str = ""
    # 用户这次发来的原图（压成 JPEG 后的 base64）。合并转发的页尾要再贴一次，
    # 群里一次连着好几单结果时才分得清哪张图搜出哪堆东西。只留在内存里，落盘不保存。
    upload_b64: str = ""
    # 视觉兜底的执行情况：{"ok", "checked", "picked": [候选图地址], "reason", "model", "error"}。
    # 只在指纹一条都没命中时才有值，落盘保留，重放时也能说清当时复核了什么。
    vision: dict | None = None
    # Grok 兜底：{"ok", "tier", "handle", "post_url", "reason", "model", "tools", "error"}。
    # tier 为 post 表示拿到具体帖子，account 表示只拿到账号主页。
    grok: dict | None = None
    # 从候选标题里推出来的疑似 X 账号：{"handle", "followers", "name", "source_url",
    # "source_title", "score"}。它不改 best，只作为「同一个人可能在这个号」的补充，
    # 因为名字推断不等于原帖。
    x_guess: dict | None = None
    # 疑似本人的付费内容线索：{"handle", "name", "page_url", "x_handle", "x_followers",
    # "instagram", "source_platform", "source_title", "score"}。泄露站按名字建收录页，
    # 有页说明这个人确实在做 OnlyFans，但仍然是同一个人而不是这张图的原帖。
    paid: dict | None = None
    # 候选里两个 X 号挂着同一条外链：{"link", "members": [{handle, name, followers}]}。
    # 同一个人的几个号常指向同一个 Patreon，这条比「名字像」硬。
    same_person: dict | None = None
    # 按微博昵称换到的 X 号：{"status": hit/seen/none, "name", "weibo": {...}, "handle",
    # "x_name", "followers", "post", "distance", "reason"}。status=hit 时结论已经换成
    # 那条帖子；seen 只说明「找到这个号了、最近没发这张」，不改结论。
    x_identity: dict | None = None
    # 命中搬运站后又去翻候选里的 X 号：{"status": hit/none, "scanned": [号], "failed": [号],
    # "handle", "post", "distance", "x_name"}。hit 时结论已经换成那条帖子；none 只用来
    # 说明「翻过哪些号、都没有」，不改结论。
    repost_author: dict | None = None
    # 按转载站标题里的作者名去 X 找号：{"status": hit/named/none, "author": "染三ran3",
    # "variants": [...], "scanned": [...], "handle", "x_name", "post", "distance"}。
    # hit 时结论已经换成那条帖子；named 只说明「名字对上了这个号」，仍要自己核对。
    title_author: dict | None = None
    # 微博站内反查：{"status": hit/none/error, "query", "kind": name/uid, "uid", "name",
    # "followers", "url", "source", "max_fans", "floor"}。hit 只是线索（这个号确实是
    # 她本人），不改 best；none 说明搜过但没搜到粉丝量像正主的同名号。
    weibo: dict | None = None
    # 按标题里的名字找号：{"name", "status": hit/seen/none, "platform": X/微博,
    # "handle", "account", "followers", "post", "distance", "profiles": [{platform,
    # handle, url, name, followers}], "source"}。hit 时结论已经换成那条帖；
    # seen 只说明「找到这些主页了」，其余平台一律只当线索，不改结论。
    name_hunt: dict | None = None
    # 按角色找人的线索：{"roles": ["明日方舟｜死芒 Necrass"],
    # "accounts": [{"handle", "fans", "post"}], "keywords": [...], "model", "reason"}。
    # 只在图搜图没结论时才有，纯线索——角色对得上不等于人是同一个，认人仍然靠指纹。
    role_hint: dict | None = None
    # 确认作者后在她自己的付费页找到的售卖信息：{"platform": patreon/fanbox, "handle",
    # "shop", "post": {title, url, published, min_cents, currency},
    # "tiers": [{title, cents, currency, free}], "preview": {title, url}}。
    # 图包里的图公开渠道搜不到，这块是唯一能给到底的地方；没有付费页时为空。
    paywall: dict | None = None
    # 从搬运站图床文件名里读到的 pixiv 作品：{"illust_id", "title", "user_name",
    # "user_id", "x_restrict", "create_date", "page_count", "shop_url", "read_from",
    # "author_matched"}。`read_from` 是那条带着作品号的文件名，`author_matched`
    # 表示文件名里的作者名跟 pixiv 返回的 userName 对上了（对不上不采纳）。
    # pixiv 未登录给不了图片地址，所以这条只能给「出处是哪个作品」，比不了指纹。
    pixiv_source: dict | None = None
    # 图集 / 泄露页面上标的来源账号：[{platform, handle, url, count, site, page,
    # author_page}]。落点在 picazor 这类站时，会从帖子页翻到发布者主页再抽一次，
    # 这时 author_page 有值——比帖子页 meta 里抄的硬，但仍然只当线索。
    gallery_sources: list[dict] = field(default_factory=list)
    # 候选落在平台搜索页（`search.bilibili.com/all?keyword=…`）时做的反解：
    # {"state": "hit"/"none"/"skip", "note": "…", "keyword": "…", "accounts": [...]}。
    # 反解到号但图对不上时，这句话必须出现在结论里——以前只挂在那条候选的
    # account_note 上，而那条候选永远是 suspect、不会被选中，等于用户看不到。
    search_page: dict | None = None
    # 候选 URL 里写明的其它平台账号（B站 / 微博 / TikTok / Instagram）：
    # {"status": "hit"/"seen", "hit": {platform, handle, name, url, image, distance,
    # source} | None, "accounts": [{kind, platform, handle, url, name, followers,
    # source, checked, verified}]}。status=hit 时结论已经换成核过图的那条；
    # seen 只说明这些账号写在候选里，没核到这张图，一律只当线索。
    # TikTok / Instagram 没有可用的公开封面，只能当账号线索。
    other_accounts: dict | None = None
    # 候选落在「求图 / 问答」帖上时对页面正文与回复的读取：
    # {"status": "hit"/"clue"/"none"/"unreadable", "page": "…", "host": "whos.tv",
    #  "links": [{kind, url, handle, id}], "handle": "Ahab_Channel",
    #  "name": "亚哈Ahab", "post": "https://x.com/…/status/…", "distance": 0}。
    # hit 时结论已经换成帖子；clue 只说明「帖子里给的出处是这条」，仍要自己核对。
    help_post: dict | None = None
    # 这次「怎么找的」：每一步的触发与结果，只在「更多」里展示。
    # 形如 [{"step": "读水印", "state": "ran"|"skip"|"hit", "note": "…"}]。
    # 用户 2026-11-20 的要求：十几距离那批不额外开反查，但得让人看得出这次读了
    # 哪一次水印、反查停在哪——不然「没触发」和「触发了没结果」在群里长得一样。
    trace: list[dict] = field(default_factory=list)
    # 展示用图片：候选图地址 → base64。只留在内存里，落盘不保存（体积太大）。
    images: dict[str, str] = field(default_factory=dict)
    # 这次结果来自按图缓存。命中时不重跑引擎，elapsed_ms 是当初那次的耗时。
    cached: bool = False
    cached_at: float = 0.0

    def to_summary(self) -> dict:
        """可落盘的摘要（不含图片）。"""
        return {
            "status": self.status,
            "best": asdict(self.best) if self.best else None,
            "candidates": [asdict(item) for item in self.candidates[:20]],
            "ai": self.ai,
            "route": self.route,
            "route_reason": self.route_reason,
            "errors": self.errors,
            "engine_counts": self.engine_counts,
            "elapsed_ms": self.elapsed_ms,
            "total_candidates": self.total_candidates,
            "image_source": self.image_source,
            "vision": self.vision,
            "grok": self.grok,
            "x_guess": self.x_guess,
            "paid": self.paid,
            "same_person": self.same_person,
            "x_identity": self.x_identity,
            "repost_author": self.repost_author,
            "title_author": self.title_author,
            "weibo": self.weibo,
            "name_hunt": self.name_hunt,
            "role_hint": self.role_hint,
            "paywall": self.paywall,
            "pixiv_source": self.pixiv_source,
            "gallery_sources": self.gallery_sources or [],
            "search_page": self.search_page,
            "other_accounts": self.other_accounts,
            "help_post": self.help_post,
            "trace": self.trace or [],
        }

    @classmethod
    def from_summary(cls, summary: dict | None) -> "SearchOutcome":
        """从落盘摘要复原。任务表和按图缓存共用这一份口径。"""
        summary = summary or {}
        best = summary.get("best")
        return cls(
            status=str(summary.get("status") or "error"),
            best=Candidate(**best) if best else None,
            candidates=[Candidate(**item) for item in summary.get("candidates") or []],
            ai=summary.get("ai"),
            route=str(summary.get("route") or "photo"),
            route_reason=str(summary.get("route_reason") or ""),
            errors=summary.get("errors") or {},
            engine_counts=summary.get("engine_counts") or {},
            elapsed_ms=int(summary.get("elapsed_ms") or 0),
            total_candidates=int(summary.get("total_candidates") or 0),
            image_source=str(summary.get("image_source") or ""),
            vision=summary.get("vision"),
            grok=summary.get("grok"),
            x_guess=summary.get("x_guess"),
            paid=summary.get("paid"),
            same_person=summary.get("same_person"),
            x_identity=summary.get("x_identity"),
            repost_author=summary.get("repost_author"),
            title_author=summary.get("title_author"),
            weibo=summary.get("weibo"),
            name_hunt=summary.get("name_hunt"),
            role_hint=summary.get("role_hint"),
            paywall=summary.get("paywall"),
            pixiv_source=summary.get("pixiv_source"),
            gallery_sources=summary.get("gallery_sources") or [],
            search_page=summary.get("search_page"),
            other_accounts=summary.get("other_accounts"),
            help_post=summary.get("help_post"),
            trace=summary.get("trace") or [],
        )


@dataclass
class SearchTask:
    task_id: str
    stream_id: str
    user_id: str
    status: str = "running"  # running / done / error
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    outcome: SearchOutcome | None = None
    error: str = ""

    @property
    def elapsed(self) -> float:
        end = self.finished_at or time.time()
        return max(0.0, end - self.created_at)


class TaskStore:
    """内存任务表 + 文字摘要落盘。"""

    def __init__(self, data_dir: str | Path, ttl_seconds: float = 900.0) -> None:
        self._ttl = float(ttl_seconds)
        self._tasks: dict[str, SearchTask] = {}
        self._latest: dict[tuple[str, str], str] = {}
        self._path = Path(data_dir) / "image_searcher_tasks.json"

    # -- 生命周期 ---------------------------------------------------------

    def set_ttl(self, ttl_seconds: float) -> None:
        self._ttl = max(60.0, float(ttl_seconds))

    def new_task(self, stream_id: str, user_id: str) -> SearchTask:
        self.cleanup()
        self._evict()
        task = SearchTask(
            task_id=self._make_id(),
            stream_id=stream_id,
            user_id=user_id,
        )
        self._tasks[task.task_id] = task
        self._latest[(stream_id, user_id)] = task.task_id
        return task

    def finish(self, task: SearchTask, outcome: SearchOutcome) -> None:
        task.outcome = outcome
        task.status = "done"
        task.finished_at = time.time()
        self._persist()

    def fail(self, task: SearchTask, error: str) -> None:
        task.status = "error"
        task.error = str(error)[:300]
        task.finished_at = time.time()
        self._persist()

    def cleanup(self) -> None:
        now = time.time()
        expired = [
            task_id
            for task_id, task in self._tasks.items()
            if now - (task.finished_at or task.created_at) > self._ttl
        ]
        for task_id in expired:
            self._tasks.pop(task_id, None)
        if expired:
            alive = {task_id for task_id in self._tasks}
            self._latest = {key: value for key, value in self._latest.items() if value in alive}

    def _evict(self, keep: int = 80) -> None:
        """任务里可能持有 1MB+ 的图片 base64，给内存留个硬上限。

        保留时长默认 1 天，条数上限跟着放宽到 80；再往上就要考虑内存了。
        """
        if len(self._tasks) <= keep:
            return
        finished = [task for task in self._tasks.values() if task.status != "running"]
        finished.sort(key=lambda task: task.created_at)
        for task in finished[: len(self._tasks) - keep]:
            self._tasks.pop(task.task_id, None)
        alive = set(self._tasks)
        self._latest = {key: value for key, value in self._latest.items() if value in alive}

    # -- 查询 -------------------------------------------------------------

    def get(self, task_id: str) -> SearchTask | None:
        self.cleanup()
        if not task_id:
            return None
        return self._tasks.get(task_id)

    def latest_for(self, stream_id: str, user_id: str) -> SearchTask | None:
        self.cleanup()
        task_id = self._latest.get((stream_id, user_id))
        return self._tasks.get(task_id) if task_id else None

    def latest_in_stream(self, stream_id: str) -> SearchTask | None:
        self.cleanup()
        candidates = [
            task
            for task in self._tasks.values()
            if task.stream_id == stream_id and task.status == "done"
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda task: task.finished_at or task.created_at)

    def running_count(self) -> int:
        return sum(1 for task in self._tasks.values() if task.status == "running")

    # -- 落盘 -------------------------------------------------------------

    def _make_id(self) -> str:
        stamp = time.strftime("%Y%m%d-%H%M")
        return f"s-{stamp}-{uuid.uuid4().hex[:4]}"

    def _persist(self) -> None:
        """只保存最近的文字摘要，图片与二进制不落盘。"""
        snapshot: dict[str, dict] = {}
        for task in sorted(self._tasks.values(), key=lambda item: item.created_at, reverse=True)[:60]:
            if task.status == "running" or task.outcome is None:
                continue
            snapshot[task.task_id] = {
                "stream_id": task.stream_id,
                "user_id": task.user_id,
                "created_at": task.created_at,
                "finished_at": task.finished_at,
                "summary": task.outcome.to_summary(),
            }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(snapshot, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:  # noqa: BLE001 - 落盘失败不影响主流程
            pass

    def load(self) -> int:
        """读回上次的摘要任务，返回恢复条数。"""
        if not self._path.is_file():
            return 0
        try:
            snapshot = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return 0
        restored = 0
        now = time.time()
        for task_id, payload in snapshot.items():
            try:
                created_at = float(payload.get("created_at") or 0)
                finished_at = float(payload.get("finished_at") or created_at)
                if now - finished_at > self._ttl:
                    continue
                summary = payload.get("summary") or {}
                outcome = SearchOutcome.from_summary(summary)
                task = SearchTask(
                    task_id=task_id,
                    stream_id=str(payload.get("stream_id") or ""),
                    user_id=str(payload.get("user_id") or ""),
                    status="done",
                    created_at=created_at,
                    finished_at=finished_at,
                    outcome=outcome,
                )
                self._tasks[task_id] = task
                self._latest[(task.stream_id, task.user_id)] = task_id
                restored += 1
            except Exception:  # noqa: BLE001
                continue
        return restored
