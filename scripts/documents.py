"""Local document inspection, matching, and extraction for OpenScite."""

from __future__ import annotations

import hashlib
import html
import json
import re
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path


__all__ = [
    "DocumentConfig",
    "PdfInspector",
    "extract_fulltexts",
    "file_sha256",
    "match_inbox",
    "normalize_doi",
]


SUPPORTED_SUFFIXES = {".pdf", ".html", ".htm", ".xml", ".txt", ".md"}
ANYDOC_PACKAGE = "@firecrawl/anydoc"
DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)
TOKEN_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)
STOPWORDS = {
    "a",
    "an",
    "and",
    "as",
    "at",
    "by",
    "for",
    "from",
    "in",
    "of",
    "on",
    "or",
    "the",
    "to",
    "with",
}


@dataclass(frozen=True)
class DocumentConfig:
    """Runtime choices shared by document matching and full-text extraction."""

    cache_root: Path
    timeout: int = 120
    max_bytes: int = 100 * 1024 * 1024
    require_page_aware: bool = False
    workers: int = 3


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest().upper()


def target_aliases(target: dict) -> dict:
    identity = target.get("identity_evidence") or {}
    resolved = target.get("resolved") or {}
    doi = identity.get("doi") or resolved.get("doi") or ""
    doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi, flags=re.I).lower()
    authors = identity.get("authors") or []
    surnames = []
    for author in authors:
        pieces = re.findall(r"[^\W\d_][\w'-]*", author or "", flags=re.UNICODE)
        if pieces:
            surnames.append(pieces[-1])
    title = identity.get("title") or resolved.get("title") or ""
    date = resolved.get("publication_date") or str(
        resolved.get("publication_year") or ""
    )
    year_match = re.search(r"\b(19|20)\d{2}\b", date)
    return {
        "doi": doi,
        "surnames": surnames,
        "year": year_match.group(0) if year_match else "",
        "title": " ".join(re.findall(r"[a-z0-9]+", title.lower())),
    }


def has_target_reference(text: str, aliases: dict) -> bool:
    lowered = text.lower()
    if aliases["doi"] and aliases["doi"] in lowered:
        return True
    normalized_text = " ".join(re.findall(r"[a-z0-9]+", lowered))
    if aliases["title"] and aliases["title"] in normalized_text:
        return True
    year = aliases["year"]
    for surname in aliases["surnames"]:
        if year and re.search(
            rf"\b{re.escape(surname)}\b.{{0,160}}\b{year}\b", text, flags=re.I | re.S
        ):
            return True
    return False


def run_process(
    args: list[str], timeout: int
) -> tuple[int | None, str, str, str | None]:
    try:
        result = subprocess.run(args, capture_output=True, check=False, timeout=timeout)
        stdout = result.stdout.decode("utf-8", errors="replace")
        stderr = result.stderr.decode("utf-8", errors="replace")
        return result.returncode, stdout, stderr, None
    except subprocess.TimeoutExpired:
        return None, "", "", f"timeout after {timeout}s"
    except OSError as exc:
        return None, "", "", str(exc)


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def inline_xml_text(element: ET.Element) -> str:
    parts = [element.text or ""]
    for child in element:
        child_text = inline_xml_text(child)
        if local_name(child.tag) == "xref" and child.get("rid"):
            child_text = f"{child_text}{{xref:{child.get('rid')}}}"
        parts.extend([child_text, child.tail or ""])
    return re.sub(r"\s+", " ", "".join(parts)).strip()


def extract_jats(raw: str) -> str:
    root = ET.fromstring(raw)
    blocks = []
    body = next(
        (element for element in root.iter() if local_name(element.tag) == "body"), None
    )
    if body is not None:
        for element in body.iter():
            tag = local_name(element.tag)
            if tag == "title":
                value = inline_xml_text(element)
                if value:
                    blocks.append(f"## {value}")
            elif tag == "p":
                value = inline_xml_text(element)
                if value:
                    blocks.append(value)

    references = []
    for element in root.iter():
        if local_name(element.tag) != "ref":
            continue
        value = inline_xml_text(element)
        anchor = element.get("id")
        if value:
            references.append(f"- {{#{anchor}}} {value}" if anchor else f"- {value}")
    if references:
        blocks.extend(["## References", *references])
    return "\n\n".join(blocks)


class StructuredHTMLParser(HTMLParser):
    BLOCKS = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "blockquote"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[str] = []
        self.current: list[str] = []
        self.current_tag: str | None = None
        self.skip_depth = 0

    def flush(self) -> None:
        value = re.sub(r"\s+", " ", "".join(self.current)).strip()
        if value:
            if self.current_tag and self.current_tag.startswith("h"):
                level = int(self.current_tag[1])
                value = f"{'#' * level} {value}"
            elif self.current_tag == "li":
                value = f"- {value}"
            self.blocks.append(value)
        self.current = []
        self.current_tag = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in {"script", "style"}:
            self.skip_depth += 1
            return
        if self.skip_depth:
            return
        if tag in self.BLOCKS:
            self.flush()
            self.current_tag = tag
        values = dict(attrs)
        anchor = values.get("id") or values.get("name")
        if anchor:
            self.current.append(f"{{#{anchor}}} ")
        if tag == "a":
            href = values.get("href") or ""
            if href.startswith("#") and len(href) > 1:
                self.current.append(f"{{xref:{href[1:]}}} ")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style"} and self.skip_depth:
            self.skip_depth -= 1
            return
        if not self.skip_depth and tag in self.BLOCKS:
            self.flush()

    def handle_data(self, data: str) -> None:
        if not self.skip_depth:
            self.current.append(data)

    def rendered(self) -> str:
        self.flush()
        return "\n\n".join(self.blocks)


def extract_markup(path: Path) -> tuple[str, str | None]:
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return "", str(exc)
    try:
        if path.suffix.lower() == ".xml":
            return extract_jats(raw), None
        parser = StructuredHTMLParser()
        parser.feed(raw)
        return html.unescape(parser.rendered()).strip(), None
    except (ET.ParseError, ValueError) as exc:
        return "", f"structured markup parse failed: {exc}"


def quality_issues(text: str, page_count: int | None = None) -> list[str]:
    issues = []
    minimum = max(1000, min(page_count or 0, 100) * 200)
    if len(text.strip()) < minimum:
        issues.append(f"too_short:{len(text.strip())}<{minimum}")
    if "\x00" in text:
        issues.append("contains_nul")
    if text and text.count("\ufffd") / len(text) > 0.005:
        issues.append("replacement_character_rate")
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    long_lines = [line for line in lines if len(line) >= 40]
    if len(long_lines) >= 20:
        repeated = len(long_lines) - len(set(long_lines))
        if repeated >= 10 and repeated / len(long_lines) > 0.30:
            issues.append("excessive_repeated_lines")
    return issues


def pdf_page_count(path: Path, pdfinfo: str | None, timeout: int) -> int | None:
    if not pdfinfo:
        return None
    code, stdout, _, _ = run_process([pdfinfo, str(path)], timeout)
    if code == 0:
        match = re.search(r"^Pages:\s+(\d+)\s*$", stdout, flags=re.M)
        if match:
            return int(match.group(1))
    return None


def parser_plan(
    require_page_aware: bool, has_anydoc: bool, has_pdftotext: bool
) -> list[str]:
    """Return the parser order without starting a subprocess.

    Anydoc produces cleaner structured text, but does not preserve dependable
    PDF page boundaries.  A strict page-aware run therefore goes straight to
    Poppler instead of paying for an Anydoc result that must be discarded.
    """
    plan = []
    if has_anydoc and not require_page_aware:
        plan.append("anydoc")
    if has_pdftotext:
        plan.append("pdftotext-layout")
    return plan


def _extract_one(
    work_id: str,
    source: Path,
    aliases: dict,
    config: DocumentConfig,
    preparsed: dict | None = None,
) -> dict:
    started = time.perf_counter()
    cache_dir = config.cache_root / "fulltext"
    cache_dir.mkdir(parents=True, exist_ok=True)
    npx = shutil.which("npx")
    pdftotext = shutil.which("pdftotext")
    pdfinfo = shutil.which("pdfinfo")
    cache_fingerprint = None
    cache_record_path = None
    result = {
        "citing_work_id": work_id,
        "source_path": str(source),
        "source_sha256": None,
        "status": "failed",
        "parser": None,
        "parser_version": None,
        "cache_path": None,
        "output_sha256": None,
        "output_characters": 0,
        "page_count": None,
        "page_aware": False,
        "target_reference_found": False,
        "cache_hit": False,
        "attempts": [],
        "elapsed_seconds": None,
    }
    try:
        resolved_source = source.resolve(strict=True)
        result["source_path"] = str(resolved_source)
        result["source_sha256"] = file_sha256(resolved_source)
        cache_fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "source_sha256": result["source_sha256"],
                    "aliases": aliases,
                    "require_page_aware": config.require_page_aware,
                    "parser_cache_version": 2,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        cache_record_path = cache_dir / f"{work_id}.result.json"
        if cache_record_path.exists():
            try:
                cached = load(cache_record_path)
                cached_result = cached.get("result") or {}
                cached_output = Path(cached_result.get("cache_path") or "")
                if (
                    cached.get("fingerprint") == cache_fingerprint
                    and cached_result.get("status") == "parsed"
                    and cached_output.is_file()
                ):
                    result.update(cached_result)
                    result["source_path"] = str(resolved_source)
                    result["source_sha256"] = file_sha256(resolved_source)
                    result["cache_hit"] = True
                    return result
            except (OSError, json.JSONDecodeError, TypeError):
                pass
        size = resolved_source.stat().st_size
        if size > config.max_bytes:
            result["attempts"].append(
                {
                    "parser": "preflight",
                    "error": f"file exceeds {config.max_bytes} bytes",
                }
            )
            return result
        suffix = resolved_source.suffix.lower()
        if suffix in {".html", ".htm", ".xml"}:
            markup_text, error = extract_markup(resolved_source)
            reference_found = bool(markup_text) and has_target_reference(
                markup_text, aliases
            )
            issues = quality_issues(markup_text)
            parser_name = "jats-xml" if suffix == ".xml" else "html"
            result["attempts"].append(
                {
                    "parser": parser_name,
                    "characters": len(markup_text),
                    "target_reference_found": reference_found,
                    "quality_issues": issues,
                    "error": error,
                }
            )
            if not issues:
                cache_path = cache_dir / f"{work_id}.structured.txt"
                cache_path.write_text(markup_text, encoding="utf-8")
                result.update(
                    {
                        "status": "parsed",
                        "parser": parser_name,
                        "cache_path": str(cache_path.resolve()),
                        "output_sha256": text_sha256(markup_text),
                        "output_characters": len(markup_text),
                        "page_aware": False,
                        "target_reference_found": reference_found,
                    }
                )
            return result

        with resolved_source.open("rb") as handle:
            if handle.read(5) != b"%PDF-":
                result["attempts"].append(
                    {"parser": "preflight", "error": "invalid PDF magic"}
                )
                return result

        page_count = pdf_page_count(resolved_source, pdfinfo, config.timeout)
        result["page_count"] = page_count

        plan = parser_plan(config.require_page_aware, bool(npx), bool(pdftotext))
        anydoc_text = ""
        anydoc_reference_found = False
        preparsed_path = Path((preparsed or {}).get("cache_path") or "")
        can_reuse_preparsed = (
            "anydoc" in plan
            and (preparsed or {}).get("parser") == "anydoc"
            and (preparsed or {}).get("source_sha256") == result["source_sha256"]
            and preparsed_path.is_file()
        )
        if can_reuse_preparsed:
            try:
                stdout = preparsed_path.read_text(encoding="utf-8-sig")
                issues = quality_issues(stdout, page_count)
                anydoc_reference_found = bool(stdout.strip()) and has_target_reference(
                    stdout, aliases
                )
                result["attempts"].append(
                    {
                        "parser": "anydoc",
                        "exit_code": 0,
                        "characters": len(stdout),
                        "target_reference_found": anydoc_reference_found,
                        "quality_issues": issues,
                        "stderr": None,
                        "error": None,
                        "cache_hit": True,
                        "reused_from": "file_identity",
                    }
                )
                if not issues:
                    anydoc_text = stdout
            except OSError:
                can_reuse_preparsed = False

        if "anydoc" in plan and not can_reuse_preparsed:
            code, stdout, stderr, error = run_process(
                [
                    npx,
                    "--yes",
                    ANYDOC_PACKAGE,
                    str(resolved_source),
                    "--ocr",
                    "reject",
                ],
                config.timeout,
            )
            anydoc_reference_found = bool(stdout.strip()) and has_target_reference(
                stdout, aliases
            )
            issues = quality_issues(stdout, page_count)
            result["attempts"].append(
                {
                    "parser": "anydoc",
                    "exit_code": code,
                    "characters": len(stdout),
                    "target_reference_found": anydoc_reference_found,
                    "quality_issues": issues,
                    "stderr": stderr[-2000:] or None,
                    "error": error,
                }
            )
            if code == 0 and not issues:
                anydoc_text = stdout

        if anydoc_text and anydoc_reference_found and not config.require_page_aware:
            cache_path = cache_dir / f"{work_id}.md"
            cache_path.write_text(anydoc_text, encoding="utf-8")
            result.update(
                {
                    "status": "parsed",
                    "parser": "anydoc",
                    "parser_version": None,
                    "cache_path": str(cache_path.resolve()),
                    "output_sha256": text_sha256(anydoc_text),
                    "output_characters": len(anydoc_text),
                    "page_aware": False,
                    "target_reference_found": anydoc_reference_found,
                }
            )
            return result

        if "pdftotext-layout" in plan:
            code, stdout, stderr, error = run_process(
                [pdftotext, "-layout", str(resolved_source), "-"], config.timeout
            )
            reference_found = bool(stdout.strip()) and has_target_reference(
                stdout, aliases
            )
            issues = quality_issues(stdout, page_count)
            result["attempts"].append(
                {
                    "parser": "pdftotext-layout",
                    "exit_code": code,
                    "characters": len(stdout),
                    "target_reference_found": reference_found,
                    "quality_issues": issues,
                    "stderr": stderr[-2000:] or None,
                    "error": error,
                }
            )
            if (
                code == 0
                and not issues
                and (reference_found or not anydoc_text or config.require_page_aware)
            ):
                cache_path = cache_dir / f"{work_id}.pages.txt"
                cache_path.write_text(stdout, encoding="utf-8")
                result.update(
                    {
                        "status": "parsed",
                        "parser": "pdftotext-layout",
                        "cache_path": str(cache_path.resolve()),
                        "output_sha256": text_sha256(stdout),
                        "output_characters": len(stdout),
                        "page_aware": True,
                        "target_reference_found": reference_found,
                    }
                )
                return result

        if anydoc_text and not config.require_page_aware:
            cache_path = cache_dir / f"{work_id}.md"
            cache_path.write_text(anydoc_text, encoding="utf-8")
            result.update(
                {
                    "status": "parsed",
                    "parser": "anydoc",
                    "parser_version": None,
                    "cache_path": str(cache_path.resolve()),
                    "output_sha256": text_sha256(anydoc_text),
                    "output_characters": len(anydoc_text),
                    "page_aware": False,
                    "target_reference_found": anydoc_reference_found,
                }
            )
        return result
    finally:
        result["elapsed_seconds"] = round(time.perf_counter() - started, 3)
        if (
            result.get("status") == "parsed"
            and not result.get("cache_hit")
            and cache_fingerprint
            and cache_record_path
        ):
            temporary = cache_record_path.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(
                    {"fingerprint": cache_fingerprint, "result": result},
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            temporary.replace(cache_record_path)


def normalize_doi(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip().lower()
    value = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", value)
    value = re.sub(r"^doi:\s*", "", value)
    return value.rstrip(".,;:)]}") or None


def normalize_text(value: str) -> str:
    return " ".join(TOKEN_RE.findall(value.lower()))


def title_tokens(value: str) -> set[str]:
    return {
        token for token in TOKEN_RE.findall(value.lower()) if token not in STOPWORDS
    }


def _cache_identity_result(
    cache_dir: Path, fingerprint: str, text: str, metadata: dict
) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    suffix = ".md" if metadata["parser"] == "anydoc" else ".txt"
    text_path = cache_dir / f"{fingerprint}{suffix}"
    text_path.write_text(text, encoding="utf-8")
    metadata.update(
        {
            "cache_path": str(text_path.resolve()),
            "output_sha256": text_sha256(text),
            "output_characters": len(text),
        }
    )
    record_path = cache_dir / f"{fingerprint}.result.json"
    temporary = record_path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(
            {"fingerprint": fingerprint, "metadata": metadata},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    temporary.replace(record_path)


def _extract_pdf_identity(path: Path, config: DocumentConfig) -> tuple[str, dict]:
    cache_dir = config.cache_root / "file-identity"
    source_hash = file_sha256(path)
    npx = shutil.which("npx")
    pdftotext = shutil.which("pdftotext")
    pdfinfo = shutil.which("pdfinfo")
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "source_sha256": source_hash,
                "has_anydoc": bool(npx),
                "has_pdftotext": bool(pdftotext),
                "identity_parser_cache_version": 1,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    record_path = cache_dir / f"{fingerprint}.result.json"
    if record_path.exists():
        try:
            cached = json.loads(record_path.read_text(encoding="utf-8-sig"))
            metadata = cached.get("metadata") or {}
            text_path = Path(metadata.get("cache_path") or "")
            if cached.get("fingerprint") == fingerprint and text_path.is_file():
                metadata["cache_hit"] = True
                return text_path.read_text(encoding="utf-8-sig"), metadata
        except (OSError, json.JSONDecodeError, TypeError):
            pass

    page_count = pdf_page_count(path, pdfinfo, config.timeout)
    attempts = []
    base_metadata = {
        "source_sha256": source_hash,
        "parser": None,
        "parser_version": None,
        "cache_path": None,
        "output_sha256": None,
        "output_characters": 0,
        "page_count": page_count,
        "page_aware": False,
        "cache_hit": False,
        "attempts": attempts,
        "error": None,
    }

    if npx:
        code, stdout, stderr, error = run_process(
            [
                npx,
                "--yes",
                ANYDOC_PACKAGE,
                str(path.resolve()),
                "--ocr",
                "reject",
            ],
            config.timeout,
        )
        issues = quality_issues(stdout, page_count)
        attempts.append(
            {
                "parser": "anydoc",
                "exit_code": code,
                "characters": len(stdout),
                "quality_issues": issues,
                "stderr": stderr[-2000:] or None,
                "error": error,
            }
        )
        if code == 0 and not issues:
            metadata = {
                **base_metadata,
                "parser": "anydoc",
                "parser_version": None,
                "page_aware": False,
            }
            _cache_identity_result(cache_dir, fingerprint, stdout, metadata)
            return stdout, metadata

    if pdftotext:
        code, stdout, stderr, error = run_process(
            [pdftotext, "-f", "1", "-l", "3", "-layout", str(path), "-"],
            config.timeout,
        )
        issues = quality_issues(stdout)
        attempts.append(
            {
                "parser": "pdftotext-layout",
                "exit_code": code,
                "characters": len(stdout),
                "quality_issues": issues,
                "stderr": stderr[-2000:] or None,
                "error": error,
            }
        )
        if code == 0 and not issues:
            metadata = {
                **base_metadata,
                "parser": "pdftotext-layout",
                "page_aware": True,
            }
            _cache_identity_result(cache_dir, fingerprint, stdout, metadata)
            return stdout, metadata

    try:
        from pypdf import PdfReader  # type: ignore

        reader = PdfReader(str(path))
        text = "\n".join((page.extract_text() or "") for page in reader.pages[:3])
        issues = quality_issues(text)
        attempts.append(
            {
                "parser": "pypdf",
                "characters": len(text),
                "quality_issues": issues,
                "error": None,
            }
        )
        if not issues:
            metadata = {**base_metadata, "parser": "pypdf", "page_aware": True}
            _cache_identity_result(cache_dir, fingerprint, text, metadata)
            return text, metadata
        base_metadata["error"] = "; ".join(issues)
    except Exception as exc:  # pragma: no cover - dependency/environment branch
        attempts.append({"parser": "pypdf", "error": str(exc)})
        base_metadata["error"] = f"PDF text extraction failed: {exc}"
    return "", base_metadata


def _extract_identity_text(path: Path, config: DocumentConfig) -> tuple[str, dict]:
    if path.suffix.lower() == ".pdf":
        return _extract_pdf_identity(path, config)

    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return "", {
            "parser": None,
            "cache_hit": False,
            "error": f"File read failed: {exc}",
        }

    if path.suffix.lower() in {".html", ".htm", ".xml"}:
        raw = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", raw, flags=re.I | re.S)
        raw = re.sub(r"<[^>]+>", " ", raw)
        raw = html.unescape(raw)
    return raw, {
        "source_sha256": file_sha256(path),
        "parser": "structured-text",
        "parser_version": None,
        "cache_path": str(path.resolve()),
        "output_sha256": text_sha256(raw),
        "output_characters": len(raw),
        "page_count": None,
        "page_aware": False,
        "cache_hit": True,
        "attempts": [],
        "error": None,
    }


def _match_file(path: Path, candidates: list[dict], config: DocumentConfig) -> dict:
    text, extraction = _extract_identity_text(path, config)
    head = text[:30000]
    normalized_head = normalize_text(head)
    detected_dois = sorted(
        {normalize_doi(match.group(0)) for match in DOI_RE.finditer(head)}
    )
    detected_dois = [doi for doi in detected_dois if doi]

    scored = []
    for candidate in candidates:
        method = None
        confidence = 0.0
        candidate_doi = candidate["doi"]
        candidate_title = normalize_text(candidate["title"])

        if candidate_doi and candidate_doi in detected_dois:
            method = "exact_doi"
            confidence = 1.0
        elif candidate_title and candidate_title in normalized_head:
            method = "exact_title"
            confidence = 0.98
        else:
            wanted = title_tokens(candidate["title"])
            present = title_tokens(head)
            coverage = len(wanted & present) / len(wanted) if wanted else 0.0
            if len(wanted) >= 4 and coverage >= 0.85:
                method = "title_token_coverage"
                confidence = round(0.75 + 0.2 * coverage, 3)

        if method:
            scored.append(
                {
                    "citing_work_id": candidate["citing_work_id"],
                    "method": method,
                    "confidence": confidence,
                    "title": candidate["title"],
                }
            )

    scored.sort(key=lambda item: (-item["confidence"], item["citing_work_id"]))
    status = "unmatched"
    chosen = None
    exact_doi_matches = [item for item in scored if item["method"] == "exact_doi"]
    if len(exact_doi_matches) == 1:
        status = "matched"
        chosen = exact_doi_matches[0]
    elif len(exact_doi_matches) > 1:
        status = "ambiguous"
    elif scored:
        separated = (
            len(scored) == 1
            or scored[0]["confidence"] - scored[1]["confidence"] >= 0.08
        )
        if scored[0]["confidence"] >= 0.90 and separated:
            status = "matched"
            chosen = scored[0]
        else:
            status = "ambiguous"

    return {
        "original_path": str(path.resolve()),
        "sha256": file_sha256(path),
        "detected_dois": detected_dois,
        "text_extraction": extraction,
        "text_extraction_error": extraction.get("error"),
        "match": {
            "status": status,
            "citing_work_id": chosen["citing_work_id"] if chosen else None,
            "method": chosen["method"] if chosen else None,
            "confidence": chosen["confidence"] if chosen else None,
            "candidates": scored[:5],
        },
    }


def match_inbox(inbox: Path, works: list[dict], config: DocumentConfig) -> list[dict]:
    """Match arbitrary user filenames to citing works and retain parsed text."""
    candidates = [
        {
            "citing_work_id": work["citing_work_id"],
            "doi": normalize_doi((work.get("resolved") or {}).get("doi")),
            "title": (work.get("resolved") or {}).get("title") or "",
        }
        for work in works
    ]
    files = (
        sorted(
            {
                path.resolve()
                for path in inbox.iterdir()
                if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES
            },
            key=lambda path: str(path).lower(),
        )
        if inbox.exists()
        else []
    )

    matched_files = []
    with ThreadPoolExecutor(max_workers=max(1, min(config.workers, 8))) as executor:
        futures = {
            executor.submit(_match_file, path, candidates, config): path
            for path in files
        }
        for future in as_completed(futures):
            matched_files.append(future.result())
    matched_files.sort(key=lambda item: item["original_path"].lower())
    return matched_files


def extract_fulltexts(
    works: list[dict], target: dict, config: DocumentConfig
) -> list[dict]:
    """Extract all locally available citing documents using one stable interface."""
    aliases = target_aliases(target)
    results = []
    with ThreadPoolExecutor(max_workers=max(1, min(config.workers, 8))) as executor:
        futures = {
            executor.submit(
                _extract_one,
                work["citing_work_id"],
                Path((work.get("full_text") or {})["local_path"]),
                aliases,
                config,
                (work.get("full_text") or {}).get("preparsed"),
            ): work["citing_work_id"]
            for work in works
            if (work.get("full_text") or {}).get("local_path")
        }
        for future in as_completed(futures):
            results.append(future.result())
    results.sort(key=lambda item: item["citing_work_id"])
    return results


class PdfInspector:
    """Inspect and cache the target PDF before metadata discovery."""

    def __init__(self, timeout: int = 120) -> None:
        self.timeout = timeout

    def inspect(self, pdf_path: Path, cache_dir: Path) -> dict:
        resolved = pdf_path.resolve(strict=True)
        with resolved.open("rb") as handle:
            if handle.read(5) != b"%PDF-":
                raise ValueError(f"Input is not a PDF: {resolved}")
        digest = file_sha256(resolved)
        cache_dir.mkdir(parents=True, exist_ok=True)
        text_path = cache_dir / f"target-{digest[:16]}-v2.txt"
        meta_path = cache_dir / f"target-{digest[:16]}-v2.json"
        if text_path.exists() and meta_path.exists():
            metadata = load(meta_path)
            metadata["text"] = text_path.read_text(encoding="utf-8-sig")
            metadata["cache_hit"] = True
            return metadata

        page_count = pdf_page_count(
            resolved, shutil.which("pdfinfo"), min(self.timeout, 30)
        )
        attempts = []
        text = ""
        parser = None
        npx = shutil.which("npx")
        if npx:
            code, stdout, stderr, error = run_process(
                [
                    npx,
                    "--yes",
                    ANYDOC_PACKAGE,
                    str(resolved),
                    "--ocr",
                    "reject",
                ],
                self.timeout,
            )
            attempts.append(
                {
                    "parser": "anydoc",
                    "exit_code": code,
                    "characters": len(stdout),
                    "stderr": stderr[-1000:] or None,
                    "error": error,
                }
            )
            minimum_characters = max(1500, (page_count or 1) * 500)
            if code == 0 and len(stdout.strip()) >= minimum_characters:
                text, parser = stdout, "anydoc"

        pdftotext = shutil.which("pdftotext")
        if not text and pdftotext:
            code, stdout, stderr, error = run_process(
                [
                    pdftotext,
                    "-f",
                    "1",
                    "-l",
                    "12",
                    "-layout",
                    str(resolved),
                    "-",
                ],
                self.timeout,
            )
            attempts.append(
                {
                    "parser": "pdftotext-layout",
                    "exit_code": code,
                    "characters": len(stdout),
                    "stderr": stderr[-1000:] or None,
                    "error": error,
                }
            )
            if code == 0 and stdout.strip():
                text, parser = stdout, "pdftotext-layout"

        if not text:
            try:
                from pypdf import PdfReader  # type: ignore

                reader = PdfReader(str(resolved))
                text = "\n".join(
                    (page.extract_text() or "") for page in reader.pages[:12]
                )
                parser = "pypdf"
                page_count = page_count or len(reader.pages)
            except Exception:
                raw = resolved.read_bytes()[:2_000_000]
                text = "\n".join(
                    value.decode("latin-1", errors="ignore")
                    for value in re.findall(rb"[ -~]{20,}", raw)
                )
                parser = "printable-pdf-strings"
        if not text.strip():
            raise ValueError("No text could be extracted from the target PDF")

        metadata = {
            "sha256": digest,
            "page_count": page_count,
            "parser": parser,
            "parser_version": None,
            "attempts": attempts,
            "cache_hit": False,
        }
        text_path.write_text(text, encoding="utf-8")
        meta_path.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return {**metadata, "text": text}
