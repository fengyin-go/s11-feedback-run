---
name: s11-feedback-run
description: solo11（26.11 领域专项，单轮 pointwise）的运行侧：把铺好的批次逐题在 Claude Code 任务镜像的新容器里单轮跑完，
  导出轨迹并取 SessionID 与 PromptID，提交并推送产物快照，录运行录屏，取证，打交付完整性分数、写 Feedback，过闸门后汇成结果表。
  仅轨迹分析使用 reviewer.env 的模型配置；其余验收、评分和 Feedback 由推进任务的当前 AI 基于证据完成，脚本负责备料、证据绑定、卡硬上限与查重；通过 SOLO2 接口上传轨迹与视频、建单并回读，同时保留本地 Excel。
  自带运行、收集、快照、录屏、取证、闸门、结果表脚本和自测，不依赖其他 skill。
  关键词：solo11、领域专项、单轮运行、轨迹、产物快照、运行录屏、交付完整性、Feedback、结果表。
---

# s11-feedback-run

数据仓库发布身份以当前电脑 `gh` 实际生效令牌为准，用 `gh api user` 读取活跃账号；不同电脑不要求固定账号，`_config/settings.json` 的 `github_owner` 不再决定发布 owner。开始真跑、完整流水线和推送前都核对活跃账号；活跃账号为 `fengyin-solo` 时必须立即停止任务并提示切换账号，不能自动回退到其他账号。任务仓库 owner 必须与当前活跃账号一致。旧交付链接与历史证据保持原样，迁移既有仓库需另行明确处理。

这一期的运行侧，从「批次目录已铺好、每题一个 GitHub 仓库」做到「结果表和 SOLO2 平台上的一条可核对提交」。造仓库、出题、铺批次由出题侧完成。S11 单次运行批次优先接收 [`s11-gsb-prompt-create`](../s11-gsb-prompt-create/SKILL.md) 的交接；已有 `cc-domain-task-create` 批次继续兼容。

工作目录是带 `_config/settings.json` 的那个文件夹（下文叫 `<root>`）。脚本按 `--root` > 环境变量 `SOLO11_ROOT` > 当前目录向上找 的顺序定位它。下面的命令都省略了脚本目录前缀 `python3 <skill>/scripts/`。

本 skill 只在明确调用时使用；Codex 的显式调用策略在 `agents/openai.yaml` 的 `policy.allow_implicit_invocation: false` 中配置。

## 流程

默认使用 [滚动调度与恢复](references/runtime-flow.md) 和 [缓存与并发](references/performance.md)：跑完一题就进入录屏、取证和独立评价，不等待整批跑题结束。下列命令均须加 `python3 <skill>/scripts/` 前缀。

当用户说“运行所有提示词”“跑完这批任务”或同义指令时，默认模式是完成平台提交前的全部操作，不把“运行”理解成只做 R1。直接使用 `fbr_flow.py pre-submit --batch … --detach`，继续处理运行、收集、快照、录屏、取证、Feedback、闸门、导出、本地表和字段核查；该命令默认发布验收通过的 GitHub 产物快照，但不提交 SOLO2 平台。平台提交仍需用户另行明确授权。

```bash
fbr_flow.py pre-submit --batch b1110-01 --detach
fbr_flow.py run --batch b1110-01
fbr_flow.py run --batch b1110-01 --execute --confirm FLOW --detach
fbr_flow.py status --batch b1110-01
fbr_flow.py wait --batch b1110-01 --events needs_task_agent --timeout 300
fbr_flow.py drain --batch b1110-01 --execute
fbr_flow.py resume --batch b1110-01 --execute --confirm FLOW --detach
fbr_work.py next --batch b1110-01
fbr_work.py closeout --batch b1110-01 --task s11-000123
```

阶段默认只读一份 `references/stage-briefs/{run,review,submit}.md`，不要同时加载全部 references。命令默认摘要模式只返回状态、阻断项、结果路径和下一条动作；`--json` 才输出完整对象。完整表格、平台 schema、errors 和 metrics 留在本地文件，定向查询，不整份回灌。

真跑默认在验收通过后主动推送 GitHub 产物快照并回读 SHA，再核查结果表；明确只做本地时加 `--no-publish`。平台提交另加 `--auto-submit --confirm-submit SUBMIT`，仍须单独获得用户明确授权。下列逐阶段命令仍可用于排错和补跑：

滚动提交不是默认模式，也不由 `pre-submit` 开启。只有用户明确要求一边生产、一边提交平台时，才使用：

```bash
fbr_flow.py run --batch b1110-01 \
  --auto-submit --confirm-submit SUBMIT --submit-mode drain \
  --execute --confirm FLOW --detach
```

平台提交由独立单线程队列串行消费，不占用运行、录屏、取证和评价并发。每条本地完成后不能直接上传，必须先执行单题提交预检：

- 对当前题重新跑 `gate_batch(..., wanted=task_id)`，覆盖单题证据、人工抽检和当前已知批内/历史查重；
- 刷新白名单交付投影，核对 Feedback、需求覆盖、18 个必填字段、快照和媒体；
- 登录平台读取实时 schema、账号领域和已有 SessionID，但预检阶段不上传、不建单；
- 结果写入 `review/submit-preflight.json`，绑定 `feedback_sha`、`source_digest`、`content_digest`、产物快照和 schema fingerprint。

只有预检 `ready` 的单条才进入平台上传。正文、分数、抽检、轨迹、录屏、快照、平台表单或账号任一变化，原预检都失效并重跑。预检失败只阻断这一条，不停止其他题的生产和提交。人工 `fbr_batch.py upload` 继续保留“所选任务全部预检通过后才开始上传”的整批语义。

需要平台质检通过后立即回收过程目录时，显式增加清理授权：

```bash
fbr_flow.py run --batch b1110-01 \
  --auto-submit --confirm-submit SUBMIT \
  --auto-cleanup --confirm-cleanup CLEANUP \
  --submit-mode drain --execute --confirm FLOW --detach
```

自动清理不是默认行为。启用后，每条只有同时满足以下条件才会删除：平台实时详情为 `QC_PASSED`、本地回执为 `confirmed`、当前正文/附件/轨迹摘要与回执一致、结果投影全部通过 SHA256 校验、任务没有活动进程。`QC_RUNNING` 会继续轮询；`PENDING_FIX`、`uncertain`、摘要漂移或活动任务只报阻塞，不删除。轮询间隔由 `--cleanup-poll-seconds` 控制，默认 60 秒。

清理前把平台回执、产物快照、题面与复现字段、录屏方案、尝试摘要、diff 和文件摘要归档到 `results/<批次>/<任务>/retention.json` 及 `platform/`。随后只删除 `origin/`、`workspace/`、大体积 `run/` 现场、失败录屏候选和映射到该题的 runtime scratch；原始轨迹、录屏、证据、Feedback、平台回执和小型任务骨架保留。

```
fbr_run.py preflight                 R0  镜像、版本、context 上限、key、本机依赖
fbr_run.py run --batch …             R1  演练；真跑加 --execute（起容器、消耗网关额度）
fbr_run.py collect --batch …         R2  导出轨迹，取 SessionID / PromptID，确认首轮及获准的中断续写
fbr_snapshot.py commit --batch …     R3  宿主机提交工作区（只动本地）
fbr_snapshot.py push --batch …           演练；真推要 --execute --confirm PUBLISH
fbr_record.py --batch …              R4  任务镜像新容器里跑演示命令，录成 mp4
fbr_facts.py --batch …               R5  取证 → evidence/facts.json
fbr_feedback.py card --batch …       R6  素材卡 → review/card.md
fbr_evidence.py ingest --task …          轨迹单次摄取 → manifest/events/pack/check
fbr_feedback.py decision --batch … --task …    compact decision-input 骨架
fbr_reviewer.py review --batch … --task … --execute
                                       仅分析轨迹，完成后返回 needs_task_agent（见 references/reviewer.md）
fbr_feedback.py gate --batch …           闸门，通过才算数
fbr_metrics.py aggregate --batch …        区分产物模型、reviewer、Feedback 修复和证据读取成本
fbr_logs.py --task … --source stream --grep '错误原文' --context 2  有界日志查询
fbr_batch.py export --batch …            白名单证据 → results/<批次>/，不复制代码
fbr_batch.py preview --batch …           抽检预览页（人看）
fbr_sheet.py --batch …               R7  result.xlsx；--watch/--watch-status/--stop 管理实时刷新
fbr_submit.py probe                     登录并只读核对平台账号、实时字段和锁定领域
fbr_submit.py preflight --batch … --task …  单题只读预检，不上传
fbr_batch.py upload --batch …            平台演练；真提交要 --execute --confirm SUBMIT
fbr_sheet.py --batch …                   回执有效时回填远端媒体、实际提交人/时间
fbr_cleanup.py audit --batch …           只读盘点 QC_PASSED 清理候选
fbr_cleanup.py restore --batch …         从完整 history 恢复被后续规则缩掉的冻结投影
fbr_cleanup.py prune --batch …           预览清理，不删除
fbr_cleanup.py verify --batch …          校验已清理任务的长期结果包
```

`fbr_cleanup.py prune` 默认只预览；真实删除必须：

```bash
fbr_cleanup.py prune --batch b1113-01 --execute --confirm CLEANUP
```

删除前先把目标原子移动到 `_verification/cleanup-trash/`，写 `cleanup.json` 和批次 `cleanup-manifest.json` 后才清空 trash。清理后仍保留 `task.json`、`state.json`、`recording-plan.json`、`run/attempts.json`、`run/key-identity.json`、`trace/`、`record/recording.mp4`、`record/recording.json`、`evidence/` 和 `review/`。依赖缓存和批次交付历史不与单题一起删除。

已清理任务的当前结果包会冻结保存，后续刷新结果表不再用新规则重新淘汰；若历史批次曾被后来的规则刷新缩减，可先运行 `fbr_cleanup.py restore --batch …`，从已校验的 delivery history 恢复原任务投影，再审计和清理。

每一步都是闸门式的：上一步没过，下一步的脚本直接拒绝；上游重做了，下游自动作废要重来。任何时候不确定走到哪了，跑 `fbr_batch.py status --batch …`，它会列出每道题卡在哪、下一条该跑什么。不要手改 `state.json`。

数据调表、Feedback 或交付闸门未通过时，默认继续修复到能够通过，不以标记“不通过/blocked”结束可处理的问题。先按 [失败修复与复验](references/remediation.md) 定位来源，再修复字段、补齐实跑材料或由当前任务 AI 修改评价，重新跑相关闸门并立即刷新本地表。只有缺少必要授权、证据确实无法补齐、服务持续不可用或既有预算耗尽时才保留明确阻塞及下一步；其他题继续处理。通过来自事实与材料完整，不能降低标准、改被测代码、伪造证据或修改失败标记来实现。

底层单阶段命令和 `run` 仍默认演练；`pre-submit` 是“运行所有任务”的默认入口，明确执行到平台提交前，不需要再次逐步确认。它默认包含验收通过后的 GitHub 产物快照推送；明确只做本地时使用 `--no-publish`。单独 `push` 仍使用 `--execute --confirm PUBLISH`；平台提交仍需单独授权并使用 `--execute --confirm SUBMIT`。平台演练会登录并读取账号、字段和历史记录；不上传附件、不建单。

## 1. 运行与收集

先按 [references/handoff-input.md](references/handoff-input.md) 接收出题侧批次。新批次会核对冻结基线、题面、需求点与录屏方案的交接摘要；发生变化需重新交接。每题独立起跑，不能接着上一题的代码跑。新出题侧每题只保留一个 `origin/` 和一个 `workspace/`，远端只有 `main`；同一提示词只执行一次首轮，不拆 A/B 两侧。`origin/` 保留冻结初始提交和出题预检依赖，唯一挂载的是干净的 `workspace/`。

运行基线：镜像 `adminfather/benzhi-claude-code2:20261110`，Claude Code `2.1.319`。Intel 与 Apple 芯片使用同一镜像名、同一组命令，由 Docker 自动选架构，不加 `--platform`。网关 `https://llm2.jzxhnh.com`、模型 `auto_model/urm` 和 Git 环境已配置在镜像内，不重复配置。镜像 Id 是本机所选架构的值，不能把另一架构的 Id 写死复用。

默认只加载 `references/stage-briefs/run.md`；需要排错时再读 `references/run-rules.md`。必须保持：

- 一题一个新容器，题面只送一次；模型自行中断只允许同一会话累计最多两次“继续”。
- 持续有模型输出、思考增量或进行中工具时不按墙钟误杀；网关/基础设施错误单独计数，不消耗模型责任机会。
- 正常结束且有代码改动才有效；零代码改动归档后恢复基线重跑，最多三次。跑得慢本身不是重跑理由。
- 轨迹完整导出并核对 SessionID、PromptID、首轮和续写后才停止容器。
- key 只通过 `--env-file` 进容器；轨迹出现 key 时保留原始证据并自动记录备注，不改写原始轨迹。
- 授权、停止、恢复、尝试上限和容器清理细节全部以 `run-rules.md`/`runtime-flow.md` 为准。

```bash
fbr_run.py preflight
fbr_run.py run --batch b1110-01                      # 演练：列出哪些题没准备好
fbr_run.py run --batch b1110-01 --execute            # 真跑，并发按 settings 里的 concurrency
fbr_run.py collect --batch b1110-01
```

## 2. 产物快照

`finalize --execute` 和完整流水线默认在验收通过后主动推送 GitHub 产物快照；明确本地模式时使用 `--no-publish`。执行顺序固定为：本地 commit → 录屏和取证 → 独立验收与 Feedback 闸门 → 立即落本地表 → 推送远程快照并回读 SHA/public → 再生成表格并核查全部必填字段。不得在独立验收前自动推送；推送失败保留验收结果，标记交付未通过并重试发布，不重新跑题。平台提交和 GitHub 发布分别遵守本次用户授权。

- 保留模型提交，不 squash/rebase/强推；宿主机只追加一个提交收走未提交改动。
- 初始提交必须是产物祖先；历史被改写时用最终文件在初始提交上重建，并只记内部 `review/snapshot.json`。
- 提交前扫描 key、密钥文件、token、超大文件和保留的中间提交；命中即停。确属样例的仅可登记 `secret-waivers.json`，网关 key 原文永不免除。
- 不要绕过脚本在 workspace 手敲 git；只推进同一 `main`，初始快照始终引用 `task.json.initial_commit`。
- `gh` token 只走环境变量；推送前后核对 public、远端 SHA 和可见性。

```bash
fbr_snapshot.py commit --batch b1110-01
fbr_snapshot.py push   --batch b1110-01              # 演练
```

## 3. 录屏

- 在任务镜像的新容器、产物干净克隆上录，不带 key；演示方案取 `recording-plan.json`，授权覆盖才用 `recording-plan.override.json`。
- 先出现提示符，再逐字输入并执行启动命令。命令失败照录，不改命令、不重试到成功；setup 失败不得冒充产物失败。
- Web 必须演示真实启动和功能操作，不能只录构建、首页或空弹窗；页面目标 8 秒，终端启动目标 10 秒，成片 1280×720/H.264/≤90 秒。
- 新录制先进入候选目录，合格后才替换正式视频；重录失败保留旧合格版本。
- 选页、pause_ms、多页面覆盖和截断判定以 `run-rules.md#录屏r4` 为准。

```bash
fbr_record.py --batch b1110-01                       # --redo 重录
```

## 4. 取证

`fbr_facts.py` 把打分要用的事实收成 `evidence/facts.json`：演示命令的实际结果；初始提交和产物提交各跑一遍 `make setup` + `make test` 的对比（新增失败、新修好）；改动清单（含被删改的测试、新加的跳过标记、新增的占位）；轨迹步骤索引；模型自己验证过什么、最后一次改动之后有没有再验证；模型最后的回答，以及回答里提到但产物里没有或没改的文件。

脚本只收事实，不下结论。初始提交上本来就有的失败不算在这次改动头上。

## 5. 打分与 Feedback

先读 [references/reviewer.md](references/reviewer.md)、`references/scoring.md`、`references/feedback-writing.md` 和 `references/feedback-language-policy.md`。**只有轨迹分析使用 `reviewer.env`（默认 `gpt-6.1-sol`）**。过程证据核查、需求完成判断、产物验收、交付完整性评分、Feedback 撰写、产物断言逐句复核、门闸修复均由推进任务的当前 AI 完成，不通过该配置调用其他模型，也不固定另一个产物验收模型。人工 spotcheck 仍由实际审核人完成。

提交或平台返修前还要通过 [references/feedback-platform-style.md](references/feedback-platform-style.md) 的本地风格检查。4 分正文必须明确写出扣分对应的失败或返工后果，并留下至少一个真实文件、命令、对象或返回值；测试层级和总数不能代替具体行为验证。系统会拦截已知的业务速记、终端成功输出和过密精确数值。

1. `fbr_feedback.py card` 汇集题面、需求点、测试对比、演示结果、改动与轨迹索引。
2. `fbr_reviewer.py review` 自动先对全部主/子轨迹及获准续写做一次全量摄取，建立事件图和精确偏移；reviewer 只读事件包，并按行号/block 回查候选原文，保存带精确出处、完整后续状态和调用回执的 `review/trace-analysis.json`，随后返回 `needs_task_agent` 检查点。轨迹配置未填写 `model` / `reasoning_effort` 时沿用 `gpt-6.1-sol` / `high`；不可用时只暂停这一阶段，不伪造分析。
3. 当前任务 AI 默认读取 `review/task-agent-brief.md`、当前阶段 brief、相关代码 hunk 和真实实跑材料，判需求完成情况与评分。先写 `review/decision-input.json`，同时填写 `language_review`：最关键业务约束原句、黑话改写和必要业务词；再运行 `fbr_reviewer.py build` 展开并 lint。只有排错或迁移时才手写完整 candidate。代码已能确证的问题不重复跑服务；缺证据则用既有 probe 补齐后刷新 card 和检查点。5 分仍须至少两个不同的、题面未明说的有效边界 probe。
4. 当前任务 AI 另做产物与文字复核轮，按 `public_units` 逐句拆出断言，核对原始连续摘录、后续修正、最终影响，以及七项语义要求，保存绑定候选和检查点的 verification。每个 `process` 句除真实 `steps` 外还必须写 `evidence`，逐条指定 `kind=tool`、step、`target/output/error/tool` 字段和原始连续摘录；没有工具步骤时用 `kind=assistant` 绑定真实助手文本。`claim_reviews.sources` 中凡是 `trace/projects/*.jsonl`，必须同时写 `start_line`、`end_line`、`block` 和 `excerpt`。独立复核轮必须引用同一摘录，并通过 `fbr_evidence.py fetch/verify` 回查精确原始区间，不能复读整份原始轨迹。过程句还必须按 [模型过程归因审计](references/process-attribution-audit.md) 完成 `process_audit`：逐句确认主体是模型、基线行为没有被误归给模型、时间线覆盖后续修改、验收 probe/录屏没有混入模型动作，并对“只、仅、从未、收尾”等范围或时序表述扫描完整轨迹。不得将自报“通过”代替逐句证据，不得冒称另一个模型或独立审核人完成。细节见 [reviewer.md](references/reviewer.md) 和 [feedback-quality.md](references/feedback-quality.md)。
5. `fbr_reviewer.py finalize` 只做确定性校验，不读取 `reviewer.env` 或调用外部模型；通过后保存 `audit.json`、`feedback.json`、`model-review.json` 与 `quality.json`，执行门闸、刷新本地表并主动推送快照，再核查交付字段；明确本地模式时使用 `--no-publish`。失败返回具体错误，由当前任务 AI 继续修复、补证和复验；材料、规则或正文变化须重新核实，不给旧记录补盖新回执。

```bash
fbr_feedback.py card  --batch b1110-01
fbr_feedback.py decision --batch b1110-01 --task s11-000123
fbr_evidence.py ingest --task /absolute/path/to/batches/b1110-01/<任务目录>
fbr_evidence.py pack   --task /absolute/path/to/batches/b1110-01/<任务目录>
fbr_evidence.py fetch  --task /absolute/path/to/batches/b1110-01/<任务目录> --ref '{"path":"trace/projects/...jsonl","start_line":554,"end_line":554,"block":0,"excerpt":"连续原文"}'
fbr_reviewer.py review --batch b1110-01 --task s11-000123 --execute
fbr_reviewer.py build --batch b1110-01 --task s11-000123 --decision /absolute/path/to/review/decision-input.json
fbr_reviewer.py lint --batch b1110-01 --task s11-000123 --candidate /path/to/candidate.json
fbr_style.py check --text '待检查的 Feedback 正文'
fbr_feedback.py probe --batch b1110-01 --task s11-000123 --cmd 'node bin/cli.js pack fixtures/empty.mp4'
fbr_score_audit.py scan --batch b1110-01
fbr_score_audit.py probe --batch b1110-01 --task s11-000123 --cmd '<复现命令>' --side both
fbr_feedback.py gate  --batch b1110-01
```

候选 Feedback 必须先通过 `fbr_reviewer.py lint`。它提前执行 finalize 的确定性检查、统一语言策略、平台已公开的 AI 文风检查及 `requirements_status` 需求覆盖检查。语言命中和改写建议写入 `review/style-report.json`，正文变化即失效；平台提交前仍会重新登录、读取实时 schema 并核对附件和账号，不能省略。

已有 4 分记录不得按比例降为 3。需要降 3 时，先用 `fbr_score_audit.py` 扫描候选并在初始基线和产物上跑同一 probe，保存结果或过程失败证据；`validate` 通过后，当前任务 AI 才能修改 audit 严重度、score、Feedback 并重新过闸。规范见 [4 分到 3 分的证据审计](references/downgrade-audit.md)。

状态与性能证据：

- `trace/manifest.json` 记录每份原始轨迹的 SHA、字节数、行数和偏移索引；`trace/events.jsonl` 是全量事件图；`trace/trace-check.json` 证明所有文件、行和工具调用都已机械扫描；`trace/review-pack.json` 是 reviewer 的输入包。
- 原始 `trace/projects/*.jsonl` 不允许直接 `cat` 或整文件读取。需要证据时只通过 `fbr_evidence.py fetch/verify` 按行号和 block 读取；同一批请求按文件去重并一次打开。
- `review/task-agent-brief.md` 是当前 AI 的默认决策包；`review/card.md`、`facts.json`、`diff.patch` 和 `task-agent-history.json` 是定向查询材料，不作为整份上下文输入。
- `fbr_work.py next` 一次只返回一题的 brief、checkpoint 和定向查询路径；完成 finalize 后再取下一题，避免单线程上下文累积整批材料。
- `review/metrics.json` 记录 reviewer 返回的 token 使用量、输入输出字节、轨迹包大小和证据访问统计。用量只作优化指标，不能替代质量闸门。
- 默认用 compact decision：当前 AI 只写需求语义、正文、claims、score_basis 和检查理由；脚本展开 audit、requirements_status、claim_reviews、process_audit 引用和 verification 骨架。唯一 locator 可自动补齐，歧义 locator 必须失败，不能猜。
- `fbr_run.py status`、`fbr_submit.py probe/preflight/submit/repair`、`fbr_batch.py upload/repair`、`fbr_score_audit.py` 默认只打印摘要。完整 schema、upload plan、validation errors 和状态明细分别落盘。

Feedback 的硬底线：

- “模型”只在第一次明确主体时出现一次，后续用“它”承接或省略主语。正文不写“轨迹显示”“录屏中”“证据表明”等取证口吻。
- 结果先按需求说明用户可观察的完成、部分完成、缺失和问题；过程最多 3 句，只保留最有解释力的真实失败、修正和最终影响。已修正的小问题不自动扣分，验证不足不等于功能失败。
- 每个问题句和过程句必须有真实锚点。过程 evidence 必须落在指定步骤的真实输入/输出；逐句复核必须回到同一原始区间并检查完整后续和最终状态。
- 5 分必须与正文呈现一致；正文若写出首测失败、错误断言、核心返工或关键竞争未验证，最高 4 分。
- 正文自然、具体，不写公司黑话、抽象名词清单、空泛完成度和机器报告字段；统一规则见 `feedback-language-policy.md` 和 `feedback-style-lexicon.json`。测试数量必须绑定同次输出，不能拿全仓总数代替本题行为。
- `language_review.criterion_sentence` 必须逐字出现在正文，说明本轮最看重的业务约束；词表命中必须改写后重新核对证据，不能自动换词。
- `requirements_status` 必须覆盖全部需求，状态只能为 completed/partial/missing/unverified，并与 audit 一致。
- 写作、评分、文风和查重细则分别以 `feedback-writing.md`、`scoring.md`、`feedback-platform-style.md` 和 `feedback-calibration.md` 为准。

平台返修不是重新建单。`PENDING_FIX` 先用 `review/prepare/finalize --platform-repair` 重做当前 Feedback，再运行 `fbr_batch.py repair`；复用原记录和附件，只更新允许字段并在回读确认后重新质检。

## 6. 抽检与结果表

环境可复现等级必须逐字属于 `fbr_submit.py probe` 读取的平台 `repro_level.options`；不得填系统架构、安装或启动经过等自由文本。来源不在选项内时阻止交付，保留冻结 task.json，核实真实环境后通过有来源的字段修订处理，不猜测或静默降级。平台暂不可达时不能声称已核实最新选项；恢复后先重新读取 schema 再提交。

结果表“闸门状态”同时检查当前独立验收、18 个必填输入字段、枚举、真实轨迹与录屏、已推送且回读确认的快照。任一缺失显示具体阻塞原因，不得仅因 Feedback 通过显示“通过”；提交人和提交时间由平台生成，未提交时保持空白。每条验收结束立即刷新本地结果及字段核查页，不等待整批完成。

```bash
fbr_batch.py export --batch b1110-01                 # 白名单证据投影，不复制代码
fbr_preview.py run --batch b1110-01 --detach         # 本地 HTTP 预览，支持视频拖动，交给人看
fbr_batch.py spotcheck --batch b1110-01 --task s11-000123 --state 退回 --by 张三 --note "第二句说的报错在日志里是另一个原因"
fbr_sheet.py --batch b1110-01
fbr_sheet.py --batch b1110-01 --watch --detach
fbr_sheet.py --batch b1110-01 --watch-status
fbr_sheet.py --batch b1110-01 --stop
fbr_submit.py probe                                # 登录并只读读取当前平台配置
fbr_submit.py preflight --batch b1110-01 --task s11-000123  # 只读预检，不上传
fbr_batch.py upload --batch b1110-01 --limit 1       # 平台演练，校验字段与交付文件摘要
fbr_batch.py upload --batch b1110-01 --limit 1 --execute --confirm SUBMIT
fbr_sheet.py --batch b1110-01                       # 确认回执后回填媒体/提交人/时间
```

- 抽检由真人执行；`spotcheck` 只登记结论，agent 不能自批通过。退回后改正文并重新过闸。
- `results/<批次>/` 是固定交付投影，只含当前有效且已过闸门的白名单证据；代码和 workspace 不外移。直接改副本无效，列定义见 `sheet-columns.md`。
- 平台 18 个必填输入字段必须逐题核查；只有 `other_issues` 可空，提交人/时间由平台生成。缺值或未验收就阻止，不猜、不占位。
- `other_issues` 只允许中断续写和“模型运行时读取了 apikey”两类轨迹事实，其余为空。
- 操作系统使用实时枚举 `MacOS/Linux`；未知枚举或 schema 变化停止并重新核实。

本地“备注”和平台“其他问题”（`other_issues`）只允许两类由原始轨迹自动生成的事实：中断续写说明，以及 `模型运行时读取了 apikey`。两类同时存在时用中文分号连接；其余时候一律提交空字符串 `""`，返修也相同。备注不从 `feedback.note`、重跑、历史改写、录屏或验收说明复制；当前脚本不自动返修，另行获准返修时也必须遵守此字段规则。

提交前只读 `references/stage-briefs/submit.md`，需要细节时再读 [references/platform-api.md](references/platform-api.md)。平台固定为 `https://solo2.jzxhnh.com`，默认使用本机保存的 `zouyonghong` 凭据，可用 `SOLO11_PLATFORM_ACCOUNT` 覆盖；每次登录、读取实时 schema/fingerprint、账号领域和历史 SessionID，遇到新增字段或类型变化就停止。账号以首次提交锁定领域，同账号不能跨领域提交。

平台只收一个主会话 `.jsonl`，子会话保留在本地交付目录。轨迹和视频先上传，再用返回的附件信息建单，最后回读每个字段。预检和正式回执分别绑定当前来源摘要，存任务源目录的 `review/upload.json`；内容变动即失效。超时或结果未知时先回查 SessionID，不盲目重发。`confirmed` 只表示建单字段一致，不代表平台质检通过。

## 工作目录

```
<root>/batches/b1110-01/
  tasks.xlsx  batch-manifest.json        出题侧铺的
  s11-000123/
    origin/                              新出题侧的初始克隆，main 固定在初始提交、保留依赖
    workspace/                           唯一运行克隆，main，模型在这里干活
    task-seed.json                       新出题侧的编号、类型、题位与仓库清单项
    task.json  recording-plan.json       出题侧交接生成，不改
    state.json                           各阶段状态
    run/      prompt.txt stream.jsonl stderr.log attempts.json attempt-N/ git-config.before
    trace/    projects/<目录>/<SessionID>.jsonl  ids.json
              manifest.json events.jsonl trace-check.json review-pack.json offsets/
    record/   recording.mp4 recording.json terminal.raw.log
    evidence/ facts.json audit.json probes.jsonl
    review/   snapshot.json diff.patch card.md task-agent-brief.md candidate-schema.json feedback.json
              model-review.json quality.json metrics.json task-agent-work.json task-closeout.json
              decision-input.json candidate.json verification.json style-report.json validation-errors.json
              build-errors.json lint-errors.json upload-plan.json
              task-agent-history.json task-agent-history/objects/ probe-requests.json gate.json
              spotcheck.json submit-preflight.json upload.json
<root>/results/b1110-01/
  delivery-manifest.json                 每个导出文件的字节数、SHA256 与源反馈版本
  result.xlsx  preview.html              最终表与人审预览
  s11-000123/
    task-summary.json                     题面/分类/初始快照/需求点，不含参考解元数据
    retention.json                        长期复现索引与保留文件摘要
    cleanup.json                          已清理任务的删除台账
    platform/                             平台回执、产物快照和预检副本
    trace/    ids.json projects/...        原样复制主会话与子会话 JSONL
    record/   recording.mp4 recording.json
    evidence/ facts.json audit.json probes.jsonl
    review/   feedback.json model-review.json quality.json style-report.json gate.json spotcheck.json（仅当前正文版本）
<root>/_registry/baseline-tests/<完整身份摘要>.json  基线、镜像、命令、架构与脚本版本绑定的缓存
<root>/_registry/platform-schema/<fingerprint>.json  实时 schema 完整快照
<root>/_registry/delivered-feedback.jsonl           已交付的 Feedback（查重用，每行 {task_id, feedback}）
<root>/_verification/status/<batch>-run.json         运行状态完整明细
<root>/_verification/metrics/<batch>.json             三类模型/修复/证据访问成本
```

`batches` 下的 `run/trace/record/evidence/review` 继续作为运行源记录，与 workspace 为兄弟目录，并未整棵搬到 results。交付层不复制 origin、workspace、.git、run、Git 配置备份、钩子、密钥豁免或额外文件；它不另建阶段状态。失效任务从当前投影移除，旧投影保留在 `_verification/delivery-history/` 供核查。原始轨迹不重命名、不改写。预览只显示当前可导出的任务，改动展示使用事实中的清单，代码留在任务仓库。

API key 填在上述独立 env 文件的 `apikey=` 后面。`settings.json` 只配置 `"key_file": "~/.codex/solo11/gateway.env"` 等路径，不填 key 原文；相同 key 文件也供返修后的本地校验使用。

仅轨迹分析使用 `/Users/fengyin/.codex/solo11/reviewer.env`，填写 `base_url=`、`apikey=`，可配置 `model=` 与 `reasoning_effort=`；入口支持 `--env-file` 改路径。其他操作使用推进任务的当前 AI，不读取这份配置，不改 CC Switch 或全局 Codex 配置。详见 [references/reviewer.md](references/reviewer.md)。

可调的配置都在 `_config/settings.json`，缺省值见 `scripts/fbrlib.py` 的 `DEFAULTS`（并发、超时、重跑次数、录屏规格、测试命令、闸门阈值）。`_config/` 里不放密钥。

## 自测

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s <skill>/tests
# 本机具备浏览器与 ffmpeg 时，可另验真实 Web/终端视频，不使用 Docker 或模型
PYTHONDONTWRITEBYTECODE=1 python3 <skill>/tests/browser_smoke.py
```

自测在临时目录里使用假批次、假轨迹和本地 Git；Docker、GitHub 和模型均替换，不跑真实任务或平台。持久子进程与本机 HTTP 的测试会真实执行，不访问外部服务。它验证的是脚本的逻辑；镜像里的真实行为（轨迹字段、流式输出、录屏成片）要拿第一道真题跑一遍才算确认，跑之前先看 `references/run-rules.md` 末尾的「首次真跑要核对的事」。
