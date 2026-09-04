# Citation stance rubric

Classify each target-bound citation statement, not the citing paper as a whole. Stance is rhetorical/evidential function, not positive or negative sentiment.

## Labels

### `supporting`

The citing paper contributes its own observation, analysis, or result that supports a relevant claim from the target paper. Repeating the target's conclusion without new evidence is not enough.

### `contrasting`

The citing paper contributes its own observation, analysis, or result that conflicts with, qualifies, limits, or fails to replicate a relevant target claim.

### `mentioning`

The target is used for background, attribution, methods, comparison, or prior claims without the citing paper's own evidence supporting or contrasting that claim. Positive or negative wording can still be mentioning.

### `unknown`

Use when the target claim is unclear, context is truncated, marker binding is uncertain, required evidence is unavailable, or confidence is too low. Prefer unknown over a forced label.

## Procedure

Load shared target claims and the output contract from `analysis-context.json` once. Process only `analysis-pending.jsonl`, whose records group all pending statements from one citing paper. Keep a paper group on one worker so nearby passages can be interpreted consistently, but emit one result object per statement.

1. Confirm that the supplied marker/context refers to the uploaded target.
2. Identify the relevant `target_claim_id`; leave it null if no claim can be bound.
3. Determine whether the citing passage reports the citing paper's own evidence.
4. Compare that evidence with the target claim.
5. Return the exact `statement_id` and `context_hash`, stance, confidence from 0 to 1, one-sentence reason, `label_source: model`, and target claim ID.

A phrase such as “consistent with previous work” is not sufficient by itself. An abstract-screening hint, citation count, journal metric, S2 intent, or influence flag is never final stance evidence.

Append results to `analysis-results.jsonl`; never redo a statement absent from the pending file. The finalizer validates IDs and hashes against `citations.json`, then leaves omitted or invalid statements pending and unknown. Human corrections use `label_source: human`.

This rubric follows the broad distinction in [Scite's citation-classification guidance](https://help.researchsolutions.com/hc/en-us/articles/31949617584148-How-are-citations-classified).
