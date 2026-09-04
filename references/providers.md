# Provider routing and keyless policy

The standard runner is local-parser-first, OpenAlex-first, and keyless-first:

```text
target PDF -> local identity extraction -> OpenAlex target + incoming edges
           -> OpenAlex OA locations -> local citing-paper parsing -> stance model
```

OpenAlex supplies metadata, incoming citation edges, OA location candidates, paper citation counts, and Source 2-year mean citedness. That Source metric is a journal citation proxy, not Clarivate Journal Impact Factor.

Semantic Scholar is outside the standard runner. A future optional adapter must not block the OpenAlex/local path when anonymous access is rate-limited.

## OpenAlex requests

Resolve a DOI before title search. Discover incoming edges with cursor pagination and the supported page size:

```text
GET https://api.openalex.org/works?filter=cites:<target-openalex-id>&per-page=100&cursor=*
```

Follow `meta.next_cursor` until no results or no next cursor. Do not substitute `referenced_works`; that is the opposite direction.

Request only needed Work fields: identity, title, publication year, citation count, authorships, abstract inverted index, primary/best OA locations, locations, and open-access metadata. Batch Source metric lookups only for the bounded triage pool.

Use bounded retries for `429` and transient `5xx`. Keep API timing/status in `run.json`; do not expose it in the human report.

## Full text

Treat `best_oa_location.pdf_url` and OA location URLs as candidates. Download in bounded parallel workers, require successful HTTP response and `%PDF-` magic, and retain source/license metadata. Do not bypass access controls.

OpenAlex cached full-text endpoints may require a key and incur cost; do not use them in keyless mode. If no direct OA PDF succeeds, give the user the best legal DOI/article link in `fulltext-requests.md`.

Downloaded papers keep their original copyright and license. Metadata availability is not permission to redistribute full text.

## Credentials

Keys are optional performance/access enhancements. Read them only from environment variables, never chat, prompts, logs, JSON, or reports. Do not ask the user for a key merely to resolve metadata.

Use the current [OpenAlex API documentation](https://help.openalex.org/api/) and [authentication guide](https://help.openalex.org/api/authentication/) as source of truth when behavior changes.
