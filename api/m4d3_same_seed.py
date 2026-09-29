"""Reproduce M4d-3 baseline and final pack metrics on one local evaluation seed.

The baseline pack is a frozen copy of the M4d-2 selection rule. The script
uses the evaluation runner's local embedder and generation capture. No external
provider call is made. Choose ``--dev-shapes`` to skip holdout entirely.
"""
import json
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
        final_dev = runner.evaluate(env, manifest, dev, seeded, capture)
        after = summarize(dev,final_dev,index)
        print("FINAL_DEV",json.dumps(after),flush=True)
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
        capture.embedding_calls -= baseline_calls
        data=build(manifest=manifest,qset=qset,records=records,
                   rows=runner.score(manifest,qset,records,index),
                   diagnostic_rows=runner.score(manifest,qset,records,index,runner.DIAGNOSTIC_OVERLAP),
                   index=index,seeded=seeded,viewer=viewer,capture=capture,settings=env.settings,
                   configured_pricing=env.configured_pricing,
                   defaults={"embedding":runner.EmbeddingPolicy().pricing_verified,
                             "rerank":runner.RerankPolicy().pricing_verified,
                             "generation":runner.GenerationPolicy().pricing_verified},
                   comparison_detected={q.id:rag.is_comparison(q.question) for q in qset.questions},
                   started=started,git=runner.git_state(),modes=runner.MODES)
        data["m4d3_same_seed_dev"]={"baseline":before,
                                      "final":after,
                                      "baseline_embedding_calls":baseline_calls,
                                      "note":"Baseline and final dev evaluated on the same seeded DB; holdout evaluated once after final rule."}
    output=Path("evaluation/rag/reports/rag-eval-2026-09-29-m4d3-pack")
    output.with_suffix(".json").write_text(json.dumps(data,ensure_ascii=False,indent=1)+"\n",encoding="utf8")
    output.with_suffix(".md").write_text(render_markdown(data),encoding="utf8")
    print("HOLDOUT_FINAL",json.dumps(summarize(holdout,final_holdout,index)),flush=True)
    print("ACL",len(data["acl"]["violations"]),len(data["acl"]["viewer"]["violations"]),"REPORT",output,flush=True)

if __name__ == "__main__": main()
