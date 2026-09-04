# Citing-paper ranking

Ranking controls which citing papers consume full-text and model budget. It never removes incoming edges from `incoming-inventory.json` and never assigns final citation stance.

## Selection checkpoint

Use `stance_first` by default. If the user did not supply a positive `N`, recommend 20 and ask only for shortlist size through `user-interaction.md`. An existing `N` is confirmation; do not ask again. `influence_first` is opt-in.

## Target claim card

Build `target-claims.json` from `target-analysis-packet.json`. Give each discrete empirical claim a stable `claim_id`. Record population, intervention/exposure, outcome, direction, and target page when supported. Do not infer a claim from citing-paper metadata.

## Abstract triage

The deterministic prefilter reconstructs OpenAlex abstracts, scores explicit contrast/support/evidence cues plus claim-token overlap, and retains at most `max(3N, 60)` candidates. Missing abstracts remain eligible.

Load `triage-context.json` once, then analyze only `triage-pending.jsonl`. Each result preserves `citing_work_id` and `input_hash`:

```json
{
  "citing_work_id": "W123",
  "input_hash": "<exact packet hash>",
  "priority_score": 0.0,
  "priority_lane": "contrast|support|exploration"
}
```

- `priority_score` estimates how valuable the full text is for finding target-relevant supporting or contrasting evidence. Use 0–1; high topical similarity without evaluative evidence stays low.
- `contrast` covers explicit null results, failed replications, conflicting estimates, reanalyses, or qualifications.
- `support` covers successful replications or new aligned evidence.
- `exploration` covers missing/vague abstracts, mixed signals, and uncertain but potentially relevant papers. Missing metadata alone is not exclusion evidence.

Append valid results to `triage-results.jsonl`. The runner validates the hash, score, and lane; stale or invalid rows simply remain pending. `--rule-triage` bypasses this pass for diagnostics.

## Scoring and queues

Metadata score is:

```text
0.50 * normalized log1p(paper citation count)
+ 0.30 * normalized OpenAlex Source 2-year mean citedness
+ 0.20 * normalized publication year
```

The source metric is a keyless citation proxy, not Clarivate Journal Impact Factor. Never label it JIF or Impact Factor.

For `stance_first`, the runner combines abstract signals with a 10% metadata tie-breaker, then allocates approximately:

- 60% possible contrast;
- 25% possible support;
- 15% uncertain/title-only exploration.

Empty lanes are backfilled by overall score. The exploration lane prevents missing abstracts and unfamiliar terminology from being systematically discarded.

For `influence_first`, rank only by the metadata score. Preserve raw values and component scores in JSON; omit them from the default report.

Abstracts often omit citation markers. A high-priority candidate can become `mentioning` or `unknown`; only a target-bound full-text citation passage can receive final stance.
