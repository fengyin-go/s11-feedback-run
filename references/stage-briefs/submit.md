# Submit Stage Brief

目标：把已通过本地闸门的当前版本提交到 SOLO2，并回读确认，不重新建单或绕过证据。

- 提交前验证 `review/attestation.json` 和当前平台 schema；不再重跑需求评审、评分或 Feedback 写作。
- 整批提交先运行 `fbr_batch.py closeout`；`batch-closeout.json` 不是 `ready` 时不上传任何文件。
- schema、凭证、正文、附件、快照、账号或领域变化都会使旧预检失效。评审输入变化只让对应题重新评价，交付变化只重做交付冻结。
- 完整 schema 和 upload plan 只从本地报告定向查询；终端摘要不是提交依据。
- 正式提交必须单独授权并使用 `--execute --confirm SUBMIT`。返修使用 `--confirm REPAIR`。
- 建单和返修写请求默认相隔 10 秒；账号级节流会跨 resume 和不同批次继续生效，人工没有特殊原因不要改小。
- 只有明确 429 才按 `Retry-After` 或 15/30/45 秒退避；重投前先按 SessionID 回查。其他 5xx、超时和未知结果不得自动重发。
- 结果未知时先按 SessionID 回查，绝不能盲目重发。
- 平台回读成功后确认 ID、字段、附件、状态和提交时间，再刷新本地表。
- 只读必要字段或阻断项；不要整读 upload.json、平台详情响应或完整 schema。

需要规则细节时只加载 `references/platform-api.md` 和 `references/feedback-platform-style.md`。
