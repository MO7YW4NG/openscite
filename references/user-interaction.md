# User interaction

The parent agent asks users; sub-agents only return records. First inspect prior messages, CLI arguments, and pending artifacts so supplied answers are not requested again.

## Capability routing

Use the first available route:

1. A structured user-question capability exposed by the harness. In Codex, use `request_user_input` when available in the active mode. In Claude Code, use `AskUserQuestion` when available. In another harness, use a tool whose description explicitly says it asks or waits for user input.
2. If no structured tool exists but the session is interactive, ask one concise question in the user's language in the final response, then stop.
3. In a non-interactive run, retain the runner's pending status and report the missing CLI argument or input-file field.

## Shortlist size

State the discovered count and ask only for `N`. Recommend 20, explaining that ranking prioritizes likely evidence while retaining exploration. In choice tools put 20 first, followed by one smaller and one larger preset. Accept a positive free-form value up to the eligible count.

## Ambiguous file mapping

Show the original filename and the smallest useful candidate set with title plus year or DOI. Include an unmatched option and store the answer in `file-mappings.json` without renaming the file.

## Missing full text

Provide `fulltext-requests.md` and ask for any legally accessible files by attachment or `fulltext/inbox/`.

## External upload or paid access

Before hosted OCR or paid access, state what leaves the local environment, destination, cost, and local alternative; proceed only after explicit approval.
