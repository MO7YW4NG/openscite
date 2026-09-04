import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.documents import DocumentConfig, extract_fulltexts, match_inbox
from scripts.openscite_core import (
    PrepareConfig,
    StageCache,
    _apply_inbox_matches,
    _build_citations,
    _parse_fulltexts,
    _triage_pool,
    _triage_pending_packets,
    _triage_packets,
    bind_target_contexts,
    extract_target_identity,
    finalize_run,
    prepare_run,
    rank_candidates,
    reconcile_analysis_results,
)
from scripts.render_report import render_report


TARGET = {
    "id": "https://openalex.org/WTARGET",
    "doi": "https://doi.org/10.1234/target",
    "title": "A Target Effect in Social Psychology",
    "display_name": "A Target Effect in Social Psychology",
    "publication_year": 2016,
    "cited_by_count": 100,
    "authorships": [
        {"author": {"display_name": "Martin Hagger"}, "author_position": "first"}
    ],
    "abstract": "Two experiments reported a reliable target effect.",
}


def citing_work(
    work_id: str,
    title: str,
    abstract: str | None,
    citations: int,
    year: int = 2020,
    source_id: str = "https://openalex.org/S1",
) -> dict:
    return {
        "id": f"https://openalex.org/{work_id}",
        "doi": f"https://doi.org/10.5555/{work_id.lower()}",
        "title": title,
        "display_name": title,
        "publication_year": year,
        "cited_by_count": citations,
        "authorships": [
            {"author": {"display_name": "Ada Researcher"}, "author_position": "first"}
        ],
        "abstract": abstract,
        "primary_location": {
            "source": {
                "id": source_id,
                "display_name": "Journal of Tests",
                "issn_l": "1234-5678",
            }
        },
        "best_oa_location": None,
        "locations": [],
    }


class FakeInspector:
    calls = 0

    def inspect(self, pdf_path: Path, cache_dir: Path) -> dict:
        self.calls += 1
        data = pdf_path.read_bytes()
        return {
            "sha256": hashlib.sha256(data).hexdigest(),
            "page_count": 2,
            "parser": "fixture",
            "text": (
                "A Target Effect in Social Psychology\n"
                "Martin Hagger\n"
                "doi:10.1234/target\n"
                "Two experiments reported a reliable target effect."
            ),
        }


class FakeProvider:
    def __init__(self) -> None:
        self.resolve_calls = 0
        self.incoming_calls = 0
        self.source_calls = 0
        self.works = [
            TARGET,
            citing_work(
                "WCONTRAST",
                "A failed replication of the target effect",
                "We found no evidence and results contradicted the original claim.",
                50,
            ),
            citing_work(
                "WSUPPORT",
                "A successful replication of the target effect",
                "Results were consistent with and replicated the original finding.",
                40,
            ),
            citing_work(
                "WMENTION",
                "A broad review of social psychology",
                "The target paper is mentioned as historical background.",
                400,
            ),
        ]

    def resolve_target(self, identity: dict) -> dict:
        self.resolve_calls += 1
        return TARGET

    def incoming_citations(self, target_id: str) -> list[dict]:
        self.incoming_calls += 1
        return self.works

    def source_metrics(self, source_ids: list[str]) -> dict[str, dict]:
        self.source_calls += 1
        return {
            "https://openalex.org/S1": {
                "id": "https://openalex.org/S1",
                "summary_stats": {"2yr_mean_citedness": 3.2, "h_index": 55},
            }
        }


class ParserRoutingTests(unittest.TestCase):
    def test_page_aware_mode_skips_anydoc(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "paper.pdf"
            source.write_bytes(b"%PDF-1.4\nfixture\n%%EOF")
            text = "doi:10.1234/target " + ("page-aware evidence " * 100)
            works = [
                {
                    "citing_work_id": "W1",
                    "full_text": {"local_path": str(source)},
                }
            ]
            config = DocumentConfig(
                cache_root=root / "cache",
                require_page_aware=True,
            )

            def find_tool(name: str) -> str | None:
                return name if name in {"npx", "pdftotext"} else None

            with (
                patch("scripts.documents.shutil.which", side_effect=find_tool),
                patch(
                    "scripts.documents.run_process",
                    return_value=(0, text, "", None),
                ) as run,
            ):
                results = extract_fulltexts(works, {"resolved": TARGET}, config)

        self.assertEqual(results[0]["parser"], "pdftotext-layout")
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][0], "pdftotext")

    def test_unchanged_fulltext_uses_per_document_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "paper.html"
            source.write_text(
                "<html><body><p>doi:10.1234/target "
                + " ".join(f"evidence{i}" for i in range(300))
                + "</p></body></html>",
                encoding="utf-8",
            )
            works = [
                {
                    "citing_work_id": "W1",
                    "full_text": {"local_path": str(source)},
                }
            ]
            config = DocumentConfig(cache_root=root / "cache", timeout=10)

            first = extract_fulltexts(works, {"resolved": TARGET}, config)[0]
            second = extract_fulltexts(works, {"resolved": TARGET}, config)[0]

            self.assertEqual(first["status"], "parsed")
            self.assertFalse(first["cache_hit"])
            self.assertTrue(second["cache_hit"])

    def test_valid_anydoc_stays_canonical_when_neither_parser_finds_reference(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "paper.pdf"
            source.write_bytes(b"%PDF-1.4\nfixture\n%%EOF")
            cache_dir = root / "cache"
            cache_dir.mkdir()
            anydoc_text = "Anydoc structured text. " * 100
            fallback_text = "Poppler text. " * 100

            with patch(
                "scripts.documents.run_process",
                side_effect=[
                    (0, anydoc_text, "", None),
                    (0, fallback_text, "", None),
                ],
            ):
                with patch(
                    "scripts.documents.shutil.which",
                    side_effect=lambda name: name
                    if name in {"npx", "pdftotext"}
                    else None,
                ):
                    result = extract_fulltexts(
                        [
                            {
                                "citing_work_id": "W1",
                                "full_text": {"local_path": str(source)},
                            }
                        ],
                        {
                            "resolved": {
                                **TARGET,
                                "doi": "10.1234/missing",
                            },
                            "identity_evidence": {
                                "doi": "10.1234/missing",
                                "authors": [],
                            },
                        },
                        DocumentConfig(cache_root=cache_dir, timeout=10),
                    )[0]

            self.assertEqual(result["parser"], "anydoc")
            self.assertFalse(result["target_reference_found"])
            self.assertEqual(
                [attempt["parser"] for attempt in result["attempts"]],
                ["anydoc", "pdftotext-layout"],
            )


class UserFileMatchingTests(unittest.TestCase):
    def test_pdf_identity_extraction_prefers_anydoc_and_reuses_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "arbitrary.pdf"
            source.write_bytes(b"%PDF-1.4\nfixture\n%%EOF")
            extracted = "# Paper title\n\ndoi:10.5555/paper\n\n" + ("body " * 300)

            def find_tool(name: str) -> str | None:
                return name if name in {"npx", "pdftotext"} else None

            works = [
                {
                    "citing_work_id": "W1",
                    "resolved": {"doi": "10.5555/paper", "title": "Paper title"},
                }
            ]
            config = DocumentConfig(cache_root=root / "cache")
            with (
                patch("scripts.documents.shutil.which", side_effect=find_tool),
                patch(
                    "scripts.documents.run_process",
                    return_value=(0, extracted, "", None),
                ) as run,
            ):
                first = match_inbox(root, works, config)[0]
                second = match_inbox(root, works, config)[0]

            first_meta = first["text_extraction"]
            second_meta = second["text_extraction"]

            self.assertEqual(first_meta["parser"], "anydoc")
            self.assertFalse(first_meta["cache_hit"])
            self.assertTrue(second_meta["cache_hit"])
            self.assertEqual(run.call_count, 1)
            self.assertIn("@firecrawl/anydoc", run.call_args_list[0].args[0])
            self.assertNotIn("@firecrawl/anydoc@", run.call_args_list[0].args[0])

    def test_exact_doi_match_dominates_weaker_title_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "download.txt"
            source.write_text(
                "doi:10.5555/exact\n"
                "A competing title appearing in the document\n" + ("content " * 100),
                encoding="utf-8",
            )
            works = [
                {
                    "citing_work_id": "WDOI",
                    "resolved": {"doi": "10.5555/exact", "title": "Exact DOI paper"},
                },
                {
                    "citing_work_id": "WTITLE",
                    "resolved": {
                        "doi": "10.5555/other",
                        "title": "A competing title appearing in the document",
                    },
                },
            ]
            result = match_inbox(
                root, works, DocumentConfig(cache_root=root / "cache")
            )[0]

        self.assertEqual(result["match"]["status"], "matched")
        self.assertEqual(result["match"]["citing_work_id"], "WDOI")
        self.assertEqual(result["match"]["method"], "exact_doi")

    def test_runner_reuses_inbox_anydoc_output_during_fulltext_parse(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            run_dir = root / "run"
            inbox = run_dir / "fulltext" / "inbox"
            inbox.mkdir(parents=True)
            source = inbox / "opaque-name.pdf"
            source.write_bytes(b"%PDF-1.4\nfixture\n%%EOF")
            extracted = (
                "# A failed replication of the target effect\n\n"
                "doi:10.5555/wcontrast\n\n"
                "The study cites doi:10.1234/target and reports no effect.\n\n"
                + ("evidence " * 300)
            )
            works = [
                {
                    "citing_work_id": "WCONTRAST",
                    "resolved": {
                        "doi": "10.5555/wcontrast",
                        "title": "A failed replication of the target effect",
                    },
                    "full_text": {"local_path": None},
                }
            ]
            config = PrepareConfig(
                target_pdf=root / "target.pdf",
                run_dir=run_dir,
                n=1,
                download_fulltext=False,
            )
            target_doc = {
                "resolved": TARGET,
                "identity_evidence": {
                    "doi": "10.1234/target",
                    "title": TARGET["title"],
                    "authors": ["Martin Hagger"],
                },
            }

            def find_tool(name: str) -> str | None:
                return name if name == "npx" else None

            with (
                patch("scripts.documents.shutil.which", side_effect=find_tool),
                patch(
                    "scripts.documents.run_process",
                    return_value=(0, extracted, "", None),
                ) as document_run,
            ):
                _apply_inbox_matches(config, works)
                calls_after_match = document_run.call_count
                parsed = _parse_fulltexts(config, target_doc, works)

            self.assertEqual(calls_after_match, 1)
            self.assertEqual(document_run.call_count, calls_after_match)
            self.assertEqual(parsed["results"][0]["parser"], "anydoc")
            self.assertTrue(parsed["results"][0]["attempts"][0]["cache_hit"])
            self.assertEqual(
                parsed["results"][0]["attempts"][0]["reused_from"],
                "file_identity",
            )


class StageCacheTests(unittest.TestCase):
    def test_cache_requires_matching_fingerprint_and_existing_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            artifact = root / "inventory.json"
            artifact.write_text("{}", encoding="utf-8")
            cache = StageCache(root / ".cache")

            cache.record("discovery", "abc", [artifact], 1.25)

            self.assertTrue(cache.is_hit("discovery", "abc", [artifact]))
            self.assertFalse(cache.is_hit("discovery", "changed", [artifact]))
            artifact.unlink()
            self.assertFalse(cache.is_hit("discovery", "abc", [artifact]))


class TargetIdentityTests(unittest.TestCase):
    def test_jstor_front_matter_does_not_append_authors_to_title(self) -> None:
        text = (
            "Power Posing: Brief Nonverbal Displays Affect Neuroendocrine Levels and Risk Tolerance "
            "Author(s): Dana R. Carney, Amy J.C. Cuddy and Andy J. Yap\n\n"
            "Source: Psychological Science, October 2010\n"
        )

        identity = extract_target_identity(text)

        self.assertEqual(
            identity["title"],
            "Power Posing: Brief Nonverbal Displays Affect Neuroendocrine Levels and Risk Tolerance",
        )

    def test_recovers_ocr_confused_doi_from_front_matter(self) -> None:
        text = (
            "Power Posing: Brief Nonverbal Displays Affect Neuroendocrine Levels and Risk Tolerance\n"
            "Author(s): Dana R. Carney, Amy J.C. Cuddy and Andy J. Yap\n"
            "Source: Psychological Science, OCTOBER 2010\n\f"
            "DOI: I O.I 177/0956797610383437\n"
        )

        identity = extract_target_identity(text)

        self.assertEqual(identity["doi"], "10.1177/0956797610383437")
        self.assertEqual(identity["publication_year"], 2010)
        self.assertEqual(identity["authors"][0], "Dana R. Carney")


class RankingTests(unittest.TestCase):
    def test_stance_first_excludes_self_edge_and_prefers_evidence_cues(self) -> None:
        works = [
            TARGET,
            citing_work(
                "WCONTRAST",
                "Failed replication",
                "We found no evidence for the target effect and contradicted the claim.",
                20,
            ),
            citing_work(
                "WSUPPORT",
                "Independent replication",
                "Our experiment replicated and supported the target effect.",
                10,
            ),
            citing_work(
                "WPOPULAR",
                "General review",
                "A historical overview mentioning social psychology.",
                10_000,
            ),
        ]

        selected, diagnostics = rank_candidates(
            works=works,
            target=TARGET,
            claims=[
                {"claim_id": "claim-01", "claim": "The target effect is reliable."}
            ],
            n=2,
            mode="stance_first",
            source_metrics={},
            model_screens={},
        )

        self.assertEqual(
            [work["openalex_id"] for work in selected],
            ["WCONTRAST", "WSUPPORT"],
        )
        self.assertEqual(diagnostics["self_edges_excluded"], 1)

    def test_prefilter_limits_model_triage_to_three_times_n_with_a_floor_of_sixty(
        self,
    ) -> None:
        works = [
            citing_work(f"W{index}", f"Paper {index}", "An empirical study.", index)
            for index in range(100)
        ]

        pool = _triage_pool(works, TARGET, [], n=20)

        self.assertEqual(len(pool), 60)


class ContextBindingTests(unittest.TestCase):
    def test_numeric_reference_binding_stays_out_of_bibliography(self) -> None:
        text = (
            "Introduction\nEarlier work established the hypothesis.\f"
            "Results\nUnlike Hagger et al. [9], we found no evidence for the effect. "
            "The estimate included zero.\f"
            "References\n[9] Hagger, M. (2016). A Target Effect in Social Psychology. "
            "https://doi.org/10.1234/target\n"
        )

        contexts = bind_target_contexts(text, TARGET, page_aware=True)

        self.assertEqual(len(contexts), 1)
        self.assertEqual(contexts[0]["marker"], "[9]")
        self.assertEqual(contexts[0]["page"], 2)
        self.assertIn("Hagger et al. [9]", contexts[0]["context"])
        self.assertNotIn("References", contexts[0]["context"])

    def test_multiauthor_target_does_not_treat_coauthors_as_citation_aliases(
        self,
    ) -> None:
        target = {
            **TARGET,
            "authors": ["Martin Hagger", "Ada Cheung", "Robin Ridder"],
            "authorships": [],
        }
        text = (
            "Discussion\nCheung (2016) described an unrelated intervention. "
            "Ridder et al. (2016) studied a different population."
        )

        self.assertEqual(bind_target_contexts(text, target, page_aware=False), [])

    def test_recovers_numbered_marker_without_references_heading(self) -> None:
        text = (
            "Results\nUnlike Hagger et al.[10], we found no reliable effect. "
            "The confidence interval crossed zero.\n\n"
            "10. Hagger, M. et al. (2016). A Target Effect in Social Psychology. "
            "doi:10.1234/target"
        )

        contexts = bind_target_contexts(text, TARGET, page_aware=False)

        self.assertEqual(len(contexts), 1)
        self.assertEqual(contexts[0]["marker"], "[10]")
        self.assertIn("Hagger et al.[10]", contexts[0]["context"])
        self.assertNotIn("doi:10.1234/target", contexts[0]["context"])

    def test_overlapping_context_windows_are_deduplicated(self) -> None:
        text = (
            "Results\nHagger et al. [9] predicted an effect, and our preregistered test of "
            "that prediction [9] found no evidence. The estimate included zero.\n\n"
            "References\n[9] Hagger, M. (2016). A Target Effect in Social Psychology. "
            "doi:10.1234/target"
        )

        contexts = bind_target_contexts(text, TARGET, page_aware=False)

        self.assertEqual(len(contexts), 1)


class PacketContractTests(unittest.TestCase):
    def test_triage_pending_rows_only_contain_model_inputs(self) -> None:
        pool = [
            {
                "openalex_id": "W1",
                "title": "Paper one",
                "abstract": "An abstract",
                "publication_year": 2024,
                "rule_screen": {"contrast_signal": 0.5},
            },
            {
                "openalex_id": "W2",
                "title": "Paper two",
                "abstract": "Another abstract",
                "publication_year": 2023,
                "rule_screen": {"support_signal": 0.5},
            },
        ]
        claims = [{"claim_id": "claim-01", "claim": "The effect is reliable."}]
        packets = _triage_packets(pool, claims, TARGET)
        completed = {
            "citing_work_id": "W1",
            "input_hash": packets[0]["input_hash"],
            "priority_score": 0.9,
            "priority_lane": "contrast",
        }

        pending, valid, diagnostics = _triage_pending_packets(packets, [completed])

        self.assertNotIn("target", packets[0])
        self.assertNotIn("required_output", packets[0])
        self.assertNotIn("rule_screen", packets[0])
        self.assertNotIn("year", packets[0]["citing_paper"])
        self.assertEqual([item["citing_work_id"] for item in pending], ["W2"])
        self.assertEqual(set(valid), {"W1"})
        self.assertEqual(diagnostics, [])

    def test_analysis_reconciliation_groups_only_pending_statements_by_paper(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            extracted = root / "parsed.txt"
            extracted.write_text(
                "Results\nHagger et al. (2016) predicted an effect.\n\n"
                "Discussion\nHagger et al. (2016) was not replicated.",
                encoding="utf-8",
            )
            works = [
                {
                    "citing_work_id": "W1",
                    "resolved": {
                        "title": "Two citations",
                        "doi": "https://doi.org/10.1/w1",
                    },
                    "full_text": {"local_path": str(extracted)},
                }
            ]
            parse_doc = {
                "results": [
                    {
                        "citing_work_id": "W1",
                        "status": "parsed",
                        "cache_path": str(extracted),
                        "page_aware": False,
                    }
                ]
            }
            target_doc = {
                "resolved": {**TARGET, "authors": ["Martin Hagger"]},
                "identity_evidence": {"authors": ["Martin Hagger"]},
            }
            citations = _build_citations(target_doc, works, parse_doc)
            first = citations[0]
            completed = {
                "statement_id": first["statement_id"],
                "context_hash": first["context_hash"],
                "stance": "mentioning",
                "confidence": 0.8,
                "reason": "Background attribution only.",
                "label_source": "model",
                "target_claim_id": "claim-01",
            }
            merged, pending, diagnostics = reconcile_analysis_results(
                citations, [completed]
            )

        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["citing_work_id"], "W1")
        self.assertEqual(len(pending[0]["statements"]), 1)
        self.assertNotIn("target_claims", pending[0])
        self.assertEqual(merged[0]["stance"], "mentioning")
        self.assertEqual(merged[1]["stance"], "unknown")
        self.assertEqual(diagnostics, [])


class AnalysisMergeTests(unittest.TestCase):
    def test_only_valid_model_result_can_set_a_non_unknown_stance(self) -> None:
        citations = [
            {
                "statement_id": "stmt-1",
                "citing_work_id": "WCONTRAST",
                "stance": "unknown",
                "confidence": 0,
                "reason": "Awaiting analysis",
                "label_source": "rule",
            }
        ]
        citations[0]["context_hash"] = "sha256:good"
        citations[0]["context_text"] = "A target-bound passage."
        results = [
            {
                "statement_id": "stmt-1",
                "context_hash": "sha256:good",
                "stance": "contrasting",
                "confidence": 0.91,
                "reason": "The citing result reports a null effect.",
                "label_source": "model",
            }
        ]

        merged, pending, diagnostics = reconcile_analysis_results(citations, results)

        self.assertEqual(merged[0]["stance"], "contrasting")
        self.assertEqual(merged[0]["label_source"], "model")
        self.assertEqual(pending, [])
        self.assertEqual(diagnostics, [])

    def test_context_hash_mismatch_is_rejected(self) -> None:
        citations = [{"statement_id": "stmt-1", "stance": "unknown"}]
        citations[0]["context_hash"] = "sha256:good"
        citations[0]["context_text"] = "A target-bound passage."
        results = [
            {
                "statement_id": "stmt-1",
                "context_hash": "sha256:tampered",
                "stance": "supporting",
                "confidence": 0.8,
                "reason": "Claimed replication.",
                "label_source": "model",
            }
        ]

        merged, pending, diagnostics = reconcile_analysis_results(citations, results)

        self.assertEqual(merged[0]["stance"], "unknown")
        self.assertEqual(pending[0]["statements"][0]["statement_id"], "stmt-1")
        self.assertIn("context_hash mismatch", diagnostics[0])

    def test_finalize_preserves_valid_results_and_retries_only_missing_statements(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir)
            citations = [
                {
                    "statement_id": f"stmt-{index}",
                    "citing_work_id": "W1",
                    "citing_paper": {
                        "citing_work_id": "W1",
                        "title": "Paper",
                        "doi": None,
                    },
                    "context_text": f"Passage {index}",
                    "context_hash": f"sha256:{index}",
                    "stance": "unknown",
                    "confidence": 0,
                    "reason": "Awaiting stance analysis.",
                    "label_source": "rule",
                }
                for index in (1, 2)
            ]
            run = {
                "schema_version": "openscite.run.v3",
                "status": "needs_analysis",
                "language": "zh-TW",
                "target": {"title": "Target", "doi": None},
                "counts": {"citing_works_discovered": 1},
            }
            works = [
                {
                    "citing_work_id": "W1",
                    "resolved": {"title": "Paper", "doi": None},
                    "full_text": {"local_path": "paper.pdf"},
                    "context_status": "context_bound",
                }
            ]
            for name, value in (
                ("run.json", run),
                ("citing-works.json", {"works": works}),
                ("citations.json", {"citations": citations}),
            ):
                (run_dir / name).write_text(json.dumps(value), encoding="utf-8")

            def result(index: int) -> dict:
                return {
                    "statement_id": f"stmt-{index}",
                    "context_hash": f"sha256:{index}",
                    "stance": "supporting",
                    "confidence": 0.8,
                    "reason": "Reports aligned evidence.",
                    "label_source": "model",
                }

            (run_dir / "analysis-results.jsonl").write_text(
                json.dumps(result(1)) + "\n", encoding="utf-8"
            )
            first = finalize_run(run_dir)
            pending = [
                json.loads(line)
                for line in (run_dir / "analysis-pending.jsonl")
                .read_text()
                .splitlines()
                if line
            ]
            (run_dir / "analysis-results.jsonl").write_text(
                json.dumps(result(1)) + "\n" + json.dumps(result(2)) + "\n",
                encoding="utf-8",
            )
            second = finalize_run(run_dir)

        self.assertEqual(first["status"], "needs_analysis")
        self.assertEqual(
            [item["statement_id"] for item in pending[0]["statements"]], ["stmt-2"]
        )
        self.assertEqual(second["status"], "complete")


class ReportTests(unittest.TestCase):
    def test_report_counts_papers_first_and_groups_repeated_passages(self) -> None:
        works = [
            {
                "citing_work_id": "WSUPPORT",
                "resolved": {
                    "title": "Supporting paper",
                    "doi": "https://doi.org/10.1/support",
                },
                "context_status": "context_bound",
            },
            {
                "citing_work_id": "WMENTION",
                "resolved": {
                    "title": "Mentioning paper",
                    "doi": "https://doi.org/10.1/mention",
                },
                "context_status": "context_bound",
            },
        ]
        citations = []
        for work_id, stance, count in (
            ("WSUPPORT", "supporting", 2),
            ("WMENTION", "mentioning", 2),
        ):
            for index in range(count):
                citations.append(
                    {
                        "statement_id": f"{work_id}-{index}",
                        "citing_work_id": work_id,
                        "stance": stance,
                        "confidence": 0.9,
                        "context_text": f"Evidence passage {index}.",
                        "reason": "Reports relevant evidence."
                        if stance == "supporting"
                        else "Background only.",
                        "page": None,
                        "section": None,
                    }
                )
        run = {
            "target": {"title": "Target", "doi": "https://doi.org/10.1/target"},
            "counts": {
                "citing_works_discovered": 10,
                "citing_works_selected": 2,
                "citing_works_with_context": 2,
            },
        }

        report = render_report(run, works, citations, zh=True)

        self.assertIn("支持：**1 篇／2 段**", report)
        self.assertIn("一般提及：**1 篇／2 段**", report)
        self.assertEqual(report.count("### [Supporting paper]"), 1)
        self.assertIn("未辨識章節", report)
        self.assertIn("| [Mentioning paper]", report)
        self.assertIn("| 2 段 |", report)


class PrepareIntegrationTests(unittest.TestCase):
    def test_prepare_writes_shared_triage_context_and_retries_only_pending_rows(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"%PDF-1.4\nfixture\n%%EOF")
            claims_file = root / "target-claims.json"
            claims_file.write_text(
                json.dumps(
                    [{"claim_id": "claim-01", "claim": "The effect is reliable."}]
                ),
                encoding="utf-8",
            )
            run_dir = root / "run"
            config = PrepareConfig(
                target_pdf=pdf,
                run_dir=run_dir,
                n=2,
                claims_file=claims_file,
                download_fulltext=False,
            )
            provider = FakeProvider()
            inspector = FakeInspector()

            first = prepare_run(config, provider=provider, inspector=inspector)
            packets = [
                json.loads(line)
                for line in (run_dir / "triage-pending.jsonl").read_text().splitlines()
                if line
            ]
            result = {
                "citing_work_id": packets[0]["citing_work_id"],
                "input_hash": packets[0]["input_hash"],
                "priority_score": 0.9,
                "priority_lane": "contrast",
            }
            (run_dir / "triage-results.jsonl").write_text(
                json.dumps(result) + "\n", encoding="utf-8"
            )
            second = prepare_run(config, provider=provider, inspector=inspector)
            pending = [
                json.loads(line)
                for line in (run_dir / "triage-pending.jsonl").read_text().splitlines()
                if line
            ]
            context = json.loads((run_dir / "triage-context.json").read_text())
            all_results = []
            for index, packet in enumerate(packets):
                all_results.append(
                    {
                        "citing_work_id": packet["citing_work_id"],
                        "input_hash": packet["input_hash"],
                        "priority_score": 0.9 - index * 0.1,
                        "priority_lane": ("contrast", "support", "exploration")[index],
                    }
                )
            (run_dir / "triage-results.jsonl").write_text(
                "".join(json.dumps(item) + "\n" for item in all_results),
                encoding="utf-8",
            )
            third = prepare_run(config, provider=provider, inspector=inspector)

        self.assertEqual(first["status"], "needs_abstract_triage")
        self.assertEqual(second["status"], "needs_abstract_triage")
        self.assertEqual(third["status"], "needs_user_files")
        self.assertEqual(len(pending), len(packets) - 1)
        self.assertNotIn(
            result["citing_work_id"], {item["citing_work_id"] for item in pending}
        )
        self.assertNotIn("target", packets[0])
        self.assertEqual(context["target"]["title"], TARGET["title"])
        self.assertFalse((run_dir / "triage-packets.jsonl").exists())
        self.assertFalse((run_dir / "triage-validation.json").exists())

    def test_second_prepare_reuses_completed_metadata_stages(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pdf = root / "arbitrary-user-filename.pdf"
            pdf.write_bytes(b"%PDF-1.4\nfixture\n%%EOF")
            run_dir = root / "run"
            claims_file = root / "target-claims.json"
            claims_file.write_text(
                json.dumps(
                    [
                        {
                            "claim_id": "claim-01",
                            "claim": "The target effect is reliable.",
                            "source": "target",
                        }
                    ]
                ),
                encoding="utf-8",
            )
            provider = FakeProvider()
            inspector = FakeInspector()
            config = PrepareConfig(
                target_pdf=pdf,
                run_dir=run_dir,
                n=2,
                mode="stance_first",
                language="zh-TW",
                claims_file=claims_file,
                rule_triage=True,
                download_fulltext=False,
            )

            first = prepare_run(config, provider=provider, inspector=inspector)
            calls_after_first = (
                provider.resolve_calls,
                provider.incoming_calls,
                provider.source_calls,
                inspector.calls,
            )
            second = prepare_run(config, provider=provider, inspector=inspector)

            self.assertEqual(first["status"], "needs_user_files")
            self.assertIn("browser-assisted", first["run"]["next_action"])
            self.assertEqual(second["status"], "needs_user_files")
            self.assertEqual(
                (
                    provider.resolve_calls,
                    provider.incoming_calls,
                    provider.source_calls,
                    inspector.calls,
                ),
                calls_after_first,
            )
            self.assertTrue(second["stages"]["target"]["cache_hit"])
            self.assertTrue(second["stages"]["discovery"]["cache_hit"])
            self.assertTrue(second["stages"]["selection"]["cache_hit"])
            for filename in (
                "run.json",
                "target.json",
                "citing-works.json",
                "selection.json",
                "citations.json",
                "analysis-context.json",
                "analysis-pending.jsonl",
                "fulltext-requests.md",
            ):
                self.assertTrue((run_dir / filename).exists(), filename)
            self.assertFalse((run_dir / "analysis-packets.jsonl").exists())
            self.assertFalse((run_dir / "analysis-validation.json").exists())
            self.assertFalse((run_dir / "fulltext-requests.json").exists())

            (run_dir / "analysis-results.jsonl").write_text("", encoding="utf-8")
            finalized = finalize_run(run_dir)
            report = (run_dir / "report.md").read_text(encoding="utf-8")
            self.assertEqual(finalized["status"], "needs_user_files")
            self.assertIn("引用立場分析", report)
            self.assertNotIn("provider_diagnostics", report)
            self.assertNotIn("is_oa", report)


if __name__ == "__main__":
    unittest.main()
