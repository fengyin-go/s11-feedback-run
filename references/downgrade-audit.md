# 4 分到 3 分的证据审计

这个流程只处理已有 4 分记录是否被低估为 4。它不按比例压分，也不把普通返工或验证不足直接改成 3。只有结果或过程证据证明存在明显问题、兼容回归、幂等失败或轻微虚假成功时，才允许重新评审为 3。

## 允许的降分类型

| downgrade_class | 必须证明 |
| --- | --- |
| required_path_failure | 题面明确要求的正常入口或用户动作实际不可用 |
| compatibility_regression | 同一 probe 在初始基线成功、在产物失败 |
| idempotency_failure | 真实并发、重放或恢复后出现重复写入、状态丢失或错误回执 |
| mild_false_success | 最终回答声称完成或验证过，产物/轨迹证明并非如此 |
| combined_detail_block | 两个以上有锚点的细节共同阻塞同一条必需业务流程 |

以下情况不能降 3：

- 只是有效 probe 少于两个；
- 过程中出现过错误断言、路径错误、类型错误或实现返工，但最终已经修正；
- 初始基线本来就存在的失败，题面也没有要求修复；
- 可选入口、范围外增强或没有用户影响的工程告警；
- 只有“可能有问题”的推断，没有可复现失败。

## 流程

```bash
fbr_score_audit.py scan --batch b1110-01
fbr_score_audit.py probe --batch b1110-01 --task s11-000123 \
  --cmd '<与题面需求直接相关的复现命令>' --side both
fbr_score_audit.py record --batch b1110-01 --task s11-000123 \
  --class compatibility_regression --requirements R2 \
  --baseline-probe D1 --artifact-probe P7 \
  --expected '同一动作返回 200 且只写一条回执' \
  --actual '产物返回 500，没有生成回执' \
  --user-impact '用户无法完成必需的确认动作' \
  --workaround '无' \
  --anchor '{"type":"probe","id":"P7"}'
fbr_score_audit.py validate --batch b1110-01 --task s11-000123
```

`probe --side both` 分别从初始提交和产物提交建立干净克隆，执行同一命令。基线 probe 写入 `evidence/downgrade-probes.jsonl`（D 编号），产物 probe 写入标准 `evidence/probes.jsonl`（P 编号），便于最终 issue 使用正常 probe 锚点。两边 setup 成功、产物侧实际失败且不是 124/126/127/137 等基础设施退出码时，才可能作为降分证据。

`record` 写入 `review/downgrade-review.json`。`validate` 会核对需求编号、基线/产物方向、预期与实际、用户影响、替代路径和 anchors。通过后只给出改分建议，不自动修改 audit 或 Feedback。

当前任务 AI 必须继续：

1. 按证据把 audit 需求级别改为“明显”或“未实现”，或把虚假成功改为“轻微”；
2. 把 score 改为 3，在 `score_basis` 写 `downgrade_from: 4`，并更新 issues、结果句和过程句；
3. 运行 `fbr_reviewer.py lint`；
4. 重新跑单题 finalize、gate、抽检和提交预检。

finalize 会重新核对 `downgrade-review.json`、标准产物 probe 和基线 probe 的摘要。证据文件在验证后变化时，原降分回执失效，必须重新 validate。

平台单已存在的记录不自动适用本流程；返修继续遵守平台字段可编辑范围与单独授权。
