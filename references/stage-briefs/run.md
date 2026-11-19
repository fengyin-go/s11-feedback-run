# Run Stage Brief

目标：在独立容器中把冻结题面跑一次并得到可核验轨迹，不改变题面或产物基线。

- 开跑前读取账号额度。额度等待复用调度队列，收到模型终态废弃时回收预留。参见 [每日新增额度](../daily-quota.md)。
- 先跑 `fbr_run.py preflight`，再由 `fbr_flow.py pre-submit --detach` 完成依赖预热和滚动运行。
- 一题一个容器；题面只从 stdin 送一次。自行中断时只允许同一会话发送“继续”，累计最多两次。
- 网关、鉴权和基础设施错误不能伪装成模型失败；持续有输出、思考增量或工具进行中时不按墙钟误杀。
- 正常完成但没有代码改动不算有效数据；归档现场、恢复基线后按既有上限重跑。
- 轨迹导出并核对 SessionID、PromptID、首轮和续写后才停止容器。
- 新 S11 Web 录屏必须声明 `pages`，页面段默认至少 12 秒，并覆盖每个页面的真实动作和可见结果。
- 录屏和取证成功后按 `cleanup.policy` 回收候选与本题 scratch；失败现场保留，详细边界见 `references/retention.md`。
- 排错先读 `_verification/status/<batch>-run.json`；日志用 `fbr_logs.py --task … --source stream --grep '错误原文'` 取有界窗口，不整读 `stream.jsonl`、stderr 或容器日志。
- 当前阶段不评分、不写 Feedback、不改阈值。运行异常按 `references/remediation.md` 处理。

需要规则细节时只加载 `references/run-rules.md` 和 `references/runtime-flow.md`。
