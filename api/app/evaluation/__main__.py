"""CLI: ``python -m app.evaluation <command>`` from any directory (``api`` is installed editable)."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.evaluation.corpus import DEFAULT_ROOT, build_pdfs
from app.evaluation.schema import load, save_questions
from app.evaluation.validate import annotate_questions, load_sources, validate


def _validate(args) -> int:
    errors = validate(args.root)
    for error in errors:
        print(error)
    manifest, qset = load(args.root)
    print(f"{len(qset.questions)} questions, {sum(q.split == 'holdout' for q in qset.questions)} holdout, "
          f"{len(manifest.papers)} papers: {'OK' if not errors else f'{len(errors)} error(s)'}")
    return 1 if errors else 0


def _annotate(args) -> int:
    manifest, qset = load(args.root)
    save_questions(args.root, annotate_questions(manifest, qset, load_sources(manifest, args.root)))
    return _validate(args)


def _build_pdfs(args) -> int:
    manifest, _ = load(args.root)
    expected = {paper.file: paper.sha256 for paper in manifest.papers if paper.format == "pdf"}
    rendered = build_pdfs(args.root, write=not args.check)
    changed = sorted(f for f, digest in rendered.items() if expected.get(f) != digest)
    for name in changed:
        print(f"{name}: rendered bytes differ from manifest")
    return 1 if changed and args.check else 0


def _run(args) -> int:
    from app.evaluation.report import render_markdown
    from app.evaluation.runner import run

    data = run(args.root, context=args.context, modes=tuple(args.modes), keep=args.keep)
    args.output.mkdir(parents=True, exist_ok=True)
    stem = args.output / f"rag-eval-{data['generated_at'][:10]}"
    stem.with_suffix(".json").write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n",
                                         encoding="utf-8", newline="\n")
    stem.with_suffix(".md").write_text(render_markdown(data), encoding="utf-8", newline="\n")
    violations = len(data["acl"]["violations"]) + len(data["acl"]["viewer"]["violations"])
    print(f"wrote {stem}.json and .md; ACL violations: {violations}")
    return 2 if violations or data["acl"]["gold_label_in_payload"] else 0


def _review_template(args) -> int:
    from app.evaluation.review import template_rows, write_sheet

    _, qset = load(args.root)
    write_sheet(args.output, template_rows(qset, args.modes))
    print(f"wrote {args.output}")
    return 0


def _score_review(args) -> int:
    from app.evaluation.review import read_sheet, score

    print(json.dumps(score(read_sheet(args.sheet)), indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.evaluation")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="evaluation data directory")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("validate", help="check schema, checksums and gold spans").set_defaults(func=_validate)
    commands.add_parser("annotate", help="fill span offsets from quotes, then validate").set_defaults(func=_annotate)
    build = commands.add_parser("build-pdfs", help="render PDF sources (needs the dev extra)")
    build.add_argument("--check", action="store_true", help="compare with the manifest without writing")
    build.set_defaults(func=_build_pdfs)
    runner = commands.add_parser("run", help="run retrieval/RAG capture on a throw-away DB (needs Compose db+minio)")
    runner.add_argument("--context", type=Path, help="explicit external synthetic context JSON")
    runner.add_argument("--output", type=Path, required=True, help="directory for the JSON and Markdown report")
    runner.add_argument("--modes", nargs="+", choices=["keyword", "hybrid"], default=["keyword", "hybrid"])
    runner.add_argument("--keep", action="store_true", help="keep the temporary database and bucket for debugging")
    runner.set_defaults(func=_run)
    template = commands.add_parser("review-template", help="write the human review sheet (CSV)")
    template.add_argument("--output", type=Path, required=True)
    template.add_argument("--modes", nargs="+", choices=["keyword", "hybrid"], default=["hybrid"])
    template.set_defaults(func=_review_template)
    scoring = commands.add_parser("score-review", help="compute human-reviewed metrics from a filled sheet")
    scoring.add_argument("sheet", type=Path)
    scoring.set_defaults(func=_score_review)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
