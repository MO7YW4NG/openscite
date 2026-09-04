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

When `fulltext-requests.md` is non-empty, inspect prior messages for browser authorization. If the user has not already chosen a route and an interactive browser capability is available, use the capability-routing rule above to ask one question in the user's language: whether to use their current authenticated browser session, including institutional or VPN access, to download the listed papers. In a structured tool, offer:

1. **Use browser (recommended):** download authorized publisher PDFs and resume the run.
2. **Show links only:** present `fulltext-requests.md` for manual download or attachment.

State the number of pending papers. Explain that the browser will use the user's existing session and save local copies; the agent will not request or enter credentials. If no browser capability exists, go directly to the links-only route.

## Browser-assisted acquisition

Browser consent applies only to the pending papers named in `fulltext-requests.md`. After opt-in:

1. The parent agent owns browser acquisition and shared-file writes. Use the first browser automation capability already exposed by the harness, such as Codex `@Browser` or an equivalent tool, and reuse the user's existing browser profile and VPN state. Keep browser work in the parent by default so authorization, download mapping, and user handoff stay coherent.
2. Skip papers that already have a valid local PDF. Open the remaining DOI or article links in separate tabs, in bounded batches of at most four. Prefer the publisher's PDF or Download PDF control and reduce concurrency if a site signals throttling.
3. Wait until each tab exposes its intended download control, then trigger the ready downloads concurrently using the harness's parallel tool-call mechanism, such as `Promise.allSettled`. Capture each download's returned filename or path when available.
4. If a tab was still loading or its first click produced no file, refresh or retry that paper once in the next batch. Keep login, CAPTCHA, terms, VPN-change, purchase, or unavailable cases unresolved; continue independent tabs and collect any required user handoffs into one request after the batch. Never read, request, or enter credentials.
5. Verify the batch after downloads settle. Prefer returned download paths; when the browser does not expose them, compare one download-directory snapshot before and after the batch instead of rescanning after every click. Require `%PDF-`, hash files for duplicate detection, and copy valid files into `<run-dir>/fulltext/inbox/` under the browser-assigned filename. Preserve existing files and collision suffixes.
6. After all batches finish, rerun the same `prepare` command once. Continue the normal flow from its returned status and report any papers still unresolved.

Use sub-agents for link preflight and post-download parsing or stance analysis. Delegate browser batches only when the harness explicitly gives workers the same authorized browser session and disjoint tab handles; workers return download records while the parent verifies files and writes shared artifacts.

Do not install a browser integration as a hidden prerequisite. When browser automation is unavailable or declined, provide `fulltext-requests.md` and accept any legally accessible files by attachment or `fulltext/inbox/`.

## External upload or paid access

Before hosted OCR or paid access, state what leaves the local environment, destination, cost, and local alternative; proceed only after explicit approval.
