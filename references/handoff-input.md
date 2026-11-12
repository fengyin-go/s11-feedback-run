# 接收出题侧批次

运行侧消费同一个 `<root>/batches/<batch_id>/`，不要重新复制其他任务的代码来替代 workspace，也不要按表格重新拼题面。一个任务目录对应一个提示词、一个独立 Git 仓库、一个容器会话和一份验收结果。

新出题入口为 [`s11-gsb-prompt-create`](../../s11-gsb-prompt-create/SKILL.md)，其 `prepare_handoff.py` 直接写本目录下的 V2 输入。任务目录与容器名统一为 `s11-六位数字`，不按 Excel 再拼一套任务路径。

新批次固定单份 `origin/` + `workspace/`：origin 留在初始提交并保存出题预检依赖；workspace 是交接时从远端 main 克隆的干净源码，起跑时不带宿主依赖缓存。运行容器只挂载 workspace。GitHub 仓库只有 main；产物快照直接推进 main，初始快照永远使用不可变的 `/commit/<initial_commit>`。

`task.json` 与批次清单均标记 `producer=s11-gsb-prompt-create`、`execution_mode=single`、`branch=main`。摘要额外绑定这些标记和 `origin`/`workspace` 路径。校验拒绝旧 A/B 子目录、origin 偏离初始提交、类型错误以及清单不一致。起跑前再检查 workspace 的分支为 main。旧 V2 摘要格式保留，不强行要求旧批次补 origin。

任务类型固定五类：功能开发、Bugfix、代码重构、功能优化、依赖升级与迁移。出题侧以 30/30/5/30/5 做整数配额并在交接时复检；运行侧原样消费，不重新归类或改题面。

新增批次的 `task.json.handoff_version=2`。每次读取时脚本核对：

- `task.json.baseline` 的摘要与 `baseline_id` 一致，且领域、子方向、语言/框架、复现等级、初始提交、镜像摘要和 Harness 等字段与快照一致。
- `handoff_hash` 绑定题面、需求点、录屏方案、基础信息及拟发布仓库；`prompt.txt` 与题面完全一致。
- `batch-manifest.json` 的任务、路径、摘要和基线快照一致；同一仓库同一初始提交使用同一份基础信息。
- 起跑前 workspace 的 HEAD、文件树与冻结基线一致，运行配置与出题环境一致。

任一输入变化就停止，回到出题侧确认并重新交接；不能继续使用旧验收状态。发布状态可正常刷新，不属于题面输入变更。摘要用于检查意外漂移，不是防恶意篡改的数字签名。

旧批次保留原路径和原有检查，不自动搬动或改写已经存在的任务。新版布局中，`workspace/` 是唯一挂给跑题容器的代码目录；同级 `run/trace/record/evidence/review` 是全过程源材料。`results/<batch_id>/` 是经有效 Feedback 闸门认可的交付投影，只包含最终表格、预览和白名单附件，不含代码仓库、Git 配置备份、hooks 或作者参考解。

本轮以现有任务说明 PDF 加 2026-11-11 用户补充执行：交付完整性分数与 Feedback 可由 AI 基于真实证据生成。素材卡不是成稿，不能编造操作或结果；人工 spotcheck 仍需实际审核人和真实结论。真实运行及推送授权边界不变。
