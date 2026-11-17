"""取证之后几个模块共用的夹具：一道取证完成、有实跑记录、audit 与 feedback 都写好的题。

不是测试模块（文件名不以 test 开头），只给 test_feedback / test_sheet_batch 引用。
这里的 audit 和正文是照 FactsCase.prepared() 留下的真实证据写的：
- 改动两个文件：src/clipcut/packer.py（M，里面留了一行 TODO）、src/clipcut/segment.py（A）
- 轨迹 4 步：读 packer.py、改 packer.py、make test、再改 packer.py（之后没有再验证）
- 最后的回答里提到 docs/usage.md，产物里没有
- 两条实跑记录：P1 分片时长为 0（退出码 2），P2 一小时以上的输入（退出码 1，RuntimeError）
"""

from __future__ import annotations

import contextlib
import copy
import io
import json
import sys
from pathlib import Path
from unittest import mock

from fixtures import BATCH
from test_record_facts import GOOD, SID, FactsCase

import fbrlib as lib
import fbr_facts
import fbr_feedback
import fbr_reviewer
import fbr_quality

P_ZERO = ("python3 -m clipcut pack fixtures/short.mp4 --segment 0", 2, "error: --segment must be a positive number")
P_LONG = ("python3 -m clipcut pack fixtures/long.mp4 --segment 10", 1,
          "Traceback (most recent call last):\nRuntimeError: input longer than 3600s is not supported")

# 两份说的是同一件事、用词不同的正文，给批内查重用
TEXT_A = (
    "模型先读了 src/clipcut/packer.py，并在第 2 步改了它。"
    "make test 在第 3 步通过。"
    "随后第 4 步又改了一次 packer.py，之后没有再跑测试。"
    "实际执行 pack fixtures/short.mp4 --segment 10 能切出 6 个分片，--segment 0 会打印 error 并以退出码 2 结束。"
    "超过一小时的输入没有做：拿 fixtures/long.mp4 去跑，直接抛出 RuntimeError: input longer than 3600s is not supported，"
    "退出码 1，packer.py 里对应的位置只留了一行 TODO，长素材完全打不了包。"
    "另外最后的回答里说新增了 docs/usage.md，产物里并没有这个文件，照着回答去找用法说明会扑空。")
TEXT_B = (
    "模型先查看 src/clipcut/packer.py，再动手改。"
    "第 3 步运行 make test 时通过。"
    "可第 4 步再次修改 packer.py 以后没有重新验证。"
    "用 fixtures/short.mp4 加 --segment 10 试过，拿到 6 个分片；--segment 0 被拒绝，返回码是 2。"
    "长时长这一项落空了：fixtures/long.mp4 一跑就是 RuntimeError: input longer than 3600s is not supported，"
    "返回码 1，packer.py 里那段逻辑只有一行 TODO 占着位置，时长过长的文件根本处理不了。"
    "收尾的说明还提到 docs/usage.md 这份文档，仓库里找不到它，按说明去查用法的人拿不到任何东西。")
PARTS = {
    TEXT_A: {"gap": "超过一小时的输入没有做", "claim": "最后的回答里说新增了 docs/usage.md，产物里并没有这个文件",
             "result1": "实际执行 pack fixtures/short.mp4 --segment 10 能切出 6 个分片，--segment 0 会打印 error 并以退出码 2 结束",
             "result2": "超过一小时的输入没有做：拿 fixtures/long.mp4 去跑，直接抛出 RuntimeError: input longer than 3600s is not supported，退出码 1，packer.py 里对应的位置只留了一行 TODO，长素材完全打不了包",
             "process": [("make test 在第 3 步通过", [3]), ("随后第 4 步又改了一次 packer.py，之后没有再跑测试", [4])]},
    TEXT_B: {"gap": "长时长这一项落空了", "claim": "收尾的说明还提到 docs/usage.md 这份文档，仓库里找不到它",
             "result1": "用 fixtures/short.mp4 加 --segment 10 试过，拿到 6 个分片；--segment 0 被拒绝，返回码是 2",
             "result2": "长时长这一项落空了：fixtures/long.mp4 一跑就是 RuntimeError: input longer than 3600s is not supported，返回码 1，packer.py 里那段逻辑只有一行 TODO 占着位置，时长过长的文件根本处理不了",
             "process": [("第 3 步运行 make test 时通过", [3]), ("可第 4 步再次修改 packer.py 以后没有重新验证", [4])]},
}


def audit() -> dict:
    return copy.deepcopy({
        "requirements": [
            {"id": "R1", "verdict": "做到", "anchors": [{"type": "demo", "index": 1},
                                                      {"type": "file", "path": "src/clipcut/segment.py"}]},
            {"id": "R2", "verdict": "有问题", "level": "未实现", "note": "一小时以上的输入直接抛 RuntimeError，代码里只有 TODO",
             "anchors": [{"type": "probe", "id": "P2"}, {"type": "file", "path": "src/clipcut/packer.py"}]},
            {"id": "R3", "verdict": "做到", "anchors": [{"type": "probe", "id": "P1"}]},
        ],
        "counterexamples": [
            {"what": "分片时长为 0", "prompt_implied": False, "probe": "P1", "found_problem": False},
            {"what": "一小时以上的输入", "prompt_implied": False, "probe": "P2", "found_problem": True},
        ],
        "false_success": "轻微", "false_success_note": "回答里说新增了 docs/usage.md，产物里没有这个文件",
        "false_success_anchors": [{"type": "missing", "path": "docs/usage.md"}],
    })


def feedback(text: str = TEXT_A, score: int = 3) -> dict:
    parts = PARTS[text]
    evidence = {
        TEXT_A: [
            {"kind": "tool", "step": 3, "field": "output", "excerpt": "Ran 1 test in 0.01s"},
            {"kind": "tool", "step": 4, "field": "target", "excerpt": "src/clipcut/packer.py"},
        ],
        TEXT_B: [
            {"kind": "tool", "step": 3, "field": "output", "excerpt": "Ran 1 test in 0.01s"},
            {"kind": "tool", "step": 4, "field": "target", "excerpt": "src/clipcut/packer.py"},
        ],
    }[text]
    process = [{"sentence": sentence, "steps": steps, "evidence": [evidence[index]]}
               for index, (sentence, steps) in enumerate(parts["process"])]
    return copy.deepcopy({
        "score": score, "feedback": text, "note": "",
        "requirements_status": [
            {"requirement_id": "R1", "status": "completed", "user_text": "短素材能够按目标时长切成分片",
             "anchors": [{"type": "demo", "index": 1}, {"type": "file", "path": "src/clipcut/segment.py"}]},
            {"requirement_id": "R2", "status": "missing", "user_text": "超过一小时的素材仍然无法打包",
             "anchors": [{"type": "probe", "id": "P2"}, {"type": "file", "path": "src/clipcut/packer.py"}]},
            {"requirement_id": "R3", "status": "completed", "user_text": "零时长参数会被拒绝并返回错误",
             "anchors": [{"type": "probe", "id": "P1"}]},
        ],
        "results": [
            {"sentence": parts["result1"], "requirement_ids": ["R1", "R3"],
             "anchors": [{"type": "probe", "id": "P1"}, {"type": "demo", "index": 1}]},
            {"sentence": parts["result2"], "requirement_ids": ["R2"],
             "anchors": [{"type": "probe", "id": "P2"}, {"type": "file", "path": "src/clipcut/packer.py"}]},
        ],
        "score_basis": {"lowest_supported_score": score, "lower_score_rejected": "更低档缺少主要功能完全不可用的证据", "anchors": [{"type": "probe", "id": "P2"}], "category": "delivery_issue", "delivered": "短输入可以生成分片且拒绝零时长",
                        "remaining": "长输入未实现且文档声明不实", "why_this_score": "主要链路部分可用但长输入明确未实现"},
        "test_claims": [{"sentence": fbr_feedback.gate.squash(m.group()), "requirement_ids": ["R1"],
                         "behavior": "改动后曾执行分片功能的测试，但末次改动后未重跑",
                         "source": {"kind": "trace", "step": 3}, "output_excerpt": "Ran 1 test in 0.01s",
                         "count": None, "count_quote": ""} for m in fbr_quality.TEST_CLAUSE.finditer(text)],
        "issues": [
            {"sentence": parts["gap"], "trigger": "输入时长超过一小时", "behavior": "抛出 RuntimeError 后以退出码 1 结束",
             "impact": "长素材完全打不了包",
             "anchors": [{"type": "req", "id": "R2"}, {"type": "probe", "id": "P2"},
                         {"type": "log", "text": "input longer than 3600s is not supported"},
                         {"type": "file", "path": "src/clipcut/packer.py"}]},
            {"sentence": parts["claim"], "trigger": "模型收尾时的回答", "behavior": "声称新增了并不存在的文档",
             "impact": "照着回答去找用法说明会扑空", "anchors": [{"type": "missing", "path": "docs/usage.md"}]},
        ],
        "process": process,
        "process_audit": [{"sentence": item["sentence"], "subject": "model", "verdict": "supported",
                           "checks": {"full_trace": True, "baseline": True, "timeline": True,
                                      "source_boundary": True, "scope": True},
                           "note": "已回查对应轨迹步骤、基线行为及后续最终状态"} for item in process],
    })


def mock_claim_reviews(doc, trace_source: str | None = None, trace_excerpt: str | None = None,
                       task_dir: Path | None = None):
    """仅模拟模型协议，不代表夹具正文已经过真实语义审核。"""
    process = {
        item["sentence"]: item.get("evidence", [{}])[0].get("excerpt")
        for item in doc.get("process", []) if isinstance(item, dict)
    }
    trace_source = trace_source or f"trace/projects/-workspace/{SID}.jsonl"
    trace_excerpt = trace_excerpt or '"type": "tool_use"'
    def locator(excerpt: str) -> dict:
        row = {"path": trace_source, "excerpt": excerpt}
        if task_dir is None:
            return {**row, "start_line": 1, "end_line": 1, "block": 0}
        path = Path(task_dir) / trace_source
        for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if excerpt in raw:
                block = 0
                try:
                    value = json.loads(raw)
                    content = (value.get("message") or {}).get("content", value.get("content"))
                    if isinstance(content, list):
                        for number, item in enumerate(content):
                            if excerpt in json.dumps(item, ensure_ascii=False):
                                block = number
                                break
                except ValueError:
                    pass
                return {**row, "start_line": line_number, "end_line": line_number, "block": block}
        first = path.read_text(encoding="utf-8").splitlines()[0]
        return {"path": trace_source, "excerpt": first, "start_line": 1, "end_line": 1, "block": 0}
    rows = []
    for sentence in fbr_quality.public_units(doc["feedback"]):
        source = (locator(process.get(sentence) or trace_excerpt)
                  if sentence in process else {"path": "task.json", "excerpt": '"task_id"'})
        rows.append({"sentence": sentence, "assertions": ["离线模拟事实断言"], "verdict": "supported",
                     "reason": "离线模拟模型独立复核的具体依据", "later_check": "离线模拟后续修正与最终状态检查",
                     "sources": [source]})
    return rows


def run_main(module, *argv: str) -> tuple[int, str, str]:
    """直接调脚本的 main()，返回 (退出码, 标准输出, 标准错误)。"""
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(sys, "argv", [module.__name__, *argv]), contextlib.redirect_stdout(out), \
            contextlib.redirect_stderr(err):
        code = module.main()
    return code, out.getvalue(), err.getvalue()


class FlowCase(FactsCase):
    def evidenced(self, number: int = 1, *, tests=None, **kwargs) -> Path:
        """做到取证完成。tests 给 [初始提交结果, 产物提交结果]，默认两边都通过。"""
        task_dir = self.prepared(number, **kwargs)
        self.results = list(tests or [GOOD, GOOD])
        # 同一秒里建的两道题初始提交相同，初始提交的测试结果会走缓存，不会再跑
        initial = lib.load_task(task_dir)["initial_commit"]
        identity = fbr_facts.test_identity(initial, lib.load_task(task_dir), self.cfg)
        if (self.root / "_registry" / "baseline-tests" / (lib.content_hash(identity) + ".json")).is_file():
            self.results.pop(0)
        self.assertEqual(fbr_facts.facts_one(task_dir, self.root, self.cfg)["state"], "ok")
        self.results = []          # 没有改动的题产物提交就是初始提交，第二份结果用不上
        return task_dir

    def probe(self, task_dir: Path, cmd: str, rc: int, tail: str, **kwargs) -> dict:
        self.results = [{"setup": (0, "ok"), "probe": (rc, tail)}]
        return fbr_feedback.probe_one(task_dir, self.cfg, cmd, 180, **kwargs)

    def put(self, task_dir: Path, *, audit_doc=None, feedback_doc=None) -> None:
        if audit_doc is not None:
            lib.save_json(task_dir / "evidence" / "audit.json", audit_doc)
        if feedback_doc is not None:
            if isinstance(feedback_doc, dict):
                # 旧逻辑用例会增补一句测试失败描述，离线夹具同步补齐出处。
                existing = {c["sentence"] for c in feedback_doc.get("test_claims", [])}
                for found in fbr_quality.TEST_CLAUSE.finditer(str(feedback_doc.get("feedback", ""))):
                    clause = fbr_feedback.gate.squash(found.group())
                    if clause not in existing:
                        feedback_doc.setdefault("test_claims", []).append({"sentence": clause,
                            "requirement_ids": ["R2"], "behavior": "长输入对应测试失败的运行结果",
                            "source": {"kind": "tests_after"}, "output_excerpt": "FAIL: test_long (tests.test_packer.T)",
                            "count": None, "count_quote": ""})
            lib.save_json(task_dir / "review" / "feedback.json", feedback_doc)
        if (task_dir / "evidence/audit.json").is_file() and (task_dir / "review/feedback.json").is_file():
            self.review_receipt(task_dir)

    def refresh_source_fixture(self, task_dir: Path) -> None:
        """测试中明确构造了新媒体/会话后，同步离线事实版本；负向漂移测试不调用。"""
        path = task_dir / "evidence/facts.json"
        facts = lib.load_json(path)
        facts["source_identity"] = fbr_facts.source_identity(task_dir)
        lib.save_json(path, facts)

    def review_receipt(self, task_dir: Path) -> None:
        """离线模拟轨迹分析；生产代码只能在真实调用成功后写回执。"""
        import fbr_trace_review
        from types import SimpleNamespace
        fbr_reviewer.trace_index(task_dir)
        paths = sorted((task_dir/'trace/projects').rglob('*.jsonl'))
        engine = SimpleNamespace(file_sha=fbr_reviewer.file_sha, quality=fbr_quality)
        import fbr_evidence
        check = fbr_evidence.ingest(task_dir)
        source_meta = {row["path"]: row for row in check["sources"]}
        trace = {'state': 'accepted', 'identity': fbr_trace_review.identity(task_dir, engine),
                 'receipt': {'model': fbr_reviewer.MODEL, 'reasoning_effort': fbr_reviewer.EFFORT, 'provider': fbr_reviewer.PROVIDER},
                 'coverage': [{'path': p.relative_to(task_dir).as_posix(),
                               'last_line': source_meta[p.relative_to(task_dir).as_posix()]['lines'],
                               'sha256': source_meta[p.relative_to(task_dir).as_posix()]['sha256']} for p in paths],
                 'statements': [{'statement': '离线轨迹分析协议夹具', 'verdict': 'supported', 'later_check': '离线夹具后续状态',
                     'sources': [{'path': paths[0].relative_to(task_dir).as_posix(),
                                  'start_line': 1, 'end_line': 1, 'block': 0,
                                  'excerpt': paths[0].read_text().splitlines()[0]}]}]}
        lib.save_json(task_dir/'review/trace-analysis.json', trace)
        receipt = {
            "state": "completed", "model": "current_task_agent", "reasoning_effort": "session",
            "workflow": "task-agent-v1", "trace-analysis_sha": fbr_reviewer.file_sha(task_dir/"review/trace-analysis.json"),
            "provider": "current_task_agent",
            "inputs": fbr_reviewer.inputs(task_dir),
            "trace_check_sha": fbr_reviewer.file_sha(task_dir / fbr_evidence.CHECK),
            "audit_sha": fbr_reviewer.file_sha(task_dir / "evidence/audit.json"),
            "feedback_sha": fbr_reviewer.file_sha(task_dir / "review/feedback.json"),
            "policy_sha": fbr_quality.policy_sha(),
        }
        lib.save_json(task_dir / "review/model-review.json", receipt)
        trace_source = paths[0].relative_to(task_dir).as_posix()
        trace_excerpt = paths[0].read_text().splitlines()[0]
        lib.save_json(task_dir / "review/quality.json", {**receipt, "version": fbr_quality.VERSION,
            "state": "accepted", "claim_reviews": mock_claim_reviews(
                lib.load_json(task_dir / "review/feedback.json"), trace_source, trace_excerpt, task_dir
            ), "checks": [{"id": name, "passed": True,
                "reason": "离线夹具已核对当前证据及评价结构"} for name in fbr_quality.CHECKS]})

    def written(self, number: int = 1, text: str = TEXT_A, score: int = 3) -> Path:
        """取证完成，跑过两条实跑记录，audit 和 feedback 都写好。"""
        task_dir = self.evidenced(number)
        self.probe(task_dir, *P_ZERO)
        self.probe(task_dir, *P_LONG)
        self.put(task_dir, audit_doc=audit(), feedback_doc=feedback(text, score))
        return task_dir

    def gated(self, number: int = 1, text: str = TEXT_A) -> Path:
        """过了闸门的题。"""
        task_dir = self.written(number, text)
        rows, _ = fbr_feedback.gate_batch(self.root, BATCH, self.cfg, task_dir.name)
        self.assertEqual([(r["state"], r["errors"]) for r in rows], [("ok", [])])
        return task_dir

    def stage(self, task_dir: Path, name: str) -> dict:
        return lib.load_state(task_dir)["stages"].get(name, {})
