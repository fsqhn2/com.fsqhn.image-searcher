# AGENTS.md — com.fsqhn.image-searcher

给接手这个插件的编码代理看。里面是边界、架构约定和实测踩过的坑；改代码前先扫一遍，能省掉大半返工。

## 边界

- 只改本目录。不改 MaiBot 主程序（`src/`、`dashboard/`、`config/`）。
- 不用 `@Action`，只用 `@Tool` / `@Command` / `@HookHandler`。
- 任何 key、代理地址、群号一律走 `config.toml`，不要写进代码。
- `config.toml` 已 gitignore（含真实密钥），仓库只提交 `config.example.toml`。
- 部署到生产（拷贝、装依赖、重载、重启）必须先汇报并取得同意。

## 架构

```
plugin.py              配置模型 + 生命周期 + 取图 + 编排 + Tool/Command/Hook
core/engines.py        Lens / Yandex / 百度 / SauceNAO 适配 + detect_ai + verify_candidate + ENGINES_VERSION
core/hashing.py        dHash 指纹 + prepare_upload（压到 480KB）
core/formatting.py     合并转发节点、路线行、重放文案
core/extract.py        平台识别、账号页判定与归一、账号去重
core/handles.py        从候选标题里认疑似 X 账号名（纯函数，不碰网络）
core/identity.py       微博 UID→昵称、昵称→X 号（解析与检索；cookie/检索要网络）
core/resultcache.py    按图缓存（纯函数 + 原子落盘）
core/vision.py         视觉复核：拼对照图、写 prompt、解析回复、命中档位（纯函数，不碰网络）
core/grok.py           Grok 兜底：写 prompt、候选排序、拼请求体、解析帖子/账号链接（纯函数，不碰网络）
core/paid.py           OnlyFans 线索：泄露站收录页解析、名字与 slug 校验（纯函数，不碰网络）
core/same_person.py    同外链归人：分组、锚点准入（纯函数，不碰网络）
core/fxtwitter.py      推文 ID 解析、fxtwitter 返回解析、核对判定 hit/miss/unknown（纯函数，不碰网络）
core/tasks.py          SearchOutcome（含 route / cached）、TaskStore（TTL / 上限 / 落盘恢复）
core/commands.py       指令正则 + split_force（「重搜」解析）
core/httpclient.py     会话与图片下载
core/maibot_compat.py  能力返回值解包
```

**两条入口一套逻辑**：`image_search` Tool 与 `@Command` 都走 `_start_task` → `_run_task` → `_search`。不要给其中一条单独写搜索流程；`force`（重搜）也要从两条入口一起传下去。

**异步任务化**：搜索 30~60 秒，Tool 立即返回 `task_id`，不能 await 到结果。

**指纹是主路，视觉只兜底**：判断「是不是同一张图」仍然只看 dHash。只有整轮一个 `match` 都没有、`[vision] enabled` 开着、且不是插画路线时，才把目标图和最接近的候选拼成一张编号网格交给 `vlm` 任务，问的是「有没有同一个人」。命中项标 `status="vision"`，文案必须写「视觉复核」。拼图、prompt、解析在 `core/vision.py`（纯函数，有单测）。

**指纹命中常落在搬运站，作者账号要另外找**：`_pick_best()` 里 X 优先的窗口是「距离 ≤ 最优值 + 1」——搬运站存原图，X 那条被压过以后距离通常高 1~2，只允许同距离就等于永远轮不到作者本人；差得更多则不算同一张图，不能为了平台把它顶上来。另外 `_hunt_x_handle()` 会从候选标题里抽像用户名的词（`core/handles.py`），逐个用 `api.fxtwitter.com/2/profile/<handle>` 验证，取粉丝 ≥ 5000 且名字主干真的在标题里出现过的那个，写进 `outcome.x_guess`。并列多个名字时**先看候选 URL 路径**（`handles.pick_by_slug()`，路径里出现过的优先），再比粉丝——搬运站路径就是它认定的主人名，标题里的别名常顺带抄进无关大号：`fapeza.com/heyymimimi/62/` 的标题「cw / heyimbee / heyymimimi」按粉丝会选 56.9 万的 `@heyimbee`，正确答案是 2.28 万的 `@heyymimimi`。`_hunt_paid()` 用同一套取舍，路径里的名字在泄露站没收录页时不拿并列别名顶上（宁缺勿错）。它**不改 best、不覆盖 Grok 已经核到的号**，结果里必须写明「从哪条候选的标题推出来的、不是这张图的原帖」——名字推断不等于原帖，这条别省。

提示词不能要求必须露脸——很多目标是只有腿、背影或局部裁切的图。门槛是「个体化证据」：脸、纹身、痣、手型、饰品、图案位置这类属于具体某个人的细节；只能说出角色名、同款服装、发色、体型、姿势的，一律返回空数组。拼图 4 列 × 320，格子小到只能认角色就是这套机制失效的根因。

**视觉超时要按「排队 + 推理」算**：`[vision] timeout` 默认 180 秒，不是 90。宿主的 LLM 并发会被记忆整理（`A_Memorix.*`）这类任务占满，一次视觉请求实测排了 78 秒才轮到选模型，90 秒会在模型刚被选中时放弃。失败路径必须打 warning（已记等待时长与模型名），否则只剩任务摘要里一行 error，查不出是排队还是模型慢。

**Grok 兜底与视觉并行，分两档**：同一个触发条件、`[grok] enabled` 开着且有 `api_key` 时，两边一起发出去（`asyncio.gather`），各自超时。发给它的是原图 + 前 8 条候选（`账号｜平台｜标题`），`build_prompt()` 让它按三步走：先读图上的水印/签名/边角小字里有没有 ID，有就按这个 ID 去 X 核实发布者，核实不到或者本来没 ID 才走图搜图、逐个比对候选（`core/grok.py` 的 `build_hints()`，日志里有 `候选=`）。判据只看返回链接：带 `status/<id>` 算「找到」，只有账号主页算「线索」。只给账号时会用 fxtwitter 的 `/2/profile/<号>` 确认账号状态（`_account_profile()`）：`missing`（查无此号）整条丢掉；`suspended` 先抓网页主页复核：确认封号（`Account suspended`）就丢弃并写明「已被 X 封禁（用户 ID …）」，复核失败就保留线索、注明「X 接口读不到、可能受限」；`unknown`（没查成）保留原样，不能当「不存在」。**候选要按社交平台优先喂**（`grok.social_first()`）：Reddit、Instagram、抖音这类页面上的用户名才是通向作者本人的线索，按原顺序排时前 8 条常被画师站插画占满，Grok 根本看不到 `u/Smeepsi` 这个名字。**模型要配备用链**（`grok.model_chain()`）：主模型超时、连不上或回 5xx 就隔 2 秒换下一个（默认 4.7 → 4.6），主备各等一次完整超时（**超时默认 210 秒，别再退回 120**：`s-20261110-1727-f388` 那单三次尝试 120+120+60 秒全超、整单 343 秒白跑，孤立补跑时 grok-4.6 用了 118.8 秒才回、grok-4.7 直接超 120，120 秒刚好卡在边缘；原来那次「短超时补一次」也删了，实测没救回过任何一单）；401/403 直接放弃，换模型没用。中转站实测会轮流挂 4.7 和 4.6，只在一个模型上重试经常白等。错误文案统一成「中转站 N 秒内没响应」，别把 requests 的堆栈直接发给群友。它跑过但空手时，结果里要有一句「Grok 也拿这张图去 X 搜过，没撞见原帖」，否则「没触发」和「触发了没结果」看起来一模一样。

**视觉命中还要过闸门**：判定集中在 `core/vision.py` 的 `verdict_of()`，检查顺序有意义，别调换——先 `VERDICT_COMMERCE`（候选落在速卖通 / 阿里 / 淘宝 / 1688 / Amazon / eBay 这类电商站，只是同款衣服），再 `VERDICT_REPOST`（`reposts` / `likes` / `with_replies` 这类列表页，图是别人发的），再 `VERDICT_PLATFORM_ID`（handle 只是个平台编号：B 站的 `BV…` 视频号，或标题里抽出来的裸数字；`is_platform_id()` 判定），再 `VERDICT_UNTRACEABLE`（整条不可追踪：不是社交平台、也没有账号），最后 `VERDICT_NO_ACCOUNT`（落在抖音、小红书这类平台页面上，却解析不出发布者，点进去也确认不了是谁发的）。只有 `VERDICT_VISION` 能当结论，其余各档一律降级成 `suspect` 只进参考区，日志记 `降级=N`，说明文案在 `DOWNGRADE_NOTES`。降级后的 best 在结论里按「未找到」，但那条链接仍要给出（`_vision_pick_line()` 优先用被降级的 best 本身），不能把一条 BV 号或转发页摆成「找到了」。视觉命中发出去时，结论必须带免责声明，文案在 `core/formatting.py` 的 `_best_block()`。降级文案要**点名是哪个站**（`DOWNGRADE_NOTES` 里的 `{platform}`）：「那是 soundcloud.com 上的页面、不是作者主页」比「找不到可核对的账号」有用得多。结论里还要把模型给的理由原样列一句（`_vision_reason()`，截 80 字）——它常按「黑蕾丝、白短袜、室内」判成同一个人，理由里甚至会把发色写错，亮出来用户一眼就能看出这轮靠不靠谱。

**图集站页面自带来源账号**：视觉挑中的落点在图集 / 搬运站（`core/gallery.py` 的 `GALLERY_HOSTS`：4play.to、v2ph、acgtang、beauty-draw、leakgallery 等）时，`_gallery_sources()` 会抓那一页 HTML，用 `parse_source_accounts()` 读出站方自己标的 IG / X 账号，写进 `vision["sources"]`，结论里单独一行「来自 … 页面标的来源」。要解决的是「视觉判定同一人、但页面不是作者主页」这类：页面本身没用，可它标的来源常常就是本人——实测 4play.to 的 meta 直接写了 `instagram.com/menruinyanko`，而插件原来只把这条降级就丢了。只认出现 3 次以上的账号（页脚、友链只出现一两次），保留路径（`/p/`、`/reel/`）不算，最多抓两页、14 秒上限，读不到就当没有。文案必须写「站方标注，未必是作者本人」——站方也会标错。同一单里「最接近的一条」也优先给这种页面（`_vision_pick_line()` 按 `page` 比对，忽略 utm 参数），因为一条盗图页的链接不如一个指向人的账号有用。

**X 上只有 ``/<号>/status/<id>`` 能当结论**：转发页（`reposts` / `likes` / `with_replies`）、账号主页、引用帖都不行。链接里那个号也未必是作者：候选常是转发地址 `x.com/<转发者>/status/<id>`，`_apply_tweet_identity()` 里只要 `fxtwitter.Tweet.reposted_by` 非空，就把 `url`/`handle`/`name` 换成 `tweet.url`/`tweet.handle`/`tweet.name`，备注写「这条是 @A 转的帖子，原文在 @B」（s-20261109-2222-b5e6 把 @sssmnms 的图判给了转发者 @redwine278，图上水印就写着 @sssmnms）。`_follow_quotes()` 在去重和挑 best 之后、组装结果之前跑一遍候选列表，**只挑 status 为 `match`/`suspect` 的 X 帖子页**（mismatch 的图本来就不是这张，改了没意义；vision 只处理 best）：按 URL 去重、6 路并发、总预算 12 秒，超时就放弃。放去重之后是因为去重按「平台 + 账号」合并，先纠正会把同一原作者的几条帖子压成一条。`/2/status/<id>` 和 `/i/status/<id>` 两种返回都带 `reposted_by`。`with_replies` 和 `reposts` 是能自动补救的两种：指纹对上（距离 ≤ 阈值）时，`_resolve_x_list_page()` 按页面类型选路——回复页用 `from:<号> filter:replies` 翻回复、取 `replying_to.status` 当父帖逐个回查（`_reply_page_lookup()`）；转发页用 `from:<号> filter:retweets`，搜索返回的就是原帖（`_retweet_lookup()`，直接比媒体键，不用再查父帖）。媒体键（`fxtwitter.media_key()`）一致就换成那条原帖并升成 `match`；翻不到就还是 `suspect`。点赞页（`/likes`）没有对应语法，只能保持疑似。**节奏和止损**：每批 8 个请求、批间 `asyncio.sleep(0.5)`，403/429 立刻置 `state["throttled"]` 停手，预算 60 秒、最多 240 个父帖、单次任务最多试 2 条列表页。开始翻之前往会话发 `formatting.TRACE_PING_TEXT`（「已经找到蛛丝马迹了…大概还要 1 分钟（最多 2 分钟）」）。失败原因（`hit`/`throttled`/`empty`/`miss`/`timeout`）由 lookup 返回，`_resolve_x_list_page()` 拼成一句话带回 `_search()`，再经 `_grok_lookup(reply_hint=...)` 写进 `grok.build_prompt()` 的 `REPLY_BLOCK`，让 Grok 换成 `from:<号>` 再查一遍。

- 页面类型统一走 `core/extract.py::x_page_kind()`，别在各处自己写正则。非帖子页即使指纹达标也只给 `suspect`，备注写清「这是 X 的回复列表页／账号主页」。`_pick_best()` 的「X 优先」同样只对帖子页生效。
- **引用帖要换成原帖**：fxtwitter 的 `quote` 字段直接给出被引用那条的地址与作者。帖子自己没配图（`media` 为空）且有 quote 时，图只能来自被引用那条，`_follow_quote()` 会把结论换成原帖并注明「原文是 @xxx 的引用帖」。实测 YuMIkAnime 引用了 @qyuuuz 的图吐槽，被当成作者发了出去；那种「自己配了图」的引用帖不要动。
- **视觉落在账号主页时先翻帖再定档**：`_settle_vision_handle()` 用 `_media_lookup()` 翻这个号自己的带图帖（`/media` 不含转发）逐张比指纹，命中就换成那条帖子，并且**把状态升成 `match`、距离写成命中那条的**——命中本身就意味着 `distance <= hash_threshold`，还留在 `vision` 会让结论写「没找到同一张图」「不是指纹比对」，跟旁边标注的「指纹距离 1」直接打架（实测 s-20261108-1241）；一条都没中就降成 `suspect` 并把计数补进 `vision["downgraded"]`。实测 s-20261105-1537 挑中 `x.com/realpakbupati`（69 粉的印尼号），理由是「同款发饰、发尾蓝白渐变」，可那张图是粉发白裙、跟原图的银紫发护士服毫无关系——主页上什么图都有，撑不起「这张是她发的」。

**名字推断与付费线索必须先有锚点**：`_identity_texts()` 返回空（一条 `match` / `vision` 都没有）时，`_hunt_x_handle()` 与 `_hunt_paid()` 直接 `return None`。这时标题里的「名字」只是噪声词，实测抽中过 106 万粉的 `@nothing`（来自画师站标题「Nothing To Hide」）和 `@Design`。同理 `same_person.find_shared(anchors=)` 的分组必须含锚点，注释里要写明「只说明这几个号是同一个人，不表示他们就是这张图的作者」。

**抽名字用的文本要剥掉帖子正文**：X 帖标题形如「名字 on X: "正文"」，整条拿去抽会把正文里的词当人名——`kiko on X: "Happy Halloween!"` 就抽出了 Halloween，一路推出 @halloween（1.44 万粉）和 `leakgallery.com/Halloween`。`Candidate.finalize` 里的名字、`plugin._name_text()` 喂给 `handles.extract()` 的文本，都只保留 `on X:` 前面那截（`core/extract.py::x_title_name()`）。

**论坛与社区站的标题不是名字**：inven、DCInside、arca.live、bcy、贴吧、5ch、PTT、Reddit、namu.wiki 这些站的「标题」是帖子标题，结果里写 `📄 标题：`，不写 `👤 名字：`。判断在 `core/extract.py::is_forum_page()`。

**命中时首次回复只给结论**：只要 `status == "ok"`（指纹、视觉、Grok 任一命中），`build_forward_nodes` 与 `build_text_summary` 都不列参考区；没找到时才把参考区发出来。改配图或转发结构时注意这条，别让命中场景又刷回四五条。

**视觉、Grok、页脚共用一条合并转发里的三个节点**：首次回复走 `build_result_forwards()`，最多返回三块——视觉的判断加它挑中的图、Grok 的账号/帖子加帖子配图、页脚（路由、任务编号、更多、重搜）。发送层展平成一次 `ctx.send.forward()`，群里只出一条消息、点开是三条。图数超过 `_MAX_FORWARD_IMAGES`（6 张）时才退回逐块发送，否则 NapCat 会拒收整条。两边都没内容时自动退回单条 `build_forward_nodes`，指纹命中场景不受影响。「更多」仍然走单条，别把候选列表也拆散。

**「更多」靠任务编号定位**：首次回复的页脚必须带 `task_id`，用户回「更多 <task_id>」重发那一次；不带编号退回本会话最近一次。**重发目标是发指令的会话，不是任务原来的会话**——私聊里要群里的任务编号，结果回私聊，别把动作广播回群（实测 s-20261109-0835-c0d9：群里搜、私聊要更多、结果又落到群里）。`_send_forward(..., target=current)` 与 `_send_engine_forwards(..., target=...)` 就是为这个传的，别改回 `task.stream_id`。结果默认保留 1 天（`task_ttl_minutes`），过期后带编号再问要明确回「缓存已清，重新搜」，不能装作没看见。页脚在「更多」那行之后还要给一条「重搜」示例（发图时带上「指令 重搜」），缓存秒回时用户才知道怎么强制重跑。`TaskStore._evict` 的 80 条上限也是为这个 TTL 服务的。

**按图缓存挡在引擎前面**：`_search()` 一进来就按「`prepare_upload` 后的图片 sha256 + `ENGINES_VERSION`」查缓存，命中直接返回，一个引擎都不调，标题标「缓存」、页脚写明复用。规则：

- **只缓存 `status == "ok"` 且有 best 的结果**。没找到的不缓存——实测同一张图过半天就能搜到，存下来只会挡住后来出现的原帖。Grok 单独给出帖子的那种「命中」也不缓存（脱离 Grok 只剩空结论）。
- 缓存里不存 Grok 的结论：它依赖当天 X 上的内容。
- 改动结果解析、候选字段含义时必须把 `core/engines.py` 的 `ENGINES_VERSION` 加一，否则旧缓存会带着旧字段回来。
- 想无视缓存：`force=True`。指令侧由 `core/commands.py` 的 `split_force()` 解析「重搜 / 重新搜 / 刷新 / 强制 / force / fresh」，等待补图窗口会把标记存进 `_pending_force` 再带到任务里；Tool 侧是 `image_search(force=true)`。
- 纯函数与落盘在 `core/resultcache.py`，有单测。

**账号只认账号页，且同一个号只报一次**：链接先过 `match_account()`（`core/extract.py`），`x.com/intent`、`weibo.com/status`、`instagram.com/p/`、`pixiv.net/artworks` 在 `ACCOUNT_PATH_DENY` 里，一律不当账号，退回标题里的 `@handle`。排序之后过 `dedupe_accounts()`，按「平台 + 账号」只留质量最好的那条（状态优先、其次距离），位置按第一次出现。pixiv `member.php?id=` 归一成 `/users/<id>`。

**Grok 给的帖子链接必须过指纹复核**：Grok 会给出对不上的链接，甚至编一条，光有 `x.com/.../status/...` 不能当结论。`_verify_tweet()` 用 `api.fxtwitter.com`（免登录，走 `[plugin] proxy`）拉那条帖子的配图，跟原图算 dHash：

- `hit`（距离 ≤ `hash_threshold`）：才算真找到，理由里带上距离。
- `miss`：有账号就降成「线索」，但**帖子链接保留**，文案里标成「存疑参考（配图相似但非同图，可能是同一套的另一张，请自行核对）」——账号通常是对的，只是那一组图里没有用户发的那张；同景同角色换个人拍，指纹也是这样，所以不能升级成「找到」。没账号才整条丢掉、清空 `post_url`。此时 `reference_url` 记下核对时距离最近的那张配图，结果首图就用它（用户可以直接肉眼比），下载不到才退回原图。
- `unknown`（图没下到、解不出指纹）：保持原样，**不要**当 `miss` —— 图床一次 403 不能把真结果判死。判定逻辑在 `core/fxtwitter.py::judge()`，有单测。

**Grok 一次给出多条帖子就逐条核**：它常常一口气列好几条看着像的帖子，只核第一条的话，它挑错一条整单就废了。`_verify_tweet()` 按顺序跑，最多 `_GROK_MAX_POSTS`（8）条，命中立刻停；全都没命中时用 `grok.pick_best_check()` 挑距离最近的一条当「存疑参考」。上限取 8 不是 5：实测一条真原帖在 Grok 的枚举结果里正好排第 5，卡 5 条太险。`x.com/i/status/<id>` 这种没有作者名的链接也算候选，别过滤掉。改这里别退回只取第一条。

**`unknown` 要重核一次**：配图偶发下载失败会让距离变成 -1，同一个帖子第一次 `unknown`、重跑一次就是 `hit`（实测过）。批量核对时漏掉一条就全废，所以 `_verify_tweet()` 里 `unknown` 且没有明确错误时再跑一次。

**参考图按指纹距离挑，并把距离写进文案**：核对全 miss 或整单没找到时，参考图优先用视觉挑中的那张（`vision.picked_images`，含后来被判成疑似的），视觉没挑出来时再取「候选里跟原图最接近的一张」（`_best_image()` → `reference_candidates()`，按距离而非引擎顺序，并跳过头像命中），Grok 有 `reference_url` 时优先用它。图注必须带上距离（`_reference_distance()`，如「指纹距离 24，不是同一张」）——不写距离，用户会把一张毫不相干的配图当成结果。距离 0 时不能再写「不是同一张」，要写「跟原图是同一张（指纹距离 0）」。用户明确要求保留图：「有个图至少有个谱，没图连谱都没了」，所以别改成只给链接。

**参考图必须说明是谁给的**：`📋 参考图` 那一行由 `_reference_hint()` 统一生成，按 `reference_kind()` 分三种：Grok 帖子配图、视觉方案挑中的那张、两者都不是（写明「不是视觉方案挑的，也不是 Grok 给的」）。用户看到图会默认它是某条结论的配图，来源不写清楚就分不清是模型挑的还是引擎搜的。

**每个引擎的参考区都要有自己最接近那张的配图**：`_ensure_engine_images()` 首次回复给每个引擎 `items[:1]` 的图（不是视觉挑中的那条），`candidate_image()` 也不许因为「这张已经当过顶部参考图」就返回空——否则该引擎的列表看起来像没配图。同一张图因此在一条转发里可能出现两次（顶部参考图 + 所属引擎列表），这是有意的。

**没找到时别把「疑似」说成「快找到了」**：`not_found` 的参考区上方要写明「另有 N 条是同角色或同款服装的图（指纹距离 a～b），不是这张照片本身」，距离取 suspect 里的 min～max，相等时不写区间。写成「疑似同图」会让 20 多距离的照骗看起来像是差一点就命中。

**头像对上不算原帖**：Lens 会把页面里的账号头像、站点图标当结果图返回，距离能到 0，X 的头像地址还长成 `pbs.twimg.com/profile_images/...`，不带 avatar 字样。`verify_candidate` 一律按 `is_avatar_image()`（地址含 `avatar` / `logo` / `favicon` / `profile_images` / `profile_image` / `profile_pic`）置 `candidate.avatar = True`，**与距离无关**：距离 ≤ 阈值时降成 suspect 并清空 `name` / `handle`（那多半是站点标题），距离更远时保持 mismatch，但标记留着。判定统一走 `is_avatar_hit()`（字段优先，旧摘要退回 note 前缀和图片地址），不能只看 note——视觉复核降级会覆盖 note。头像条目：不挑成顶部参考图（`reference_candidates()` 跳过）、不进「视觉复核最近一条」、视觉复核点到也跳过（不当成「同一个人」，也不触发账号核对）、`formatting` 里单独一行说明且**不能算进「同角色或同款服装」**，否则同一张头像会同时被说成「像同一个人」和「不是这张」。

**`x.com/i/status/<id>` 是有用的链接**：这种 X 内部跳转链接没有作者名，但 status id 是真的，fxtwitter 能顺着它解析出真实作者。`find_posts()` 只对这一种保留（handle 记空），别的保留路径（`home`、`search` 等）照样丢。核对成功后要把解析出来的账号回填到 `info["handle"]`，否则 `miss` 分支会因为「没账号」把帖子整个扔掉。

**`xid` 指令是同一套核对的入口**：`麦麦寻人 xid <推文链接或 ID>`，图和指令发在同一条消息里，直接回「对得上 / 对不上 / 没法比对」+ 作者 + 链接。它只做核对，不搜索，不进任务表。

**图片字节自给自足**：引擎都上传图片字节，不依赖 QQ 图链时效；只有 Lens 上传失败才回退图链。

**分流靠结果不靠输入**：不判断输入是照片还是绘制，用 SauceNAO 相似度 ≥ `route_similarity` 反推是插画，再切引擎顺序。

**分流在验证之前**：`_sauce_route()` 只看相似度、不下载图片，所以在 `_verify()` 之前调用。判成插画后不再逐张比对：其他引擎各留 `drawing_other_limit` 条参考、状态保持 `unverified`，最佳匹配从 SauceNAO 的 `match` 里挑，并优先 pixiv 主站（`is_pixiv_platform()`）。

**取数分两档**：`deep_scan=true`（默认）时每引擎取到 `deep_scan_limit`、验证 `deep_verify_top_n`、「更多」展示 `deep_more_limit`；关掉后退回 `max_results` / `verify_top_n`（Lens 2N）/ `more_limit`。改取数只动这一处，别在引擎里另加截断。

**比对必须用原图**：Yandex 的 `thumb` 是裁成 148×90 的方框，裁剪会把 dHash 打乱。`to_yandex_candidate()` 优先取 `originalImage.url`，没有才回退 `thumb` 并在 note 里标明。新增引擎照这个规矩来。

**照片路线优先核对社交平台**：`_verify()` 里按每引擎配额取完候选后，还要把配额之外的社交平台候选（`is_social_platform()`）补进验证队列，上限 `social_verify_limit`，开关 `social_verify_all`。原帖基本都在这些平台，按固定条数截断最容易正好漏掉。

**配置热更新有限**：`self.config` 会热更新，但 `@Command` 的 `pattern` 是模块导入时生成的。改 `command` / `aliases` 必须重载插件。

**用户可见文案不要写死插件名**：转发标题、补图提示、「更多」提示统一用 `COMMAND`（来自配置），只有 `_manifest.json`、包名和 README 用固定的「麦麦寻人」。

**SauceNAO 库策略**：`db` 是全量库，`db_strict` 是收紧库，`db_policy_enabled` 开关（默认开启），`db_full_groups` 名单，`db_policy_invert` 反转。选库逻辑在 `core/engines.py` 的 `resolve_saucenao_db()`，纯函数、有独立用例（`tests/test_dbpolicy.py`）。开关关闭时一律 `db`；开着时名单上的群走 `db`、其他群走 `db_strict`，**名单为空等于全部收紧**，别把「名单空回退全量」加回去。

**群号到会话 ID 的映射**：入站钩子里 `_remember_group()` 把 `stream_id` 和群号记到内存表（`_stream_groups`），`_group_of()` 反查。注意这张表是内存态，重载后为空。

## 平台事实（2026-10-22 实测）

| 事实 | 影响 |
|---|---|
| **消息入库有延迟**：消息刚到时 `message.get_by_id` 常返回 `None`，几秒后同样的 ID 就能查到 | 必须挂 `chat.receive.before_process` 钩子先缓存图片二进制 |
| **`get_by_id` 的 `chat_id` 是 `session_id`（哈希）** | 传错的 `chat_id` 会静默返回 `None`；先不带 `chat_id` 查，失败再带 |
| **图片消息的 `processed_plain_text` 可能是空字符串**（VLM 没出描述时） | 靠「整条消息只有 `[…]`」的正则接图会漏，要用钩子接 |
| Command 传入的 `message` 是 `include_binary_data=False` 的 | 图片没有 base64；钩子缓存的副本有 |
| 钩子的 `stream_id` 参数可能是空的 | 从 `message['session_id']` 兜底 |
| 图片段的 base64 键名是 `binary_data_base64` | 兼容 `base64` / `image_base64` / `file_base64` |
| `ctx.send.forward` 节点是 `{"nickname": ..., "content": [{"type": "text"/"image", "content": ...}]}` | `content` 由 Host 转成 `binary_data_base64` |
| `ctx.send.text(..., set_reply=True)` 失败只返回 `{"success": false}`，不抛异常 | 要检查返回值并回落普通发送 |
| 能力返回值被 SDK 解包 | 统一走 `unwrap_capability` 判断 |
| 指令匹配用 `compiled_pattern.search(text)` | 用 `core/commands.py` 的 `build_command_pattern()`，它容忍前置/嵌套占位符 |
| SerpAPI 的 `thumbnail` 在 gstatic，国内不可达；`image` 才是推文原图 | 验证与配图都用 `image` |
| SerpAPI Image API 上限 500KB | `prepare_upload` 压到 480KB |
| Yandex 页面比图床更依赖线路：2026-11 实测 `yandex.ru` / `yandex.com.tr` 走代理会间歇 SSL 15 秒失败，直连反而 3 秒出结果；`yandex.com` 无论代理还是直连都只回 1.7KB「under construction」空壳页 | hosts 顺序 `yandex.ru → yandex.com.tr → yandex.com`；每条线路失败就换下一条（代理 → 直连），读超时同线路重试一次；空壳页由 `is_yandex_shell_page()` 单独识别并在错误里点名，别笼统写成「结果页结构变化」 |
| SauceNAO 匿名 API 返回 403 | 无 key 时自动跳过；免费档 `short_limit=4`、每天 100 次 |
| 百度 `image_src` 是 `w=889&h=500` 裁剪图 | 真实匹配常落「疑似」（距离 10~14），不要为此放宽 `hash_threshold` |
| 部分 CDN 直接封服务器 IP（`cdn.donmai.us` 403、`lookaside.instagram.com` 返回非图片） | 只能标「未验证」放参考区 |
| Sightengine 单次 `genai` 计 5 ops | 只在低置信分支调用 |

## 疑难案例台账

逐单记录在 [docs/cases.md](docs/cases.md)：哪一单、现象是什么、卡在哪一步、改了什么、哪个版本好的。

「踩过的坑」写规律，这张表写事实。**每处理一单疑难图，必须往台账追加一行台账和一段逐单记录**，不然同一类毛病复发时只能重新翻一遍日志。用户发图时会带单号（`s-YYYYMMDD-HHMM-xxxx`）、正确答案和一句现象，先按 `docs/cases.md` 末尾的「排查一单的顺序」走。

## 踩过的坑

1. **像素统计判不了「照片 vs 绘制」**：试过六组特征全部无法分开。已删掉 `core/imagetype.py`，不要重造。
2. **指令正则不要全文锚定**：`^麦麦寻人$` 匹配不到 `[image] 麦麦寻人`；引用消息时占位符还会嵌套一层方括号；图直接发在指令前面时，图片段在纯文本里是空的，只剩前导空格（` 麦麦寻人`），所以开头要允许 `\s*`。
3. **不要抓 x.com 取账号**：要登录还要跑 JS。handle 从结果 URL 或标题里的 `@handle` 取。
4. **不要把「疑似」当最佳匹配**：只有 `status=match`（以及视觉/Grok 命中）才算 best。
5. **插画模式曾经整条没图**：现在代理失败会自动直连，最佳匹配图失败会并发取后面 4 条候选图，全失败退回用户原图并注明。
6. **等图窗口要按「会话 + 用户」配对**，重搜标记 `_pending_force` 也跟着这个 key 走。
7. **`enabled=false` 要下沉到 `_start_task`**：只挡 Tool 不挡 Command 会漏。
8. **Tool 与 Command 的 `user_id` 不一致**：查最近任务要 `latest_for` 失败后 `latest_in_stream` 兜底。
9. **没匹配上不等于不发图**：`not_found` 时也要取候选里跟原图指纹最近的那张（`_best_image()` 按距离排序，不是按引擎顺序），挂在参考区并在图注里写出距离。
10. **单条合并转发最多放 6 张图**：实测 6 张能过、13 张被拒（错误信息为空）。「更多」在图片超过 6 张时按引擎拆多条，每条带 `1/N` 页码，最佳匹配只在第一批、页脚只在最后一批。
11. **候选图下载要分线路**：`httpclient.download_routes()` 按主机决定顺序（Yandex 系直连优先，其余代理优先），`download_image_multi()` 负责回退。新增引擎别忘了走 multi 版本。
12. **展示按引擎分组，配图分两档**：首次回复每个引擎只贴最接近那张，回「更多」时每个候选都贴图，图压到 60KB 左右。`outcome.images` 只在内存、不落盘。
13. **vlm 组里的模型都能读图，但推理型的会截断正文**：`dots3-note-prev` 把 600 输出额度全花在 reasoning 上、`content` 为空。所以 `[vision] model` 默认留空、按任务组顺序挑；真想点名就填 `Qwen/Qwen3-VL-32B-Instruct`。测多模态一定要用真实尺寸的图。
14. **视觉调用要传 `task_name`**：`model` 传任务名会被当成具体模型名。正确写法 `ctx.call_capability("llm.generate", prompt=..., task_name="vlm", ...)`，图片走 Data URL。
15. **Grok 读水印会读错，读到的只能当线索**：实测把 `@ste11ar021` 认成 `@butter021`，所以它读出来的 ID 用 `watermark_of()` 单独存着，只进日志（`Grok 图上读到 ID：…`）和结果标注（`_watermark_line()`：一致时说「图上印的 ID 就是这个号」，不一致时说「与上面的结论不同，已按核实结果为准」），绝不参与判定。判定只认返回链接，账号存在性靠 `_account_profile()` 兜（`/2/profile/<号>`，能区分查无此号，读不到时写数字 ID）。
16. **Grok 只能走 `/v1/responses` + `tools:[{"type":"x_search"}]`**：`chat/completions` 带 `search_parameters` 会被打回 410；图片只认 data URL。一次完整检索约 48 秒，服务端抖动时超时不能省。
17. **拼图要逐张解码**：`vision.build_grid` 是解码→缩放→贴画布→丢引用，`MAX_SOURCE_PIXELS` 是兜底，别去掉。
18. **缓存别存「没找到」**：实测同一张图 12 小时后就能搜到，存下来反而挡住后来的原帖。
19. **Grok 给的链接不是证据**：它会把别人的帖子当答案，也可能报一个格式正确但内容无关的 status。必须用 fxtwitter 拉配图比指纹再定档；`unknown` 不能当 `miss`。
20. **xid 只在有图时才有意义**：图和指令不在同一条消息时不要进等待窗口（那会被当成普通搜图），直接提示重新发。
21. **dHash 的 8×8 别再加密**：同一张图的两个来源（orig 与 webp large），8 格距离 0、16 格 3、32 格 14——32 格已经超过阈值 8，等于把同一张图判成对不上。加密放大的还有裁切、白边、缩放位移。当前那些距离 13/24 的误判不是分辨率不够，加密只会把它们拉得更开。
22. **pHash 不用换**：实测同图的 webp 版 dHash 0 / pHash 0，粉色床单那张 dHash 24 / pHash 24，判别结果一模一样；耗时也都是 38ms（解码 37.6ms，哈希本身 0.5ms，服务器 2 核）。想再准只能换判据模型，或者在文案上说清「dHash 只负责同一张图」。
23. **视觉提示词拦不住「认衣服」**：门槛改成「个体化证据」后，模型照样把「刺绣细节」「袜口褶皱」当证据，理由是换了词、判断没变。真正起效的是分辨率：同一批 13 张候选，5 列 × 224 挑 6 条，4 列 × 320 只挑 1 条。所以别改回 5 列 / 224；也**别用减少候选数换分辨率**——候选是按距离截断的，砍掉后半段就是把真命中扔了。
24. **QQ 转发压缩不改变指纹**：同一张原图压到 JPEG q55、缩到 720 宽、转 WebP q70，距离全是 0；加 20px 白边是 5、裁 5% 是 4。所以看到距离 13、24 就是「真不是同一张」，别拿压缩当借口放宽 `hash_threshold`。
25. **别给「同角色 / 同款服装 / 同发色」当证据**：实测这几种理由撑起了绝大多数视觉误判。视觉只做兜底，`hash_threshold` 也不因为它远就放宽——dHash 距离和「是不是同一个人」没有对应关系，按距离阈值切档是伪精度。
26. **合并转发不会因为条数被限流**：3 条消息就是 3 次 `send_group_forward_msg`，离群消息频控线差得远，日志里也从没出现过「合并转发失败 / 限流 / rate limit」。三条现在塞进同一次调用的三个节点，群里只出一条。真正会失败的是单条图数超 6 张被 NapCat 拒收，以及宿主 `cap.call` 的 30 秒超时（19:22:50 那次合并转发就超时过）。别再凭感觉加 sleep 或拆条。
27. **正文「帖子：」字段优先于 annotations**：annotations 会把模型检索时撞见的页面全标上，不是它给出的结论。实测一单它正文写「帖子：无、账号：@Kuukow002」，annotations 里却挂着一条 2022 年的无关帖子，旧解析因此判成 post 档、还把账号丢了。现在 `field_posts()` 先看这一行：写了「无」就不再拿 annotations 顶替；`i/status` 没有作者名时用正文「账号：」补。
28. **引用帖不是原帖**：`x.com/YuMIkAnime/status/...` 那条正文在吐槽「有人盗图」，图是引用 @qyuuuz 的，结果被当成 YuMIkAnime 发的。fxtwitter 的 `quote` 字段带着原帖地址与作者，纯引用（自身 `media` 为空）时一定要回退，别只在 `reposts` / `likes` 这类列表页上做文章。
29. **账号主页撑不起结论**：视觉挑中 `x.com/realpakbupati`（69 粉，名字是印尼语「县长先生」），理由写「同款发饰、发尾蓝白渐变」，可那张图是粉发白裙。主页上什么图都有，必须先翻 `/media` 比指纹，翻不到就降级。
30. **X 帖正文里的词会被当成人名**：`kiko on X: "Happy Halloween!"` 抽出了 Halloween，推出 @halloween（1.44 万粉）和泄露站页面。`on X:` 后面是正文，抽名字前必须剥掉。
31. **视觉的理由可能取自别的格子**：同一单里它说「发尾蓝白渐变」，挑中的却是粉发那张——拼图里不同格的特征被串了。所以理由要原样发给用户（`_vision_reason()`），它自己就是最好的可信度提示。
32. **转发地址的 `author` 是原作者，转发者另存 `reposted_by`**：`x.com/redwine278/status/1994540292142501924` 看着像 @redwine278 发的，fxtwitter 返回的却是 `handle=sssmnms`、`name=哎我黑切訥`、`url` 指向 sssmnms 那条，转发者在 `reposted_by=redwine278`。别拿 URL 里的号当作者，也别因为 URL 和 `url` 不一致就当解析出错——那正是转发的标志。
33. **视觉顺带读的水印只能当线索**：同一张拼图里让模型抄 `TARGET` 格上的水印／签名／角落小字（`vision.watermark_of()`），别让它抄候选格的——那是网站或上传者加的，微博那条的 `@颜撩` 就只说明文件是她的号传的，认不出照片里的人。模型读小号白字会错（`@ste11ar021` 读成 `@butter021`），所以文案固定带「视觉模型识别，可能读错」，`handle_from_watermark()` 也只认明写的 `@xxx` 或整串裸账号，中文名一律不猜。
34. **封号要两道证据，别听接口一句话**：`jxporn` / `lanrenbl` / `4khd` 这类付费资源站标题里的 `@账号` 是站方抄来做标签的，不是作者主页，结论里要单写一行点明（`formatting.leak_site_of()`，清单只放实测见过的站）。fxtwitter 的 `/2/profile/<号>` 回 `suspended` 只算第一道，还要抓 `x.com/<号>` 的 HTML 找 `Account suspended`（`_homepage_suspended()`）：实测封号号返回 44KB 且带 `UserUnavailable`/`unavailable_reason: Suspended`，正常号 160KB+ 带简介 meta。两道都命中才写「该账号已被 X 封禁（用户 ID …），主页已经打不开了」，只命中一道就写「X 接口读不到这个号（用户 ID …），可能受限或已封」。**别被截图骗**：封号页会保留名字、简介、加入日期（那是缓存下来的资料），内容区是灰块、粉丝数显示 0，看着像「主页能打开」。数字 ID 记在 `Candidate.account_note` 单独占一行（塞进 `note` 会渲染成「匹配：…」）。纯数字 handle 多半是微博 UID / B 站 UID，别去 X 上白查。

35. **水印号只核「存在」等于没核**：`@Miharu` 确实存在（2010 年的老号，6143712），可它不是作者；Grok 读的 `@Moliuwy` 又查无此人——真号 `@Moliww`（莫璃今天吃什么，104 万粉）正好在两个读法之间，一单就漏干净了。所以现在**两个模型读到的号都走 `_verify_watermark_reading()`：核账号 → 翻它自己的带图帖比指纹**，谁命中谁当结论；查无此人时还可以按近似拼写再试（`handles.typo_variants()`，`uwy→ww` 这类 w 读散的还原排在最前），由 `[vision] watermark_variants` 控制。猜写法本身不安全，所以门槛只有一道而且很硬：**候选号自己的帖子里必须真有这张图**。别把「读到的号存在」写成「就是她」，也别因为一个号对不上图就断言水印读错——handle 会被换手。拼图那一步的水印只能当草稿：`TARGET` 格只有 320px，整轮没结论时会用原图（1600 长边）再读一次（`_reread_watermark()`）。**水印落在本人另一个号上时，别写成「与结论不同」**：78c8 那单水印 `@Asagiriairi001`、结论 `@asagiriairi002`，两边名字都是「朝霧愛理」，是同一个人的大号小号，文案该写「是她自己的另一个号」；同时 Grok 那句「🏷 图上读到 ID：…（与上面的结论不同）」要跳过（`_grok_lines(skip_mark=)`），否则同一件事说两遍、还说得像插件自己没弄明白。比较两个号是不是同一个人要用 `_squash_name()`，别用 `_squash_handle()`——后者会把中文整个抹掉，任意两个中文名都会相等。
36. **微博这类站认不了图**：候选只给 `weibo.com/7652767178/...` 这样的 UID，视觉翻来翻去只能说「同一个人」，可那条微博发的是**另一张**照片（距离 29）。这一单的号本身没找错——`7652767178` 就是 `纸悦Etsu_ko` 本人的微博（15.3 万粉、动漫博主），只是这张图她只发在 X 上。所以现在结论停在「同一个人」且落点带微博 UID 时，会走 `_hunt_x_from_name()`：`identity.fetch_weibo_profile()` 拿昵称（`ajax/profile/info` 要先取一次访客 cookie，不带只回 `Forbidden`）→ `identity.search_x_handles()` 用 Lens 那套 SerpAPI key 跑一次 Google 网页检索 → **要求 X 的 name 跟微博昵称对得上** → 翻它自己的带图帖比指纹。命中才升成结论（`engine="identity"`），没命中只写「找到号了、最近没发这张」，别因为名字像就改结论。开关是 `[vision] x_from_name`。别再把微博 UID 当成「查无此人」，也别反过来把它当成原帖出处。
37. **图包里的图搜不到，要去作者的付费页找那一期**：`jh361` 那单原图是 ran3 的付费包（Patreon `Rossi 108p`，2026-10-07 发布），X 上只有一张免费预告，公开渠道永远搜不到同图——所以确认作者后要再走一步 `_track_paywall()`：读她 X 资料里的链接（`paywall.links_of()`），Patreon 用 `/api/campaigns?filter[vanity]=` 换 campaign id、`/api/posts?filter[campaign_id]=` 列最近二十期、`?include=rewards` 拿档位；FANBOX 用 `api.fanbox.cc/post.listCreator` 与 `plan.listCreator`（**必须带 `Origin`/`Referer`，否则被挡**）。挑期的依据是标题里的作品名（`paywall.pick_post()`），关键词从中日文一起抽——FANBOX 标题写「阿米娅 アーミヤ 105p」，只收拉丁词永远对不上。**别把「有 Free 档」当成「这期免费」**：Patreon 不登录只读得到 `current_user_can_view=false`，读不到每期门槛（`min_cents_pledged_to_view` 为空），所以只写档位下限；FANBOX 每期带 `feeRequired`（日元整额，不是分），能写准这一期多少円。付费帖正文与封面读不到，文案只承诺「卖在哪、多少钱、有没有免费预览」。开关是 `[vision] paywall`。**别拿「有 Free 档」当免费预览**，也别把按作品名对上的公开帖写成「同一张图」：`_hunt_teaser()` 搜 `from:<号> <作品名>`，命中的帖再交给 `paywall.pick_teaser()` 按发布时间跟这一期比（`paywall.post_date()` 要能吃两种写法：付费页 `2026-11-09`、X 的 `Fri Oct 03 13:43:28 +0000 2025`，缺年份时用这一期的年份补、补出来比这期晚一周以上就往前推一年）。**差 14 天以内才算这一期的预告**；隔得远的写成 `🔎 顺带看到：…（她 X 上 <日期> 提到过这个角色，不是这一期）`，不能挂 🎁。实测 ran3 的「万圣节妃咲 106p」只能搜到去年那条「妃咲(兔女郎) 44p」的上架通知——两条都带「妃咲」，指着两期，只按关键词对上就会张冠李戴；而 `Rossi 108p` 那期她 X 上一个字都没提（免费预告只写了「小红帽🐺」），搜不到就是搜不到，别为了凑一行硬塞一条无关的帖。**没对上这一期就不要翻预告**：78c8 那单售卖页二十期里一条都没对上，`_hunt_teaser()` 翻出来的恰好就是结论帖本身，还被写成「她 X 上 2026-11-10 提到过这个角色，不是这一期」——既没有「这一期」可比，又把自己引用了一遍。现在只在 `post` 有 `url` 时才翻，并把结论帖地址一并排除。

38. **Lens 返回 0 条不能当「网上没有这张图」**：SerpAPI 的 google_lens 会对同一张图随机抽空（实测 6 次里 3 次空，命中时恰好是同图原帖），所以空结果一律重试（`[lens] retries`，默认 2 次、共三枪）。但三枪全空是真会发生的：`s-20261112-1250-f895` 当天别的单 lens 都是 59~60 条，只有它 0 条，48 条候选全来自 Yandex／百度／SauceNAO，而正确答案就在 X 上。现在重试完仍为空、且这一轮没走过 Serper 时，会用 Serper 的 Lens 再补一枪（`[serper] lens_fallback`，一次扣 3 次额度，只补一次、不参与轮询）；Serper 自己失败**不改变结论**，但必须留一行日志（`LensEngine._say()` → `Serper 补枪：补出 N 条`／`Serper 补枪失败：原因`），`s-20261112-2026-2504` 就是因为这枪全静默，事后分不清「没补成」和「补了 0 条」。**别把这条兜底理解成「Serper 只在 key 用完时才用」**——那是 0.8.5 之前的语义。另外 Grok 全 5xx 时那一整条线等于不存在，日志写「Grok 兜底：档位=none」，别当成插件没找。
39. **水印读错一个字母，署名能把它纠回来**：`s-20261112-1511-7496` 那单，追查模式从距离 3 的候选图上读出 `@kufu_xox`（r 读成 f），X 上查无此号，插件记 `状态=unknown` 就放过了；而标题里明明白白写着「Vivian cosplay by KURU」——两个放在一起只差一个字母，真号是 `@kuru_xox`（クル / KURU，6859 粉，`patreon.com/KURUkuruxx`）。现在 `handles.signature_fix()` 会把水印号按署名改一遍（等长、Hamming 距离 1 才改），改出来的号排在同名检索结果**前面**先试，仍要翻它自己的带图帖命中指纹才认。**别指望「放宽按名字搜到的候选数」能兜住这类单**：实测拿真 key 搜 `KURU`，Google 回来的是 `Kuru1530`、`kuru88303829`、`kuruupdates`、`KuruKaoru_`，`kuru_xox` 压根不在结果里——所以顺带把 `handles_from_search()` 改成 handle 含这个名字的优先（`kuru_xox`、`kuru8k` 这类整段开头的排最前），它只是锦上添花，真正救命的是署名纠写法。**也别把「核到号」当成「原帖还在」**：`kuru_xox` 2026-11-01 发过「把敏感的投稿全删了」，现有 17 条带图帖里没有这张（最近的一张距离 23），这种情况下文案只能写「找到号了、最近没发这张」，不能写成「就是她发的这张」。
40. **「翻不到原帖」不等于「没认对人」**：`s-20261112-1710-9934` 水印已经从候选图读出 `@kuru_xox`、核到号存在（用户 ID 1834551930628321280），就因为翻她 16 条带图帖没有这张，整单判 not_found，只在下面挂了一句「可能是模型读错了」——而她是 2026-11-01 自己把敏感帖删了，对这个人的每一张图，翻带图帖这条路**永远命中不了**。现在 `handles.name_matches_handle(name, handle)` 把「图上水印」和「搬运帖标题署名」做成一次交叉验证：名字至少四个字母、必须是 handle 的整段开头（`KURU` ↔ `kuru_xox`、`kuru8k` 不算），或者差一个字母能被 `signature_fix()` 改回来（`kufu_xox` → `kuru_xox`）；成立就用 `_promote_cross_author()` 把作者定下来（`engine="author"`、状态 ok、`url=""` 只给主页），文案写「作者是她，但这张的帖子已经翻不到」。**两条边界**：① 只有「已经有落到人身上的结论」（指纹命中的帖子、核过的账号）才不被覆盖，搬运页／图集页该换掉就换掉；② **图上没水印的单不算**——没有水印就没有交叉验证，`1713` 那种只有「reddit 距离 1 + 标题 by KURU」的仍然停在搬运页。**别把这条做成「作者名册」**：用户 11-12 17:22 明确否掉，每次都重新搜，免得名字撞车认错人。
41. **没找到时别配一张「明显不是同一张」的参考图**：`s-20261112-2026-2504` 视觉挑中一张同款球衣的候选（距离 27），照样当参考图发出去，群里只能补一句「指纹距离 27，不是同一张」，看着像插件认错了人。现在 `reference_candidates()` 多了 `max_distance`，只在 `best is None`（真没找到）时传 `REFERENCE_MAX_DISTANCE = 12`：距离超线、以及**没算出距离**（图没下下来）的候选一律不进参考图；一张都没过线就干脆不配图，也不退回用户原图凑数。**别把这个门槛套到「有结论」的单上**——有 best 时参考图就是结论那张，与距离无关。另外用户明确否掉「把水印里的非账号文字（`101 EVOL` 这类）当线索」，噪音太大，读到就丢。
42. **「视觉说像」不算结论，除非那人的帖子里真有这张**：`s-20261112-1435-9aed` 视觉凭「粉色双马尾、同款粉色系服装」在 16 张里挑了一条，百度那条落点是微博 UID、指纹距离 28，整单却写成 ok 发出去了；正确答案 `@myao_nyaa_` 的帖 11-11 才发，太新没被索引，68 条候选里一条都没有。现在 `_require_vision_proof()` 在**所有兜底之后**卡一道（水印、署名、按名字换号这些硬线索先跑完，能换结论就轮不到它）：`vision.needs_post_proof()` 为真（距离超过 `hash_threshold`，或者压根没算出距离）且走 photo 路线时——落点在 X 且有 handle 就去 `_media_lookup()` 翻她自己的带图帖，翻到就升 `match`（结论换成那条帖、距离重算）；翻不到／`fxtwitter` 报错／落点根本翻不了帖（微博 UID、Instagram）一律降 `suspect`、`best` 置空，整单回到未找到。**三条边界**：① 距离在阈值内的不动——那种图本身就是同一张，只是落点不好（转发页、商品页），别把看图拼出来的正确落点也砍掉；② 只管 `photo` 路线，插画那条仍有 SauceNAO 相似度兜底；③ 降级不等于删掉，候选还留在参考区当疑似，别让用户以为插件什么都没看见。`_settle_vision_handle()` 仍然先管「落点是 X 主页」那一类，别把两处逻辑合成一处。
43. **别让「没核实过的猜测」挡住硬线索**：`s-20261113-0106-ed9a` 里三条路同时被堵死——① 标题署名反查只在「没结论」或「结论是搬运页」时跑，视觉先猜了个 `@sleamPM`（距离 32），整条被跳过；② 疑似 X 号推断只要 Grok 报过 handle 就跳过，而 `@lazyfoam` 只是 Grok 读图上水印的读数；③ 搬运页找作者要求距离在阈值内，那张最接近的合集图是 15，够不着。结果 lens 四条候选的标题里明明写着「推特lazycataw」，插件一个名字都没用上。现在：`handles._SIG_TWITTER` 认「推特xxx／twitter: xxx／Твиттер: xxx」（`twitter.com/...` 这种网址不算，不然会抽出 `com`）；署名反查在 `best.status == "vision"` 时也跑；疑似号推断只在 Grok 真核到 `tier=post/account` 时才跳过；署名认到号但翻不到这张（`seen`）时放进 `x_guess`，文案写「她自己最近的带图帖里翻过，没有这张」。**边界**：抽出来的名字只是候选，仍然要 `_profile` 核账户存在、名字对得上、再翻带图帖比指纹才算 hit；`seen` 只当线索，不改结论（用户 11-13 07:43 明确确认：标题写着「推特xxx」也只能算线索，不写成「作者是她」）。另外注意 `_name_text()` 会把 X 帖标题里 `on X:` 之后的正文整段丢掉（防 `@halloween` 那种噪声），所以正文里的 @ 不会被当署名。

44. **微博号读到了就要去微博核，别直接丢**：`s-20261113-1108-73e0` 的图上水印读出「轻颜 / @青生w」，Grok 单独读成「微博@萌生w」——两个都是微博号的写法，可插件的水印取号只认 ASCII（`[A-Za-z0-9_]`），中文昵称一个字符都抽不出来，线索整条消失。现在补了两段：`identity.search_weibo_users()` 拿一次访客 cookie（`passport.weibo.com/visitor`，和查 UID 资料同一套）去请求 `s.weibo.com/user?q=昵称`，这个页面是服务端渲染的，卡片里直接有 UID、昵称、粉丝数；`handles.weibo_targets()` 认「微博@某某」「微博：某某」和 `weibo.com/u|n/xxx` 链接，`handles.at_names()` 连中文 @ 一起认。插件侧 `_hunt_weibo()` 的触发条件有三条：文本里明写了微博、水印上 @ 后面跟着汉字（X 只认 ASCII，汉字的不可能是 X 号）、以及那个号在 X 上查无此人（`PROFILE_MISSING`）。**三条边界**：① 只当线索，不改 `best`、不参与匹配判定；② 微博昵称就是账号名，重名的路人号能占满整页（搜「青生w」出来 15 个「阮青生w*」，粉丝 1~102），所以 `[weibo] min_fans`（默认 200）以下的号不往结果里塞，只在文案里说一句「同名的都是路人号（粉丝最多 102）」；③ `m.weibo.cn` 那套接口不行——带上访客 cookie 也只回一页 `Sina Visitor System` HTML，`weibo.com/ajax/side/search` 的 `user` 字段恒为空，别再去试这两条路；④ **微博昵称匹配必须严于 `name_matches()`，仅「包含」不算同一个人**：`s-20261113-1227-6704` 的水印读成 `didasne`（阿迪达斯 `adidasneo` 的常见拼错），照搬宽松匹配就把阿迪官方号（109.6 万粉）当线索发了出去，用户直接回「找的啥玩意」。现在 `identity.weibo_name_matches()` 单独一套：完全相等直接过；前后缀只放行**多出来的部分 ≤3 字符**（`萌生w` → `萌生www` 过，`kuru` → `kuruupdates001` 不过）；其余按编辑距离，短名（<8 字符）容 1、长名容 2。`青生w` → `阮青生w09`（前缀多 1 字、后缀多 2 字）也被拒——微博昵称是人自己起的，不是 X handle 那种可预判的 ID，宁缺勿滥。过滤发生在按粉丝排序**之前**，否则排在前面的高粉错配会一直被优先选中。
45. **`s.weibo.com` 搜用户必须带 UA**：线上第一版 `search_weibo_users()` 只在请求上设了 `Referer`，忘了 `User-Agent`——访客 session 的默认头里没有 UA，requests 会填自己的 `python-requests/x.y`，微博直接回 **HTTP 432**（反爬状态码，不是 404、也不是空结果，看着特别像「这个昵称搜不到人」）。现在 UA 写进了 `_visitor_session()` 的 session 默认头，且每次请求也显式带上。**探针和线上代码要跑同一份**：以前用 `C:/temp` 里的独立探针跑通了 200，换成插件里的实现就 432，差别就藏在这种「探针里手写了 UA、实现里没写」的地方。另外**登录态失效必须说出来**：填了 `[weibo] cookie` 又收到 401/403/432 时，`_hunt_weibo()` 返回 `{"status":"error","reason":"cookie"}`，文案写「登录态失效了，重新复制一次就能恢复」——不说这句，用户只会以为微博反查整个没反应。用户 11-13 12:00 明确否掉「插件内私聊扫码登录」那条链路，发布版只走「README 教手工复制 cookie」，`tools/weibo_login.py` 留给能登服务器的人自己用。**cookie 拆成 `sub` / `subp` 两格**（用户 11-13 12:32 要求）：`WeiboConfig` 保留整串 `cookie` 只为兼容老配置，`identity.weibo_cookie_header(sub, subp, cookie)` 是个纯函数，容忍带 `SUB=` 前缀和引号，拆分优先、整串兜底——别再在插件里手拼 `f"SUB={x}; SUBP={y}"`，三处调用点都走 `plugin._weibo_cookie()`。新增字段时注意：`PluginConfigBase` 的 WebUI 描述会原样给用户看，别写「拼接」这种操作说明，写「复制 SUB 那一项的值」。

46. **转载页的正文就是原微博的正文，搜帖能直接回原帖**：`s-20261113-1250-26a8` 的正确答案是 G44不会受伤 的微博（`weibo.com/2727362643/Rr1ciyuH8`），但候选里只有一条 `sina.cn` 的转载页（距离 11）——页面自己没写微博链接，插件就把它当普通网页丢掉了。实测那页正文与微博原文一字不差，页头还带「G44不会受伤 / 微博认证：动漫博主」，两条路都能回到原帖：① 页头署名 → 微博用户搜索 → 翻她最近一页带图帖比指纹；② 正文首行 + 话题（`加速世界-黑雪姬 #二次元cos大赏#`）→ `s.weibo.com/weibo?q=` → 卡片里直接有 uid / mblogid / pic_ids。两条路实测都命中同一条帖、指纹距离 1。**四条边界**：① 帖搜索和翻帖**都要登录 cookie**，没配就整条跳过（免登录只会拿到访客墙）；② 只在「还没有可靠结论」时跑，最多抓 2 页、核 3 条帖、每条比 3 张配图；③ 搜帖只负责找人，**必须指纹对上才算命中**，对不上什么都不改；④ 解析靠卡片属性（`mid=`、`copyurl('https://weibo.com/uid/mblogid`、`nick-name=`、`action-data="...pic_ids=..."`），微博改版就把 `core/weibopost.py` 的测试和真实页面一起对一遍。

47. **水印读不准时先定位再放大，不要一上来切六块**：`s-20261113-1250-78fc` 的图是 4000×6000，右下角白字写着 `@清水奶油蛋糕`；拼图那步模型回空串，Grok 整图读成 `@清水如浊蛋黄`，两个都不对。切成六块能读出来但一次六调用、四十秒，而且最容易读出假号（坑 41 里的 MURUA / MIRUI）。现在改成两步：先问「水印在哪」（返回归一化框，**不抄字**，`max_tokens=120`），再按这个框从**原图**裁下来放大到 1600 长边读一次——裁的时候四周留 8% 余量，框偏一点也不切在字上。最多两次调用，且只作最后兜底：整图（原图 + 候选图）读到账号就完全不触发。`vision.parse_locate_reply()` 容忍 `box:[x,y,w,h]` 与 `x/y/w/h` 两种写法，也容忍模型把相对值写成百分数；框小于画面 2% 的直接当没定位到，别拿噪点当字。

## 排障顺序

1. 插件没加载：看 `_manifest.json` 是否合法、`create_plugin()` 是否存在、生命周期方法是否齐全。
2. 指令没反应：确认 `command` 与配置一致（改过要重载），确认正则容忍占位符。
3. 一直「没看到图片」：看日志有没有「已缓存入站图片」；有缓存还失败，看「按消息 ID 没有取回消息体」。
4. 结果全是「未验证」：候选图下载失败，检查代理与 `download_timeout`；403 属于对端封 IP。
5. 引擎全失败：看任务文件里 `summary.errors` 的 HTTP 状态；Yandex / SerpAPI 先怀疑代理。
6. 追问「更多」没结果：任务过期（`task_ttl_minutes`）或插件重启过（内存任务表重建）。
7. 结果秒回但明显是旧的：那是按图缓存命中，日志里有「按图缓存命中」；要新结果用「重搜」。

## 代理与配置段

只有一个代理设置：`[plugin] proxy`，引擎、候选图下载、AI 检测全走它。写 `direct` / `none` / `off` 表示强制直连，留空等同直连。已去掉引擎段里的 proxy 与 `image_proxy`，不要再加回来。

SSRF 防护在 `core/httpclient.is_public_url()`：非公网 http(s) 一律拒绝。**fake-ip 例外**：Clash / mihomo 默认把域名解析到 `198.18.0.0/15`，被当成非公网，于是候选图全报「图片地址不是公网 http(s)」。开关是 `[plugin] allow_fake_ip`（默认 `false`），放行范围是 `[plugin] fake_ip_ranges`（默认 `["198.18.0.0/15"]`），两者经 `fake_ip_networks()` 解析成网段元组后传下去——关着返回空元组；单个条目写错只跳过它自己，不会退化成全部放行。内核换了 `fake-ip-range`（如 `100.64.0.0/10`、IPv6 `fc00::/18`）时改这个列表即可，`10/172.16/192.168/127/169.254` 与元数据地址在未显式列出时照旧拒绝。新增下载入口必须把 `allow_nets` 透传下去（`is_public_url` / `download_image` / `download_image_multi` / `engines.verify_candidate` / `plugin._download` / `plugin._fake_ip_nets()`），否则开关会失效一半。

四个引擎是顶层配置段 `[lens]` / `[yandex]` / `[baidu]` / `[saucenao]`，与 `[vision]` / `[grok]` / `[ai_detect]` 平级。**不要**改回 `[engines.lens]` 这种二阶嵌套：SDK 只把顶层配置拆成独立区块，二阶嵌套在 WebUI 里会渲染成 `[object Object]`。

`0.5.5` 把 `vision_*` / `grok_*` 从 `[plugin]` 拆成了 `[vision]` / `[grok]`（字段名去掉前缀，`fallback` 改名 `enabled`）。老配置靠 `core/config.py` 的 `migrate_legacy_sections()` 走 `normalize_plugin_config` 一次性搬过去，只在目标键还不存在时写入，别删掉这段，否则已有用户的 Grok key 会丢。

## 测试

```bash
python -m unittest discover -s tests -v
```

目前 682 项。新增引擎适配、改阈值、改指纹逻辑、改缓存或账号归一规则时补用例。改动 `_verify` / `_rank` / 取图链路后，用真实图跑一次端到端，确认「匹配 → 最佳匹配 → 带图」不断。

改完 `plugin.py` 必须重载验证：日志里应出现 `麦麦寻人已加载：… 上限=…` 与 `vX.Y.Z 加载成功`，且无 traceback。改源码会重启插件进程，内存里的任务表会重建。

## 发布前检查

- **`host_application.max_version` 不能写 999.999.999**：插件市场的校验规则是前两位不能为 99 或 999，写错不会报错，只会把 Release 静默记进 `plugin_versions.json` 的 `rejected_releases`，市场索引永远停在最后一个通过的版本。现在写 `1.9.999`（第三位不限）。改完拿市场仓库的 `.github/scripts/validate_manifest.cjs` 跑一遍 `collectManifestErrors(manifest, {historical:false})`，确认零错误再发版。
- `config.toml` 不要提交；确认仓库里只有 `config.example.toml`。
- 仓库内不要留 `plugin.py.upload_tmp`、`__pycache__`、日志、任务数据与缓存文件（`image_searcher_cache.json`）。
- 不要在 README / AGENTS / 代码里出现真实 key、代理 IP、群号。
- 下载候选图统一走 `download_image_multi`，并且先过 `httpclient.is_public_url`。
- **取图窗口要能解释清楚**：模型侧优先按 `msg_id` 取图，其次引用，最后才是「最近 3 分钟内的图」；超窗时 `_resolve_image` 带回过期秒数，由 `stale_image_hint()` 告诉模型传 msg_id。
- 解码前先看像素数（`hashing.MAX_PIXELS`）：**不要改 `Image.MAX_IMAGE_PIXELS`**，那是 PIL 的模块级全局，会连累同一进程里的其他插件。
- 不提交个人环境定制值（改过的 `command`、`nickname`、代理地址、`db_full_groups` 里的真实群号）。
- **截图必须脱敏**：群名、昵称、头像、账号、露骨标题都要遮盖；真人裸露照片不要放原图，效果图优先用虚构字段的示意图。
- 提交前扫一遍：`grep -rn "@[A-Za-z0-9_]\{2,\}" README.md AGENTS.md` 与 `grep -rn "127.0.0.1\|OnlyFans" README.md`。
- **Release 正文里的 `@handle` 会被 GitHub 当成用户提及**：写成 `@qyuuuz` 这种，Release 页面底部会自动长出一个 Contributors 区，列出被艾特到的真实 GitHub 账号——连占位符 `@xxx` 都能命中。所有 handle 一律用反引号包起来（写作 `` `@qyuuuz` ``），发完用 API 拉一次正文，确认没有任何裸露的 `@`。

