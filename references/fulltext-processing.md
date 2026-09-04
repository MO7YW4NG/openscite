# Full-text parsing and compact analysis

The runner owns parsing, caching, and marker binding. Model workers receive compact citation windows, never whole PDFs when deterministic extraction succeeds.

## Parser routing

For ordinary text PDFs, `scripts/openscite.py prepare` uses the latest local Anydoc release when Node.js 20+ and `npx` are available:

```text
npx --yes @firecrawl/anydoc <input.pdf> --ocr reject
```

The same Anydoc path handles target inspection, arbitrary-filename inbox matching, and selected-paper parsing. Inbox files are parsed concurrently and cached by content hash; after a match, full-text analysis reuses that extraction instead of invoking Anydoc again.

The runner records the parser, source and output hashes, stderr, elapsed time, page awareness, and cache path in `fulltext-manifest.json` and `fulltext-parse.json`. Quality failure falls back to `pdftotext -layout`. When good Anydoc output lacks target-reference evidence, the runner may probe Poppler for recall, but switches only if Poppler finds the reference; otherwise Anydoc remains the canonical extraction.

Anydoc output does not preserve dependable PDF page boundaries. With `--require-page-aware`, the parser plan skips Anydoc and goes directly to Poppler, avoiding work that would be discarded. HTML and JATS/XML are parsed structurally.

Each successful document parse has a source/config fingerprint. Repeating a run, or adding one new user file, reuses unchanged parse results. Parser concurrency is bounded by `--workers` (default 4, maximum 8).

Anydoc exit 3 means OCR is required. Keep `--ocr reject` as the default. Hosted OCR uploads the document; request explicit user consent first. Prefer local OCR or a searchable PDF/HTML/JATS copy in keyless-local mode.

## Context binding

Resolve the target bibliography entry, infer its author-year, numeric, or DOI marker, then search only the citing paper's body. For works with three or more authors, use the first-author surname rather than every coauthor as an author-year alias. Recover numbered entries from Anydoc Markdown tables and from documents whose References heading was lost. Capture the marker sentence plus adjacent sentences or paragraph, preserving source text verbatim; collapse identical or nested windows for the same marker. Retain page or section evidence when available.

Bibliography-only and topic-only hits are not citation contexts. A readable extraction can still have no reliably bound marker; record `no_context_found`, not `mentioning`.

## Parallel model work

When sub-agents are available, use disjoint batches for:

1. `triage-context.json` plus `triage-pending.jsonl`: title/abstract priority screening;
2. `analysis-context.json` plus paper-grouped `analysis-pending.jsonl`: final statement-level stance classification.

Pending files contain only missing or invalid work; `citations.json` is the analysis ledger. The parent writes shared files and validates returned IDs and hashes. Workers return records only. If sub-agents are unavailable, process the same compact pending records in bounded sequential batches.

Do not ask sub-agents to reread complete PDFs, repeat API pagination, or edit `citations.json`, `citing-works.json`, or `report.md`.

OpenScite follows the current npm release. Consult the [Anydoc repository](https://github.com/firecrawl/anydoc) for current requirements and limitations.
