#!/usr/bin/env python3
"""Continue rejected annotation rounds through the frozen reviewer and all its gates."""
from __future__ import annotations
import argparse
import json
import importlib.util
from pathlib import Path
import fbrlib as lib
import fbr_reviewer as reviewer
import fbr_gate as gate


def load_engine():
    # Each rolling worker gets separate invoke globals; concurrent tasks must not
    # replace one another's model caller or write candidates into the wrong task.
    spec = importlib.util.spec_from_file_location('fbr_reviewer_recovery_engine', reviewer.__file__)
    engine = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(engine)
    return engine


def recover(task: Path, base: str, secret: str) -> dict:
    # Do not alter the hashed reviewer/quality policy or recertify old receipts.
    with lib.file_lock(task / '.operation.lock'):
        engine = load_engine()
        errors = engine.provenance_errors(task)
        if not errors:
            return {'state': 'already_valid', 'task_id': task.name}
        directory = task / 'review/recovery'
        directory.mkdir(parents=True, exist_ok=True)
        original = engine.invoke
        prior = lib.load_json(task / 'review/quality.json', default={})
        sequence = max((int(p.stem.split('-')[1]) for p in directory.glob('call-*.json')), default=0)
        cycle = max((int(p.name.split('-')[1]) for p in directory.glob('cycle-*-before.json')), default=0)
        latest_candidate = None

        def tracked(base, secret, cwd, prompt, **kwargs):
            nonlocal sequence, latest_candidate
            sequence += 1
            independent = '你是此题 Feedback 的独立复核者' in prompt
            guidance = '\n补充执行提示（不改变任何评分、写作或证据标准）：\n'
            guidance += '正文禁止标记为：' + json.dumps(gate.BANNED_MARKS, ensure_ascii=False) + '。\n'
            guidance += ('所有含测试、用例、test、pytest的完整分句均须登记 test_claims；'
                         '开发时临时补装客户端依赖也要用真实 trace/probe 来源，不能编造输出。'
                         '阅读 fbr_quality.source_text 确认实际来源字段和日志内容，再填 output_excerpt。\n'
                         'sources.excerpt 必须逐字存在于对应文件的原始文本。JSONL 中解码后的换行、引号'
                         '与文件序列化文本不同；请用 Python 验证 excerpt in Path(path).read_text()，'
                         '不成立就重新摘取真实连续片段，不通过修改原始材料满足摘录。\n')
            if not independent and prior:
                guidance += '上一轮失败材料，仅用于定位修复；所有结论仍需原文核查：\n' + json.dumps(
                    {'quality': prior, 'candidate': latest_candidate}, ensure_ascii=False)
            call = {'kind': 'independent_check' if independent else 'generation',
                    'cycle': cycle, 'at': lib.now_iso(), 'prompt': prompt + guidance}
            path = directory / f'call-{sequence:04d}.json'
            lib.save_json(path, call)
            try:
                text, receipt = original(base, secret, cwd, prompt + guidance, **kwargs)
                # original invoke already rejects outputs containing the secret.
                call.update(response=text, receipt=receipt, state='completed')
                lib.save_json(path, call)
                if not independent:
                    latest_candidate = engine.decode_output(text)
                return text, receipt
            except Exception:
                call['state'] = 'invocation_failed'
                lib.save_json(path, call)
                raise

        engine.invoke = tracked
        try:
            while True:
                cycle += 1
                lib.save_json(directory / f'cycle-{cycle:04d}-before.json', prior)
                try:
                    result = engine.review(task, base, secret)
                    lib.save_json(directory / 'result.json', result)
                    return result
                except lib.FbrError as exc:
                    if str(exc) != 'Feedback 连续 3 版未通过，停止；原因见 review/quality.json，不采用失败候选':
                        raise
                    prior = lib.load_json(task / 'review/quality.json')
                    lib.save_json(directory / f'cycle-{cycle:04d}-rejected.json', prior)
                    print(json.dumps({'task_id':task.name, 'state':'repairing', 'cycle':cycle,
                                      'errors':prior['history'][-1]['errors']}, ensure_ascii=False), flush=True)
                    # Annotation contract rejection is repairable; transport failures or
                    # missing evidence still return to the orchestrator, never bypass gates.
        finally:
            engine.invoke = original


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root')
    parser.add_argument('--batch', required=True)
    parser.add_argument('--task', required=True)
    parser.add_argument('--env-file', default=reviewer.DEFAULT_ENV)
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    if not args.execute:
        print(json.dumps({'state':'dry','detail':'继续修复评价并保留候选；不修改产物或提交平台'},ensure_ascii=False))
        return
    root = lib.find_root(args.root)
    task = lib.task_dirs(root, args.batch, args.task)[0]
    base, secret = reviewer.credentials(Path(args.env_file).expanduser())
    print(json.dumps(recover(task, base, secret), ensure_ascii=False), flush=True)

if __name__ == '__main__':
    main()
