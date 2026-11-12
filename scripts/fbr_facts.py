#!/usr/bin/env python3
"""R5 取证：把打分要用的事实收成 evidence/facts.json。脚本只收事实，不下结论。

    fbr_facts.py --batch b1110-01 [--tasks …] [--redo]

内容：
- demo         录屏里每条演示命令的退出码和输出末尾
- tests        初始提交与产物提交各在任务镜像的新容器里跑一遍 setup + test，对比新增失败与新增通过
- changes      改动清单：文件、增删行、被删改的测试、新加的跳过标记、新增的 TODO 与占位
- trace        轨迹索引：步骤号、工具、目标、是否报错、报错原文
- verification 模型自己跑过的测试 / 构建及结果，最后一次改动之后有没有再验证
- claims       模型最终回答原文，以及回答里提到、但产物里不存在或没改动的文件
"""

from __future__ import annotations

import argparse
import re
import hashlib
import platform
import shutil
import copy
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fbrlib as lib  # noqa: E402
import fbr_trace  # noqa: E402

TEST_FILE_RE = re.compile(
    r"(^|/)(tests?|__tests__|spec|specs)/|(^|/)test_[^/]+\.py$|_test\.(py|go|cc|cpp|c)$|\.(test|spec)\.[cm]?[jt]sx?$"
    r"|Tests?\.java$")
SKIP_RE = re.compile(r"(@pytest\.mark\.(skip|xfail)|pytest\.skip\(|unittest\.skip|\b(it|test|describe)\.(skip|todo)\("
                     r"|\bx(it|describe)\(|@Disabled|@Ignore|DISABLED_)")
PLACEHOLDER_RE = re.compile(r"(\bTODO\b|\bFIXME\b|\bXXX\b|NotImplementedError|not implemented|unimplemented|待实现"
                            r"|占位|\bplaceholder\b|\bstub\b)", re.I)
CODE_EXT = {"py", "ts", "tsx", "js", "jsx", "mjs", "cjs", "java", "c", "cc", "cpp", "h", "hpp", "md", "json", "yml",
            "yaml", "toml", "xml", "sh", "sql", "txt", "cfg", "ini", "html", "css", "csv", "lock", "mk", "glsl"}
# re.A：\w 只认 ASCII，文件名紧挨着中文（「改了packer.py文件」）也能切出来
PATH_RE = re.compile(r"(?<![\w./-])((?:[\w.-]+/)*[\w.-]+\.([A-Za-z]{1,5}))(?![\w/-])", re.A)


def parse_tests(text: str) -> dict:
    """从测试输出里尽量认出失败的用例名和通过 / 失败数。认不出来就留空，结论以退出码为准。"""
    names = set()
    for pattern in (r"^(?:FAILED|ERROR)\s+(\S+::\S+)", r"^\s*not ok \d+\s+-?\s*(.+?)\s*$",
                    r"^\s*[✖✗×]\s+(.+?)(?:\s+\(\d+(?:\.\d+)?m?s\))?\s*$", r"^\[ERROR\]\s+([\w.$]+\.[\w$]+):\d+",
                    r"^(?:FAIL|ERROR):\s+(\S+(?:\s+\([\w.]+\))?)", r"^\s*(?:FAIL|✕)\s+(\S+)\s*$"):
        names.update(m.group(1).strip() for m in re.finditer(pattern, text, flags=re.M))
    passed = failed = None
    for pattern, slot in ((r"(\d+) passed", "p"), (r"(\d+) failed", "f"), (r"^# pass (\d+)", "p"),
                          (r"^# fail (\d+)", "f"), (r"^ℹ pass (\d+)", "p"), (r"^ℹ fail (\d+)", "f"),
                          (r"Tests\s+(\d+) passed", "p")):
        found = re.findall(pattern, text, flags=re.M)
        if found:
            if slot == "p":
                passed = int(found[-1])
            else:
                failed = int(found[-1])
    maven = re.findall(r"Tests run: (\d+), Failures: (\d+), Errors: (\d+)(?:, Skipped: (\d+))?", text)
    if maven:
        total, bad, err, skipped = (int(v or 0) for v in maven[-1])
        passed, failed = max(0, total - bad - err - skipped), bad + err
    ran = list(re.finditer(r"^Ran (\d+) tests?\b", text, flags=re.M))
    if ran and passed is None:
        # 只读最后一轮 unittest 的汇总，不把跳过或预期失败算成通过。
        summary = re.search(r"^(?:OK|FAILED)(?: \(([^\n]*)\))?\s*$", text[ran[-1].end():], flags=re.M)
        counts = dict(re.findall(r"([a-z ]+)=(\d+)", summary.group(1) or "")) if summary else {}
        counts = {k.strip(): int(v) for k, v in counts.items()}
        bad = sum(counts.get(k, 0) for k in ("failures", "errors", "unexpected successes"))
        omitted = counts.get("skipped", 0) + counts.get("expected failures", 0)
        passed, failed = max(0, int(ran[-1].group(1)) - bad - omitted), bad
    return {"passed": passed, "failed": failed, "failed_names": sorted(names)}


def run_tests(image: str, clone: Path, cfg: dict) -> dict:
    conf = cfg["facts"]
    rows = lib.run_steps_in_image(image, clone, [
        {"name": "setup", "cmd": conf["setup_cmd"], "timeout": conf["setup_timeout"]},
        {"name": "test", "cmd": conf["test_cmd"], "timeout": conf["test_timeout"]}], tail=200)
    setup, test = rows
    full_text = Path(test['output_file']).read_text(errors='replace') if test.get('output_file') else test['tail']
    parsed = parse_tests(full_text) if test["rc"] is not None else {"passed": None, "failed": None, "failed_names": []}
    return {"setup_rc": setup["rc"], "setup_tail": lib.clip(setup["tail"][-1500:], 1500) if setup["rc"] else "",
            "test_rc": test["rc"], "test_seconds": test["seconds"], "test_tail": test["tail"][-4000:],
            "container_name": setup.get("container_name", ""), "clone": str(clone),
            "outputs": [{k: row[k] for k in ('name', 'cmd', 'output_file', 'output_sha256') if k in row} for row in rows], **parsed}


def test_identity(initial: str, record: dict, cfg: dict) -> dict:
    actual = lib.run(["docker", "image", "inspect", "--format", "{{.Id}}", record["image"]], timeout=60)
    if actual.returncode or actual.stdout.strip() != cfg["image_id"]:
        raise lib.FbrError("取证镜像 ID 与预检配置不符，不使用缓存")
    return {"initial_commit": initial, "image_id": actual.stdout.strip(), "arch": platform.machine(),
            "commands": cfg["facts"], "policy": lib.content_hash({p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                     for p in (Path(__file__), Path(lib.__file__))})}


def source_identity(task_dir: Path) -> str:
    paths = [task_dir / name for name in ('trace/ids.json', 'record/recording.json', 'record/recording.mp4')]
    paths += sorted((task_dir / 'trace/projects').rglob('*.jsonl'))
    values = {}
    for path in paths:
        if path.is_symlink():
            raise lib.FbrError('取证来源不能是符号链接')
        if path.is_file():
            digest = hashlib.sha256()
            with path.open('rb') as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                    digest.update(chunk)
            values[str(path.relative_to(task_dir))] = digest.hexdigest()
    snapshot = lib.load_json(task_dir / 'review/snapshot.json', default={})
    values['commit'] = snapshot.get('commit')
    return lib.content_hash(values)


def compare_tests(before: dict, after: dict) -> dict:
    new = sorted(set(after["failed_names"]) - set(before["failed_names"]))
    fixed = sorted(set(before["failed_names"]) - set(after["failed_names"]))
    before_ok = before["setup_rc"] == 0 and before["test_rc"] == 0
    after_ok = after["setup_rc"] == 0 and after["test_rc"] == 0
    def can_run(item: dict) -> bool:
        return item["setup_rc"] == 0 and item["test_rc"] not in (None, 124, 126, 127, 137)

    runnable = can_run(after)
    return {"before_ok": before_ok, "after_ok": after_ok, "after_runnable": runnable,
            "new_failures": new, "fixed": fixed,
            # 产物上测试不过，并且不是初始提交上原样就有的失败（初始提交上本来就跑不起来的不算在产物头上）
            "regression": (not after_ok) and (before_ok or bool(new) or (not runnable and can_run(before)))}


def collect_changes(workspace: Path, initial: str, commit: str) -> dict:
    status = {}
    for line in lib.git_out(workspace, "diff", "--no-renames", "--name-status", initial, commit).splitlines():
        kind, _, path = line.partition("\t")
        status[path] = kind[:1]
    files = []
    for line in lib.git_out(workspace, "diff", "--no-renames", "--numstat", initial, commit).splitlines():
        added, deleted, path = line.split("\t", 2)
        files.append({"path": path, "status": status.get(path, "M"),
                      "added": int(added) if added.isdigit() else 0,
                      "deleted": int(deleted) if deleted.isdigit() else 0,
                      "binary": not added.isdigit(), "is_test": bool(TEST_FILE_RE.search(path))})
    skips, placeholders, current = [], [], ""
    patch = lib.git(workspace, "diff", "--no-renames", "--unified=0", "--no-color", initial, commit).stdout
    for line in patch.splitlines():
        if line.startswith("+++ b/"):
            current = line[6:]
        elif line.startswith("+") and not line.startswith("+++"):
            body = line[1:].strip()
            if SKIP_RE.search(body) and len(skips) < 30:
                skips.append({"file": current, "line": lib.clip(body, 160)})
            if PLACEHOLDER_RE.search(body) and len(placeholders) < 30:
                placeholders.append({"file": current, "line": lib.clip(body, 160)})
    return {
        "no_change": not files, "files": files,
        "totals": {"files": len(files), "added": sum(f["added"] for f in files),
                   "deleted": sum(f["deleted"] for f in files)},
        "tests_deleted": [f["path"] for f in files if f["is_test"] and f["status"] == "D"],
        "tests_weakened": [f["path"] for f in files if f["is_test"] and f["status"] == "M" and f["deleted"] > f["added"]],
        "tests_added": [f["path"] for f in files if f["is_test"] and f["status"] == "A"],
        "skips_added": skips, "placeholders": placeholders,
    }


def check_claims(answer: str, tree: list[str], changed: set[str]) -> dict:
    """最终回答里点到的文件：产物里有没有、这次改没改。只是线索，是否算虚假成功由打分的人判断。"""
    names = {}
    for path in tree:
        names.setdefault(path.rsplit("/", 1)[-1], []).append(path)
    missing, unchanged, seen = [], [], set()
    for found in PATH_RE.finditer(answer or ""):
        token, ext = found.group(1).lstrip("./"), found.group(2).lower()
        if token in seen or ext not in CODE_EXT or not re.search(r"[A-Za-z]", token.rsplit(".", 1)[0]):
            continue
        seen.add(token)
        hits = [token] if token in tree else [p for p in names.get(token.rsplit("/", 1)[-1], [])
                                              if p.endswith("/" + token) or "/" not in token]
        if not hits:
            missing.append(token)
        elif not any(hit in changed for hit in hits):
            unchanged.append(token)
    return {"final_answer": answer or "", "mentioned": sorted(seen), "mentioned_missing": missing,
            "mentioned_unchanged": unchanged}


@lib.task_locked
def facts_one(task_dir: Path, root: Path, cfg: dict, redo: bool = False) -> dict:
    record = lib.load_task(task_dir)
    name, workspace, initial = record["task_id"], task_dir / "workspace", record["initial_commit"]
    identity = test_identity(initial, record, cfg)
    if lib.stage_ok(task_dir, "facts") and not redo:
        previous = lib.load_json(task_dir / "evidence/facts.json", default={})
        if previous.get("test_identity") == identity and previous.get('source_identity') == source_identity(task_dir):
            return {"task_id": name, "state": "skip", "detail": "已经取证过"}
    lib.require_stage(task_dir, "record")
    lib.restore_git_config(task_dir)
    snapshot = lib.load_json(task_dir / "review" / "snapshot.json")
    commit = snapshot["commit"]
    recording = lib.load_json(task_dir / "record" / "recording.json")
    ids = lib.load_json(task_dir / "trace" / "ids.json")
    lib.set_stage(task_dir, "facts", "dry", reason="正在重新取证")

    def tested(sha: str) -> dict:
        clone = lib.clean_clone(workspace, sha, f"s11-facts-{name}")
        return run_tests(record["image"], clone, cfg)

    cache = root / "_registry" / "baseline-tests" / f"{lib.content_hash(identity)}.json"
    import fbr_dependencies
    with fbr_dependencies.wait_lock(cache.with_suffix('.lock'), __import__('time').time()+cfg['facts']['setup_timeout']+cfg['facts']['test_timeout']+180):
        cached = lib.load_json(cache, default={})
        before = cached.get("result", {}) if cached.get("identity") == identity else {}
        if any(not Path(item['output_file']).is_file() or hashlib.sha256(Path(item['output_file']).read_bytes()).hexdigest() != item.get('output_sha256')
               for item in before.get('outputs', []) if item.get('output_file')):
            before = {}
        if not before:
            before = tested(initial)
            if before["setup_rc"] == 0 and before["test_rc"] is not None:
                lib.save_json(cache, {"identity": identity, "result": before})
    after = before if commit == initial else tested(commit)
    before, after = copy.deepcopy(before), copy.deepcopy(after)
    for label, result in (('before', before), ('after', after)):
        for index, output in enumerate(result.get('outputs', [])):
            if output.get('output_file'):
                source = Path(output['output_file'])
                target = task_dir / 'evidence/command-logs' / label / f'{index}.log'
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
                output['output_file'] = str(target.relative_to(task_dir))
    changes = collect_changes(workspace, initial, commit)
    changes.update(model_commits=snapshot["model_commits"], host_commit=snapshot["host_commit"],
                   history_rewritten=snapshot["history_rewritten"])
    tree = lib.git_out(workspace, "ls-tree", "-r", "--name-only", commit).splitlines()
    trace = fbr_trace.summarize(task_dir / "trace" / "projects", ids["session_file"])
    facts = {
        "task_id": name, "initial_commit": initial, "commit": commit, "generated_at": lib.now_iso(),
        "test_identity": identity,
        "source_identity": source_identity(task_dir),
        "demo": {"setup_ok": recording["setup_ok"],
                 "failure_kind": recording.get('failure_kind', 'none'),
                 "status": recording.get('status', 'observed'),
                 "page_errors": recording.get('page_errors', []),
                 "setup_tail": recording["setup_runs"][-1]["tail"] if recording.get("setup_runs") and not recording["setup_ok"] else "",
                 "steps": [{k: s.get(k) for k in ("cmd", "show", "rc", "cut", "skipped", "tail")} for s in recording["steps"]]},
        "tests": {"setup_cmd": cfg["facts"]["setup_cmd"], "test_cmd": cfg["facts"]["test_cmd"],
                  "before": before, "after": after, **compare_tests(before, after)},
        "changes": changes,
        "trace": {"steps": trace["steps"], "counts": trace["counts"], "started_at": trace["started_at"],
                  "ended_at": trace["ended_at"], "api_errors": trace["api_errors"],
                  "assistant_answers": trace["assistant_answers"], "continuations": ids.get("continuations", [])},
        "verification": trace["verification"],
        "claims": check_claims(trace["final_answer"], tree, {f["path"] for f in changes["files"]}),
        "tree_files": tree[:20000],
    }
    lib.save_json(task_dir / "evidence" / "facts.json", facts)
    lib.set_stage(task_dir, "facts", "ok", commit=commit)
    tests = facts["tests"]
    return {"task_id": name, "state": "ok",
            "detail": f"改动 {changes['totals']['files']} 个文件，产物测试{'通过' if tests['after_ok'] else '不通过'}，"
                      f"新增失败 {len(tests['new_failures'])} 个，轨迹 {trace['counts']['tools']} 步"}


def main() -> int:
    parser = argparse.ArgumentParser(description="solo11 取证")
    parser.add_argument("--root")
    parser.add_argument("--batch", required=True)
    parser.add_argument("--tasks")
    parser.add_argument("--redo", action="store_true")
    args = parser.parse_args()
    try:
        root = lib.find_root(args.root)
        cfg = lib.settings(root)
        todo = [p for p in lib.task_dirs(root, args.batch, args.tasks) if lib.stage_ok(p, "record")]

        def safe(task_dir: Path) -> dict:
            try:
                return facts_one(task_dir, root, cfg, args.redo)
            except lib.FbrError as exc:
                lib.set_stage(task_dir, "facts", "failed", reason=str(exc))
                return {"task_id": task_dir.name, "state": "failed", "detail": str(exc)}

        with ThreadPoolExecutor(max_workers=max(1, int(cfg["record_parallel"]))) as pool:
            rows = list(pool.map(safe, todo))
        for row in rows:
            print(f"{row['task_id']}  {row['state']}  {row['detail']}", flush=True)
        return 0 if all(r["state"] in ("ok", "skip") for r in rows) else 1
    except lib.FbrError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
