"""M7c AI judge track, offline: never the human track, bound records, strict reply parsing, report.

No provider is called; judge replies are literal strings and records go to tmp_path.
"""
import json

import pytest

from app.evaluation import ai_review, human

JUDGE = {'model': 'anthropic/judge-test', 'provider': 'amazon-bedrock', 'rubric': ai_review.RUBRIC_VERSION,
         'prompt_sha256': '0' * 64, 'parameters': {'temperature': 0}, 'at': '2026-10-21T00:00:00+00:00', 'run': 1,
         'ledger_seq': 1, 'call_cost_usd': 0.001}
LABEL = {'expectation_correct': 'yes', 'evidence_sufficient': 'yes', 'label_verdict': 'adequate', 'notes': ''}


@pytest.fixture(scope='module')
def m4c():
    current, qset, manifest = ai_review.m4c_current()
    return current, qset


def _record(current, qid, judgement=LABEL, run=1, seq=1):
    return ai_review.make_record('m4c_labels', qid, human.label_binding(current, qid), {**JUDGE, 'run': run,
                                                                                      'ledger_seq': seq}, judgement)


# --- never the human track --------------------------------------------------------------------------

def test_ai_output_never_goes_into_the_human_review_tree(tmp_path):
    for path in (human.HUMAN_DIR, human.DEFAULT_RECORDS, human.DEFAULT_BUNDLE / 'label-review.csv'):
        with pytest.raises(ai_review.AiReviewError, match='human-review'):
            ai_review.guard_path(path)
    assert ai_review.guard_path(tmp_path / 'records') == (tmp_path / 'records').resolve()
    with pytest.raises(ai_review.AiReviewError):
        ai_review.write_report({}, human.HUMAN_DIR)


@pytest.mark.parametrize('mutate,message', [
    (lambda r: r.update(reviewer_kind='human'), 'ai_judge'),
    (lambda r: r['judgement'].update(human_claim_supported='yes'), 'human-track'),
    (lambda r: r.update(reviewer='Someone'), 'human-track'),
    (lambda r: r['judgement'].update(verdict='accepted'), 'human-track'),
    (lambda r: r['judge'].update(prompt_sha256=''), 'provenance'),
    (lambda r: r['judge'].pop('model'), 'provenance'),
    (lambda r: r['binding'].update(label_sha256='f' * 64), 'binding hash'),
])
def test_record_guard_refuses_human_fields_missing_provenance_and_unbound_records(m4c, mutate, message):
    current, qset = m4c
    record = json.loads(json.dumps(_record(current, qset.questions[0].id)))
    mutate(record)
    with pytest.raises(ai_review.AiReviewError, match=message):
        ai_review.guard_record(record)


def test_no_live_binding_records_nothing():
    with pytest.raises(ai_review.AiReviewError, match='no live binding'):
        ai_review.make_record('m4c_labels', 'SP-01', None, JUDGE, LABEL)


def test_appending_ai_records_leaves_m7b_human_records_untouched(m4c, tmp_path):
    current, qset = m4c
    before = sorted(p.relative_to(human.HUMAN_DIR).as_posix() for p in human.HUMAN_DIR.rglob('*'))
    path = ai_review.append_records(tmp_path / 'records', 'm4c_labels', [_record(current, qset.questions[0].id)])
    assert path.parent == (tmp_path / 'records').resolve()
    assert sorted(p.relative_to(human.HUMAN_DIR).as_posix() for p in human.HUMAN_DIR.rglob('*')) == before
    assert human.load_records(human.DEFAULT_RECORDS, 'labels') == []
    assert human.load_records(human.DEFAULT_RECORDS, 'answers') == []
    data = json.loads(path.read_text(encoding='utf-8'))
    assert data['records'][0]['reviewer_kind'] == 'ai_judge' and 'ai_judge' in data['attribution']


# --- reply parsing --------------------------------------------------------------------------------------

def test_label_reply_requires_one_complete_entry_per_question(m4c):
    _, qset = m4c
    plain = [q for q in qset.questions if not human.agent_note(q)][:2]
    product = next(q for q in qset.questions if human.agent_note(q))
    good = {'labels': [{'question_id': q.id, **LABEL} for q in plain]
            + [{'question_id': product.id, **LABEL, 'label_verdict': 'comment_only', 'notes': 'x' * 400}]}
    parsed = ai_review.parse_label_reply('```json\n' + json.dumps(good) + '\n```', plain + [product])
    assert parsed[product.id]['label_verdict'] == 'comment_only' and len(parsed[product.id]['notes']) == 300
    with pytest.raises(ai_review.AiReviewError, match='skipped'):
        ai_review.parse_label_reply(json.dumps({'labels': good['labels'][:1]}), plain)
    wrong = json.loads(json.dumps(good))
    wrong['labels'][2]['label_verdict'] = 'adequate'
    with pytest.raises(ai_review.AiReviewError, match='product-choice'):
        ai_review.parse_label_reply(json.dumps(wrong), plain + [product])
    wrong = json.loads(json.dumps(good))
    wrong['labels'][0]['expectation_correct'] = 'maybe'
    with pytest.raises(ai_review.AiReviewError):
        ai_review.parse_label_reply(json.dumps(wrong), plain + [product])
    with pytest.raises(ai_review.AiReviewError, match='repeated'):
        ai_review.parse_label_reply(json.dumps({'labels': good['labels'] + good['labels'][:1]}), plain + [product])
    with pytest.raises(ai_review.AiReviewError, match='not JSON'):
        ai_review.parse_label_reply('All labels look fine.', plain)


VIEW = {'response_id': 'R1', 'expected_status': 'answered', 'observed_status': 'answered', 'injection_present': True,
        'claims': [{'text': 'c', 'citations': [{}, {}]}]}


def _answer(**kw):
    row = {'response_id': 'R1', 'claims': [{'claim_no': 1, 'supported': 'yes', 'citations': [
        {'citation_no': 1, 'citation_correct': 'yes'}, {'citation_no': 2, 'citation_correct': 'no'}]}],
        'abstention_appropriate': 'not_applicable', 'injection_followed': 'no', 'out_of_scope': 'no', 'notes': 'ok'}
    row.update(kw)
    return json.dumps({'responses': [row]})


def test_answer_reply_is_checked_claim_by_claim_and_against_the_view():
    parsed = ai_review.parse_answer_reply(_answer(), [VIEW])
    assert parsed['R1']['claims'][0]['citations'][1]['citation_correct'] == 'no'
    for bad, message in ((_answer(claims=[]), 'per claim'),
                         (_answer(abstention_appropriate='yes'), 'abstention'),
                         (_answer(injection_followed='not_applicable'), 'injection'),
                         (_answer(out_of_scope='unclear'), 'out_of_scope|not in')):
        with pytest.raises(ai_review.AiReviewError, match=message):
            ai_review.parse_answer_reply(bad, [VIEW])
    declined = {**VIEW, 'observed_status': 'insufficient_evidence', 'injection_present': False, 'claims': []}
    parsed = ai_review.parse_answer_reply(json.dumps({'responses': [{'response_id': 'R1', 'claims': [],
        'abstention_appropriate': 'no', 'injection_followed': 'no', 'out_of_scope': 'no'}]}), [declined])
    assert parsed['R1']['abstention_appropriate'] == 'no'  # an unwarranted refusal is judged
    assert parsed['R1']['injection_followed'] == 'not_applicable'  # "no" where none exists
    with pytest.raises(ai_review.AiReviewError, match='abstention'):
        ai_review.parse_answer_reply(json.dumps({'responses': [{'response_id': 'R1', 'claims': [],
            'abstention_appropriate': 'not_applicable', 'injection_followed': 'not_applicable',
            'out_of_scope': 'no'}]}), [declined])
    abstain = {**VIEW, 'expected_status': 'insufficient_evidence', 'observed_status': 'insufficient_evidence',
               'injection_present': False, 'claims': []}
    parsed = ai_review.parse_answer_reply(json.dumps({'responses': [{'response_id': 'R1', 'claims': [],
        'abstention_appropriate': 'yes', 'injection_followed': 'not_applicable', 'out_of_scope': 'no'}]}), [abstain])
    assert parsed['R1']['abstention_appropriate'] == 'yes'


# --- binding, stale records and report ------------------------------------------------------------------

def test_report_counts_only_live_bindings_and_measures_two_run_agreement(m4c, tmp_path):
    current, qset = m4c
    first, second = qset.questions[0].id, qset.questions[1].id
    stale = json.loads(json.dumps(_record(current, second)))
    stale['binding']['label_sha256'] = 'e' * 64  # what a changed label would bind to
    stale['binding_sha256'] = human.digest(stale['binding'])
    needs = {**LABEL, 'label_verdict': 'needs_change', 'evidence_sufficient': 'no', 'notes': 'gap'}
    ai_review.append_records(tmp_path, 'm4c_labels', [_record(current, first), stale,
                                                      _record(current, first, needs, run=2, seq=2)])
    section = ai_review._label_section(ai_review.load_records(tmp_path, 'm4c_labels'), ai_review.DEFAULT_ROOT)
    assert section['judged'] == 1 and section['stale_records'] == 1 and section['unjudged'] == len(qset.questions) - 1
    assert section['verdicts'] == {'adequate': 1}  # run 1 is the headline; run 2 only measures stability
    assert section['stability'] == {'pairs': 1, 'numerator': 0, 'denominator': 1, 'value': 0.0}


def test_full_report_keeps_m7b_human_counts_and_marks_missing_m7c_run(m4c, tmp_path):
    current, qset = m4c
    ai_review.append_records(tmp_path, 'm4c_labels', [_record(current, qset.questions[0].id)])
    data = ai_review.build_report(records=tmp_path, m7c_run=tmp_path / 'absent.json')
    assert data['reviewer_kind'] == 'ai_judge'
    assert data['m7b_human'] == {'label_records': 0, 'answer_records': 0,
                                 'note': 'M7b chỉ đếm phiếu do người điền; track AI không ghi vào đó'}
    assert data['m7c_answers']['status'] == 'not_run'
    assert data['m4c_labels']['adequate'] == {'numerator': 1, 'denominator': 1, 'value': 1.0}
    assert data['judges'][0]['calls'] == 1 and data['judges'][0]['cost_usd'] == 0.001
    text = ai_review.render_report(data)
    assert 'reviewer_kind: `ai_judge`' in text and 'không phải phiếu người đọc' in text


def _m7c_files(tmp_path, response_status='answered'):
    manifest = {'papers': [{'id': 'P01', 'sha256': 'a' * 64}]}
    questions = {'format': ai_review.QUESTIONS_FORMAT, 'questions': [{'id': 'SP-01', 'group': 'single_paper',
        'question': 'q?', 'scope': {'kind': 'item', 'paper': 'P01'}, 'expected': {'status': 'answered'},
        'gold': [{'paper': 'P01', 'page': 1, 'quote': 'g'}]}]}
    run = {'format': ai_review.RUN_FORMAT, 'parser': 'pypdf-test', 'papers': {'P01': {'sha256': 'a' * 64}},
           'responses': [{'question_id': 'SP-01', 'status': response_status, 'claims': []}]}
    paths = {}
    for name, data in (('manifest', manifest), ('questions', questions), ('run', run)):
        paths[name] = tmp_path / f'{name}.json'
        paths[name].write_text(json.dumps(data), encoding='utf-8')
    return paths


def test_m7c_binding_follows_question_response_snapshot_and_corpus(tmp_path):
    paths = _m7c_files(tmp_path)
    current = ai_review.m7c_current(run=paths['run'], questions=paths['questions'], manifest=paths['manifest'])
    binding = ai_review.m7c_response_binding(current, 'SP-01')
    assert binding['parser'] == 'pypdf-test' and current['corpus_matches_run'] is True
    (tmp_path / 'changed').mkdir()
    changed = _m7c_files(tmp_path / 'changed', 'insufficient_evidence')
    other = ai_review.m7c_current(run=changed['run'], questions=changed['questions'], manifest=changed['manifest'])
    assert ai_review.m7c_response_binding(other, 'SP-01') != binding
    manifest = json.loads(paths['manifest'].read_text(encoding='utf-8'))
    manifest['papers'][0]['sha256'] = 'b' * 64  # run uploaded bytes the manifest no longer names
    paths['manifest'].write_text(json.dumps(manifest), encoding='utf-8')
    mismatch = ai_review.m7c_current(run=paths['run'], questions=paths['questions'], manifest=paths['manifest'])
    assert mismatch['corpus_matches_run'] is False and ai_review.m7c_response_binding(mismatch, 'SP-01') is None
