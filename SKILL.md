---
name: s11-feedback-run
description: solo11（26.11 领域专项，单轮 pointwise）的运行侧：把铺好的批次逐题在 Claude Code 任务镜像的新容器里单轮跑完，
  导出轨迹并取 SessionID 与 PromptID，提交并推送产物快照，录运行录屏，取证，打交付完整性分数、写 Feedback，过闸门后汇成结果表。
  仅轨迹分析使用 reviewer.env 的模型配置；其余验收、评分和 Feedback 由推进任务的当前 AI 基于证据完成，脚本负责备料、证据绑定、卡硬上限与查重；通过 SOLO2 接口上传轨迹与视频、建单并回读，同时保留本地 Excel。
  自带运行、收集、快照、录屏、取证、闸门、结果表脚本和自测，不依赖其他 skill。
  关键词：solo11、领域专项、单轮运行、轨迹、产物快照、运行录屏、交付完整性、Feedback、结果表。
---

# s11-feedback-run

这一期的运行侧，从「批次目录已铺好、每题一个 GitHub 仓库」做到「结果表和 SOLO2 平台上的一条可核对提交」。造仓库、出题、铺批次由出题侧完成。S11 单次运行批次优先接收 [`s11-gsb-prompt-create`](../s11-gsb-prompt-create/SKILL.md) 的交接；已有 `cc-domain-task-create` 批次继续兼容。

工作目录是带 `_config/settings.json` 的那个文件夹（下文叫 `<root>`）。脚本按 `--root` > 环境变量 `SOLO11_ROOT` > 当前目录向上找 的顺序定位它。下面的命令都省略了脚本目录前缀 `python3 <skill>/scripts/`。

本 skill 只在明确调用时使用；Codex 的显式调用策略在 `agents/openai.yaml` 的 `policy.allow_implicit_invocation: false` 中配置。

## 流程

默认使用 [滚动调度与恢复](references/runtime-flow.md) 和 [缓存与并发](references/performance.md)：跑完一题就进入录屏、取证和独立评价，不等待整批跑题结束。下列命令均须加 `python3 <skill>/scripts/` 前缀。先演练；获得相应授权后才真跑、发布或提交。

```bash
fbr_flow.py run --batch b1110-01
fbr_flow.py run --batch b1110-01 --execute --confirm FLOW --detach
fbr_flow.py status --batch b1110-01
fbr_flow.py drain --batch b1110-01 --execute
fbr_flow.py resume --batch b1110-01 --execute --confirm FLOW --detach
```

真跑默认在验收通过后主动推送 GitHub 产物快照并回读 SHA，再核查结果表；明确只做本地时加 `--no-publish`。平台提交另加 `--auto-submit --confirm-submit SUBMIT`，仍须单独获得用户明确授权。下列逐阶段命令仍可用于排错和补跑：

```
fbr_run.py preflight                 R0  镜像、版本、context 上限、key、本机依赖
fbr_run.py run --batch …             R1  演练；真跑加 --execute（起容器、消耗网关额度）
fbr_run.py collect --batch …         R2  导出轨迹，取 SessionID / PromptID，确认首轮及获准的中断续写
fbr_snapshot.py commit --batch …     R3  宿主机提交工作区（只动本地）
fbr_snapshot.py push --batch …           演练；真推要 --execute --confirm PUBLISH
fbr_record.py --batch …              R4  任务镜像新容器里跑演示命令，录成 mp4
fbr_facts.py --batch …               R5  取证 → evidence/facts.json
fbr_feedback.py card --batch …       R6  素材卡 → review/card.md
fbr_reviewer.py review --batch … --task … --execute
                                       仅分析轨迹，完成后返回 needs_task_agent（见 references/reviewer.md）
fbr_feedback.py gate --batch …           闸门，通过才算数
fbr_batch.py export --batch …            白名单证据 → results/<批次>/，不复制代码
fbr_batch.py preview --batch …           抽检预览页（人看）
fbr_sheet.py --batch …               R7  result.xlsx
fbr_submit.py probe                     登录并只读核对平台账号、实时字段和锁定领域
fbr_batch.py upload --batch …            平台演练；真提交要 --execute --confirm SUBMIT
fbr_sheet.py --batch …                   回执有效时回填远端媒体、实际提交人/时间
```

每一步都是闸门式的：上一步没过，下一步的脚本直接拒绝；上游重做了，下游自动作废要重来。任何时候不确定走到哪了，跑 `fbr_batch.py status --batch …`，它会列出每道题卡在哪、下一条该跑什么。不要手改 `state.json`。

数据调表、Feedback 或交付闸门未通过时，默认继续修复到能够通过，不以标记“不通过/blocked”结束可处理的问题。先按 [失败修复与复验](references/remediation.md) 定位来源，再修复字段、补齐实跑材料或由当前任务 AI 修改评价，重新跑相关闸门并立即刷新本地表。只有缺少必要授权、证据确实无法补齐、服务持续不可用或既有预算耗尽时才保留明确阻塞及下一步；其他题继续处理。通过来自事实与材料完整，不能降低标准、改被测代码、伪造证据或修改失败标记来实现。

脚本默认演练。用户授权执行完整流水线后，默认包含验收通过后的 GitHub 产物快照推送；明确只做本地时使用 `--no-publish`。单独 `push` 仍使用 `--execute --confirm PUBLISH`；平台提交仍需单独授权并使用 `--execute --confirm SUBMIT`。平台演练会登录并读取账号、字段和历史记录；不上传附件、不建单。

## 1. 运行与收集

先按 [references/handoff-input.md](references/handoff-input.md) 接收出题侧批次。新批次会核对冻结基线、题面、需求点与录屏方案的交接摘要；发生变化需重新交接。每题独立起跑，不能接着上一题的代码跑。新出题侧每题只保留一个 `origin/` 和一个 `workspace/`，远端只有 `main`；同一提示词只执行一次首轮，不拆 A/B 两侧。`origin/` 保留冻结初始提交和出题预检依赖，唯一挂载的是干净的 `workspace/`。

运行基线：镜像 `adminfather/benzhi-claude-code2:20261110`，Claude Code `2.1.319`。Intel 与 Apple 芯片使用同一镜像名、同一组命令，由 Docker 自动选架构，不加 `--platform`。网关 `https://llm2.jzxhnh.com`、模型 `auto_model/urm` 和 Git 环境已配置在镜像内，不重复配置。镜像 Id 是本机所选架构的值，不能把另一架构的 Id 写死复用。

先读 `references/run-rules.md`，里面有预检项、容器启动参数、什么情况重跑、轨迹怎么导、key 怎么放。要点：

- 一题一个新容器，容器名就是任务编号。题面原文从 stdin 送进去，只送一次。模型自行中断时，可以在同一容器、同一会话中只输入“继续”，同题累计最多 2 次；不补充需求、不提示解法。
- 新出题侧先用 `prepare_handoff.py` 生成 V2 `task.json`、`prompt.txt`、录屏方案和批次清单，并从任务仓库 `main` 铺好 `workspace`。S11 类型固定为功能开发/Bugfix/代码重构/功能优化/依赖升级与迁移，整体配比由出题侧按 30/30/5/30/5 校验。
- 工作区必须停在初始提交、除运行侧缓存回执绑定的忽略依赖外没有任何多余文件才起跑；S11 交接的当前分支必须是 `main`。任务仓库没发布（`task.json` 里 `publish.state` 不是 `published`）不起跑。
- 模型自行中断优先用上述“继续”处理，达到上限仍中断就保留全部内容并停止，不靠重跑刷结果。有效模型责任尝试最多 3 次；网关限流、5xx 与基础设施故障单独计数，不消耗模型机会，基础设施最多 3 次、网关压力最多 8 次，另受已授权物理派发次数和预算约束。没有可恢复会话的无故中断归模型责任。正常结束且最终树有代码改动才算有效；改错仍是有效数据。零代码改动必须归档、恢复基线、重建容器重跑，连首次最多 3 次；仅文档、日志、缓存或改后还原不充数。跑得慢本身不是重跑理由。
- 不算数的那次运行整份归档在 `run/attempt-N/`，工作区恢复到初始提交后再跑。
- 轨迹导出并确认完整之后才停止、删除容器，不强删。有效轨迹为首轮题面加最多 2 次有中断记录的“继续”；其他追加输入、跨会话续写或 SessionID 对不上均判失败。评价综合首轮和所有续写的过程、回答及最终产物；填表题面、SessionID 与 TurnID/PromptID 仍取第一轮。
- key 文件路径由 `_config/settings.json` 的 `key_file` 配置，默认 `/Users/fengyin/.codex/solo11/gateway.env`（其他机器为 `~/.codex/solo11/gateway.env`），权限 600、内容一行 `apikey=你的API_KEY`；只通过 `--env-file` 进容器。不要把它写进命令行、仓库、轨迹或任何输出；它只用于跑题，不用于写 Feedback。

```bash
fbr_run.py preflight
fbr_run.py run --batch b1110-01                      # 演练：列出哪些题没准备好
fbr_run.py run --batch b1110-01 --execute            # 真跑，并发按 settings 里的 concurrency
fbr_run.py collect --batch b1110-01
```

## 2. 产物快照

`finalize --execute` 和完整流水线默认在验收通过后主动推送 GitHub 产物快照；明确本地模式时使用 `--no-publish`。执行顺序固定为：本地 commit → 录屏和取证 → 独立验收与 Feedback 闸门 → 立即落本地表 → 推送远程快照并回读 SHA/public → 再生成表格并核查全部必填字段。不得在独立验收前自动推送；推送失败保留验收结果，标记交付未通过并重试发布，不重新跑题。平台提交和 GitHub 发布分别遵守本次用户授权。

- 模型自己的提交原样保留，不 squash、不 rebase、不强推。宿主机只在它后面补一个提交，收走它没提交的改动。
- 初始提交必须是产物提交的祖先。模型改写了历史时，用它最终的文件在初始提交上另建一个提交，并记录在内部 `review/snapshot.json`，不写入备注。
- 提交前扫密钥（网关 key 原文、`.env` 和各类密钥文件、token 或带口令连接串）和超过 50MB 的文件，密钥扫描覆盖二进制文件原始内容、将保留的模型中间提交及其提交消息，已删除的文件也不能把密钥带进历史。命中就停下等人看。确认是仓库本来就该有的样例文件，在 `review/secret-waivers.json` 里写 `[{"path": …, "reason": …}]` 再重跑；该豁免只适用于样例文件名和密钥形态，确切网关 key 原文永远不能豁免。
- workspace 在运行期间归被测助手所有。宿主机上所有 git 调用都不跑钩子，并且先把 `.git/config` 换回起跑前的备份。不要绕过脚本直接在 workspace 里敲 git。
- 产物提交只推进同一个 `main`；不创建 A/B 或额外基线分支。初始快照始终引用 `task.json.initial_commit` 的 `/commit/<SHA>` 地址，不能拿已推进的 main 代替。`origin/` 保留在初始提交，不随产物推进。
- 推送的 token 只从 `gh` 登录态取，走环境变量，不落盘。推前确认 public，推完回读远端 SHA 和可见性，对不上就报错。

```bash
fbr_snapshot.py commit --batch b1110-01
fbr_snapshot.py push   --batch b1110-01              # 演练
```

## 3. 录屏

- 在任务镜像的新容器里、产物提交的干净克隆上录，不带 key。演示命令来自任务目录的 `recording-plan.json`（出题时写好的）。用户授权补齐演示时，使用 `review/recording-plan.override.json`，记录原方案 content_hash、授权原文和新 plan；不改冻结交接文件。覆盖内容写入录屏元数据并参与取证。
- 容器不带 `--rm`。录制结束只终止本次创建的容器，保留容器和挂载克隆；名字与目录记录在 `record/containers.json`，核查后由用户清理。
- 准备命令在开录前跑完，不进画面；失败会自动重来，最多 3 次，还不行就保存失败现场并阻止有效录屏交付。
- 命令报错、跑不起来也照录，不重试到成功为止，不改演示命令去迎合产物。
- 成片 1280×720、H.264、不超过 90 秒；截断或遗漏必需步骤会使本次录屏失败。每次录制进入独立候选目录，只有合格版本才替换正式视频；重录失败保留旧合格版本。
- Web 项目必须采用服务启动 → 页面访问及功能操作；构建和语法检查仅是准备/测试，不得作为运行录屏。复用 [cc-gsb-create 录屏方案](../cc-gsb-create/references/recording.md)：真实 PTY 启动段目标至少 10 秒（低于 8 秒不合格），页面段至少 8 秒，保留同一服务进程。产物启动失败如实录报错；setup 失败不得冒充产物启动失败。页面失败和浏览器错误如实保存。方案格式见运行规则。
- 页面成功打开后继续操作实际功能，不能只停在首页或打开一个弹窗就结束。多页面项目优先演示 3–4 个与本题相关的实际页面，先覆盖本题核心入口，再串联相关列表、详情、配置或结果页；每页执行有意义的操作并展示结果。页面不足时按实际数量演示，单页面可展示多个功能状态。用 `steps[].pause_ms` 给关键结果留 1–2 秒阅读时间，页面较多时通常分配约 30–60 秒给页面段，整片仍不超过 90 秒；不靠快速切页、重复点击或长时间静止凑覆盖。详细选页与预算规则见 [运行规则](references/run-rules.md#录屏r4)。

```bash
fbr_record.py --batch b1110-01                       # --redo 重录
```

## 4. 取证

`fbr_facts.py` 把打分要用的事实收成 `evidence/facts.json`：演示命令的实际结果；初始提交和产物提交各跑一遍 `make setup` + `make test` 的对比（新增失败、新修好）；改动清单（含被删改的测试、新加的跳过标记、新增的占位）；轨迹步骤索引；模型自己验证过什么、最后一次改动之后有没有再验证；模型最后的回答，以及回答里提到但产物里没有或没改的文件。

脚本只收事实，不下结论。初始提交上本来就有的失败不算在这次改动头上。

## 5. 打分与 Feedback

先读 [references/reviewer.md](references/reviewer.md)、`references/scoring.md` 和 `references/feedback-writing.md`。**只有轨迹分析使用 `reviewer.env`（默认 `gpt-6.1-sol`）**。过程证据核查、需求完成判断、产物验收、交付完整性评分、Feedback 撰写、产物断言逐句复核、门闸修复均由推进任务的当前 AI 完成，不通过该配置调用其他模型，也不固定另一个产物验收模型。人工 spotcheck 仍由实际审核人完成。

1. `fbr_feedback.py card` 汇集题面、需求点、测试对比、演示结果、改动与轨迹索引。
2. `fbr_reviewer.py review` 只分析全量主/子轨迹及获准续写，保存带原文出处、完整后续状态和调用回执的 `review/trace-analysis.json`，随后返回 `needs_task_agent` 检查点。轨迹配置未填写 `model` / `reasoning_effort` 时沿用 `gpt-6.1-sol` / `high`；不可用时只暂停这一阶段，不伪造分析。
3. 当前任务 AI 读取分析报告、最终代码与真实实跑材料，判需求完成情况、评分并写 `audit` 和 `feedback`。代码已能确证的问题不重复跑服务；缺证据则用既有 probe 补齐后刷新 card 和检查点。5 分仍须至少两个不同的、题面未明说的有效边界 probe。
4. 当前任务 AI 另做产物与文字复核轮，按 `public_units` 逐句拆出断言，核对原始连续摘录、后续修正、最终影响，以及七项语义要求，保存绑定候选和检查点的 verification。不得将自报“通过”代替逐句证据，不得冒称另一个模型或独立审核人完成。细节见 [reviewer.md](references/reviewer.md) 和 [feedback-quality.md](references/feedback-quality.md)。
5. `fbr_reviewer.py finalize` 只做确定性校验，不读取 `reviewer.env` 或调用外部模型；通过后保存 `audit.json`、`feedback.json`、`model-review.json` 与 `quality.json`，执行门闸、刷新本地表并主动推送快照，再核查交付字段；明确本地模式时使用 `--no-publish`。失败返回具体错误，由当前任务 AI 继续修复、补证和复验；材料、规则或正文变化须重新核实，不给旧记录补盖新回执。

```bash
fbr_feedback.py card  --batch b1110-01
fbr_reviewer.py review --batch b1110-01 --task s11-000123 --execute
fbr_feedback.py probe --batch b1110-01 --task s11-000123 --cmd 'node bin/cli.js pack fixtures/empty.mp4'
fbr_feedback.py gate  --batch b1110-01
```

Feedback 正文提及被测 AI 时统一称为“模型”，不使用“助手”“AI 助手”等称谓；对应的过程句、测试断言和逐句复核中的正文副本保持一致。原始轨迹、日志和证据引文保留原文，不为统一称谓改写。

两条底线：

- 每个问题句的锚点必须真实存在于证据里。文件要在产物或改动清单里，步骤号要在轨迹里，报错要是日志里的原文。
- 不写没有发生过的事。没实跑过就不说跑过，没打开看过就不说看过；正文里不写验收人自己的动作。

打分先看最终交付，再解释过程与结果的联系。在确凿证据支持的档位中尽可能取最低合理分；逐项说明为何不能再低，独立复核同时拦截无证据压分和有证据却偏高。不得以目标分布替代事实。已修正且无残余影响的过程失误不自动扣分；验证不足不等于功能失败。正文必须同时有结果和过程事实，实际存在的过程问题和产物问题都要覆盖，写清触发节点、实际行为、业务影响及具体文件、报错或未实现需求；不凭空补问题。写作执行内部事实清单、正文组织和逐句编辑三轮，具体删改方法见 feedback-writing.md；正文直接写事实，不提“分析轨迹得出”等取证方法。内部 results、process、score_basis、test_claims 分别供核对，不会拼成固定正文模板。测试描述优先解释本题行为；提到通过数量必须绑定同次输出，再复核其实际范围。

同一批的正文不要用同一个骨架去填。闸门会比对批内及历史的相同片段、开头和替换技术名词后的句式骨架；独立语义复核再检查机械组织、空泛赞美与装饰细节。自然表达按可观察的文本问题把关，不宣称能判断作者来源。参考表的 9 条数据只用于学习具体、克制的表达，样本中的评分偏差不能照搬，见 [references/feedback-calibration.md](references/feedback-calibration.md)。

## 6. 抽检与结果表

环境可复现等级必须逐字属于 `fbr_submit.py probe` 读取的平台 `repro_level.options`；不得填系统架构、安装或启动经过等自由文本。来源不在选项内时阻止交付，保留冻结 task.json，核实真实环境后通过有来源的字段修订处理，不猜测或静默降级。平台暂不可达时不能声称已核实最新选项；恢复后先重新读取 schema 再提交。

结果表“闸门状态”同时检查当前独立验收、18 个必填输入字段、枚举、真实轨迹与录屏、已推送且回读确认的快照。任一缺失显示具体阻塞原因，不得仅因 Feedback 通过显示“通过”；提交人和提交时间由平台生成，未提交时保持空白。每条验收结束立即刷新本地结果及字段核查页，不等待整批完成。

```bash
fbr_batch.py export --batch b1110-01                 # 白名单证据投影，不复制代码
fbr_preview.py run --batch b1110-01 --detach         # 本地 HTTP 预览，支持视频拖动，交给人看
fbr_batch.py spotcheck --batch b1110-01 --task s11-000123 --state 退回 --by 张三 --note "第二句说的报错在日志里是另一个原因"
fbr_sheet.py --batch b1110-01
fbr_submit.py probe                                # 登录并只读读取当前平台配置
fbr_batch.py upload --batch b1110-01 --limit 1       # 平台演练，校验字段与交付文件摘要
fbr_batch.py upload --batch b1110-01 --limit 1 --execute --confirm SUBMIT
fbr_sheet.py --batch b1110-01                       # 确认回执后回填媒体/提交人/时间
```

- 抽检是人做的。`spotcheck` 只登记人看完之后的结论，agent 不要自己给自己登记通过。被退回的要改正文重新过闸门。
- 最终结果在 `results/<批次>/`，代码仍在 `batches/<批次>/<任务>/workspace/`。`export` 只复制当前有效且 Feedback 过闸门任务的白名单证据，生成文件清单、字节数和 SHA256；表格、预览与上传计划使用交付层媒体。
- 结果表和交付投影每次从各任务目录重新生成，直接改副本会被覆盖。列定义和每列从哪来见 `references/sheet-columns.md`。
- 只有 Feedback 过了闸门、之后正文和分数都没再动过的任务才进「结果」页；「数据状态」是「可提交」的才能交。「进度」页列出每道题卡在哪。
- 交付前生成「必填字段核查」页和 `field-audit.json`，另有「字段修订核对」页展示全部题目的相关字段和来源，明确验收状态；逐题检查平台 18 个必填输入字段，缺失或未验收的字段阻止交付，不得猜值、填占位值或把本地提交链接当作已发布快照。平台 19 字段中仅备注可空；提交人/时间由平台自动产生，未提交保持空白，不以评价时间或 Git 身份替代。有效回执回读后采用实际账号和平台提交时间。
- 操作系统使用平台枚举 `MacOS/Linux`；`Linux arm64` 等原始架构事实留在运行证据。子方向明确别名 `数据采集、爬虫与RPA` 映射为平台实际 `数据采集、爬虫与 RPA`，只处理已核实的等价值，未知枚举停止并核对实时 schema。

本地“备注”和平台“其他问题”（`other_issues`）都只填写中断续写说明，由首轮及续写轨迹核对后自动生成。除此之外一律提交空字符串 `""`，返修也相同；重跑、历史改写、录屏和验收说明仅留内部证据，不能进入本地备注或平台其他问题。当前脚本不自动返修，另行获准返修时也必须遵守此字段规则。

提交前必须读 [references/platform-api.md](references/platform-api.md)。平台固定为 `https://solo2.jzxhnh.com`，使用本机保存的 `gonghao` 凭据，忽略旧凭据文件中的平台地址。每次读取当前表单和指纹，按最新字段校验；遇到新增字段或类型变化就停止。账号以首次提交锁定领域，同账号不能跨领域提交。

平台只收一个主会话 `.jsonl`，子会话保留在本地交付目录。轨迹和视频先上传，再用返回的附件信息建单，最后回读每个字段。回执存任务源目录的 `review/upload.json`，绑定本次正文、字段和附件 SHA256；内容变动即失效。超时或结果未知时先回查 SessionID，不能盲目重发。再次运行默认演练可回读已有回执和最新质检状态；`confirmed` 代表已建单且字段一致，平台质检结论另看 `status`，不把建单成功写成质检通过。

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
    record/   recording.mp4 recording.json terminal.raw.log
    evidence/ facts.json audit.json probes.jsonl
    review/   snapshot.json diff.patch card.md feedback.json model-review.json quality.json probe-requests.json gate.json spotcheck.json upload.json
<root>/results/b1110-01/
  delivery-manifest.json                 每个导出文件的字节数、SHA256 与源反馈版本
  result.xlsx  preview.html              最终表与人审预览
  s11-000123/
    task-summary.json                     题面/分类/初始快照/需求点，不含参考解元数据
    trace/    ids.json projects/...        原样复制主会话与子会话 JSONL
    record/   recording.mp4 recording.json
    evidence/ facts.json audit.json probes.jsonl
    review/   feedback.json model-review.json quality.json gate.json spotcheck.json（仅当前正文版本）
<root>/_registry/baseline-tests/<完整身份摘要>.json  基线、镜像、命令、架构与脚本版本绑定的缓存
<root>/_registry/delivered-feedback.jsonl           已交付的 Feedback（查重用，每行 {task_id, feedback}）
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
