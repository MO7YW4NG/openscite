---
name: openscite
description: Analyze a scholarly PDF by finding papers that cite it, prioritizing likely supporting or contrasting evidence, retrieving authorized full text, and producing an evidence-linked citation-stance report.
---

# OpenScite

Analyze papers citing one user-provided scholarly PDF. The uploaded PDF is the target, not a source of references to follow. Preserve user filenames and write the report in the user's language while keeping titles and quoted passages in their source language. Use keyless OpenAlex.

## Use the resumable runner

Resolve this skill's directory as `<skill-dir>`. Run commands from the user's workspace so default artifacts are created there. Do not generate a substitute pipeline script.

```text
python "<skill-dir>/scripts/openscite.py" prepare "<target.pdf>" --language <zh-TW|en> [--n N] [--run-dir <dir>]
```

Make `prepare` the first PDF operation. It runs the latest local Anydoc release, caches the output, matches arbitrary inbox filenames by DOI/title, and falls back only when its artifacts record an extraction failure. Add `--require-page-aware` only when exact page locators matter; this intentionally uses the page-aware fallback. Use `--rule-triage` only for diagnostics.

Reuse the same target, run directory, `N`, mode, and language on every retry. Follow the returned status:

- `needs_user_selection`: create `target-claims.json` first if needed, then read `references/user-interaction.md`, ask only for shortlist size, and rerun with `--n N`. Suggest 20; reuse an existing positive `N` without asking.
- `needs_target_claims`: read `target-analysis-packet.json`, extract discrete claims from the target paper, write a JSON array to `target-claims.json`, and rerun. Each item must include `claim_id`, `claim`, and `source`; include population, intervention/exposure, outcome, direction, and page when supported. Do not ask the user for this internal analysis.
- `needs_abstract_triage`: read `references/ranking.md`, load `triage-context.json` once, and analyze only `triage-pending.jsonl`. When sub-agents are available, dispatch up to four disjoint batches in one parallel wave. Append results to `triage-results.jsonl`, preserving each ID/hash, then rerun `prepare`.
- `needs_analysis`: read `references/labeling.md`, load `analysis-context.json` once, and analyze only `analysis-pending.jsonl`. Keep each paper group on one worker; when sub-agents are available, dispatch up to four paper-group batches in one parallel wave. Append one result per statement to `analysis-results.jsonl`, preserving IDs/hashes and setting `label_source: model`, then run:

```text
python "<skill-dir>/scripts/openscite.py" finalize --run-dir "<run-dir>"
```

- `needs_user_files`: read `references/user-interaction.md`. When an interactive browser capability is available, ask once whether to use the user's current browser session (including institutional/VPN access) to download the authorized PDFs listed in `fulltext-requests.md`. On opt-in, use the bounded parallel browser procedure; otherwise present the links and accept attachments or files in `fulltext/inbox/`. Keep original filenames and rerun the same `prepare` command after files arrive.
- `partial` or `complete`: deliver `report.md` and briefly state coverage. `partial` is a valid evidence report when unavailable full text or unbound contexts are explicitly `unknown`.

Only pending rows are retried; valid result rows remain reusable. If `finalize` returns `needs_analysis`, process the regenerated pending file and rerun it.

If `fulltext-manifest.json` contains an ambiguous match, read `references/user-interaction.md` and ask one mapping question. Store the confirmed mapping in `file-mappings.json`:

```json
{"mappings":[{"file_name":"original-name.pdf","citing_work_id":"W123"}]}
```

Then rerun `prepare`. Sub-agents return records to the parent and do not ask users or edit shared artifacts.

## Evidence and access rules

Read `references/providers.md` for provider/OA issues, `references/fulltext-processing.md` for parsing or matching failures, and `references/output-schema.md` only when extending artifacts.

Use user-provided files and legitimate OA URLs. Validate PDF magic; obtain explicit consent before hosted OCR. Keep diagnostics and ranking internals out of `report.md`.

Assign stance per target-bound citation passage. Bibliography-only/topic-only hits and missing evidence stay `unknown`; rule output never becomes a model label.

## Completion check

Require `run.json`, `target.json`, `selection.json`, `citing-works.json`, `citations.json`, `fulltext-requests.md`, and `report.md`. Every non-`unknown` statement needs exact context, immutable hash, confidence, reason, and `label_source: model|human`. Report unique papers first and passages second, in the user's language and without debugging metadata.
