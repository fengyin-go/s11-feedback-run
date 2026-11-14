# Submit Stage Brief

目标：把已通过本地闸门的当前版本提交到 SOLO2，并回读确认，不重新建单或绕过证据。

- 提交前按当前平台 schema fingerprint 重新预检；schema、正文、附件、快照、账号或领域变化都会使旧预检失效。
- 完整 schema 和 upload plan 只从本地报告定向查询；终端摘要不是提交依据。
- 正式提交必须单独授权并使用 `--execute --confirm SUBMIT`。返修使用 `--confirm REPAIR`。
- 结果未知时先按 SessionID 回查，绝不能盲目重发。
- 平台回读成功后确认 ID、字段、附件、状态和提交时间，再刷新本地表。
- 只读必要字段或阻断项；不要整读 upload.json、平台详情响应或完整 schema。

需要规则细节时只加载 `references/platform-api.md` 和 `references/feedback-platform-style.md`。
