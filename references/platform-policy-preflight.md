# 平台规则提交前预检

`dfzr10012 / b1115-01` 的 39 条回执显示，`PENDING_FIX` 不一定能改 Feedback：

- 05：有效工具调用 42 次，当时被平台按 P6 打回；
- 20/31：读取 `/home/node/.claude/projects/...jsonl` 中的本轮会话轨迹，平台判为 memory 作弊；
- 12 条 `DISCARDED` 是 P5 H3 不成立；
- 2 条 `DISCARDED` 是 A-5 公共长片段查重。

因此运行侧在附件上传和建单前必须执行本地平台规则预检。结果写入
`review/platform-rules.json`，失败只阻断当前题。

## 检查顺序

1. 读取批次 `repo-topic-map.json`。新 S11 批次缺失文件即阻断；旧批次完全缺失时进入 legacy
   兼容模式并记录 warning。文件存在但没有 H3 v3 契约、缺少八项假 H3 守卫或缺少
   `workload_design` 时阻断。预期有效工具调用低于 55、工作切片少于 4、验证路径少于 2
   同样阻断。
2. 独立读取 `prompt-generation-manifest.json`，不能因上一步缺文件而跳过。新 S11 批次缺失
   文件即阻断；旧批次完全缺失时记录 warning。文件存在且 `gate_ok`、`raw_a5_ok` 或
   `cross_project.platform_dedup_ok` 为假时，任何批次都阻断。
3. 读取 `trace/review-pack.json`，记录 `paired_tools` 或回退的 `logical_tool_uses` 作为
   证据。实际调用次数低于 50 不再由本地预检阻断，是否命中 P6 以平台回执为准。
4. 扫描 `tool_events[].target` 和 `result_preview`。`Read/Bash/Grep/Glob` 访问任务容器内
   `.claude/projects`、`.claude/todos`、`.claude/sessions`、`.claude/history.jsonl` 或
   `.claude/memory` 时阻断。这里的 `.claude` 是被测任务运行目录，与 Codex skill 无关。
5. 只有 `review/platform-rules.json` 和 `review/submit-preflight.json` 都为 ready，才允许上传。

机会地图及其绑定不是运行侧输入。批次没有 `prompt_opportunities`、地图文件已移走或旧批次
只有历史路径时，不阻断也不告警。H3/A-5 由上面的独立文件检查决定，与机会地图无关。

扫描只使用已经做有界裁剪的 review pack，不整读原始 JSONL。证据保留命中的步骤、工具、目标
和返回摘要，用于重新运行或出题返修。

## 失败归属

| 平台检查 | 本地动作 | 禁止动作 |
|---|---|---|
| `trace_complexity_floor` | 换 H3 或重出题 | 改 Feedback / PUT |
| `trace_tool_call_floor` | 平台实际返回后按回执扩展工作量并重跑 | 把本地计数改写成通过 |
| `dedup` / `dedup_semantic` | 重写故事形态并重新查重 | 只换名词或同义词 |
| `trace_memory` | 用干净会话重新运行 | 修改 `other_issues` |
| `trace_skill` / `trace_standard_leak` / `trace_docker_call` | 检查运行隔离后重跑 | 改 Feedback |
| `DOMAIN_MISMATCH` | 回出题侧换主任务 | 反复 PUT |

## 运行期策略

新容器仍必须清空预置 `projects`、`todos` 和 `history.jsonl`。本次失败说明模型还会读取本轮
刚生成的会话文件，因此单靠“容器启动时清空”不够。运行侧采用两层保护：

- 新批次使用 H3 v3、A-5 raw gate 和 workload design，避免带病起跑；
- 轨迹收集后扫描私有会话路径，命中后标记 `runtime_rerun_required`，不进入平台提交。

若后续镜像验证 `claude --restricted` 与当前工具集兼容，可再前移到工具权限层；在未完成真实
跑题验证前，不直接改镜像参数或伪造成功。
