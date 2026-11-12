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
import sys
from pathlib import Path
from unittest import mock

from fixtures import BATCH
from test_record_facts import GOOD, FactsCase

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
    "先读了 src/clipcut/packer.py，第 2 步改了它，第 3 步跑 make test 是通过的，"
    "但第 4 步又改了一次 packer.py，之后没有再跑测试。"
    "实际执行 pack fixtures/short.mp4 --segment 10 能切出 6 个分片，--segment 0 会打印 error 并以退出码 2 结束。"
    "超过一小时的输入没有做：拿 fixtures/long.mp4 去跑，直接抛出 RuntimeError: input longer than 3600s is not supported，"
    "退出码 1，packer.py 里对应的位置只留了一行 TODO，长素材完全打不了包。"
    "另外最后的回答里说新增了 docs/usage.md，产物里并没有这个文件，照着回答去找用法说明会扑空。")
TEXT_B = (
    "过程上是读完 src/clipcut/packer.py 就动手改，第 3 步的 make test 通过，"
    "可第 4 步再次修改 packer.py 以后没有重新验证。"
    "用 fixtures/short.mp4 加 --segment 10 试过，拿到 6 个分片；--segment 0 被拒绝，返回码是 2。"
    "长时长这一项落空了：fixtures/long.mp4 一跑就是 RuntimeError: input longer than 3600s is not supported，"
    "返回码 1，packer.py 里那段逻辑只有一行 TODO 占着位置，时长过长的文件根本处理不了。"
    "收尾的说明还提到 docs/usage.md 这份文档，仓库里找不到它，按说明去查用法的人拿不到任何东西。")
PARTS = {
    TEXT_A: {"gap": "超过一小时的输入没有做", "claim": "最后的回答里说新增了 docs/usage.md，产物里并没有这个文件",
             "process": [("第 3 步跑 make test 是通过的", [3]), ("第 4 步又改了一次 packer.py，之后没有再跑测试", [4])]},
    TEXT_B: {"gap": "长时长这一项落空了", "claim": "收尾的说明还提到 docs/usage.md 这份文档，仓库里找不到它",
             "process": [("第 3 步的 make test 通过", [3]), ("第 4 步再次修改 packer.py 以后没有重新验证", [4])]},
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
        "counterexamples": [{"what": "分片时长为 0", "probe": "P1", "found_problem": False},
                            {"what": "一小时以上的输入", "probe": "P2", "found_problem": True}],
        "false_success": "轻微", "false_success_note": "回答里说新增了 docs/usage.md，产物里没有这个文件",
        "false_success_anchors": [{"type": "missing", "path": "docs/usage.md"}],
    })


def feedback(text: str = TEXT_A, score: int = 3) -> dict:
    parts = PARTS[text]
    return copy.deepcopy({
        "score": score, "feedback": text, "note": "",
        "results": [{"sentence": "--segment 0 会打印 error 并以退出码 2 结束" if text == TEXT_A else "--segment 0 被拒绝，返回码是 2",
                     "requirement_ids": ["R3"], "anchors": [{"type": "probe", "id": "P1"}]}],
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
        "process": [{"sentence": sentence, "steps": steps} for sentence, steps in parts["process"]],
    })


def mock_claim_reviews(doc):
    """仅模拟模型协议，不代表夹具正文已经过真实语义审核。"""
    return [{"sentence": sentence, "assertions": ["离线模拟事实断言"], "verdict": "supported",
             "reason": "离线模拟模型独立复核的具体依据", "later_check": "离线模拟后续修正与最终状态检查",
             "sources": [{"path": "task.json", "excerpt": '"task_id"'}]}
            for sentence in fbr_quality.public_units(doc["feedback"])]


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
        """离线模拟指定模型完成生成；生产代码只能在实际模型调用成功后写回执。"""
        receipt = {
            "state": "completed", "model": fbr_reviewer.MODEL, "reasoning_effort": fbr_reviewer.EFFORT,
            "provider": fbr_reviewer.PROVIDER, "base_url": "https://review.example.test/v1",
            "inputs": fbr_reviewer.inputs(task_dir),
            "audit_sha": fbr_reviewer.file_sha(task_dir / "evidence/audit.json"),
            "feedback_sha": fbr_reviewer.file_sha(task_dir / "review/feedback.json"),
            "policy_sha": fbr_quality.policy_sha(),
        }
        lib.save_json(task_dir / "review/model-review.json", receipt)
        lib.save_json(task_dir / "review/quality.json", {**receipt, "version": fbr_quality.VERSION,
            "state": "accepted", "claim_reviews": mock_claim_reviews(lib.load_json(task_dir / "review/feedback.json")), "checks": [{"id": name, "passed": True,
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
