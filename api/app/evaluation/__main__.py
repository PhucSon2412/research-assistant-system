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


def _human_errors(errors: list[str]) -> int:
    for error in errors:
        print(f"error: {error}")
    return 1


def _human_prepare(args) -> int:
    from app.evaluation import human, human_bundle

    try:
        if args.m7a_state and args.m7a_snapshot:
            return _human_errors(["pass --m7a-state or --m7a-snapshot, not both"])
        if args.m7a_state:
            snapshot = human.dump_json(human.m7a_snapshot(args.m7a_state, args.m7a_run_id))
        elif args.m7a_snapshot:
            snapshot = args.m7a_snapshot.read_bytes()
        elif (args.bundle / human.SNAPSHOT_FILE).is_file():
            snapshot = (args.bundle / human.SNAPSHOT_FILE).read_bytes()
        else:
            snapshot = None
        outputs = human_bundle.build(args.root, snapshot)
        if args.check:
            problems = human_bundle.check(args.bundle, outputs)
            for problem in problems:
                print(problem)
            print(f"{args.bundle}: {'up to date' if not problems else f'{len(problems)} difference(s)'}")
            return 1 if problems else 0
        human_bundle.write(args.bundle, outputs, force=args.force)
    except human.ReviewError as exc:
        return _human_errors(exc.errors)
    manifest = json.loads(outputs["manifest.json"])
    print(f"wrote {args.bundle}: {manifest['sheets'][human.LABEL_SHEET]['rows']} label rows "
          f"({manifest['m4c']['holdout']} holdout), {manifest['sheets'][human.ANSWER_SHEET]['rows']} M7a answer rows "
          f"from {len(manifest['m7a']['responses'])} responses (available={manifest['m7a']['available']})")
    return 0


def _human_import(args, dry_run: bool) -> int:
    from app.evaluation import human

    try:
        if args.kind == "labels":
            result = human.import_labels(args.sheet, bundle=args.bundle, records=args.records, root=args.root,
                                         dry_run=dry_run, replace=getattr(args, "replace", False))
        else:
            result = human.import_answers(args.sheet, bundle=args.bundle, records=args.records,
                                          dry_run=dry_run, replace=getattr(args, "replace", False))
    except human.ReviewError as exc:
        return _human_errors(exc.errors)
    if result["errors"]:
        _human_errors(result["errors"])
        print(f"{args.sheet}: rejected, {len(result['errors'])} error(s); nothing was recorded")
        return 1
    action = "checked (not recorded)" if dry_run else "recorded" if result["written"] else "nothing new to record"
    print(f"{args.sheet}: {action}; reviewed rows {len(result['reviewed'])}, pending rows {len(result['pending'])}, "
          f"{json.dumps(result['counts'])}")
    return 0


def _human_report(args) -> int:
    from app.evaluation import human

    try:
        data = human.build_report(bundle=args.bundle, records=args.records, root=args.root)
    except human.ReviewError as exc:
        return _human_errors(exc.errors)
    json_path, md_path = human.write_report(data, args.output)
    labels, answers = data["m4c_labels"], data["m7a_answers"]
    print(f"status {data['status']}; labels {labels['status']} {json.dumps(labels['counts'])}; "
          f"M7a answers {answers['status']}; wrote {json_path} and {md_path}")
    return 0


def _add_human(commands) -> None:
    from app.evaluation import human

    def places(command, records: bool = True):
        command.add_argument("--bundle", type=Path, default=human.DEFAULT_BUNDLE, help="M7b review bundle directory")
        if records:
            command.add_argument("--records", type=Path, default=human.DEFAULT_RECORDS,
                                 help="directory of imported human review records")

    prepare = commands.add_parser("human-prepare", help="build the offline M7b review bundle (labels + M7a answers)")
    places(prepare, records=False)
    prepare.add_argument("--m7a-state", type=Path, help="M7a evidence state directory (reads runtime results only)")
    prepare.add_argument("--m7a-snapshot", type=Path, help="an M7a snapshot JSON written by an earlier prepare")
    prepare.add_argument("--m7a-run-id", default=human.DEFAULT_RUN_ID)
    prepare.add_argument("--check", action="store_true", help="compare the bundle on disk with a fresh build")
    prepare.add_argument("--force", action="store_true", help="overwrite even if a bundle sheet has filled cells")
    prepare.set_defaults(func=_human_prepare)
    for name, dry_run in (("human-validate", True), ("human-import", False)):
        command = commands.add_parser(name, help=("check a filled sheet without recording it" if dry_run
                                                  else "record a filled sheet; all-or-nothing"))
        command.add_argument("kind", choices=["labels", "answers"])
        command.add_argument("sheet", type=Path)
        places(command)
        if not dry_run:
            command.add_argument("--replace", action="store_true",
                                 help="supersede this reviewer's earlier judgement of the same content")
        command.set_defaults(func=lambda args, dry_run=dry_run: _human_import(args, dry_run))
    report = commands.add_parser("human-report", help="count reviewed/pending labels and score M7a answers")
    places(report)
    report.add_argument("--output", type=Path, default=human.HUMAN_DIR)
    report.set_defaults(func=_human_report)


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
    _add_human(commands)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
