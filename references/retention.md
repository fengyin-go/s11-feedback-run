# 分阶段保留与回收

清理默认服从 `_config/settings.json`：

```json
"cleanup": {
  "policy": "minimal",
  "auto": true,
  "history_keep": 1,
  "compact_record_attempts": true,
  "compact_scratch_after_facts": true,
  "compact_task_after_local_seal": true,
  "require_remote_snapshot": true,
  "purge_dependencies_on_batch_complete": true
}
```

策略：

- `safe`：只允许平台 `QC_PASSED` 后的单题清理。
- `balanced`：回收录屏候选、本题 scratch 和旧交付历史，不删除 workspace。
- `minimal`：在 `balanced` 基础上，本地交付封存后删除 `origin/`、`workspace/`、`run-tree/` 和大体积运行现场；批次终止后删除依赖缓存。

`auto: false` 关闭所有流程内自动回收。`require_remote_snapshot: true` 时，只有 GitHub 产物快照已推送并回读后才允许压缩代码现场；`--no-publish` 因此不会删除代码目录。

## 阶段触发

| 触发 | 自动回收 | 必须保留 |
| --- | --- | --- |
| 录屏 `record` 成功 | `record/attempts/`、`containers.json`、`last-attempt.json` | `recording.mp4`、`recording.json`、`terminal.raw.log` |
| 取证 `facts` 成功 | 名称含本题 ID 的 runtime scratch 克隆 | `evidence/` 和其中的命令日志 |
| 每次交付投影成功 | 超出 `history_keep` 的旧 `delivery-history` | 当前 `results/` 和最近一份历史快照 |
| 本地交付封存 | `origin/`、`workspace/`、`run-tree/`、大体积 `run/`、失败录屏候选、本题 scratch | task 骨架、`run/attempts.json`、`run/key-identity.json`、`trace/`、正式录屏、`evidence/`、`review/`、`results/` |
| 平台 `QC_PASSED` | 本地封存后剩余现场，并写最终 `retention.json` | 平台回执、长期证据和冻结结果包 |
| 批次全部终止 | `_registry/dependencies/` | repo profile、baseline 结果、平台 schema、反馈查重库、最近一份交付历史 |

本地封存同时要求：

- `run`、`collect`、`commit`、`record`、`facts`、`feedback` 全部通过；
- 当前交付清单中的任务记录和结果包摘要一致；后续 Feedback 规则更新本身不阻碍代码现场回收，因为轨迹、证据和远端快照仍完整；
- 结果包所有白名单文件通过大小和 SHA256 校验；
- `run/attempts.json` 与 `run/key-identity.json` 存在；
- 没有存活的运行进程；
- 快照已推送并回读，除非显式关闭远端快照要求。

`QC_RUNNING`、`SUBMITTED`、`PENDING_FIX`、`uncertain` 或摘要漂移不会阻止已经满足本地封存条件的代码现场压缩，但不会删除轨迹、录屏、证据和平台回执。需要继续做代码 probe 时先恢复 workspace。

## 恢复 workspace

```bash
fbr_cleanup.py hydrate --batch b1113-01 --task dfzr10007-01-development
```

恢复使用 `task.json.publish.repo_url` 和 `review/snapshot.json.commit`，检出本地 `main`，并重新生成 `run/git-config.before`。恢复是临时的；后续 probe 或复核完成后，如果 `cleanup.json` 仍为 `local_compacted`，流程会再次压缩现场。

## 手动维护

维护默认只预览：

```bash
fbr_cleanup.py maintain --batch b1113-01
fbr_cleanup.py maintain --batch b1113-01 --execute --confirm MAINTAIN
fbr_cleanup.py verify --batch b1113-01
```

`maintain` 会处理可安全本地封存的任务、裁剪历史快照，并在整批没有活动任务且所有任务都终止后回收依赖缓存。`prune` 仍只负责平台 `QC_PASSED` 的最终单题清理，需要 `--execute --confirm CLEANUP`。

清理失败不会阻断已经通过的交付；具体错误写入 `review/retention-warning.json` 或 `_verification/retention/`，已移动但未完成记账的目录保留在 `_verification/cleanup-trash/`。
