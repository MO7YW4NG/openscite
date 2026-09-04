# OpenScite artifact contract

All JSON and Markdown use UTF-8. The direction is `citing paper -> uploaded target`. Never embed API keys or complete paper text in JSON/Markdown.

## Runner states

`run.json` uses `openscite.run.v3` and one status:

- `needs_user_selection`: supply positive `--n`;
- `needs_target_claims`: create `target-claims.json`;
- `needs_abstract_triage`: process `triage-pending.jsonl` and append to `triage-results.jsonl`;
- `needs_analysis`: process `analysis-pending.jsonl`, append to `analysis-results.jsonl`, then finalize;
- `needs_user_files`: attach requested full text and rerun prepare;
- `partial`: report exists but explicit unknown cases remain;
- `complete`: every selected statement is finalized.

It preserves the original `started_at` across retries, increments `invocation_count`, and records the current invocation's stage cache hits and elapsed times. Provider diagnostics stay here, not in `report.md`.

## Deterministic artifacts

### `target.json`

`openscite.target.v2` contains the unchanged input path/hash/page count/parser, local identity evidence, resolved OpenAlex identity, and the target claim card.

### `incoming-inventory.json`

`openscite.incoming-inventory.v1` preserves the complete incoming OpenAlex inventory before shortlist selection. Self-edges are retained here for audit and excluded during ranking.

### `selection.json`

`openscite.selection.v2` records pending or confirmed state, requested `N`, ranking mode, candidate-pool size, selected IDs, abstract-triage source, self-edge count, and queue coverage. A pending selection must not trigger citing-paper downloads.

### `citing-works.json`

`openscite.citing-works.v2` contains selected works only; the complete inventory remains in `incoming-inventory.json`. Every work includes:

- `citing_work_id`, `resolved`, `selection`, and `ranking_metadata`;
- `abstract_triage`, explicitly separated from final stance;
- `full_text.candidate_urls`, local path/acquisition when present, and download attempts;
- `context_status`: `context_bound`, `no_context_found`, `awaiting_user_full_text_and_context`, or `extraction_failed`.

### `fulltext-manifest.json`

`openscite.fulltext-manifest.v2` records original filenames, hashes, DOI/title matching evidence, Anydoc-first identity-extraction attempts/cache metadata, and `matched|ambiguous|unmatched`. It never renames files. A unique exact DOI match overrides weaker title candidates. Confirm genuinely ambiguous mappings in `file-mappings.json`:

```json
{"mappings":[{"file_name":"original.pdf","citing_work_id":"W123"}]}
```

### `fulltext-parse.json`

`openscite.fulltext-parse.v2` records parser/version, input/output hashes, attempts, elapsed time, page awareness, target-reference gate, cache hit, and `parsed|failed` status. `target_reference_found` is an extraction-integrity signal, not a stance.

### `citations.json`

`openscite.citations.v3` contains one record per bound citation context, or one explicit unknown record per selected work with no context:

```json
{
  "statement_id": "W123-stmt-01",
  "citing_work_id": "W123",
  "citing_paper": {"citing_work_id": "W123", "title": "...", "doi": "..."},
  "citation_marker": "[9]",
  "context_text": "verbatim passage",
  "context_hash": "sha256:...",
  "page": 4,
  "section": "Results",
  "binding_method": "bibliography_numeric",
  "target_claim_id": "claim-01",
  "stance": "supporting|contrasting|mentioning|unknown",
  "confidence": 0.9,
  "reason": "short evidence-based explanation",
  "label_source": "model|human|rule"
}
```

Only `unknown` placeholders may retain `label_source: rule`. Every non-unknown stance requires validated model or human output.

## Model handoff files

### `target-analysis-packet.json` -> `target-claims.json`

The packet contains target metadata and a bounded target-text excerpt. Output a non-empty JSON array with `claim_id`, `claim`, and `source`; add structured claim fields when supported.

### Triage handoff

`triage-context.json` stores the shared target and output contract. `triage-pending.jsonl` contains only citing-paper title/abstract, ID, and immutable input hash. Append `priority_score` and `priority_lane` results to `triage-results.jsonl`; stale or invalid rows remain pending on the next run.

### Analysis handoff

`analysis-context.json` stores shared target claims, task, and output fields. `analysis-pending.jsonl` groups unfinished statements by citing paper. `citations.json` is the complete ledger. Emit one result object per statement and append it to `analysis-results.jsonl`:

Return one object per analyzed statement:

```json
{
  "statement_id": "W123-stmt-01",
  "context_hash": "sha256:...",
  "stance": "contrasting",
  "confidence": 0.91,
  "reason": "The citing study reports a target-relevant null result.",
  "label_source": "model",
  "target_claim_id": "claim-01"
}
```

The finalizer rejects changed context hashes, invalid labels/confidence, missing reasons, and invented statement IDs. It preserves valid prior results and rewrites `analysis-pending.jsonl` so retries process only unfinished statements.

## User-facing artifacts

`report.md` uses the user's language and presents target/coverage, contrasting and supporting evidence first, mentioning compactly, unresolved counts, and the statement-level caveat. Stance totals count unique citing papers first and citation passages second; repeated passages from one paper share one paper entry. It omits provider IDs, `is_oa`, API pagination, parser diagnostics, cache paths, and raw ranking weights.

`fulltext-requests.md` contains selected missing works only, with title, year, and best legal link. It tells the user to keep original filenames.
