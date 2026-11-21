# Submit Stage Brief

目标：把已通过本地闸门的当前版本提交到 SOLO2，并回读确认，不重新建单或绕过证据。

- 先处理今晚到期的题。概览含跨日返修时核对首次建单日。每日上限拒绝会暂停新增，平台今日提交数回落后可在当日补交，普通 429 继续走原节流。参见 [每日新增额度](../daily-quota.md)。
- 提交前验证 `review/attestation.json` 和当前平台 schema；不再重跑需求评审、评分或 Feedback 写作。
- 4 分必须通过最终交付缺口一致性检查；纯 `process_issue`、正文没有最终缺口或
  `score_basis.remaining` 对不上时，预检直接阻断。
- 账号从 `_config/settings.json.platform.account` 读取，默认 `zouyonghong`；环境变量只作显式覆盖。
- 同时验证 `review/platform-rules.json`：出题侧 P5/P6 契约或 A-5 风险未清零时不得上传。
  私有会话读取不作为本地阻断条件。不统计轨迹工具调用总数，也没有 50/55 次数量门槛；
  P6 只核对工作切片和验证路径等可观察设计，具体数量以平台实际回执为准。
- 整批提交先运行 `fbr_batch.py closeout`；`batch-closeout.json` 不是 `ready` 时不上传任何文件。
- schema、凭证、正文、附件、快照、账号或领域变化都会使旧预检失效。评审输入变化只让对应题重新评价，交付变化只重做交付冻结。
- 完整 schema 和 upload plan 只从本地报告定向查询；终端摘要不是提交依据。
- 运行任务默认进入滚动提交。手工提交使用 `--execute --confirm SUBMIT`。返修使用 `--confirm REPAIR`。
- 平台返修只允许更新原记录非锁定字段。绝对禁止修改 `task.json.prompt`、`prompt.txt`
  或平台 `user_prompt` 后重跑；只能靠改题面通过的失败写
  `review/platform-deprecated.json`，本地废弃后不再处理。
- 建单和返修写请求默认相隔 10 秒；账号级节流会跨 resume 和不同批次继续生效，人工没有特殊原因不要改小。
- 只有明确 429 才按 `Retry-After` 或 15/30/45 秒退避；重投前先按 SessionID 回查。其他 5xx、超时和未知结果不得自动重发。
- 结果未知时先按 SessionID 回查，绝不能盲目重发。
- 平台回读成功后确认 ID、字段、附件、状态和提交时间，再刷新本地表。
- 只读必要字段或阻断项；不要整读 upload.json、平台详情响应或完整 schema。

需要规则细节时只加载 `references/platform-api.md` 和 `references/feedback-platform-style.md`。

滚动提交后查看 `fbr_work.py next --inline` 的 `platform_repair` 项，当前 AI 核对质检意见、原题面和允许字段，完成原单返修。`QC_WAITING` 满 600 秒结束跟进；`QC_RUNNING` 继续等结果。对 `delete_guard_unavailable` 报告原单编号和平台条件删除限制。
