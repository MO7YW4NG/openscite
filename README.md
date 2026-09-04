# OpenScite

> Give your agent one scholarly PDF. Get a ranked, evidence-linked map of how later papers support, contrast with, or mention it.

OpenScite is an open-source [Agent Skill](https://agentskills.io/) for incoming-citation analysis. It resolves the paper represented by the PDF, finds works that cite it, prioritizes the citing papers most likely to contain useful evidence, retrieves legally accessible full text, extracts the exact citation passages, and writes the report in the user's language.

The standard workflow is local-first and keyless-first: [OpenAlex](https://openalex.org/) supplies the citation graph and metadata, while the latest [Anydoc](https://github.com/firecrawl/anydoc) CLI parses text-based PDFs locally. Semantic Scholar is not required or used by the standard runner.

## Why another citation tool?

A citation count tells you that later papers cited a work, but not what they said about it. Searching every citing paper manually is slow, and ranking only by citation count tends to bury replications, null results, and direct critiques under highly cited background mentions.

OpenScite uses a two-stage process:

1. Rank incoming citations using target claims, title/abstract evidence cues, paper citation count, publication year, and an OpenAlex journal-level citation proxy.
2. Assign stance only after binding the target paper to a citation passage in the citing paper's full text.

The final labels apply to individual citation passages—not to an entire paper:

| Label | Meaning |
| --- | --- |
| `supporting` | Reports evidence that aligns with or replicates a target claim. |
| `contrasting` | Reports conflicting evidence, a failed replication, null result, reanalysis, or substantive qualification. |
| `mentioning` | Uses the target as background or attribution without evaluating its claims. |
| `unknown` | Full text, target binding, or evidence is insufficient for a reliable label. |

## Workflow

```text
target PDF
  -> identify the paper and extract its claims
  -> find incoming citations with OpenAlex
  -> prioritize likely contrast/support candidates from abstracts
  -> select top N papers
  -> retrieve OA full text, optionally use an authorized browser session,
     or request user-provided files
  -> parse and bind exact citation passages
  -> classify each passage and render report.md
```

The agent handles this as a resumable workflow. It asks for the shortlist size only when the user has not supplied one, recommends `N=20`, and accepts citing-paper files under their original filenames. If direct open-access retrieval fails, the agent can ask to use an available browser automation tool with the user's current institutional or VPN session. Approved browser downloads run in bounded parallel tab batches; the user can decline and use the DOI or article links in `fulltext-requests.md` instead.

Completed metadata, PDF conversions, abstract triage, and stance labels are cached. Rerunning the same analysis processes only missing or invalid work.

## Install

The recommended command installs OpenScite once at user scope through the [Skills CLI](https://skills.sh/docs/cli):

```bash
npx skills add MO7YW4NG/openscite -g
```

### Developing from a local clone

The repository root is the skill package, so this command should list `openscite`:

```bash
npx skills add . --list
```

Do not install `.` at project scope while standing inside this source repository: the destination would be nested under its own source. Install globally during development, or install from a separate consumer project:

```bash
npx skills add . -g
```

## Use

Attach or reference a scholarly PDF and explicitly ask your agent to use OpenScite. For example:

```text
Use OpenScite to analyze ./paper.pdf. Prioritize 20 citing papers that are
most likely to support or contrast with its main empirical claims.
```

You can omit the number and let the agent ask once, or choose an alternate ranking mode:

| Mode | Best for | Ranking behavior |
| --- | --- | --- |
| `stance_first` | Finding replications, conflicts, and direct evaluations | Default. Prioritizes abstract evidence signals, with metadata as a tie-breaker and an exploration lane for missing or uncertain abstracts. |
| `influence_first` | Surveying the most influential citing literature | Ranks by paper citations, OpenAlex Source 2-year mean citedness, and publication year. |

The source metric is a citation proxy, not Clarivate Journal Impact Factor. OpenScite never reports it as JIF or Impact Factor.

## Output

The run directory defaults to `artifacts/openscite/<paper-name>/` and includes:

| File | Purpose |
| --- | --- |
| `report.md` | Concise user-language result, grouped by citing paper and stance. |
| `fulltext-requests.md` | Legal download links for selected papers whose full text is still unavailable. |
| `citations.json` | Passage-level evidence, immutable context hashes, labels, confidence, and reasons. |
| `citing-works.json` | Selected citing papers and their acquisition/context status. |
| `run.json` | Compact workflow status, coverage counts, and resumable stage state. |

Titles and quoted passages remain in their source language. Provider diagnostics, parser internals, and ranking scores stay out of the human-facing report.

## Keyless and local-first behavior

- Basic OpenAlex queries can run without an API key, although anonymous use has a smaller daily budget than a free authenticated account. See the current [OpenAlex authentication policy](https://help.openalex.org/api/authentication/).
- Anydoc runs locally as the current `@firecrawl/anydoc` release with `--ocr reject`. `npx` downloads its platform binary on first use; ordinary PDF contents are not sent to Firecrawl.
- OpenScite downloads only legitimate open-access candidates and validates that a response is a PDF. It does not bypass paywalls or use paid OpenAlex content endpoints.
- Browser-assisted publisher downloads are opt-in. They use the user's existing authenticated session or institutional VPN and run in batches of up to four tabs. The agent does not request or enter credentials; blocked papers remain unresolved for user handoff.
- Scanned or image-only PDFs are not uploaded for OCR automatically. Hosted OCR requires explicit user consent; a searchable PDF, HTML/JATS version, or local OCR is preferred.
- User-provided citing papers keep their filenames and are matched by DOI/title. They are not renamed or moved.

## Requirements

The Skills CLI installs the skill files, not these runtime dependencies:

| Requirement | Role |
| --- | --- |
| Python 3.11+ | Required for the resumable runner. Runtime code uses the standard library. |
| Internet access | Required for OpenAlex metadata and open-access downloads. |
| Node.js 20+ and `npx` | Recommended default PDF path; runs the latest local Anydoc package. |
| Poppler (`pdftotext`, `pdfinfo`) | Optional fallback; required for dependable page-aware extraction. |
| Browser automation | Optional; can retrieve authorized publisher PDFs through the user's existing institutional/VPN session. |

No permanent parser API key is required.

## Limitations

- Abstract triage predicts which papers are worth opening; it does not assign the final stance.
- A readable paper can still return `unknown` when its in-text citation cannot be reliably bound to the target.
- Full-text coverage depends on legitimate OA availability, authorized browser access, or files supplied by the user.
- One citing paper can contain multiple passages with different labels.
- Exact page locators require `--require-page-aware`, which intentionally uses the Poppler path instead of Anydoc.

## Development

The agent-facing skill calls one deterministic runner rather than generating an ad hoc pipeline script:

```bash
python scripts/openscite.py prepare ./paper.pdf --n 20 --language zh-TW
python scripts/openscite.py status --run-dir ./artifacts/openscite/<run>
python scripts/openscite.py finalize --run-dir ./artifacts/openscite/<run>
```

`prepare` may stop with a next action for the agent: extract target claims, triage abstracts, request missing full text, or classify pending citation passages. The same command resumes from cached artifacts after that work is supplied.

Run the regression suite:

```bash
python -m unittest discover -s tests -v
```

## Layout

```text
openscite/
├── SKILL.md                 # canonical Agent Skill instructions
├── agents/openai.yaml       # OpenAI/Codex skill metadata
├── scripts/
│   ├── openscite.py         # only CLI entrypoint
│   ├── openscite_core.py    # resumable workflow and validation
│   ├── documents.py         # local parsing, matching, and extraction
│   └── render_report.py     # concise Markdown report rendering
├── references/              # ranking, labeling, providers, and artifact contracts
└── tests/                   # deterministic regression suite
```

## License

[MIT](LICENSE)
