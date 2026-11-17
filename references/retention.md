# 分阶段保留与回收

清理默认服从 `_config/settings.json`：

```json
"cleanup": {
  "policy": "minimal",
  "auto": true,
  "history_keep": 1,
  "compact_record_attempts": true,
  "compact_scratch_after_facts": true,
  "compact_task_after_qc_passed": true,
  "require_remote_snapshot": true,
  "purge_dependencies_on_batch_complete": true
}
```

策略：

- `safe`：只允许平台 `QC_PASSED` 后的单题清理。
- `balanced`：回收录屏候选、本题 scratch 和旧交付历史，不删除 workspace。
- `minimal`：在 `balanced` 基础上，平台提交并确认 `QC_PASSED` 后删除
  `origin/`、`workspace/`、`run-tree/` 和大体积运行现场；批次终止后删除依赖缓存。
  本地封存、批次 closeout、提交预检和等待提交期间都保留代码现场。

`auto: false` 关闭所有流程内自动回收。`require_remote_snapshot: true` 时，只有
GitHub 产物快照已推送并回读、且平台状态为 `QC_PASSED` 后才允许压缩代码现场；
`--no-publish` 因此不会删除代码目录。

## 阶段触发

| 触发 | 自动回收 | 必须保留 |
| --- | --- | --- |
| 录屏 `record` 成功 | `record/attempts/`、`containers.json`、`last-attempt.json` | `recording.mp4`、`recording.json`、`terminal.raw.log`、`pages/` |
| 取证 `facts` 成功 | 名称含本题 ID 的 runtime scratch 克隆 | `evidence/` 和其中的命令日志 |
| 每次交付投影成功 | 超出 `history_keep` 的旧 `delivery-history` | 当前 `results/` 和最近一份历史快照 |
| 本地交付封存 | 只回收失败录屏候选和本题 scratch，不删除代码现场 | `origin/`、`workspace/`、`run-tree/`、task 骨架、`run/attempts.json`、`run/key-identity.json`、`trace/`、正式录屏、`evidence/`、`review/`、`results/` |
| 平台 `QC_PASSED` | `origin/`、`workspace/`、`run-tree/`、大体积 `run/` 和剩余 scratch，并写最终 `retention.json` | 平台回执、长期证据和冻结结果包 |
| 批次全部终止 | `_registry/dependencies/` | repo profile、baseline 结果、平台 schema、反馈查重库、最近一份交付历史 |

本地封存同时要求：

- `run`、`collect`、`commit`、`record`、`facts`、`feedback` 全部通过；
- 当前交付清单中的任务记录和结果包摘要一致；本地封存只冻结交付，不回收代码现场；
- 结果包所有白名单文件通过大小和 SHA256 校验；
- `run/attempts.json` 与 `run/key-identity.json` 存在；
- 没有存活的运行进程；
- 快照已推送并回读，除非显式关闭远端快照要求。

`QC_RUNNING`、`SUBMITTED`、`PENDING_FIX`、`uncertain` 或摘要漂移都继续保留
代码现场。批检、提交预检和等待提交仍需要 `origin/`、`workspace/` 与
`run-tree/`；只有平台实时状态为 `QC_PASSED` 才允许最终删除。

## 恢复 workspace

```bash
fbr_cleanup.py hydrate --batch b1113-01 --task dfzr10007-01-development
```

恢复使用 `task.json.publish.repo_url`、`task.json.initial_commit` 和
`review/snapshot.json.commit`，同时恢复：

- `origin/`：本地 `main` 固定在初始提交；
- `workspace/`：本地 `main` 固定在产物快照提交。

恢复还会重新生成 `run/git-config.before`。该命令主要用于旧批次已经
`local_compacted` 的迁移，或平台提交后仍需做定向代码 probe；新流程不会在
提交前自动压缩代码现场。

## 手动维护

维护默认只预览：

```bash
fbr_cleanup.py maintain --batch b1113-01
fbr_cleanup.py maintain --batch b1113-01 --execute --confirm MAINTAIN
fbr_cleanup.py verify --batch b1113-01
```

`maintain` 只处理录屏候选、本题 scratch 和历史快照；即使使用
`--execute`，未达到平台 `QC_PASSED` 的任务也不会删除代码现场。`prune` 仍只负责
平台 `QC_PASSED` 的最终单题清理，需要 `--execute --confirm CLEANUP`。

清理失败不会阻断已经通过的交付；具体错误写入 `review/retention-warning.json` 或 `_verification/retention/`，已移动但未完成记账的目录保留在 `_verification/cleanup-trash/`。
