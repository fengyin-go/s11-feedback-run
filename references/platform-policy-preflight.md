# 平台规则提交前预检

`dfzr10012 / b1115-01` 的 39 条回执显示，`PENDING_FIX` 不一定能改 Feedback：

- 有题目曾被平台按 P6 打回，但本地不再统计工具调用次数；
- 12 条 `DISCARDED` 是 P5 H3 不成立；
- 2 条 `DISCARDED` 是 A-5 公共长片段查重。

因此运行侧在附件上传和建单前必须执行本地平台规则预检。结果写入
`review/platform-rules.json`，滚动提交按题阻断。手工批交先预检本次选择的所有题，全部通过才开始上传。

## 检查顺序

1. 读取批次 `repo-topic-map.json`。新 S11 批次缺失文件即阻断；旧批次完全缺失时进入 legacy
   兼容模式并记录 warning。文件存在但没有 H3 v3 契约、缺少八项假 H3 守卫或缺少
   `workload_design` 时阻断。工作切片少于 4、覆盖模块少于 2 或验证路径少于 2 时阻断；不读取、不校验
   `expected_effective_tool_calls`，也不设 50/55 次工具调用门槛。
2. 独立读取 `prompt-generation-manifest.json`，不能因上一步缺文件而跳过。新 S11 批次缺失
   文件即阻断；旧批次完全缺失时记录 warning。文件存在且 `gate_ok`、`raw_a5_ok` 或
   `cross_project.platform_dedup_ok` 为假时，任何批次都阻断。
3. 不扫描轨迹中的 `.claude/projects` 或其他私有运行目录，也不把相关读取记录作为重跑、
   废弃或阻断条件。平台规则预检只使用上一步的出题契约。
4. 只有 `review/platform-rules.json` 和 `review/submit-preflight.json` 都为 ready，才允许上传。

机会地图及其绑定不是运行侧输入。批次没有 `prompt_opportunities`、地图文件已移走或旧批次
只有历史路径时，不阻断也不告警。H3/A-5 由上面的独立文件检查决定，与机会地图无关。

## 失败归属

| 平台检查 | 本地动作 | 禁止动作 |
|---|---|---|
| `trace_complexity_floor` | 已有平台单时本地废弃，不处理 | 改题重跑 / 改 Feedback / PUT |
| `trace_tool_call_floor` | 本地不统计；若平台明确返回该检查，已有平台单时本地废弃 | 改题重跑 / 把本地计数改写成通过 |
| `dedup` / `dedup_semantic` | 已有平台单时本地废弃，不处理 | 改题重跑 / 只换名词或同义词 |
| `trace_skill` / `trace_standard_leak` / `trace_docker_call` | 已有平台单时本地废弃，不重启运行 | 改 Feedback / 重跑 |
| `DOMAIN_MISMATCH` | 已有平台单时本地废弃，不处理 | 换主任务后重跑 / 反复 PUT |

## 运行期策略

新容器仍必须清空预置 `projects`、`todos` 和 `history.jsonl`，作为跨题运行隔离措施。该清理
不再配合轨迹私有会话路径扫描；轨迹出现 `.claude/projects` 等访问记录时，本地平台规则
预检不阻断、不要求重跑，也不写入平台废弃记录。

平台单进入 `PENDING_FIX` 后，返修只允许原记录非锁定字段更新，绝不允许修改提示词后重跑。
失败只能靠改题面处理时，`fbr_submit.py repair` 写 `review/platform-deprecated.json`，
后续 `repair/upload/export/status/work` 一律跳过。若后续镜像验证
`claude --restricted` 与当前工具集兼容，可再前移到工具权限层；在未完成真实跑题验证前，
不直接改镜像参数或伪造成功。
