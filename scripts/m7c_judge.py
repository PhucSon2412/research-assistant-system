"""M7c AI judge runs: prompts from app.evaluation.ai_review, calls through the M7c ledger.

    python scripts/m7c_judge.py --state <M7c state> labels [--batch 16] [--run 1] [--only 0 2]
    python scripts/m7c_judge.py --state <dir> m7a [--run 1]
    python scripts/m7c_judge.py --state <dir> m7c --chunks <state>/runtime/chunks.json [--batch 3] [--run 1] [--only ...]
    python scripts/m7c_judge.py report

Judge (orchestrator choice under the owner's delegation, M7c): ``anthropic/claude-sonnet-5.5``, a family
other than the generation model (OpenAI), pinned to the Amazon Bedrock endpoint that OpenRouter lists as
ZDR, no fallback, data collection denied, ``max_price`` at that endpoint's listed price. Model-default
temperature (the endpoint lists no temperature parameter), JSON object output. Records go to evaluation/ai-review/records (``reviewer_kind: ai_judge``), never to
the M7b human records or sheets. A reply that does not parse records nothing; its cost stays in the ledger.
"""
from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'scripts'))
sys.path.insert(0, str(REPO / 'api'))
import m7a_live  # noqa: E402
import m7c_live  # noqa: E402
from app.evaluation import ai_review  # noqa: E402

JUDGE = m7c_live.JudgeRoute(model='anthropic/claude-sonnet-5.5', provider='amazon-bedrock',
                            provider_name='Amazon Bedrock', input_usd_per_m=Decimal('2'),
                            output_usd_per_m=Decimal('10'), max_input_tokens=60_000, max_output_tokens=4_000)


def judge_meta(messages, outcome, run, targets, rubric=ai_review.RUBRIC_VERSION) -> dict:
    receipt = outcome['receipt'] or {}
    return {'model': JUDGE.model, 'provider': JUDGE.provider, 'provider_name': JUDGE.provider_name,
            'served_by': receipt.get('selected_provider') or receipt.get('provider'),
            'response_model': receipt.get('model'), 'rubric': rubric,
            'prompt_sha256': m7c_live.prompt_sha256(messages),
            'parameters': {'temperature': 'model default (not supported by the endpoint)',
                           'max_tokens': JUDGE.max_output_tokens, 'response_format': 'json_object',
                           'routing': 'only amazon-bedrock, no fallback, data_collection deny, zdr, max_price'},
            'at': m7a_live.now(), 'run': run, 'ledger_seq': outcome['seq'], 'generation_id': receipt.get('id'),
            'call_cost_usd': outcome['cost_usd'], 'batch_targets': targets,
            'usage': receipt.get('usage')}


class JudgeStopped(SystemExit):
    pass


def call(transport, messages, label):
    outcome = transport(messages, label)
    # Raw reply kept outside the repo (state dir), so a parser fix never needs a second paid call.
    replies = transport.state / 'judge-replies'
    replies.mkdir(exist_ok=True)
    (replies / f"{outcome['seq']:04d}-{label}.json").write_text(json.dumps(
        {'label': label, 'prompt_sha256': m7c_live.prompt_sha256(messages), 'content': outcome['content'],
         'receipt': outcome['receipt']}, ensure_ascii=False), encoding='utf-8')
    print(json.dumps({'label': label, 'seq': outcome['seq'], 'status': outcome['status'],
                      'error': outcome['error_code'], 'cost_usd': outcome['cost_usd']}), flush=True)
    if outcome['status'] != 'completed' or outcome['error_code']:
        # Fail fast: a provider/route problem repeats on every batch and each failed slot keeps its ceiling.
        raise JudgeStopped(f"judge call {label} not completed ({outcome['error_code']}); stopping, nothing more sent")
    return outcome


def batches(items, size, only):
    groups = [items[i:i + size] for i in range(0, len(items), size)]
    return [(index, group) for index, group in enumerate(groups) if not only or index in only]


def labels(transport, size, run, only) -> int:
    current, qset, manifest = ai_review.m4c_current()
    corpus = ai_review.m4c_corpus_text(manifest)
    failures = 0
    for index, group in batches(list(qset.questions), size, only):
        messages = ai_review.label_messages(group, corpus)
        outcome = call(transport, messages, f'm4c-labels-{run}-{index}')
        try:
            parsed = ai_review.parse_label_reply(outcome['content'], group)
        except ai_review.AiReviewError as exc:
            failures += 1
            print(f'batch {index}: unusable judge reply ({exc}); nothing recorded', flush=True)
            continue
        meta = judge_meta(messages, outcome, run, len(group))
        ai_review.append_records(ai_review.DEFAULT_RECORDS, 'm4c_labels', [
            ai_review.make_record('m4c_labels', q.id, ai_review.human.label_binding(current, q.id), meta, parsed[q.id])
            for q in group])
    return 1 if failures else 0


def m7a(transport, run) -> int:
    current, responses, extra = ai_review.m7a_current()
    views = ai_review.m7a_views(current, responses, extra)
    messages = ai_review.answer_messages(views)
    outcome = call(transport, messages, f'm7a-answers-{run}')
    try:
        parsed = ai_review.parse_answer_reply(outcome['content'], views)
    except ai_review.AiReviewError as exc:
        print(f'unusable judge reply ({exc}); nothing recorded')
        return 1
    meta = judge_meta(messages, outcome, run, len(views), ai_review.ANSWER_RUBRIC_VERSION)
    ai_review.append_records(ai_review.DEFAULT_RECORDS, 'm7a_answers', [
        ai_review.make_record('m7a_answers', v['response_id'], ai_review.m7a_response_binding(current, v['response_id']),
                              meta, parsed[v['response_id']]) for v in views])
    return 0


def m7c(transport, chunks_path: Path, size, run, only) -> int:
    current = ai_review.m7c_current()
    if current is None:
        raise SystemExit('no M7c run snapshot; run scripts/m7c_runtime.py snapshot first')
    chunks = json.loads(chunks_path.read_text(encoding='utf-8'))
    texts = {cid: c.get('text', '') for cid, c in chunks.items() if isinstance(c, dict)}
    views = ai_review.m7c_views(current, texts)
    failures = 0
    for index, group in batches(views, size, only):
        messages = ai_review.answer_messages(group)
        outcome = call(transport, messages, f'm7c-answers-{run}-{index}')
        try:
            parsed = ai_review.parse_answer_reply(outcome['content'], group)
        except ai_review.AiReviewError as exc:
            failures += 1
            print(f'batch {index}: unusable judge reply ({exc}); nothing recorded', flush=True)
            continue
        meta = judge_meta(messages, outcome, run, len(group), ai_review.ANSWER_RUBRIC_VERSION)
        ai_review.append_records(ai_review.DEFAULT_RECORDS, 'm7c_answers', [
            ai_review.make_record('m7c_answers', v['response_id'],
                                  ai_review.m7c_response_binding(current, v['response_id']), meta,
                                  parsed[v['response_id']]) for v in group])
    return 1 if failures else 0


def m7c_reparse(state: Path, chunks_path: Path, size, run, only) -> int:
    """Record batches from their saved raw reply (no call) when the rebuilt prompt hash is identical."""
    current = ai_review.m7c_current()
    chunks = json.loads(chunks_path.read_text(encoding='utf-8'))
    views = ai_review.m7c_views(current, {cid: c.get('text', '') for cid, c in chunks.items() if isinstance(c, dict)})
    saved = {json.loads(p.read_text(encoding='utf-8'))['label']: (p, json.loads(p.read_text(encoding='utf-8')))
             for p in sorted((state / 'judge-replies').glob('*.json'))}
    for index, group in batches(views, size, only):
        label = f'm7c-answers-{run}-{index}'
        path, reply = saved[label]
        messages = ai_review.answer_messages(group)
        if m7c_live.prompt_sha256(messages) != reply['prompt_sha256']:
            raise SystemExit(f'{label}: rebuilt prompt differs from the saved reply; not recorded')
        parsed = ai_review.parse_answer_reply(reply['content'], group)
        seq = int(path.name.split('-', 1)[0])
        receipt = reply['receipt'] or {}
        outcome = {'receipt': receipt, 'seq': seq, 'cost_usd': (receipt.get('usage') or {}).get('cost')}
        meta = judge_meta(messages, outcome, run, len(group), ai_review.ANSWER_RUBRIC_VERSION)
        meta['reparsed_from_saved_reply'] = True
        ai_review.append_records(ai_review.DEFAULT_RECORDS, 'm7c_answers', [
            ai_review.make_record('m7c_answers', v['response_id'],
                                  ai_review.m7c_response_binding(current, v['response_id']), meta,
                                  parsed[v['response_id']]) for v in group])
        print(f'{label}: recorded from saved reply seq {seq}')
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument('--state', type=Path)
    p.add_argument('--key-file', type=Path, default=m7c_live.KEY_FILE)
    p.add_argument('action', choices=['labels', 'm7a', 'm7c', 'm7c-reparse', 'report'])
    p.add_argument('--batch', type=int)
    p.add_argument('--run', type=int, default=1)
    p.add_argument('--only', type=int, nargs='*')
    p.add_argument('--chunks', type=Path)
    a = p.parse_args()
    if a.action == 'report':
        json_path, md_path = ai_review.write_report(ai_review.build_report())
        print(f'wrote {json_path} and {md_path}')
        return 0
    if a.state is None:
        raise SystemExit('--state required for judge calls')
    transport = m7c_live.JudgeTransport(a.state.resolve(), m7c_live.load_key(a.key_file), JUDGE)
    if a.action == 'labels':
        return labels(transport, a.batch or 16, a.run, a.only)
    if a.action == 'm7a':
        return m7a(transport, a.run)
    if a.chunks is None:
        raise SystemExit('--chunks required')
    if a.action == 'm7c-reparse':
        return m7c_reparse(a.state.resolve(), a.chunks, a.batch or 3, a.run, a.only)
    return m7c(transport, a.chunks, a.batch or 3, a.run, a.only)


if __name__ == '__main__':
    sys.exit(main())
