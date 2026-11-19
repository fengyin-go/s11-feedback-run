# Review Stage Brief

目标：依据原始轨迹和最终产物，生成可逐句回溯、无矛盾且能过闸门的 Feedback。

- 用 `fbr_work.py next --inline --wait` 领取当前题去重 `review-capsule.json`；不再分别读取 task-agent brief 和 review contract。按需查询相关 diff hunk、最终代码和实跑日志，不整读 facts、diff、原始 JSONL 或历史。
- 评分前先完成 `code_review`：逐文件审查相关生产代码和测试改动，沿入口追到数据和实际结果，并区分基线行为；每条需求至少两个代码检查项。已有代码或功能缺陷优先写 finding 和 delivery_issue；只写模型未验收不能形成 verification_gap。
- Capsule 已内联 review contract：结果与测试句必须落到其中的业务行为和真实测试映射；环境噪声不得作为模型扣分依据，`key_business_constraint` 用于语言复核。
- 当前题完成 finalize 和 closeout 后立即领取下一题，不等整批结束；同一批次不重复读取 stage brief。
- reviewer 读取 `trace/review-pack.json`。模型行为只能从 `trace/projects/*.jsonl` 的精确行号/block 取证；事件包不是最终证据。
- 使用 `fbr_feedback.py decision` 写 compact `decision-input.json`，再用 `fbr_reviewer.py build` 展开。人工完整 candidate 仍受同一契约支持。
- build 会自动生成 requirements_status、process_audit、缺省评分字段和 process claim 来源；查看 `review/build-report.json`，不要手工重复维护这些派生字段。
- 先按 `feedback-language-policy.md` 写正文，填写 `language_review`；不写公司黑话、抽象名词清单、空泛完成度句和机器报告字段。
- 过程句只写模型实际动作。probe、录屏、当前 AI 的验收动作和基线已有行为不能冒充模型过程。
- 逐句复核必须检查完整后续和最终状态；无法由原文支持时标 insufficient、缩小范围或补 probe，不能猜。
- finalize、快照发布和字段核查成功后生成 `review/attestation.json`；提交期只验证它，不重新做本阶段判断。
- 本地交付封存且远端产物快照已回读后，保留 `origin/`、`workspace/` 和
  `run-tree/` 供 batch closeout、提交预检及等待提交使用；只有平台
  `QC_PASSED` 后才压缩可重建代码现场。原始轨迹、录屏、证据、Feedback
  和长期结果包始终保留。
先沿本次改动追到真实调用方，再从每条需求的入口追到交付结果；初看完整时复查高风险边界。按实际影响定档，已修好的过程波折如实保留。交付完整且五分所需边界验证通过时可给五分。
- 4 分必须在正文留下最终交付缺口；纯 `process_issue` 不再可交付。`score_basis.category` 只能
  使用 `verification_gap` 或 `delivery_issue`，`remaining` 要与正文同一句对应。verification_gap
  还必须列出已执行 probe 和客观 blocked_reason。
- 只加载当前需要的 `reviewer.md`、`feedback-quality.md`、`feedback-writing.md`、`feedback-language-policy.md`、`scoring.md` 和归因规范。

当前任务 AI 汇总本次所有命中，回到对应文件、调用链或原始操作补证；证据不足就撤回或收窄断言。更正 decision 后重新 build、lint、独立逐句复核和 finalize，重跑提交预检。仅在客观材料缺失或实际执行阻塞时记录阻塞。
