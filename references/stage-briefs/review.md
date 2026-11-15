# Review Stage Brief

目标：依据原始轨迹和最终产物，生成可逐句回溯、无矛盾且能过闸门的 Feedback。

- 先读 `review/task-agent-brief.md`。按需查询相关 diff hunk、最终代码和实跑日志，不整读 facts、diff、原始 JSONL 或历史。
- reviewer 读取 `trace/review-pack.json`。模型行为只能从 `trace/projects/*.jsonl` 的精确行号/block 取证；事件包不是最终证据。
- 使用 `fbr_feedback.py decision` 写 compact `decision-input.json`，再用 `fbr_reviewer.py build` 展开。人工完整 candidate 仍受同一契约支持。
- 先按 `feedback-language-policy.md` 写正文，填写 `language_review`；不写公司黑话、抽象名词清单、空泛完成度句和机器报告字段。
- 过程句只写模型实际动作。probe、录屏、当前 AI 的验收动作和基线已有行为不能冒充模型过程。
- 逐句复核必须检查完整后续和最终状态；无法由原文支持时标 insufficient、缩小范围或补 probe，不能猜。
- 评分先看最终交付，过程问题按 scoring.md；5 分必须与正文呈现的过程一致。
- 只加载当前需要的 `reviewer.md`、`feedback-quality.md`、`feedback-writing.md`、`feedback-language-policy.md`、`scoring.md` 和归因规范。
