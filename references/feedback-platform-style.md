# 平台 AI 化文风与返修

2026-11-12 的 15 条平台提交中，13 条通过，2 条只因 E5「Feedback AI 化」打回；业务语义、轨迹锚点、快照、难度和时效均通过。打回原文指出：编号和时间点过密、功能被压成抽象名词、像把测试报告字段串成句，并且没有说明自己最看重什么。

## 写作防线

- 结果段用人话回答用户看到了什么，不要把功能逐项列成名词表。
- 任务编号、计划编号、时间点、水位、条数和工具报错只在解释问题必需时保留；一条正文通常不超过一两个。
- 过程段最多 3 句，写清卡在哪、造成什么后果、后来怎么修正。
- 明确写一句本轮最看重的业务约束，例如“关键是历史信息没有被后来的改期覆盖”。
- 不直接搬 `python -I`、`_enable`、`TS7053` 等工具字段；改写成“导入不了应用”“类型检查失败”“提交动作预期写错”。

平台风格闸门在 `fbr_submit.py` 的提交/返修准备阶段执行，不进入 Feedback 质量回执指纹，避免规则升级无谓作废已经通过的历史记录。

## 2026-11-13 新规则打回

- 本批 30 条首次提交后有 9 条进入 `PENDING_FIX`：测试总数单列、计数密度和抽象摘要、问题定位不具体、2 分未明确缺口、领域规则 L2 各占一类。按下面规则返修并回读后，9 条全部转为 `QC_PASSED`。
- V2 轨迹核对会逐项比较 Feedback 里的具体日期、水位、状态和工具结果。没有和原始轨迹逐字一致时，不要写具体值；例如轨迹是另一个合法日期或没有明确对应关系时，写成“改成合法日期后重跑”。
- Feedback 复检不接受只报测试总数。写 `16 项测试通过` 仍无法说明覆盖范围；应逐项写清回归覆盖了唯一探测、队首、跨代理调度、中断原位、冷却等哪些需求行为。如果无法建立覆盖关系，就省略总数，只写实际验证过的行为。
- `32/32 通过`、`后端 7 项和前端 5 项通过`、`64 项服务断言通过` 同样属于只报数量。优先删除总数，直接写测试覆盖了哪些业务行为。
- 一条 Feedback 里的“两条、三次、四项、两轮”等精确计数超过约 3 至 4 个时，会读成机器摘要。只保留解释问题必需的数量。
- 过程问题不能只写“并发场景”“验证中”“过程中”出错；必须给出具体文件、模块、命令或可定位动作。
- 2 分及以下的 Feedback 必须直接写出具体问题、缺口或用户无法完成的动作，不能只描述错误现象而不收口。
- 平台领域规则 L2 打回时，先用实时 schema 核对是否存在有证据的 `sub_direction` 修订；不得为了过检随意改题面或域。

## 返修流程

```bash
fbr_reviewer.py review --batch b1110-01 --task s11-000123 --platform-repair --execute
fbr_reviewer.py prepare --batch b1110-01 --task s11-000123 --platform-repair --execute
# 修改 candidate 和 verification
fbr_reviewer.py finalize --batch b1110-01 --task s11-000123 \
  --candidate ... --verification ... --platform-repair --execute

fbr_batch.py repair --batch b1110-01 --tasks s11-000123
fbr_batch.py repair --batch b1110-01 --tasks s11-000123 \
  --comment "按平台打回原因重写 Feedback" --execute --confirm REPAIR
```

`repair` 只接受 `PENDING_FIX + editable=true`，保留 `locked_fields`、原轨迹和原录屏，只更新允许编辑的字段。PUT 成功后必须重新 GET：版本递增、字段一致，且 `qc_running=true` 或 `stage=QC_RUNNING/QC_WAITING`。如果请求已生效但回执未落盘，再次运行应识别 `already_applied`，不得重复 PUT。

返修后继续轮询详情：只有 `QC_PASSED` 才算通过；新的 `PENDING_FIX` 要按最新 `qc_summary` 和失败 `qc_checks` 再修。
