"""Reproduce M4d-3 baseline and final pack metrics on one local evaluation seed.

The baseline pack is a frozen copy of the M4d-2 selection rule. The script
uses the evaluation runner's local embedder and generation capture. No external
provider call is made. Choose ``--dev-shapes`` to skip holdout entirely.
"""
import json
import ast
import subprocess
import uuid
import sys
from pathlib import Path
from unittest import mock
from datetime import datetime, timezone

from app import rag
from app.evaluation import runner
from app.evaluation.schema import load
from app.evaluation.report import build, render_markdown, _pack_sizes

ROOT = Path(__file__).resolve().parents[1] / "evaluation" / "rag"
PREVIOUS_CODE_COMMIT = "8f0d04d3725c2f7912c6c81a4947fa35332fb8a4"


def previous_comparison_resolver():
    """Load the reviewed PR #14 resolver from its committed source for same-seed comparison."""
    source = subprocess.run(["git", "show", f"{PREVIOUS_CODE_COMMIT}:api/app/rag.py"],
                            cwd=ROOT.parents[1], capture_output=True, text=True, check=True).stdout
    node = next(item for item in ast.parse(source).body
                if isinstance(item, ast.FunctionDef) and item.name == "_comparison_papers")
    namespace = {"re": rag.re, "_pack_words": rag._pack_words,
                 "_PACK_ABBREVIATION": rag._PACK_ABBREVIATION}
    exec(compile(ast.Module(body=[node], type_ignores=[]), "committed_previous_resolver", "exec"), namespace)
    return namespace["_comparison_papers"]


def changed_questions(before, after):
    old = {(row["question_id"], row["mode"]): row for row in before if row["stage"] == "rag_pack"}
    changed = []
    for row in after:
        if row["stage"] != "rag_pack":
            continue
        previous = old[row["question_id"], row["mode"]]
        fields = ("hits", "rag_status", "rag_reason", "generation_called")
        if any(previous[field] != row[field] for field in fields):
            changed.append({"question_id": row["question_id"], "mode": row["mode"],
                            "before": {field: previous[field] for field in fields},
                            "after": {field: row[field] for field in fields}})
    return changed

def summarize(qset, records, index):
    rows = runner.score(None, qset, records, index)
    out = {}
    for stage in ("search:keyword", "search:hybrid", "rag_pack:keyword", "rag_pack:hybrid"):
        for group in ("all", "comparison"):
            selected = [r for r in rows if r.stage == stage and (group == "all" or r.group == group)]
            out[f"{stage}:{group}"] = {
                "sides": f"{sum(x is not None and (stage.startswith('rag_pack') or x <= 5) for r in selected for x in r.ranks)}/{sum(len(r.ranks) for r in selected)}",
                "full": f"{sum(all(x is not None and (stage.startswith('rag_pack') or x <= 5) for x in r.ranks) for r in selected if len(r.ranks)>1)}/{sum(len(r.ranks)>1 for r in selected)}",
                "pack_errors": [r.question_id for r in selected if r.outcome == 'evidence_pack']}
    out["generation"] = {m:sum(r["generation_called"] for r in records if r["stage"] == "rag_pack" and r["mode"] == m) for m in ("keyword","hybrid")}
    out["no_answer_sent"] = {m:sum(r["generation_called"] for r in records if r["stage"] == "rag_pack" and r["mode"] == m and next(q for q in qset.questions if q.id == r["question_id"]).expected.status != "answered") for m in ("keyword","hybrid")}
    return out

def main():
    measured_git = runner.git_state()
    if measured_git["dirty"]:
        raise SystemExit("Commit code and start from a clean working tree before measuring M4d-3")
    manifest, qset = load(ROOT)
    dev = qset.model_copy(update={"questions": [q for q in qset.questions if q.split == "dev"]})
    holdout = qset.model_copy(update={"questions": [q for q in qset.questions if q.split == "holdout"]})
    capture = runner.Capture()
    started = datetime.now(timezone.utc)
    with runner.environment() as env, runner.instrumented(capture):
        seeded = runner.seed(env, manifest, ROOT)
        seed_calls = capture.embedding_calls
        index = {key:runner.indexed_hits(env, seeded, key) for key in seeded.ingest}
        def old_pack(hits, settings, *, comparing=False, question=""):
            policy=settings.generation; pack={}; used=0; papers=set()
            eligible=[(h,len(h["text"].encode("utf8"))) for h in hits
                      if h["section_kind"] in ("body","abstract")
                      and len(h["text"].encode("utf8"))<=policy.max_evidence_bytes]
            def add(h,size):
                nonlocal used
                key=uuid.UUID(h["chunk_id"])
                if key in pack or len(pack)>=policy.max_evidence_chunks:return False
                if h["item_id"] not in papers and len(papers)>=policy.max_evidence_papers:return False
                if used+size>policy.max_evidence_bytes:return False
                pack[key]=h;used+=size;papers.add(h["item_id"]);return True
            if comparing:
                grouped={}
                for h,size in eligible:grouped.setdefault(h["item_id"],[]).append((h,size))
                selected=list(grouped)[:policy.max_evidence_papers]
                minimum={paper:min(size for _,size in grouped[paper]) for paper in selected}
                for pos,paper in enumerate(selected):
                    reserved=sum(minimum[other] for other in selected[pos+1:])
                    for h,size in grouped[paper]:
                        if used+size+reserved<=policy.max_evidence_bytes:
                            add(h,size);break
            for h,size in eligible:
                if len(pack)>=policy.max_evidence_chunks:break
                add(h,size)
            return pack
        with mock.patch("app.rag._pack", old_pack):
            baseline = runner.evaluate(env, manifest, dev, seeded, capture)
        before = summarize(dev,baseline,index)
        baseline_calls = capture.embedding_calls - seed_calls
        print("BASELINE",json.dumps(before),flush=True)
        with mock.patch("app.rag._comparison_papers", previous_comparison_resolver()):
            review_previous_dev = runner.evaluate(env, manifest, dev, seeded, capture)
        review_before = summarize(dev, review_previous_dev, index)
        review_calls = capture.embedding_calls - seed_calls - baseline_calls
        print("REVIEW_BEFORE_DEV", json.dumps(review_before), flush=True)
        final_dev = runner.evaluate(env, manifest, dev, seeded, capture)
        after = summarize(dev,final_dev,index)
        review_changed = changed_questions(review_previous_dev, final_dev)
        print("FINAL_DEV",json.dumps(after),flush=True)
        print("REVIEW_CHANGED_DEV", json.dumps([(row["question_id"], row["mode"])
                                                 for row in review_changed]), flush=True)
        if "--dev-shapes" in sys.argv:
            shapes={"baseline":_pack_sizes(dev,baseline),"final":_pack_sizes(dev,final_dev),
                    "baseline_metrics":before,
                    "final_metrics":after,
                    "note":"Both dev policies ran on this one seeded DB; holdout was not evaluated in this invocation."}
            Path("evaluation/rag/reports/m4d3-dev-pack-shapes.json").write_text(
                json.dumps(shapes,ensure_ascii=False,indent=1)+"\n",encoding="utf8")
            print("DEV_SHAPES",json.dumps(shapes),flush=True)
            return
        final_holdout = runner.evaluate(env, manifest, holdout, seeded, capture)
        records = final_dev + final_holdout
        viewer = runner.check_viewer(seeded,records)
        capture.embedding_calls -= baseline_calls + review_calls
        data=build(manifest=manifest,qset=qset,records=records,
                   rows=runner.score(manifest,qset,records,index),
                   diagnostic_rows=runner.score(manifest,qset,records,index,runner.DIAGNOSTIC_OVERLAP),
                   index=index,seeded=seeded,viewer=viewer,capture=capture,settings=env.settings,
                   configured_pricing=env.configured_pricing,
                   defaults={"embedding":runner.EmbeddingPolicy().pricing_verified,
                             "rerank":runner.RerankPolicy().pricing_verified,
                             "generation":runner.GenerationPolicy().pricing_verified},
                   comparison_detected={q.id:rag.is_comparison(q.question) for q in qset.questions},
                   started=started,git=measured_git,modes=runner.MODES)
        data["command"] = "& $py api\\m4d3_same_seed.py"
        data["measurement"] = {"base_commit": measured_git["commit"],
                               "working_tree_dirty_before_output": measured_git["dirty"],
                               "note": "Git state captured before seeding and before report files were written."}
        data["m4d3_same_seed_dev"]={"baseline":before,
                                      "final":after,
                                      "pack_sizes": {"baseline": _pack_sizes(dev, baseline),
                                                     "final": _pack_sizes(dev, final_dev)},
                                      "baseline_embedding_calls":baseline_calls,
                                      "note":"M4d-2 baseline and final dev evaluated on the same seeded DB; holdout evaluated once after final rule."}
        data["review_pair_reference_dev"] = {"previous_code_commit": PREVIOUS_CODE_COMMIT,
                                             "before": review_before, "after": after,
                                             "changed_questions": review_changed,
                                             "previous_embedding_calls": review_calls,
                                             "note": "Previous PR #14 resolver and final resolver ran on the same dev seed; only final policy evaluated on holdout."}
    output=Path("evaluation/rag/reports/rag-eval-2026-09-29-m4d3-pack")
    output.with_suffix(".json").write_text(json.dumps(data,ensure_ascii=False,indent=1)+"\n",encoding="utf8")
    output.with_suffix(".md").write_text(render_markdown(data),encoding="utf8")
    print("HOLDOUT_FINAL",json.dumps(summarize(holdout,final_holdout,index)),flush=True)
    print("ACL",len(data["acl"]["violations"]),len(data["acl"]["viewer"]["violations"]),"REPORT",output,flush=True)

if __name__ == "__main__": main()
