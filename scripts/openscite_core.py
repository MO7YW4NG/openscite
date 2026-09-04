"""Deterministic, resumable core for the OpenScite agent skill.

The harness owns language-model work.  This module owns metadata I/O, ranking,
full-text acquisition/parsing, citation binding, cache invalidation, and
artifact validation.  The split keeps prompts compact and makes retries cheap.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Protocol

from scripts.documents import (
    DocumentConfig,
    PdfInspector,
    extract_fulltexts,
    file_sha256,
    match_inbox,
    normalize_doi,
)
from scripts.render_report import render_artifacts


RUN_SCHEMA = "openscite.run.v3"
PIPELINE_VERSION = "2026-09-04.4"
OPENALEX_FIELDS = (
    "id,doi,display_name,title,publication_year,publication_date,cited_by_count,"
    "authorships,abstract_inverted_index,primary_location,best_oa_location,locations,open_access"
)
VALID_STANCES = {"supporting", "contrasting", "mentioning", "unknown"}
VALID_LABEL_SOURCES = {"model", "human"}
DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)
TOKEN_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)
STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "in",
    "is",
    "of",
    "on",
    "or",
    "that",
    "the",
    "this",
    "to",
    "was",
    "were",
    "with",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def hash_value(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def hash_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def read_jsonl(path: Path) -> list[dict]:
    records = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8-sig").splitlines(), 1
    ):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"{path}:{line_number} must contain a JSON object")
        records.append(value)
    return records


def write_jsonl(path: Path, records: Iterable[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = "".join(
        json.dumps(record, ensure_ascii=False) + "\n" for record in records
    )
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(rendered, encoding="utf-8")
    temporary.replace(path)


def short_openalex_id(value: str | None) -> str | None:
    if not value:
        return None
    return value.rstrip("/").rsplit("/", 1)[-1]


def title_tokens(value: str | None) -> set[str]:
    return {
        token
        for token in TOKEN_RE.findall((value or "").lower())
        if token not in STOPWORDS
    }


def reconstruct_abstract(index: dict[str, list[int]] | None) -> str | None:
    if not index:
        return None
    positions = [
        (position, word) for word, values in index.items() for position in values
    ]
    positions.sort()
    return " ".join(word for _, word in positions) or None


class StageCache:
    """Small persistent cache keyed by explicit stage fingerprints."""

    def __init__(self, cache_dir: Path) -> None:
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.cache_dir / "stage-state.json"
        try:
            self.state = read_json(self.state_path)
        except (OSError, json.JSONDecodeError):
            self.state = {"schema_version": "openscite.stage-cache.v1", "stages": {}}

    def is_hit(self, stage: str, fingerprint: str, artifacts: Iterable[Path]) -> bool:
        record = self.state.get("stages", {}).get(stage) or {}
        return record.get("fingerprint") == fingerprint and all(
            path.exists() for path in artifacts
        )

    def record(
        self,
        stage: str,
        fingerprint: str,
        artifacts: Iterable[Path],
        elapsed_seconds: float,
    ) -> None:
        self.state.setdefault("stages", {})[stage] = {
            "fingerprint": fingerprint,
            "completed_at": utc_now(),
            "elapsed_seconds": round(elapsed_seconds, 3),
            "artifacts": [str(path.resolve()) for path in artifacts],
        }
        write_json(self.state_path, self.state)


class MetadataProvider(Protocol):
    def resolve_target(self, identity: dict) -> dict: ...

    def incoming_citations(self, target_id: str) -> list[dict]: ...

    def source_metrics(self, source_ids: list[str]) -> dict[str, dict]: ...


class DocumentInspector(Protocol):
    def inspect(self, pdf_path: Path, cache_dir: Path) -> dict: ...


class OpenAlexProvider:
    """Keyless-first OpenAlex adapter with bounded retries and cursor paging."""

    base_url = "https://api.openalex.org"
    cache_key = "openalex-v1"

    def __init__(self, mailto: str | None = None, timeout: int = 30) -> None:
        self.mailto = mailto
        self.timeout = timeout
        self.diagnostics: list[dict] = []

    def _get(self, endpoint: str, params: dict[str, Any]) -> dict:
        clean = {key: value for key, value in params.items() if value is not None}
        if self.mailto:
            clean["mailto"] = self.mailto
        url = f"{self.base_url}{endpoint}?{urllib.parse.urlencode(clean)}"
        last_error: Exception | None = None
        for attempt in range(3):
            started = time.perf_counter()
            request = urllib.request.Request(
                url,
                headers={"Accept": "application/json", "User-Agent": "OpenScite/0.1"},
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                self.diagnostics.append(
                    {
                        "provider": "openalex",
                        "endpoint": endpoint,
                        "status": 200,
                        "elapsed_seconds": round(time.perf_counter() - started, 3),
                    }
                )
                return payload
            except urllib.error.HTTPError as exc:
                last_error = exc
                self.diagnostics.append(
                    {
                        "provider": "openalex",
                        "endpoint": endpoint,
                        "status": exc.code,
                        "elapsed_seconds": round(time.perf_counter() - started, 3),
                    }
                )
                if exc.code not in {429, 500, 502, 503, 504} or attempt == 2:
                    break
                retry_after = exc.headers.get("Retry-After")
                delay = (
                    min(float(retry_after), 5.0)
                    if retry_after and retry_after.isdigit()
                    else 0.5 * (2**attempt)
                )
                time.sleep(delay)
            except (OSError, json.JSONDecodeError) as exc:
                last_error = exc
                if attempt == 2:
                    break
                time.sleep(0.5 * (2**attempt))
        raise RuntimeError(f"OpenAlex request failed: {endpoint}: {last_error}")

    def resolve_target(self, identity: dict) -> dict:
        doi = normalize_doi(identity.get("doi"))
        if doi:
            encoded = urllib.parse.quote(f"https://doi.org/{doi}", safe="")
            try:
                return self._get(f"/works/{encoded}", {"select": OPENALEX_FIELDS})
            except RuntimeError:
                pass
        title = identity.get("title")
        if not title:
            raise ValueError("Could not extract a DOI or title from the target PDF")
        payload = self._get(
            "/works",
            {"search": title, "per_page": 10, "select": OPENALEX_FIELDS},
        )
        candidates = payload.get("results") or []
        wanted = title_tokens(title)
        if not candidates:
            raise ValueError(f"OpenAlex could not resolve target title: {title}")
        wanted_year = identity.get("publication_year")
        wanted_surnames = {
            re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ'-]+", name)[-1].lower()
            for name in (identity.get("authors") or [])
            if re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ'-]+", name)
        }

        def match_score(work: dict) -> float:
            coverage = len(wanted & title_tokens(work.get("display_name"))) / max(
                len(wanted), 1
            )
            year_score = (
                1.0
                if wanted_year and work.get("publication_year") == wanted_year
                else 0.0
            )
            candidate_surnames = {
                (
                    ((item.get("author") or {}).get("display_name") or "").split()
                    or [""]
                )[-1].lower()
                for item in (work.get("authorships") or [])
            }
            author_score = len(wanted_surnames & candidate_surnames) / max(
                len(wanted_surnames), 1
            )
            return 0.80 * coverage + 0.10 * year_score + 0.10 * author_score

        best = max(candidates, key=match_score)
        coverage = len(wanted & title_tokens(best.get("display_name"))) / max(
            len(wanted), 1
        )
        if coverage < 0.65:
            raise ValueError(
                f"OpenAlex title match is too weak ({coverage:.2f}) for target: {title}"
            )
        return best

    def incoming_citations(self, target_id: str) -> list[dict]:
        cursor = "*"
        results: list[dict] = []
        while cursor:
            payload = self._get(
                "/works",
                {
                    "filter": f"cites:{short_openalex_id(target_id)}",
                    "per_page": 100,
                    "cursor": cursor,
                    "select": OPENALEX_FIELDS,
                },
            )
            results.extend(payload.get("results") or [])
            next_cursor = (payload.get("meta") or {}).get("next_cursor")
            if not next_cursor or next_cursor == cursor or not payload.get("results"):
                break
            cursor = next_cursor
        deduplicated = {}
        for work in results:
            key = short_openalex_id(work.get("id")) or normalize_doi(work.get("doi"))
            if key:
                deduplicated[key] = work
        return list(deduplicated.values())

    def source_metrics(self, source_ids: list[str]) -> dict[str, dict]:
        metrics: dict[str, dict] = {}
        unique_ids = sorted({short_openalex_id(value) for value in source_ids if value})
        for offset in range(0, len(unique_ids), 40):
            chunk = unique_ids[offset : offset + 40]
            if not chunk:
                continue
            payload = self._get(
                "/sources",
                {
                    "filter": "openalex_id:" + "|".join(chunk),
                    "per_page": len(chunk),
                    "select": "id,display_name,issn_l,summary_stats,works_count,cited_by_count",
                },
            )
            for source in payload.get("results") or []:
                metrics[source.get("id")] = source
                metrics[short_openalex_id(source.get("id"))] = source
        return metrics


def _first_author_name(work: dict) -> str | None:
    authorships = work.get("authorships") or []
    if not authorships:
        return None
    return (authorships[0].get("author") or {}).get("display_name")


def _work_abstract(work: dict) -> str | None:
    return work.get("abstract") or reconstruct_abstract(
        work.get("abstract_inverted_index")
    )


def _location_source(work: dict) -> dict:
    return (work.get("primary_location") or {}).get("source") or {}


def _candidate_urls(work: dict) -> list[dict]:
    candidates: list[dict] = []
    seen: set[str] = set()
    locations = [work.get("best_oa_location"), *(work.get("locations") or [])]
    for location in locations:
        if not location:
            continue
        for kind, field in (("oa_pdf", "pdf_url"), ("oa_fulltext", "landing_page_url")):
            url = location.get(field)
            if not url or url in seen:
                continue
            if kind == "oa_pdf" and not (
                location.get("is_oa") or work.get("open_access", {}).get("is_oa")
            ):
                continue
            seen.add(url)
            candidates.append(
                {
                    "kind": kind,
                    "url": url,
                    "license": location.get("license"),
                    "source": "openalex",
                }
            )
    doi = normalize_doi(work.get("doi"))
    if doi:
        candidates.append(
            {"kind": "doi", "url": f"https://doi.org/{doi}", "source": "doi"}
        )
    return candidates


def normalize_work(work: dict) -> dict:
    source = _location_source(work)
    work_id = short_openalex_id(work.get("id") or work.get("openalex_id"))
    title = work.get("display_name") or work.get("title") or "Untitled"
    doi = normalize_doi(work.get("doi"))
    source_id = source.get("id") or (work.get("ranking_metadata") or {}).get(
        "source_id"
    )
    return {
        "openalex_id": work_id,
        "citing_work_id": work_id,
        "doi": doi,
        "title": title,
        "publication_year": work.get("publication_year"),
        "cited_by_count": work.get("cited_by_count") or 0,
        "first_author": _first_author_name(work),
        "abstract": _work_abstract(work),
        "source_id": source_id,
        "source_name": source.get("display_name"),
        "source_issn_l": source.get("issn_l"),
        "candidate_urls": _candidate_urls(work),
        "raw": work,
    }


CONTRAST_CUES = {
    "contradict",
    "contradicted",
    "contrary",
    "failed",
    "failure",
    "inconsistent",
    "no evidence",
    "not replicate",
    "null effect",
    "reanalysis",
    "re-examination",
    "questioned",
    "challenge",
    "overestimated",
    "did not support",
}
SUPPORT_CUES = {
    "consistent with",
    "confirmed",
    "corroborated",
    "replicated",
    "replication",
    "supported",
    "supports",
    "robust",
    "reproduced",
    "converging evidence",
}
EVIDENCE_CUES = {
    "experiment",
    "experimental",
    "data",
    "results",
    "sample",
    "meta-analysis",
    "systematic review",
    "estimate",
    "effect size",
    "analysis",
    "study",
}


def _cue_score(text: str, cues: set[str]) -> float:
    lowered = text.lower()
    hits = sum(1 for cue in cues if cue in lowered)
    return min(1.0, hits / 2.0)


def rule_screen(work: dict, claims: list[dict], target: dict) -> dict:
    text = " ".join(filter(None, [work.get("title"), work.get("abstract")]))
    claim_text = " ".join(str(item.get("claim") or "") for item in claims)
    target_text = " ".join(
        filter(None, [target.get("title"), target.get("display_name"), claim_text])
    )
    wanted = title_tokens(target_text)
    present = title_tokens(text)
    relevance = len(wanted & present) / max(min(len(wanted), 12), 1)
    contrast = _cue_score(text, CONTRAST_CUES)
    support = _cue_score(text, SUPPORT_CUES)
    evidence = _cue_score(text, EVIDENCE_CUES)
    if not work.get("abstract"):
        uncertainty = 1.0
        evidence *= 0.5
    else:
        uncertainty = max(
            0.1, 1.0 - max(contrast, support, evidence, min(relevance, 1.0))
        )
    relationship = "unclear"
    if contrast > support and contrast >= 0.5:
        relationship = "possible_contrast"
    elif support > contrast and support >= 0.5:
        relationship = "possible_support"
    elif relevance >= 0.25:
        relationship = "likely_mention"
    return {
        "claim_relevance": round(min(relevance, 1.0), 4),
        "contrast_signal": round(contrast, 4),
        "support_signal": round(support, 4),
        "evidence_signal": round(evidence, 4),
        "uncertainty": round(uncertainty, 4),
        "relationship_hint": relationship,
        "screen_source": "title_abstract_rule",
    }


def _minmax(values: list[float]) -> list[float]:
    if not values:
        return []
    low, high = min(values), max(values)
    if math.isclose(low, high):
        return [0.5 for _ in values]
    return [(value - low) / (high - low) for value in values]


def _source_metric(source_metrics: dict[str, dict], source_id: str | None) -> float:
    if not source_id:
        return 0.0
    source = (
        source_metrics.get(source_id)
        or source_metrics.get(short_openalex_id(source_id))
        or {}
    )
    stats = source.get("summary_stats") or {}
    return float(stats.get("2yr_mean_citedness") or 0.0)


def rank_candidates(
    works: list[dict],
    target: dict,
    claims: list[dict],
    n: int,
    mode: str,
    source_metrics: dict[str, dict],
    model_screens: dict[str, dict],
) -> tuple[list[dict], dict]:
    """Rank citing works without treating an abstract hint as final stance."""
    target_id = short_openalex_id(target.get("id") or target.get("openalex_id"))
    target_doi = normalize_doi(target.get("doi"))
    candidates = []
    self_edges = 0
    for raw in works:
        work = normalize_work(raw)
        if (target_id and work["openalex_id"] == target_id) or (
            target_doi and work["doi"] == target_doi
        ):
            self_edges += 1
            continue
        screen = rule_screen(work, claims, target)
        model = model_screens.get(work["openalex_id"] or "")
        if model:
            screen = {
                **screen,
                "priority_score": model["priority_score"],
                "relationship_hint": {
                    "contrast": "possible_contrast",
                    "support": "possible_support",
                    "exploration": "unclear",
                }[model["priority_lane"]],
                "screen_source": "title_abstract_model",
            }
        work["abstract_triage"] = screen
        candidates.append(work)

    citation_values = [
        math.log1p(max(0, work["cited_by_count"])) for work in candidates
    ]
    source_values = [
        _source_metric(source_metrics, work["source_id"]) for work in candidates
    ]
    years = [float(work["publication_year"] or 0) for work in candidates]
    citation_scores = _minmax(citation_values)
    source_scores = _minmax(source_values)
    recency_scores = _minmax(years)

    for index, work in enumerate(candidates):
        screen = work["abstract_triage"]
        metadata_score = (
            0.50 * citation_scores[index]
            + 0.30 * source_scores[index]
            + 0.20 * recency_scores[index]
        )
        stance_score = (
            float(screen.get("priority_score"))
            if "priority_score" in screen
            else (
                0.60 * float(screen.get("contrast_signal") or 0)
                + 0.25 * float(screen.get("support_signal") or 0)
                + 0.10 * float(screen.get("evidence_signal") or 0)
                + 0.05 * float(screen.get("uncertainty") or 0)
            )
        )
        score = (
            0.90 * stance_score + 0.10 * metadata_score
            if mode == "stance_first"
            else metadata_score
        )
        work["ranking_metadata"] = {
            "publication_year": work["publication_year"],
            "citation_count": work["cited_by_count"],
            "source_id": work["source_id"],
            "source_name": work["source_name"],
            "source_issn_l": work["source_issn_l"],
            "journal_proxy_2yr_mean_citedness": source_values[index] or None,
            "metadata_score": round(metadata_score, 6),
            "stance_priority_score": round(stance_score, 6),
        }
        work["score"] = round(score, 6)

    candidates.sort(
        key=lambda item: (
            -item["score"],
            -item["cited_by_count"],
            -(item["publication_year"] or 0),
            item["openalex_id"] or "",
        )
    )

    queue_counts = {"contrast": 0, "support": 0, "exploration": 0, "backfill": 0}
    if mode == "stance_first" and n >= 2:
        contrast_queue = [
            item
            for item in candidates
            if item["abstract_triage"]["relationship_hint"] == "possible_contrast"
        ]
        support_queue = [
            item
            for item in candidates
            if item["abstract_triage"]["relationship_hint"] == "possible_support"
        ]
        exploration_queue = [
            item
            for item in candidates
            if item["abstract_triage"]["relationship_hint"]
            not in {"possible_contrast", "possible_support"}
        ]
        contrast_budget = max(1, int(n * 0.60))
        support_budget = max(1, int(n * 0.25))
        if contrast_budget + support_budget > n:
            contrast_budget = max(1, n - support_budget)
        exploration_budget = max(0, n - contrast_budget - support_budget)
        selected = [
            *contrast_queue[:contrast_budget],
            *support_queue[:support_budget],
            *exploration_queue[:exploration_budget],
        ]
        selected_ids = {item["openalex_id"] for item in selected}
        queue_counts.update(
            {
                "contrast": min(len(contrast_queue), contrast_budget),
                "support": min(len(support_queue), support_budget),
                "exploration": min(len(exploration_queue), exploration_budget),
            }
        )
        for item in candidates:
            if len(selected) >= n:
                break
            if item["openalex_id"] not in selected_ids:
                selected.append(item)
                selected_ids.add(item["openalex_id"])
                queue_counts["backfill"] += 1
    else:
        selected = candidates[: max(0, n)]
    for rank, work in enumerate(selected, 1):
        work["selection"] = {
            "selected": True,
            "rank": rank,
            "score": work["score"],
            "ranking_mode": mode,
        }
        work["resolved"] = {
            "title": work["title"],
            "doi": f"https://doi.org/{work['doi']}" if work["doi"] else None,
            "openalex_id": work["openalex_id"],
            "authors": [work["first_author"]] if work["first_author"] else [],
        }
        work["full_text"] = {
            "candidate_urls": work["candidate_urls"],
            "local_path": None,
            "acquisition": None,
        }
        for key in (
            "raw",
            "score",
            "doi",
            "title",
            "abstract",
            "publication_year",
            "cited_by_count",
            "first_author",
            "source_id",
            "source_name",
            "source_issn_l",
            "candidate_urls",
        ):
            work.pop(key, None)
    return selected, {
        "self_edges_excluded": self_edges,
        "eligible_count": len(candidates),
        "selected_count": len(selected),
        "queue_counts": queue_counts,
    }


def extract_target_identity(text: str) -> dict:
    head = text[:80_000]
    front = head[:20_000]
    doi_match = next(
        (
            DOI_RE.search(line)
            for line in front.splitlines()
            if "doi" in line.lower() and DOI_RE.search(line)
        ),
        None,
    ) or DOI_RE.search(front[:8_000])
    recovered_doi = None
    if not doi_match:
        confused = re.search(r"(?im)^\s*doi\s*:\s*([^\r\n]{6,100})", front)
        if confused:
            candidate = re.sub(r"\s+", "", confused.group(1))
            if "/" in candidate:
                prefix, suffix = candidate.split("/", 1)
                prefix = prefix.translate(
                    str.maketrans({"I": "1", "i": "1", "l": "1", "O": "0", "o": "0"})
                )
                normalized_candidate = f"{prefix}/{suffix}"
                recovered = DOI_RE.search(normalized_candidate)
                if recovered:
                    recovered_doi = normalize_doi(recovered.group(0))
    lines = [re.sub(r"\s+", " ", line).strip() for line in head.splitlines()]
    title = None
    for line in lines[:80]:
        line = re.split(r"\s+Author\(s\):", line, maxsplit=1, flags=re.IGNORECASE)[
            0
        ].strip()
        lowered = line.lower()
        if not 20 <= len(line) <= 300:
            continue
        if DOI_RE.search(line) or lowered.startswith(
            ("abstract", "doi", "http", "copyright")
        ):
            continue
        if len(title_tokens(line)) >= 4:
            title = line
            break
    author_match = re.search(r"(?im)^\s*Author\(s\):\s*([^\r\n]+)", front)
    authors = []
    if author_match:
        authors = [
            re.sub(r"\s+", " ", value).strip(" ,")
            for value in re.split(
                r",\s*|\s+and\s+", author_match.group(1), flags=re.IGNORECASE
            )
            if value.strip(" ,")
        ]
    year_match = re.search(r"\b(?:19|20)\d{2}\b", front[:8_000])
    return {
        "doi": normalize_doi(doi_match.group(0)) if doi_match else recovered_doi,
        "title": title,
        "authors": authors,
        "publication_year": int(year_match.group(0)) if year_match else None,
    }


def _target_aliases(target: dict) -> dict:
    authors = target.get("authorships") or []
    names = [((item.get("author") or {}).get("display_name") or "") for item in authors]
    names.extend(target.get("authors") or [])
    ordered_names = []
    seen_names = set()
    for name in names:
        normalized_name = re.sub(r"\s+", " ", str(name)).strip()
        key = normalized_name.casefold()
        if normalized_name and key not in seen_names:
            ordered_names.append(normalized_name)
            seen_names.add(key)
    surnames = []
    for name in ordered_names:
        pieces = re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ'-]+", name)
        if pieces:
            surnames.append(pieces[-1])
    # Three-or-more-author works are cited as first-author + et al.; treating every
    # coauthor as an alias creates many false matches in large collaboration papers.
    citation_surnames = surnames if len(surnames) <= 2 else surnames[:1]
    return {
        "doi": normalize_doi(target.get("doi")),
        "title_tokens": title_tokens(target.get("display_name") or target.get("title")),
        "surnames": list(dict.fromkeys(citation_surnames)),
        "year": str(target.get("publication_year") or ""),
    }


def _entry_matches_target(entry: str, aliases: dict) -> bool:
    normalized_entry = title_tokens(entry)
    doi_hit = bool(aliases["doi"] and aliases["doi"] in entry.lower())
    title_hit = bool(aliases["title_tokens"]) and (
        len(aliases["title_tokens"] & normalized_entry) / len(aliases["title_tokens"])
        >= 0.7
    )
    author_year_hit = bool(aliases["year"]) and any(
        re.search(
            rf"\b{re.escape(surname)}\b.{{0,180}}\b{re.escape(aliases['year'])}\b",
            entry,
            flags=re.IGNORECASE | re.DOTALL,
        )
        for surname in aliases["surnames"]
    )
    return doi_hit or title_hit or author_year_hit


def _split_body_references(text: str, aliases: dict | None = None) -> tuple[str, str]:
    matches = list(
        re.finditer(
            r"(?im)(?:^|[\n\f])[ \t]*(?:#{1,4}[ \t]*)?(references|bibliography|literature cited|works cited)[ \t]*$",
            text,
        )
    )
    if matches:
        marker = matches[-1]
        return text[: marker.start()], text[marker.end() :]
    if aliases:
        # Some extractors drop the References heading. Only infer a boundary from
        # a numbered entry that itself strongly identifies the target paper.
        entry_pattern = re.compile(
            r"(?ims)(?:^|\n)\s*(?:\[(\d+)\]|(\d{1,3})[.)])\s+(.+?)(?=(?:^|\n)\s*(?:\[\d+\]|\d{1,3}[.)])\s+|\Z)"
        )
        for match in entry_pattern.finditer(text):
            if _entry_matches_target(match.group(3), aliases):
                return text[: match.start()], text[match.start() :]
    return text, ""


def _reference_markers(references: str, aliases: dict) -> list[str]:
    if not references:
        return []
    entry_pattern = re.compile(
        r"(?ms)^\s*(?:\[(\d+)\]|(\d+)[.)])\s+(.+?)(?=^\s*(?:\[\d+\]|\d+[.)])\s+|\Z)"
    )
    markers = []
    for match in entry_pattern.finditer(references):
        entry = match.group(3)
        if _entry_matches_target(entry, aliases):
            markers.append(f"[{match.group(1) or match.group(2)}]")

    # Anydoc can preserve multi-column reference lists as Markdown tables. In
    # that layout numbered entries are split across rows, so recover the number
    # nearest a strong DOI anchor and first-author surname.
    doi = aliases["doi"]
    if doi:
        table_rows = []
        for line in references.splitlines():
            if line.startswith("|") and line.endswith("|"):
                cells = [cell.strip() for cell in line[1:-1].split("|")]
                if cells and not all(
                    re.fullmatch(r":?-{3,}:?", cell) for cell in cells
                ):
                    table_rows.append(cells)
        table_views = []
        if table_rows:
            width = max(len(row) for row in table_rows)
            table_views = [
                "\n".join(row[column] for row in table_rows if column < len(row))
                for column in range(width)
            ]
        anchored_table_views = [view for view in table_views if doi in view.lower()]
        reference_views = anchored_table_views or [references]
        for reference_view in reference_views:
            for doi_match in re.finditer(
                re.escape(doi), reference_view, flags=re.IGNORECASE
            ):
                window_start = max(0, doi_match.start() - 1_500)
                prefix = reference_view[window_start : doi_match.end()]
                candidates = []
                for surname in aliases["surnames"]:
                    pattern = re.compile(
                        rf"(?:^|[\n|])\s*(?:\[(\d+)\]|(\d{{1,3}})[.)])\s*[^\n|]{{0,80}}?\b{re.escape(surname)}",
                        flags=re.IGNORECASE,
                    )
                    candidates.extend(pattern.finditer(prefix))
                if candidates:
                    nearest = max(candidates, key=lambda item: item.start())
                    markers.append(f"[{nearest.group(1) or nearest.group(2)}]")
    return sorted(set(markers))


def _citation_matches(
    body: str, aliases: dict, numeric_markers: list[str]
) -> list[tuple[int, int, str]]:
    matches: list[tuple[int, int, str]] = []
    for marker in numeric_markers:
        number = re.escape(marker[1:-1])
        pattern = re.compile(
            rf"(?<!\d)(?:\[{number}\]|\((?:[^)]*,\s*)?{number}(?:\s*,[^)]*)?\))(?!\d)"
        )
        for match in pattern.finditer(body):
            matches.append((match.start(), match.end(), match.group(0)))
    for surname in aliases["surnames"]:
        if not aliases["year"]:
            continue
        pattern = re.compile(
            rf"\b{re.escape(surname)}\b(?:\s+et\s+al\.)?.{{0,80}}?\b{re.escape(aliases['year'])}[a-z]?\b",
            flags=re.IGNORECASE | re.DOTALL,
        )
        for match in pattern.finditer(body):
            matches.append((match.start(), match.end(), match.group(0)))
    doi = aliases["doi"]
    if doi:
        for match in re.finditer(re.escape(doi), body, flags=re.IGNORECASE):
            matches.append((match.start(), match.end(), match.group(0)))
    deduplicated = []
    for item in sorted(matches):
        if deduplicated and item[0] - deduplicated[-1][0] < 20:
            continue
        deduplicated.append(item)
    return deduplicated


def _sentence_spans(text: str) -> list[tuple[int, int]]:
    spans = []
    start = 0
    for match in re.finditer(r"[.!?](?:[\"')\]]*)\s+", text):
        fragment = text[max(start, match.start() - 20) : match.end()].lower()
        if re.search(
            r"(?:et\s+al|e\.g|i\.e|fig|eq|dr|mr|mrs|prof)\.[\"')\]]*\s+$", fragment
        ):
            continue
        end = match.end()
        spans.append((start, end))
        start = end
    if start < len(text):
        spans.append((start, len(text)))
    return spans


def _context_window(body: str, start: int, end: int) -> str:
    paragraph_start = max(body.rfind("\n\n", 0, start), body.rfind("\f", 0, start)) + 1
    next_paragraph = [
        value for value in (body.find("\n\n", end), body.find("\f", end)) if value >= 0
    ]
    paragraph_end = min(next_paragraph) if next_paragraph else len(body)
    paragraph = body[paragraph_start:paragraph_end]
    local_start, local_end = start - paragraph_start, end - paragraph_start
    spans = _sentence_spans(paragraph)
    current = next(
        (
            index
            for index, (left, right) in enumerate(spans)
            if left <= local_start < right
        ),
        None,
    )
    if current is None:
        value = paragraph[
            max(0, local_start - 350) : min(len(paragraph), local_end + 350)
        ]
    else:
        left = spans[max(0, current - 1)][0]
        right = spans[min(len(spans) - 1, current + 1)][1]
        value = paragraph[left:right]
    return re.sub(r"\s+", " ", value).strip()


def _section_at(body: str, position: int) -> str | None:
    lines = body[:position].splitlines()
    for line in reversed(lines[-40:]):
        stripped = re.sub(r"^#+\s*", "", line).strip()
        if (
            2 <= len(stripped) <= 80
            and len(stripped.split()) <= 10
            and not stripped.endswith(".")
        ):
            if re.search(
                r"(?:introduction|method|results|discussion|conclusion|background|analysis)",
                stripped,
                re.I,
            ):
                return stripped
    return None


def bind_target_contexts(text: str, target: dict, page_aware: bool) -> list[dict]:
    """Bind in-text citation markers to the target bibliography entry."""
    aliases = _target_aliases(target)
    body, references = _split_body_references(text, aliases)
    markers = _reference_markers(references, aliases)
    matches = _citation_matches(body, aliases, markers)
    contexts = []
    for start, end, matched_marker in matches:
        context = _context_window(body, start, end)
        if not context:
            continue
        page = body.count("\f", 0, start) + 1 if page_aware else None
        marker = next(
            (value for value in markers if value.strip("[]") in matched_marker),
            matched_marker,
        )
        contexts.append(
            {
                "marker": marker,
                "context": context,
                "context_hash": hash_text(context),
                "page": page,
                "section": _section_at(body, start),
                "binding_method": "bibliography_numeric"
                if markers
                else "author_year_or_doi",
            }
        )
    unique: list[dict] = []
    for context in contexts:
        normalized = re.sub(r"\s+", " ", context["context"]).strip().casefold()
        duplicate_index = next(
            (
                index
                for index, existing in enumerate(unique)
                if context["marker"] == existing["marker"]
                and (
                    normalized
                    in re.sub(r"\s+", " ", existing["context"]).strip().casefold()
                    or re.sub(r"\s+", " ", existing["context"]).strip().casefold()
                    in normalized
                )
            ),
            None,
        )
        if duplicate_index is None:
            unique.append(context)
        elif len(context["context"]) > len(unique[duplicate_index]["context"]):
            unique[duplicate_index] = context
    return unique


def reconcile_analysis_results(
    citations: list[dict], results: list[dict]
) -> tuple[list[dict], list[dict], list[str]]:
    """Validate results, merge valid labels, and return paper-grouped pending work."""
    expected = {
        citation["statement_id"]: citation
        for citation in citations
        if citation.get("context_hash") and citation.get("context_text")
    }
    valid: dict[str, dict] = {}
    diagnostics = []
    for result in results:
        statement_id = result.get("statement_id")
        if statement_id not in expected:
            diagnostics.append(f"Unknown statement_id: {statement_id}")
            continue
        citation = expected[statement_id]
        if result.get("context_hash") != citation.get("context_hash"):
            diagnostics.append(f"context_hash mismatch for {statement_id}")
            continue
        if result.get("stance") not in VALID_STANCES:
            diagnostics.append(
                f"Invalid stance for {statement_id}: {result.get('stance')}"
            )
            continue
        if result.get("label_source") not in VALID_LABEL_SOURCES:
            diagnostics.append(f"Invalid label_source for {statement_id}")
            continue
        confidence = result.get("confidence")
        if not isinstance(confidence, (int, float)) or not 0 <= float(confidence) <= 1:
            diagnostics.append(f"Invalid confidence for {statement_id}")
            continue
        if not str(result.get("reason") or "").strip():
            diagnostics.append(f"Missing reason for {statement_id}")
            continue
        valid[statement_id] = result

    merged = []
    for citation in citations:
        statement_id = citation.get("statement_id")
        if statement_id not in expected:
            merged.append(citation)
            continue
        base = {
            **citation,
            "target_claim_id": None,
            "stance": "unknown",
            "confidence": 0.0,
            "reason": "Awaiting stance analysis.",
            "label_source": "rule",
        }
        result = valid.get(statement_id)
        if not result:
            merged.append(base)
            continue
        merged.append(
            {
                **base,
                "stance": result["stance"],
                "confidence": round(float(result["confidence"]), 4),
                "reason": str(result["reason"]).strip(),
                "label_source": result["label_source"],
                "target_claim_id": result.get("target_claim_id"),
            }
        )

    pending_by_work: dict[str, dict] = {}
    for statement_id, citation in expected.items():
        if statement_id in valid:
            continue
        work_id = citation.get("citing_work_id") or statement_id
        group = pending_by_work.setdefault(
            work_id,
            {
                "citing_work_id": work_id,
                "citing_paper": citation.get("citing_paper"),
                "statements": [],
            },
        )
        group["statements"].append(
            {
                "statement_id": statement_id,
                "context_hash": citation["context_hash"],
                "citation_context": {
                    "text": citation["context_text"],
                    "marker": citation.get("citation_marker"),
                    "page": citation.get("page"),
                    "section": citation.get("section"),
                },
            }
        )
    return merged, list(pending_by_work.values()), diagnostics


@dataclass(frozen=True)
class PrepareConfig:
    target_pdf: Path
    run_dir: Path
    n: int | None = None
    mode: str = "stance_first"
    language: str = "zh-TW"
    claims_file: Path | None = None
    triage_results: Path | None = None
    rule_triage: bool = False
    download_fulltext: bool = True
    workers: int = 4
    require_page_aware: bool = False
    timeout: int = 120
    mailto: str | None = None


def _stage_result(cache_hit: bool, elapsed: float) -> dict:
    return {"cache_hit": cache_hit, "elapsed_seconds": round(elapsed, 3)}


def _target_document(pdf_path: Path, inspection: dict, resolved: dict) -> dict:
    local_identity = extract_target_identity(inspection["text"])
    provider_title = resolved.get("display_name") or resolved.get("title")
    local_title = local_identity.get("title")
    provider_tokens = title_tokens(provider_title)
    local_tokens = title_tokens(local_title)
    local_extends_provider = bool(provider_tokens) and provider_tokens <= local_tokens
    title = (
        local_title
        if local_title and (not provider_title or local_extends_provider)
        else provider_title
    )
    doi = normalize_doi(resolved.get("doi")) or local_identity.get("doi")
    authors = [
        (authorship.get("author") or {}).get("display_name")
        for authorship in (resolved.get("authorships") or [])
        if (authorship.get("author") or {}).get("display_name")
    ]
    return {
        "schema_version": "openscite.target.v2",
        "input": {
            "original_path": str(pdf_path.resolve()),
            "sha256": inspection["sha256"],
            "page_count": inspection.get("page_count"),
            "parser": inspection.get("parser"),
            "parser_version": inspection.get("parser_version"),
        },
        "identity_evidence": {
            "doi": doi,
            "title": title,
            "authors": authors,
        },
        "resolved": {
            "openalex_id": short_openalex_id(resolved.get("id")),
            "id": resolved.get("id"),
            "doi": f"https://doi.org/{doi}" if doi else None,
            "title": title,
            "authors": authors,
            "publication_date": resolved.get("publication_date"),
            "publication_year": resolved.get("publication_year"),
            "abstract": _work_abstract(resolved),
        },
        "claim_card": [],
    }


def _target_for_algorithms(target_doc: dict) -> dict:
    resolved = target_doc.get("resolved") or {}
    return {
        "id": resolved.get("id") or resolved.get("openalex_id"),
        "openalex_id": resolved.get("openalex_id"),
        "doi": resolved.get("doi"),
        "title": resolved.get("title"),
        "display_name": resolved.get("title"),
        "publication_year": resolved.get("publication_year"),
        "authors": resolved.get("authors") or [],
        "authorships": [
            {
                "author": {"display_name": name},
                "author_position": "first" if index == 0 else "middle",
            }
            for index, name in enumerate(resolved.get("authors") or [])
        ],
    }


def _write_target_packet(run_dir: Path, target_doc: dict, text: str) -> None:
    resolved = target_doc.get("resolved") or {}
    packet = {
        "schema_version": "openscite.target-analysis-packet.v1",
        "task": "Extract the target paper's central empirical claims; do not assess citing papers.",
        "target": {
            "title": resolved.get("title"),
            "doi": resolved.get("doi"),
            "authors": resolved.get("authors"),
            "abstract": resolved.get("abstract"),
        },
        "local_text_excerpt": text[:24_000],
        "required_output": {
            "path": "target-claims.json",
            "fields": ["claim_id", "claim", "source"],
        },
    }
    write_json(run_dir / "target-analysis-packet.json", packet)


def _load_claims(config: PrepareConfig, run_dir: Path) -> list[dict]:
    source = config.claims_file or (run_dir / "target-claims.json")
    if not source.exists():
        return []
    value = read_json(source)
    claims = value.get("claims") if isinstance(value, dict) else value
    if not isinstance(claims, list) or not claims:
        raise ValueError("target-claims.json must contain a non-empty array")
    normalized = []
    for index, claim in enumerate(claims, 1):
        if not isinstance(claim, dict) or not str(claim.get("claim") or "").strip():
            raise ValueError(f"Invalid target claim at index {index}")
        normalized.append(
            {
                **claim,
                "claim_id": claim.get("claim_id") or f"claim-{index:02d}",
                "source": claim.get("source") or "target_full_text",
            }
        )
    write_json(run_dir / "target-claims.json", normalized)
    return normalized


def _triage_pool(
    works: list[dict], target: dict, claims: list[dict], n: int
) -> list[dict]:
    target_id = short_openalex_id(target.get("id") or target.get("openalex_id"))
    target_doi = normalize_doi(target.get("doi"))
    candidates = []
    for raw in works:
        work = normalize_work(raw)
        if (target_id and work["openalex_id"] == target_id) or (
            target_doi and work["doi"] == target_doi
        ):
            continue
        screen = rule_screen(work, claims, target)
        prefilter_score = (
            0.50 * screen["contrast_signal"]
            + 0.20 * screen["support_signal"]
            + 0.15 * screen["claim_relevance"]
            + 0.10 * screen["evidence_signal"]
            + 0.05 * min(math.log1p(work["cited_by_count"]) / 10, 1)
        )
        candidates.append(
            {**work, "rule_screen": screen, "prefilter_score": prefilter_score}
        )
    candidates.sort(
        key=lambda work: (
            -work["prefilter_score"],
            -work["cited_by_count"],
            work["openalex_id"] or "",
        )
    )
    return candidates[: min(len(candidates), max(3 * n, 60))]


def _triage_packets(pool: list[dict], claims: list[dict], target: dict) -> list[dict]:
    packets = []
    for work in pool:
        shared_hash_payload = {
            "target": {
                "title": target.get("title") or target.get("display_name"),
                "claims": claims,
            },
            "citing_paper": {
                "title": work["title"],
                "abstract": work["abstract"],
            },
        }
        packets.append(
            {
                "citing_work_id": work["openalex_id"],
                "citing_paper": shared_hash_payload["citing_paper"],
                "input_hash": hash_value(shared_hash_payload),
            }
        )
    return packets


def _triage_pending_packets(
    packets: list[dict], result_records: list[dict]
) -> tuple[list[dict], dict[str, dict], list[str]]:
    expected = {packet["citing_work_id"]: packet for packet in packets}
    valid = {}
    diagnostics = []
    for result in result_records:
        work_id = result.get("citing_work_id")
        if work_id not in expected:
            diagnostics.append(f"Unknown triage citing_work_id: {work_id}")
            continue
        if result.get("input_hash") != expected[work_id]["input_hash"]:
            diagnostics.append(f"Triage input_hash mismatch for {work_id}")
            continue
        score = result.get("priority_score")
        if not isinstance(score, (int, float)) or not 0 <= float(score) <= 1:
            diagnostics.append(f"Invalid priority_score for {work_id}")
            continue
        if result.get("priority_lane") not in {"contrast", "support", "exploration"}:
            diagnostics.append(f"Invalid priority_lane for {work_id}")
            continue
        valid[work_id] = result
    pending = [packet for packet in packets if packet["citing_work_id"] not in valid]
    return pending, valid, diagnostics


def _inbox_fingerprint(inbox: Path) -> list[dict]:
    if not inbox.exists():
        return []
    records = []
    for path in sorted(
        (item for item in inbox.iterdir() if item.is_file()),
        key=lambda item: item.name.lower(),
    ):
        records.append(
            {
                "name": path.name,
                "size": path.stat().st_size,
                "mtime_ns": path.stat().st_mtime_ns,
            }
        )
    return records


def _apply_inbox_matches(config: PrepareConfig, works: list[dict]) -> dict:
    run_dir = config.run_dir
    inbox = run_dir / "fulltext" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    document_config = DocumentConfig(
        cache_root=run_dir / ".cache",
        timeout=config.timeout,
        require_page_aware=config.require_page_aware,
        workers=config.workers,
    )
    manifest_files = match_inbox(inbox, works, document_config)
    valid_ids = {work["citing_work_id"] for work in works}
    mapping_path = run_dir / "file-mappings.json"
    confirmed: dict[str, str] = {}
    if mapping_path.exists():
        raw_mappings = read_json(mapping_path)
        entries = (
            raw_mappings.get("mappings", []) if isinstance(raw_mappings, dict) else []
        )
        for entry in entries:
            work_id = entry.get("citing_work_id")
            original = entry.get("original_path") or entry.get("file_name")
            if work_id in valid_ids and original:
                confirmed[str(original).lower()] = work_id
    for item in manifest_files:
        original_path = str(item.get("original_path") or "")
        work_id = confirmed.get(original_path.lower()) or confirmed.get(
            Path(original_path).name.lower()
        )
        if work_id:
            item["match"] = {
                "status": "matched",
                "citing_work_id": work_id,
                "method": "user_confirmed",
                "confidence": 1.0,
                "candidates": item.get("match", {}).get("candidates", []),
            }
    by_id = {
        (item.get("match") or {}).get("citing_work_id"): item
        for item in manifest_files
        if (item.get("match") or {}).get("status") == "matched"
    }
    for work in works:
        matched = by_id.get(work["citing_work_id"])
        if matched:
            work["full_text"].update(
                {
                    "local_path": matched["original_path"],
                    "acquisition": "user_file",
                    "sha256": matched["sha256"],
                    "preparsed": matched.get("text_extraction"),
                }
            )
    manifest = {
        "schema_version": "openscite.fulltext-manifest.v2",
        "generated_at": utc_now(),
        "inbox": str(inbox.resolve()),
        "candidate_count": len(works),
        "files": manifest_files,
    }
    write_json(run_dir / "fulltext-manifest.json", manifest)
    return manifest


def _download_one(work: dict, destination_dir: Path, timeout: int) -> dict:
    candidates = [
        item
        for item in (work.get("full_text") or {}).get("candidate_urls", [])
        if item.get("kind") == "oa_pdf" and item.get("url")
    ]
    destination = destination_dir / f"{work['citing_work_id']}.pdf"
    if destination.exists():
        with destination.open("rb") as handle:
            if handle.read(5) == b"%PDF-":
                return {
                    "status": "downloaded",
                    "path": str(destination.resolve()),
                    "cache_hit": True,
                }
    attempts = []
    for candidate in candidates:
        url = candidate["url"]
        request = urllib.request.Request(
            url, headers={"User-Agent": "OpenScite/0.1", "Accept": "application/pdf"}
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = response.read(100 * 1024 * 1024 + 1)
                content_type = response.headers.get("Content-Type")
            if len(data) > 100 * 1024 * 1024:
                raise ValueError("PDF exceeds 100 MiB")
            if not data.startswith(b"%PDF-"):
                raise ValueError(f"response is not a PDF ({content_type})")
            temporary = destination.with_suffix(".pdf.tmp")
            temporary.write_bytes(data)
            temporary.replace(destination)
            return {
                "status": "downloaded",
                "path": str(destination.resolve()),
                "url": url,
                "sha256": file_sha256(destination),
                "cache_hit": False,
                "attempts": attempts,
            }
        except (OSError, urllib.error.URLError, ValueError) as exc:
            attempts.append({"url": url, "error": str(exc)})
    return {"status": "missing", "attempts": attempts}


def _acquire_fulltexts(config: PrepareConfig, works: list[dict]) -> None:
    _apply_inbox_matches(config, works)
    if not config.download_fulltext:
        return
    destination = config.run_dir / "fulltext" / "downloaded"
    destination.mkdir(parents=True, exist_ok=True)
    pending = [
        work for work in works if not (work.get("full_text") or {}).get("local_path")
    ]
    with ThreadPoolExecutor(max_workers=max(1, min(config.workers, 8))) as executor:
        futures = {
            executor.submit(_download_one, work, destination, config.timeout): work
            for work in pending
        }
        for future in as_completed(futures):
            work = futures[future]
            result = future.result()
            if result["status"] == "downloaded":
                work["full_text"].update(
                    {
                        "local_path": result["path"],
                        "acquisition": "open_access_download",
                        "source_url": result.get("url"),
                        "sha256": result.get("sha256"),
                        "cache_hit": result.get("cache_hit", False),
                    }
                )
            work["full_text"]["download_attempts"] = result.get("attempts", [])


def _parse_fulltexts(
    config: PrepareConfig, target_doc: dict, works: list[dict]
) -> dict:
    document_config = DocumentConfig(
        cache_root=config.run_dir / ".cache",
        timeout=config.timeout,
        require_page_aware=config.require_page_aware,
        workers=config.workers,
    )
    results = extract_fulltexts(works, target_doc, document_config)
    return {
        "schema_version": "openscite.fulltext-parse.v2",
        "generated_at": utc_now(),
        "require_page_aware": config.require_page_aware,
        "workers": config.workers,
        "results": results,
    }


def _build_citations(
    target_doc: dict, works: list[dict], parse_doc: dict
) -> list[dict]:
    parsed_by_id = {
        item["citing_work_id"]: item for item in parse_doc.get("results", [])
    }
    target = _target_for_algorithms(target_doc)
    citations = []
    for work in works:
        work_id = work["citing_work_id"]
        parsed = parsed_by_id.get(work_id)
        contexts = []
        if parsed and parsed.get("status") == "parsed" and parsed.get("cache_path"):
            extracted = Path(parsed["cache_path"]).read_text(encoding="utf-8-sig")
            contexts = bind_target_contexts(
                extracted, target, bool(parsed.get("page_aware"))
            )
        if contexts:
            work["context_status"] = "context_bound"
            for index, context in enumerate(contexts, 1):
                statement_id = f"{work_id}-stmt-{index:02d}"
                citation = {
                    "statement_id": statement_id,
                    "citing_work_id": work_id,
                    "citing_paper": {
                        "citing_work_id": work_id,
                        "title": (work.get("resolved") or {}).get("title"),
                        "doi": (work.get("resolved") or {}).get("doi"),
                    },
                    "citation_marker": context["marker"],
                    "context_text": context["context"],
                    "context_hash": context["context_hash"],
                    "page": context["page"],
                    "section": context["section"],
                    "binding_method": context["binding_method"],
                    "target_claim_id": None,
                    "stance": "unknown",
                    "confidence": 0.0,
                    "reason": "Awaiting stance analysis.",
                    "label_source": "rule",
                }
                citations.append(citation)
        else:
            local_path = (work.get("full_text") or {}).get("local_path")
            if not local_path:
                context_status = "awaiting_user_full_text_and_context"
                reason = "No accessible full text; citation context is unavailable."
            elif parsed and parsed.get("status") == "parsed":
                context_status = "no_context_found"
                reason = "Full text parsed, but no in-text marker was reliably bound to the target."
            else:
                context_status = "extraction_failed"
                reason = "Full text was present, but extraction failed."
            work["context_status"] = context_status
            citations.append(
                {
                    "statement_id": f"{work_id}-unknown-01",
                    "citing_work_id": work_id,
                    "citing_paper": {
                        "citing_work_id": work_id,
                        "title": (work.get("resolved") or {}).get("title"),
                        "doi": (work.get("resolved") or {}).get("doi"),
                    },
                    "citation_marker": None,
                    "context_text": None,
                    "context_hash": None,
                    "page": None,
                    "section": None,
                    "binding_method": None,
                    "target_claim_id": None,
                    "stance": "unknown",
                    "confidence": 0.0,
                    "reason": reason,
                    "label_source": "rule",
                }
            )
    return citations


def _run_counts(works: list[dict], citations: list[dict], discovered: int) -> dict:
    labels = {stance: 0 for stance in VALID_STANCES}
    for citation in citations:
        labels[citation.get("stance", "unknown")] = (
            labels.get(citation.get("stance", "unknown"), 0) + 1
        )
    return {
        "citing_works_discovered": discovered,
        "citing_works_selected": len(works),
        "citing_works_with_fulltext": sum(
            bool((work.get("full_text") or {}).get("local_path")) for work in works
        ),
        "citing_works_with_context": sum(
            work.get("context_status") == "context_bound" for work in works
        ),
        "citation_statements": len(citations),
        "stances": labels,
    }


def _write_run(
    run_dir: Path,
    status: str,
    language: str,
    target_doc: dict,
    works: list[dict],
    citations: list[dict],
    discovered: int,
    stages: dict,
    provider: MetadataProvider,
    next_action: str | None,
) -> dict:
    run_path = run_dir / "run.json"
    previous = read_json(run_path) if run_path.exists() else {}
    resolved = target_doc.get("resolved") or {}
    run = {
        "schema_version": RUN_SCHEMA,
        "pipeline_version": PIPELINE_VERSION,
        "status": status,
        "language": language,
        "started_at": previous.get("started_at") or utc_now(),
        "updated_at": utc_now(),
        "invocation_count": int(previous.get("invocation_count") or 0) + 1,
        "target": {
            "title": resolved.get("title"),
            "doi": resolved.get("doi"),
            "openalex_id": resolved.get("openalex_id"),
        },
        "counts": _run_counts(works, citations, discovered),
        "stages": stages,
        "provider_diagnostics": getattr(provider, "diagnostics", []),
        "next_action": next_action,
    }
    write_json(run_path, run)
    return run


def _early_result(
    config: PrepareConfig,
    provider: MetadataProvider,
    target_doc: dict,
    discovered: int,
    stages: dict,
    status: str,
    next_action: str,
    selection_extra: dict | None = None,
) -> dict:
    selection = {
        "schema_version": "openscite.selection.v2",
        "status": "pending",
        "ranking_mode": config.mode,
        "requested_n": config.n,
        **(selection_extra or {}),
    }
    write_json(config.run_dir / "selection.json", selection)
    write_json(
        config.run_dir / "citing-works.json",
        {"schema_version": "openscite.citing-works.v2", "works": []},
    )
    write_json(
        config.run_dir / "citations.json",
        {"schema_version": "openscite.citations.v3", "citations": []},
    )
    write_jsonl(config.run_dir / "analysis-pending.jsonl", [])
    if not (config.run_dir / "analysis-context.json").exists():
        write_json(
            config.run_dir / "analysis-context.json",
            {"schema_version": "openscite.analysis-context.v1", "target_claims": []},
        )
    run = _write_run(
        config.run_dir,
        status,
        config.language,
        target_doc,
        [],
        [],
        discovered,
        stages,
        provider,
        next_action,
    )
    render_artifacts(config.run_dir, config.language)
    return {
        "status": status,
        "run_dir": str(config.run_dir.resolve()),
        "stages": stages,
        "run": run,
    }


def prepare_run(
    config: PrepareConfig,
    provider: MetadataProvider | None = None,
    inspector: DocumentInspector | None = None,
) -> dict:
    """Prepare all deterministic artifacts and return the next harness action."""
    if config.mode not in {"stance_first", "influence_first"}:
        raise ValueError("mode must be stance_first or influence_first")
    if config.language not in {"zh-TW", "en"}:
        raise ValueError("language must be zh-TW or en")
    if config.n is not None and not 1 <= config.n <= 500:
        raise ValueError("n must be between 1 and 500")
    if not 1 <= config.workers <= 8:
        raise ValueError("workers must be between 1 and 8")

    target_pdf = config.target_pdf.resolve(strict=True)
    run_dir = config.run_dir.resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    for obsolete_name in (
        "triage-packets.jsonl",
        "triage-validation.json",
        "analysis-packets.jsonl",
        "analysis-validation.json",
        "fulltext-requests.json",
    ):
        (run_dir / obsolete_name).unlink(missing_ok=True)
    (run_dir / "fulltext" / "inbox").mkdir(parents=True, exist_ok=True)
    provider = provider or OpenAlexProvider(mailto=config.mailto)
    inspector = inspector or PdfInspector(config.timeout)
    cache = StageCache(run_dir / ".cache")
    stages: dict[str, dict] = {}

    target_path = run_dir / "target.json"
    target_text_path = run_dir / ".cache" / "target-text.txt"
    target_fingerprint = hash_value(
        {
            "version": PIPELINE_VERSION,
            "pdf_sha256": file_sha256(target_pdf),
            "provider": getattr(provider, "cache_key", provider.__class__.__name__),
        }
    )
    started = time.perf_counter()
    target_hit = cache.is_hit(
        "target", target_fingerprint, [target_path, target_text_path]
    )
    if target_hit:
        target_doc = read_json(target_path)
        target_text = target_text_path.read_text(encoding="utf-8-sig")
    else:
        inspection = inspector.inspect(target_pdf, run_dir / ".cache" / "target")
        identity = extract_target_identity(inspection["text"])
        resolved = provider.resolve_target(identity)
        target_doc = _target_document(target_pdf, inspection, resolved)
        target_text = inspection["text"]
        write_json(target_path, target_doc)
        target_text_path.parent.mkdir(parents=True, exist_ok=True)
        target_text_path.write_text(target_text, encoding="utf-8")
        cache.record(
            "target",
            target_fingerprint,
            [target_path, target_text_path],
            time.perf_counter() - started,
        )
    stages["target"] = _stage_result(target_hit, time.perf_counter() - started)
    _write_target_packet(run_dir, target_doc, target_text)

    inventory_path = run_dir / "incoming-inventory.json"
    target_openalex_id = (target_doc.get("resolved") or {}).get("openalex_id")
    discovery_fingerprint = hash_value(
        {
            "version": PIPELINE_VERSION,
            "target_openalex_id": target_openalex_id,
            "provider": getattr(provider, "cache_key", provider.__class__.__name__),
        }
    )
    started = time.perf_counter()
    discovery_hit = cache.is_hit("discovery", discovery_fingerprint, [inventory_path])
    if discovery_hit:
        inventory = read_json(inventory_path)
        incoming_works = inventory.get("works") or []
    else:
        incoming_works = provider.incoming_citations(target_openalex_id)
        inventory = {
            "schema_version": "openscite.incoming-inventory.v1",
            "generated_at": utc_now(),
            "target_openalex_id": target_openalex_id,
            "count": len(incoming_works),
            "works": incoming_works,
        }
        write_json(inventory_path, inventory)
        cache.record(
            "discovery",
            discovery_fingerprint,
            [inventory_path],
            time.perf_counter() - started,
        )
    stages["discovery"] = _stage_result(discovery_hit, time.perf_counter() - started)
    discovered = len(incoming_works)

    claims = _load_claims(config, run_dir)
    if config.n is None:
        return _early_result(
            config,
            provider,
            target_doc,
            discovered,
            stages,
            "needs_user_selection",
            "Ask the user for N and ranking mode, then rerun prepare.",
            {"suggested_n": 20, "available_count": discovered},
        )
    if not claims:
        return _early_result(
            config,
            provider,
            target_doc,
            discovered,
            stages,
            "needs_target_claims",
            "Analyze target-analysis-packet.json, write target-claims.json, then rerun prepare.",
        )
    target_doc["claim_card"] = claims
    write_json(target_path, target_doc)
    target_for_algorithms = _target_for_algorithms(target_doc)

    write_json(
        run_dir / "analysis-context.json",
        {
            "schema_version": "openscite.analysis-context.v1",
            "target_claims": claims,
            "task": "Classify each citation statement as supporting, contrasting, mentioning, or unknown.",
            "required_output": [
                "statement_id",
                "context_hash",
                "stance",
                "confidence",
                "reason",
                "label_source",
                "target_claim_id",
            ],
        },
    )

    pool = _triage_pool(incoming_works, target_for_algorithms, claims, config.n)
    triage_packets = _triage_packets(pool, claims, target_for_algorithms)
    write_json(
        run_dir / "triage-context.json",
        {
            "schema_version": "openscite.triage-context.v1",
            "target": {
                "title": target_for_algorithms.get("title")
                or target_for_algorithms.get("display_name"),
                "claims": claims,
            },
            "required_output": [
                "citing_work_id",
                "input_hash",
                "priority_score",
                "priority_lane",
            ],
        },
    )
    model_screens: dict[str, dict] = {}
    triage_results_path = config.triage_results or (run_dir / "triage-results.jsonl")
    if config.mode == "stance_first" and not config.rule_triage:
        triage_results = (
            read_jsonl(triage_results_path) if triage_results_path.exists() else []
        )
        triage_pending, model_screens, triage_diagnostics = _triage_pending_packets(
            triage_packets, triage_results
        )
        write_jsonl(run_dir / "triage-pending.jsonl", triage_pending)
        if triage_pending:
            return _early_result(
                config,
                provider,
                target_doc,
                discovered,
                stages,
                "needs_abstract_triage",
                "Analyze triage-pending.jsonl with triage-context.json, append valid results to triage-results.jsonl, then rerun prepare.",
                {
                    "candidate_pool_size": len(triage_packets),
                    "pending_count": len(triage_pending),
                    "invalid_result_count": len(triage_diagnostics),
                },
            )
    else:
        write_jsonl(run_dir / "triage-pending.jsonl", [])

    selected_cache_path = run_dir / ".cache" / "selected-works.json"
    selection_path = run_dir / "selection.json"
    selection_fingerprint = hash_value(
        {
            "version": PIPELINE_VERSION,
            "inventory": hash_value(incoming_works),
            "claims": claims,
            "n": config.n,
            "mode": config.mode,
            "model_screens": model_screens,
            "rule_triage": config.rule_triage,
        }
    )
    started = time.perf_counter()
    selection_hit = cache.is_hit(
        "selection", selection_fingerprint, [selected_cache_path, selection_path]
    )
    if selection_hit:
        selected_works = read_json(selected_cache_path).get("works") or []
    else:
        source_ids = [work["source_id"] for work in pool if work.get("source_id")]
        source_metrics = provider.source_metrics(source_ids)
        selected_works, diagnostics = rank_candidates(
            incoming_works,
            target_for_algorithms,
            claims,
            config.n,
            config.mode,
            source_metrics,
            model_screens,
        )
        write_json(
            selection_path,
            {
                "schema_version": "openscite.selection.v2",
                "status": "confirmed",
                "ranking_mode": config.mode,
                "requested_n": config.n,
                "selected_n": len(selected_works),
                "abstract_triage": "rule" if config.rule_triage else "model",
                "candidate_pool_size": len(pool),
                "diagnostics": diagnostics,
                "selected_ids": [work["citing_work_id"] for work in selected_works],
            },
        )
        write_json(
            selected_cache_path,
            {"schema_version": "openscite.selected-works.v1", "works": selected_works},
        )
        cache.record(
            "selection",
            selection_fingerprint,
            [selected_cache_path, selection_path],
            time.perf_counter() - started,
        )
    stages["selection"] = _stage_result(selection_hit, time.perf_counter() - started)

    citing_path = run_dir / "citing-works.json"
    manifest_path = run_dir / "fulltext-manifest.json"
    acquisition_fingerprint = hash_value(
        {
            "version": PIPELINE_VERSION,
            "selected": selected_works,
            "download": config.download_fulltext,
            "inbox": _inbox_fingerprint(run_dir / "fulltext" / "inbox"),
            "file_mappings": (
                read_json(run_dir / "file-mappings.json")
                if (run_dir / "file-mappings.json").exists()
                else None
            ),
            "workers": config.workers,
        }
    )
    started = time.perf_counter()
    acquisition_hit = cache.is_hit(
        "acquisition", acquisition_fingerprint, [citing_path, manifest_path]
    )
    if acquisition_hit:
        selected_works = read_json(citing_path).get("works") or []
    else:
        # Work on a detached JSON copy so stage cache inputs remain immutable.
        selected_works = json.loads(json.dumps(selected_works))
        _acquire_fulltexts(config, selected_works)
        write_json(
            citing_path,
            {"schema_version": "openscite.citing-works.v2", "works": selected_works},
        )
        cache.record(
            "acquisition",
            acquisition_fingerprint,
            [citing_path, manifest_path],
            time.perf_counter() - started,
        )
    stages["acquisition"] = _stage_result(
        acquisition_hit, time.perf_counter() - started
    )

    parse_path = run_dir / "fulltext-parse.json"
    local_inputs = []
    for work in selected_works:
        local_path = (work.get("full_text") or {}).get("local_path")
        if local_path and Path(local_path).exists():
            path = Path(local_path)
            local_inputs.append(
                {
                    "work_id": work["citing_work_id"],
                    "sha256": file_sha256(path),
                    "path": str(path.resolve()),
                }
            )
    parse_fingerprint = hash_value(
        {
            "version": PIPELINE_VERSION,
            "local_inputs": local_inputs,
            "require_page_aware": config.require_page_aware,
        }
    )
    started = time.perf_counter()
    parse_hit = cache.is_hit("parse", parse_fingerprint, [parse_path])
    if parse_hit:
        parse_doc = read_json(parse_path)
    else:
        parse_doc = _parse_fulltexts(config, target_doc, selected_works)
        write_json(parse_path, parse_doc)
        cache.record(
            "parse", parse_fingerprint, [parse_path], time.perf_counter() - started
        )
    stages["parse"] = _stage_result(parse_hit, time.perf_counter() - started)

    citations_path = run_dir / "citations.json"
    contexts_fingerprint = hash_value(
        {
            "version": PIPELINE_VERSION,
            "parse": parse_doc,
            "selected_ids": [work["citing_work_id"] for work in selected_works],
        }
    )
    started = time.perf_counter()
    contexts_hit = cache.is_hit(
        "contexts", contexts_fingerprint, [citations_path, citing_path]
    )
    if contexts_hit:
        citations = read_json(citations_path).get("citations") or []
        selected_works = read_json(citing_path).get("works") or []
    else:
        citations = _build_citations(target_doc, selected_works, parse_doc)
        write_json(
            citations_path,
            {"schema_version": "openscite.citations.v3", "citations": citations},
        )
        write_json(
            citing_path,
            {"schema_version": "openscite.citing-works.v2", "works": selected_works},
        )
        cache.record(
            "contexts",
            contexts_fingerprint,
            [citations_path, citing_path],
            time.perf_counter() - started,
        )
    stages["contexts"] = _stage_result(contexts_hit, time.perf_counter() - started)

    analysis_results_path = run_dir / "analysis-results.jsonl"
    analysis_results = (
        read_jsonl(analysis_results_path) if analysis_results_path.exists() else []
    )
    citations, analysis_pending, _ = reconcile_analysis_results(
        citations, analysis_results
    )
    write_jsonl(run_dir / "analysis-pending.jsonl", analysis_pending)
    write_json(
        citations_path,
        {"schema_version": "openscite.citations.v3", "citations": citations},
    )

    missing_fulltext = any(
        work.get("context_status") == "awaiting_user_full_text_and_context"
        for work in selected_works
    )
    unknown = any(citation.get("stance") == "unknown" for citation in citations)
    if analysis_pending:
        status = "needs_analysis"
        next_action = "Analyze analysis-pending.jsonl with analysis-context.json, append valid results to analysis-results.jsonl, then run finalize."
    elif missing_fulltext:
        status = "needs_user_files"
        next_action = "Ask the user to attach any available requested full text, then rerun prepare."
    elif unknown:
        status = "partial"
        next_action = "Review extraction failures or no-context cases."
    else:
        status = "complete"
        next_action = None
    run = _write_run(
        run_dir,
        status,
        config.language,
        target_doc,
        selected_works,
        citations,
        discovered,
        stages,
        provider,
        next_action,
    )
    render_artifacts(run_dir, config.language)
    return {"status": status, "run_dir": str(run_dir), "stages": stages, "run": run}


def finalize_run(run_dir: Path, results_path: Path | None = None) -> dict:
    run_dir = run_dir.resolve(strict=True)
    run_path = run_dir / "run.json"
    run = read_json(run_path)
    citations_path = run_dir / "citations.json"
    citations_doc = read_json(citations_path)
    citations = citations_doc.get("citations") or []
    result_path = results_path or (run_dir / "analysis-results.jsonl")
    results = read_jsonl(result_path) if result_path.exists() else []
    merged, pending, _ = reconcile_analysis_results(citations, results)
    write_jsonl(run_dir / "analysis-pending.jsonl", pending)
    write_json(
        citations_path,
        {"schema_version": "openscite.citations.v3", "citations": merged},
    )
    works = read_json(run_dir / "citing-works.json").get("works") or []
    unknown = sum(item.get("stance") == "unknown" for item in merged)
    missing_files = any(
        work.get("context_status") == "awaiting_user_full_text_and_context"
        for work in works
    )
    if pending:
        run["status"] = "needs_analysis"
        run["next_action"] = (
            "Analyze analysis-pending.jsonl with analysis-context.json, append valid results "
            "to analysis-results.jsonl, then run finalize."
        )
    elif unknown == 0:
        run["status"] = "complete"
        run["next_action"] = None
    elif missing_files:
        run["status"] = "needs_user_files"
        run["next_action"] = (
            "Attach any available requested full text, then rerun prepare."
        )
    else:
        run["status"] = "partial"
        run["next_action"] = "Review unknown cases and extraction diagnostics."
    run["updated_at"] = utc_now()
    run["counts"] = _run_counts(
        works,
        merged,
        int((run.get("counts") or {}).get("citing_works_discovered") or 0),
    )
    write_json(run_path, run)

    render_artifacts(run_dir, run.get("language") or "zh-TW")
    return {"status": run["status"], "run_dir": str(run_dir), "counts": run["counts"]}
