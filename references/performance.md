# 缓存、评价并发与即时交付

评价并发默认 5，配置超过 5 时仍取 5。每题跑题完成后立即进入录屏、取证和评价，不等整批结束，也不等凑齐五题。补证使用单独队列，不占轨迹模型槽。`needs_task_agent` 是当前 AI 的滚动待办：默认用 `fbr_work.py next --wait` 持续领取最老一题，修复候选、补证、执行 finalize 和 closeout；不能等任务全部跑完再集中处理。

实时结果表使用单实例 watcher。它比较状态指纹而不是固定每轮重建：心跳、无变化轮询和纯进度重复值都不触发写入；阶段状态变化才合并刷新。`result.xlsx` 固定在 `results/<批次>/`，由目录外临时文件原子替换；交付目录本身不移动。证据文件只在任务有效集合或源摘要变化时同步，不随每次进度刷新重复制。

滚动平台提交使用单独的串行通道。运行、录屏、取证和评价线程只报告本地完成；验收通过时生成 `review/attestation.json`，冻结评审输入和交付输入。提交通道只验证凭证、字段、重复项和实时 schema，通过后按稳定顺序一次上传建单一条。人工整批提交先运行 `fbr_batch.py closeout`，只做批次级确定性核对，不再重跑需求判断、评分或 Feedback 写作。预检回执与正式上传回执分别绑定来源摘要，任何上游变化都会使旧结果失效。

批次提交前的结果写在 `results/<批次>/batch-closeout.json`，包含每题凭证摘要、来源摘要、内容摘要、SessionID、领域和查重结论。只有 `ready` 才允许整批上传；单题失败只让该题回到执行期评价或交付修复。

回收使用三个边界：阶段成功、交付投影成功和批次终止，详细规则见 [retention.md](retention.md)。录屏成功后只保留正式视频和元数据；取证成功后回收本题 scratch；每次 `results` 投影成功后把 `delivery-history` 限制为 `cleanup.history_keep` 份；本地交付封存后压缩 `origin/workspace/run-tree`。平台返回 `QC_PASSED` 后，清理 worker 再回读详情，校验结果投影、轨迹、录屏和平台附件摘要，写入最终 `retention.json` 与 `cleanup.json`。

依赖缓存不在单题清理范围内，只在整批没有活动任务且所有任务都进入 `local_compacted`、`cleaned` 或 `abandoned` 后由批次维护回收。依赖缓存可以按身份重建，回收后不影响现有证据和平台提交；下一次同基线任务会重新准备。`cleanup.policy` 分 `safe`、`balanced`、`minimal`，默认 `minimal`；`--no-publish` 只停用代码现场压缩，阶段 scratch 和历史裁剪仍可执行。任何删除先进入 `_verification/cleanup-trash/`，验证或记账失败时保留现场。

仅轨迹分析调用 reviewer.env；过程证据核查、产物验收、评分、Feedback 和修复均由推进任务的当前 AI 完成，不指定模型或推理强度。轨迹未变化可复用分析；当前 AI 核实的过程断言及引用未变化可复用证据核查；产物和评分变化仍须当前 AI 重新核对。

## 轨迹证据访问

原始 `trace/projects/*.jsonl` 只由 `fbr_evidence.py ingest` 做一次全量流式摄取，生成 manifest、行偏移、全量事件图、review pack 和 trace-check。reviewer 读取事件包，不读取完整原始轨迹。当前 AI 复核 claim 时通过批量 `fetch/verify` 读取精确行号和 block；同一来源在一次请求中只打开一次，相同区间自动去重。读取统计写到批次外部的 `_verification/evidence-access/`，不在交付任务目录里追加日志。

事件图保留原始重复块。同一 `tool_use_id` 因上下文压缩被重复输出时，review pack 按逻辑 tool ID 选用实际完成结果对应的调用；`trace-check` 同时记录 raw tool blocks、logical tools、duplicates 和真正 unpaired 数，避免把合法重复误判成未配对。

事件包不是最终证据。每条过程结论仍必须回到原始区间核对连续摘录和后续最终状态；索引、facts、card 或生成侧摘要不能替代原文。轨迹在摄取后变化时，清单与当前文件大小/mtime 不一致，必须重新摄取，旧偏移和旧分析不能复用。

`review/task-agent-brief.md` 是当前 AI 的默认决策包，完整 `card.md`、`facts.json`、`diff.patch` 和事件图只用于定向查询。`review/metrics.json` 记录 reviewer 返回的 token usage、prompt/response 字节、raw trace 大小、pack 大小和证据访问次数。Token 下降不是通过标准；任何无法由原始区间支持的模型过程描述都必须被闸门拒绝。

批量处理用 `fbr_work.py next --batch …` 逐题领取精简包，只返回当前题的 stage brief、checkpoint、trace pack 和候选 schema 路径。完成一题后运行 `closeout` 写结构化任务摘要；不要把多题 card、facts、diff 或 history 一起放进同一上下文。

`fbr_metrics.py aggregate --batch …` 分开统计产物模型、reviewer、Feedback attempt 和证据访问。`fbr_flow.py wait` 在服务端等待 `needs_task_agent/blocked/completed` 等事件，避免当前 AI 反复轮询状态。

命令默认摘要，完整对象落盘：运行状态在 `_verification/status/`，批次指标在 `_verification/metrics/`，平台 schema 在 `_registry/platform-schema/`，upload plan 在 `review/upload-plan.json`，校验错误在 `review/*-errors.json`。只有 `--json` 或定向查询才展开，不能把摘要当成数据源。

运行 stream、stderr、测试日志和 probe 日志只通过 `fbr_logs.py` 查询。默认按错误文本取上下文并限制返回字节；只有明确排查缺口时才提高窗口，不整文件读取。

## 同基线依赖

`dependencies.enabled` 默认 true。从冻结基础代码准备 Linux 依赖缓存，身份优先绑定依赖清单元数据/内容摘要、锁文件、镜像 ID、OS、架构、准备及 smoke 命令，因此不同任务 commit 只要依赖内容相同即可共享；发现本地 `file:`、`link:`、workspace 或 editable 依赖时改用完整 commit 隔离。并发构建使用文件锁。命中后复制到每题无 Git 运行树的同位置，不用共享可写目录或硬链接。宿主 MacOS 的 native 依赖不能直接复制到 Linux；首次在实际任务镜像中安装并验证，后续同环境复用。

真跑在模型派发前调用批次依赖预热。预热逐题写入 `dependency-prewarm.json`，完成后再填满模型线程池；这样首次启动不会出现一个任务先建缓存、其余任务长时间停留在 starting 的情况。

自动识别 package.json 同级 node_modules 和 Python 清单同级 .venv；其他目录通过 `dependencies.paths` 显式指定（当前支持 node_modules/.venv/venv/vendor）。目录必须在冻结基线中被 Git 忽略且未跟踪。安装没有产生可复制目录时记录 unavailable，沿用原 setup，不搬全局 site-packages。自动识别兼容 monorepo hoist；显式配置的路径则必须全部存在。

缓存首次构建使用 facts.setup_cmd，且执行 dependencies.smoke_cmd（缺省沿用 facts.test_cmd）；必须有实际成功日志。应将 smoke_cmd 配为该仓库真实的无安装启动检查。首次准备失败保留日志并阻止采用缓存，不能把失败当作已验证依赖。安装后还须复制到另一份干净克隆，在新容器中执行恢复准备和 smoke，确认不依赖原容器的 /tmp 或全局安装；验证失败沿用原 setup。

录屏和取证的干净克隆也可复用缓存；命中后默认不再执行原安装命令。项目若还需要建库、生成配置等非安装准备，配置 dependencies.setup_after_restore_cmd 为这些具体操作，不能照搬包含 npm ci/pip install 的原 setup。产物改变清单、锁文件或完整提交时缓存身份变化，先重新验证，避免用旧依赖掩盖迁移问题。

快照扫描检查最终树和旧批次保留的模型中间提交，即使历史里曾强制跟踪 node_modules/.venv，也拒绝发布含依赖的历史。初始基线也必须无依赖文件。新批次依赖目录只在无 Git 运行树和内部缓存存在，运行树同步时按依赖路径排除。

## 公共字段与交付

出题交接按基础仓库、基线提交和镜像建立 `_registry/repo-profiles/` 档案，所属领域、子方向、语言/框架、Harness、Harness 版本、操作系统、环境可复现等级只核实一次。后续同身份缺省字段从档案补齐；明确冲突必须核实来源，不按题面重新猜。题面、需求点、会话、分数、Feedback 和快照仍逐题生成。

验收通过后先落表，主动推送 main，回读 SHA/public，再落表核查必填项；显式 --no-publish 只豁免发布步骤，其余必填项仍检查。推送失败保留验收和修复日志，恢复时只补交付。字段有可验证的同基线修订时自动复用；无法确定的值交当前 AI 核实来源，不能猜值通过。平台提交保持单独授权。
