#!/usr/bin/env python3
"""Stable command-line interface for the OpenScite skill."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.openscite_core import PrepareConfig, finalize_run, prepare_run, read_json


def default_run_dir(target_pdf: Path) -> Path:
    slug = re.sub(r"[^a-z0-9]+", "-", target_pdf.stem.lower()).strip("-") or "paper"
    digest = hashlib.sha256(target_pdf.resolve().read_bytes()).hexdigest()[:8]
    return Path("artifacts") / "openscite" / f"{slug[:60]}-{digest}-incoming"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare and finalize a resumable incoming-citation stance analysis."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser(
        "prepare", help="Run deterministic stages and emit the next model task."
    )
    prepare.add_argument("target_pdf", type=Path)
    prepare.add_argument("--run-dir", type=Path)
    prepare.add_argument("--n", type=int)
    prepare.add_argument(
        "--mode", choices=("stance_first", "influence_first"), default="stance_first"
    )
    prepare.add_argument("--language", choices=("zh-TW", "en"), default="zh-TW")
    prepare.add_argument("--claims-file", type=Path)
    prepare.add_argument("--triage-results", type=Path)
    prepare.add_argument(
        "--rule-triage",
        action="store_true",
        help="Skip model abstract triage; useful only for fast diagnostics.",
    )
    prepare.add_argument("--no-download", action="store_true")
    prepare.add_argument("--workers", type=int, default=4)
    prepare.add_argument("--require-page-aware", action="store_true")
    prepare.add_argument("--timeout", type=int, default=120)
    prepare.add_argument("--mailto")

    finalize = subparsers.add_parser(
        "finalize", help="Validate model labels and render reports."
    )
    finalize.add_argument("--run-dir", required=True, type=Path)
    finalize.add_argument("--results", type=Path)

    status = subparsers.add_parser(
        "status", help="Print the current compact run record."
    )
    status.add_argument("--run-dir", required=True, type=Path)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            target_pdf = args.target_pdf.resolve(strict=True)
            run_dir = (args.run_dir or default_run_dir(target_pdf)).resolve()
            result = prepare_run(
                PrepareConfig(
                    target_pdf=target_pdf,
                    run_dir=run_dir,
                    n=args.n,
                    mode=args.mode,
                    language=args.language,
                    claims_file=args.claims_file,
                    triage_results=args.triage_results,
                    rule_triage=args.rule_triage,
                    download_fulltext=not args.no_download,
                    workers=args.workers,
                    require_page_aware=args.require_page_aware,
                    timeout=args.timeout,
                    mailto=args.mailto,
                )
            )
        elif args.command == "finalize":
            result = finalize_run(args.run_dir, args.results)
        else:
            result = read_json(args.run_dir.resolve(strict=True) / "run.json")
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        parser.exit(1, f"openscite: {exc}\n")
    sys.stdout.write(
        json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
