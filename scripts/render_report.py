"""Render concise, user-language OpenScite reports from machine artifacts."""

from __future__ import annotations

import json
import re
from pathlib import Path


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def markdown_link(label: str, url: str | None) -> str:
    safe = label.replace("[", "\\[").replace("]", "\\]")
    return f"[{safe}]({url})" if url else safe


def citation_work_id(citation: dict) -> str | None:
    return citation.get("citing_work_id") or (citation.get("citing_paper") or {}).get(
        "citing_work_id"
    )


def locator(citation: dict, zh: bool) -> str:
    if citation.get("page") is not None:
        return f"第 {citation['page']} 頁" if zh else f"p. {citation['page']}"
    if citation.get("section"):
        return str(citation["section"])
    return "未辨識章節" if zh else "section not identified"


def best_user_link(work: dict) -> str | None:
    candidates = (work.get("full_text") or {}).get("candidate_urls") or []
    priority = {"oa_pdf": 0, "oa_fulltext": 1, "doi": 2}
    visible = [
        candidate
        for candidate in candidates
        if candidate.get("kind") in priority and candidate.get("url")
    ]
    visible.sort(key=lambda candidate: priority[candidate["kind"]])
    if visible:
        return visible[0]["url"]
    return (work.get("resolved") or {}).get("doi")


def render_requests(works: list[dict], zh: bool) -> str:
    pending = [
        work
        for work in works
        if work.get("context_status")
        in {"awaiting_user_full_text_and_context", "extraction_failed"}
    ]
    pending.sort(key=lambda work: (work.get("selection") or {}).get("rank") or 10**9)
    if zh:
        lines = [
            f"# 尚缺全文（{len(pending)} 篇）",
            "",
            "請下載你有權存取的版本，直接附加檔案或放入 `fulltext/inbox/`。不必改檔名，OpenScite 會用 DOI 與標題自動配對。",
            "",
            "| 論文 | 年份 | 下載或論文頁 |",
            "|---|---:|---|",
        ]
    else:
        lines = [
            f"# Full text needed ({len(pending)} papers)",
            "",
            "Download a copy you are authorized to access, then attach it or place it in `fulltext/inbox/`. Keep the original filename; OpenScite matches DOI and title automatically.",
            "",
            "| Paper | Year | Download or article page |",
            "|---|---:|---|",
        ]
    for work in pending:
        resolved = work.get("resolved") or {}
        metadata = work.get("ranking_metadata") or {}
        title = str(resolved.get("title") or "Untitled").replace("|", "\\|")
        url = best_user_link(work)
        action = markdown_link("開啟" if zh else "open", url) if url else "—"
        lines.append(
            f"| {title} | {metadata.get('publication_year') or '—'} | {action} |"
        )
    return "\n".join(lines) + "\n"


def render_report(run: dict, works: list[dict], citations: list[dict], zh: bool) -> str:
    work_by_id = {work.get("citing_work_id"): work for work in works}
    grouped: dict[str, dict[str, list[dict]]] = {
        label: {} for label in ("contrasting", "supporting", "mentioning", "unknown")
    }
    for citation in citations:
        label = citation.get("stance", "unknown")
        work_id = (
            citation_work_id(citation) or citation.get("statement_id") or "unknown"
        )
        grouped.setdefault(label, {}).setdefault(work_id, []).append(citation)
    target = run.get("target") or {}
    counts = run.get("counts") or {}
    target_link = markdown_link(
        target.get("title") or "Target paper", target.get("doi")
    )
    selected = counts.get("citing_works_selected", len(works))
    contextual = counts.get("citing_works_with_context", 0)
    discovered = counts.get("citing_works_discovered", 0)

    def stance_count(label: str) -> str:
        papers = len(grouped[label])
        passages = sum(len(items) for items in grouped[label].values())
        return (
            f"{papers} 篇／{passages} 段"
            if zh
            else f"{papers} papers / {passages} passages"
        )

    if zh:
        lines = [
            f"# 引用立場分析：{target.get('title') or '目標論文'}",
            "",
            f"目標論文：{target_link}",
            "",
            f"找到 **{discovered}** 篇引用論文，篩選 **{selected}** 篇；目前有 **{contextual}** 篇取得可驗證的引用上下文。",
            "",
            f"- 反駁或限定：**{stance_count('contrasting')}**",
            f"- 支持：**{stance_count('supporting')}**",
            f"- 一般提及：**{stance_count('mentioning')}**",
            f"- 尚無法判定：**{stance_count('unknown')}**",
            "",
            "> 摘要篩選只決定閱讀順序；最終立場只依實際引用段落判定。",
            "",
        ]
    else:
        lines = [
            f"# Citation stance analysis: {target.get('title') or 'Target paper'}",
            "",
            f"Target: {target_link}",
            "",
            f"Found **{discovered}** citing papers and selected **{selected}**; **{contextual}** currently have verifiable citation context.",
            "",
            f"- Contrasting or qualifying: **{stance_count('contrasting')}**",
            f"- Supporting: **{stance_count('supporting')}**",
            f"- General mentions: **{stance_count('mentioning')}**",
            f"- Not yet classifiable: **{stance_count('unknown')}**",
            "",
            "> Abstract triage controls reading order only; final stance is based on the actual citation passage.",
            "",
        ]

    def evidence_section(label: str, heading_zh: str, heading_en: str) -> None:
        lines.extend([f"## {heading_zh if zh else heading_en}", ""])
        if not grouped[label]:
            lines.extend(["目前沒有可靠案例。" if zh else "No reliable cases yet.", ""])
            return
        for work_id, evidence in grouped[label].items():
            work = work_by_id.get(work_id, {})
            resolved = work.get("resolved") or {}
            fallback_paper = (evidence[0].get("citing_paper") or {}) if evidence else {}
            title = markdown_link(
                resolved.get("title") or fallback_paper.get("title") or "Untitled",
                resolved.get("doi") or fallback_paper.get("doi"),
            )
            ranked = sorted(
                evidence,
                key=lambda citation: (
                    -float(citation.get("confidence") or 0),
                    citation.get("statement_id") or "",
                ),
            )
            confidence = ranked[0].get("confidence")
            confidence_text = (
                f"{round(float(confidence) * 100)}%" if confidence is not None else "—"
            )
            lines.extend(
                [
                    f"### {title}",
                    "",
                    (
                        f"共 {len(evidence)} 段引用；最高信心：{confidence_text}"
                        if zh
                        else f"{len(evidence)} citation passages; highest confidence: {confidence_text}"
                    ),
                    "",
                ]
            )
            for citation in ranked[:2]:
                context = re.sub(
                    r"\s+", " ", str(citation.get("context_text") or "")
                ).strip()
                lines.extend(
                    [
                        f"**{('位置：' if zh else 'Location: ')}{locator(citation, zh)}**",
                        "",
                        f"> {context or ('未取得引用上下文' if zh else 'Citation context unavailable')}",
                        "",
                        (
                            f"判定：{citation.get('reason') or '—'}"
                            if zh
                            else f"Assessment: {citation.get('reason') or '—'}"
                        ),
                        "",
                    ]
                )
            if len(ranked) > 2:
                lines.extend(
                    [
                        (
                            f"另有 {len(ranked) - 2} 段，詳見 `citations.json`。"
                            if zh
                            else f"{len(ranked) - 2} more passages are available in `citations.json`."
                        ),
                        "",
                    ]
                )

    evidence_section("contrasting", "反駁或限定", "Contrasting or qualifying")
    evidence_section("supporting", "支持", "Supporting")

    lines.extend(["## 一般提及" if zh else "## General mentions", ""])
    if grouped["mentioning"]:
        lines.extend(
            [
                "| 論文 | 引用段落 | 摘要 |"
                if zh
                else "| Paper | Citation passages | Summary |",
                "|---|---|---|",
            ]
        )
        for work_id, evidence in grouped["mentioning"].items():
            work = work_by_id.get(work_id, {})
            resolved = work.get("resolved") or {}
            fallback_paper = (evidence[0].get("citing_paper") or {}) if evidence else {}
            title = markdown_link(
                resolved.get("title") or fallback_paper.get("title") or "Untitled",
                resolved.get("doi") or fallback_paper.get("doi"),
            )
            locations = list(
                dict.fromkeys(locator(citation, zh) for citation in evidence)
            )
            reason = str(evidence[0].get("reason") or "—").replace("|", "\\|")
            count_text = f"{len(evidence)} 段" if zh else f"{len(evidence)} passages"
            lines.append(
                f"| {title} | {count_text} | {', '.join(locations[:3])}；{reason} |"
            )
    else:
        lines.append("目前沒有可靠案例。" if zh else "No reliable cases yet.")
    lines.append("")

    pending = sum(
        work.get("context_status")
        in {"awaiting_user_full_text_and_context", "extraction_failed"}
        for work in works
    )
    no_context = sum(work.get("context_status") == "no_context_found" for work in works)
    if zh:
        lines.extend(
            [
                "## 尚無法判定",
                "",
                f"尚有 **{pending}** 篇需要全文或重新解析，另有 **{no_context}** 篇未能可靠綁定引用位置。詳見 [全文清單](fulltext-requests.md)。",
                "",
                "## 判讀範圍",
                "",
                "立場是針對每一個引用段落，不代表整篇 citing paper 對目標論文的總體態度。",
                "",
            ]
        )
    else:
        lines.extend(
            [
                "## Not yet classifiable",
                "",
                f"**{pending}** papers still need full text or re-extraction; **{no_context}** had no reliably bound citation location. See [full-text requests](fulltext-requests.md).",
                "",
                "## Scope",
                "",
                "Stance is assessed per citation passage and does not describe the citing paper as a whole.",
                "",
            ]
        )
    return "\n".join(lines)


def render_artifacts(run_dir: Path, language: str) -> None:
    run_dir = run_dir.resolve()
    run = load(run_dir / "run.json")
    works = load(run_dir / "citing-works.json").get("works", [])
    citations = load(run_dir / "citations.json").get("citations", [])
    zh = language == "zh-TW"
    (run_dir / "fulltext" / "inbox").mkdir(parents=True, exist_ok=True)
    (run_dir / "report.md").write_text(
        render_report(run, works, citations, zh) + "\n", encoding="utf-8"
    )
    (run_dir / "fulltext-requests.md").write_text(
        render_requests(works, zh), encoding="utf-8"
    )
