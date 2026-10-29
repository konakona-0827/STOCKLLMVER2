"""Fabricated transport/price fixtures verify real-case mechanics, not returns."""
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest

from support_scout.advantest_case import (build_real_bundle, checkpoint_definitions,
    load_real_case, run_real_case, replay_real_case, fetch_official_documents)
from support_scout.contracts import DataError, TOKYO
from support_scout.discovery import SearchCompletion
from support_scout.japan_demo import demo_bundle
from support_scout.providers import Completion


def fixture(invalidated=False):
    source = demo_bundle()
    source.update(synthetic=False, observation_only=True, as_of="2026-10-29T18:00:00+09:00",
        collection={"status": "COMPLETE", "sources": []})
    source["securities"] = [source["securities"][2 if invalidated else 0]]
    item = source["securities"][0]
    item.update(symbol="6857.T", name="TEST ONLY", source="YAHOO_CHART_UNOFFICIAL_DAILY",
        price_basis="raw_unverified", observation_only=True, point_in_time=False,
        actions_reviewed=False, volume_comparable=False, tradability_reviewed=False)
    for bar in item["bars"]:
        bar["available_at"] = source["as_of"]
    case = load_real_case()
    case.update(evaluation_start="2026-09-01", evaluation_end="2026-09-08", additional_checkpoints=[])
    return source, case


class FakeSearch:
    model, mode = "fake-search", "test_transport"
    def __init__(self): self.calls = 0
    def complete(self, packet):
        self.calls += 1
        candidate = packet["core"]["candidates"][0]
        return SearchCompletion({"selections": [{"candidate_id": candidate["candidate_id"],
            "evidence_ids": candidate["required_evidence_ids"], "rationale": "單元測試，非真實選股。"}]})


class FakeReview:
    model, mode = "fake-review", "test_transport"
    def __init__(self): self.calls = []
    def complete(self, packet, role):
        self.calls.append((deepcopy(packet), role))
        return Completion({"verdict": "WATCH", "evidence_ids": packet["core"]["required_confirmation_evidence"],
            "summary": "僅作案例診斷，不能稱歷史買點。", "risks": ["資料未核實"], "missing_data": ["PIT"]})


class RealCaseTests(unittest.TestCase):
    def test_prefix_keeps_real_retrieval_and_excludes_future_price_bars(self):
        source, case = fixture()
        checkpoint = {"id": "d20260904", "market_data_cutoff": "2026-09-04T16:00:00+09:00"}
        bundle = build_real_bundle(source, case, checkpoint)
        self.assertEqual(bundle["securities"][0]["bars"][-1]["session"], "2026-09-04")
        self.assertEqual(bundle["securities"][0]["bars"][0]["available_at"], source["as_of"])
        self.assertFalse(bundle["securities"][0]["point_in_time"])
        self.assertTrue(bundle["observation_only"])
        self.assertFalse(bundle["synthetic"])
        self.assertEqual(bundle["analysis_context"]["market_data_cutoff"], checkpoint["market_data_cutoff"])
        self.assertNotIn("public_information_policy", bundle)

    def test_date_only_release_not_available_to_same_day_close(self):
        source, case = fixture()
        documents = [{"id": "test", "published_date": "2026-09-04", "time_precision": "DATE_ONLY",
            "fetched_at": source["as_of"], "status": "ARCHIVED", "facts": [{"value": 123}],
            "raw_file": "unused.pdf"}]
        early = build_real_bundle(source, case, {"id": "a", "market_data_cutoff": "2026-09-04T16:00:00+09:00"}, documents=documents)
        late = build_real_bundle(source, case, {"id": "b", "market_data_cutoff": "2026-09-04T23:59:59.999999+09:00"}, documents=documents)
        self.assertEqual(early["analysis_context"]["visible_official_facts"], [])
        self.assertEqual(late["analysis_context"]["visible_official_facts"][0]["facts"], [{"value": 123}])
        self.assertIn("NOT_VERIFIED_RELEASE_TIME", late["analysis_context"]["visible_official_facts"][0]["timing_assumption"])
        self.assertEqual(late["securities"][0]["pattern_context"]["catalysts"], [])

    def test_unavailable_source_never_becomes_visible_fact(self):
        source, case = fixture()
        bundle = build_real_bundle(source, case, {"id": "a", "market_data_cutoff": "2026-09-08T16:00:00+09:00"},
            documents=[{"published_date": "2026-09-01", "status": "UNAVAILABLE", "fetched_at": source["as_of"], "facts": [{"value": 999}]}])
        self.assertEqual(bundle["analysis_context"]["visible_official_facts"], [])

    def test_invalid_synthetic_warmup_and_future_cutoff_fail(self):
        source, case = fixture()
        checkpoint = {"id": "a", "market_data_cutoff": "2026-09-08T16:00:00+09:00"}
        bad = deepcopy(source); bad["synthetic"] = True
        with self.assertRaises(DataError): build_real_bundle(bad, case, checkpoint)
        bad = deepcopy(source); bad["securities"][0]["bars"] = bad["securities"][0]["bars"][-10:]
        with self.assertRaisesRegex(DataError, "60_COMPLETE"): build_real_bundle(bad, case, checkpoint)
        with self.assertRaises(DataError):
            build_real_bundle(source, case, {"id": "future", "market_data_cutoff": "2026-10-30T16:00:00+09:00"})

    def test_daily_checkpoint_list_requires_last_real_day(self):
        source, case = fixture()
        self.assertEqual(len(checkpoint_definitions(case, source)), 6)
        case["evaluation_end"] = "2026-09-09"
        with self.assertRaises(DataError): checkpoint_definitions(case, source)

    def test_numeric_run_and_saved_input_replay_use_no_models_or_ledger(self):
        source, case = fixture()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root/"case.json"; path.write_text(json.dumps(case), encoding="utf-8")
            comparison, target = run_real_case(case_path=path, output_root=root/"out", source_bundle=source, documents=[])
            self.assertEqual(comparison["model_call_count"], 0)
            self.assertFalse(comparison["production_ledger_written"])
            self.assertFalse(comparison["empirical_validation"])
            self.assertEqual(replay_real_case(target)["status"], "MATCH")
            self.assertEqual(list(root.rglob("*.sqlite3")), [])
            self.assertIn("不是歷史當時", (target/"comparison.md").read_text(encoding="utf-8"))

    def test_case_budget_stops_half_pair_and_forced_diagnostic_not_search_hit(self):
        source, case = fixture(invalidated=True)
        search, review = FakeSearch(), FakeReview()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); path = root/"case.json"; path.write_text(json.dumps(case), encoding="utf-8")
            comparison, target = run_real_case(case_path=path, output_root=root/"out", source_bundle=source,
                documents=[], agents=True, checkpoint_ids=["d20260908"], diagnostic_review=True,
                max_total_calls=1, search_provider=search, researcher=review, reviewer=review)
            self.assertEqual(comparison["model_call_count"], 0)
            self.assertEqual(review.calls, [])
            self.assertEqual(search.calls, 0)

    def test_diagnostic_pair_reads_original_packet_but_never_becomes_formal_hit(self):
        source, case = fixture(invalidated=True)
        search, review = FakeSearch(), FakeReview()
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); path=root/"case.json"; path.write_text(json.dumps(case),encoding="utf8")
            comparison,target=run_real_case(case_path=path,output_root=root/"out",source_bundle=source,
                documents=[],agents=True,checkpoint_ids=["d20260908"],diagnostic_review=True,
                max_total_calls=2,search_provider=search,researcher=review,reviewer=review)
            result=comparison["results"][0]
            self.assertEqual(comparison["model_call_count"],2)
            self.assertEqual(search.calls,0)
            self.assertEqual(len(review.calls),2)
            self.assertEqual(result["status"],"DIAGNOSTIC_REVIEW_NOT_SEARCH_HIT")
            self.assertEqual(result["search_selected"],[])
            self.assertEqual(result["standard_reviews"],{})
            self.assertEqual(set(result["diagnostic_reviews"]),{"researcher","reviewer"})
            self.assertEqual(result["trade_conclusion"],"NO_TRADE")
            self.assertTrue(all(packet["core"]["observation_only"] for packet,_ in review.calls))
            self.assertTrue(all(packet["core"]["analysis_context"]["market_data_cutoff"].startswith("2026-09-08") for packet,_ in review.calls))
            self.assertEqual(replay_real_case(target)["status"],"MATCH")

    def test_official_downloader_archives_bytes_and_does_not_claim_pdf_extraction(self):
        case = load_real_case()
        case["official_documents"] = case["official_documents"][:1]
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, maximum): return b"%PDF-1.4 fabricated transport fixture"
        with tempfile.TemporaryDirectory() as directory:
            documents = fetch_official_documents(case, directory, opener=lambda *a, **k: Response(),
                now=datetime(2026,10,29,18,tzinfo=TOKYO))
            self.assertEqual(documents[0]["status"], "ARCHIVED")
            self.assertTrue(Path(documents[0]["raw_file"]).exists())
            self.assertIsNone(documents[0]["market_consensus"])
            self.assertNotIn("verified", documents[0])

    def test_replay_rejects_artifact_change(self):
        source, case = fixture()
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); path=root/"case.json"; path.write_text(json.dumps(case),encoding="utf8")
            _,target=run_real_case(case_path=path,output_root=root/"out",source_bundle=source,documents=[])
            item=json.loads((target/"comparison.json").read_text(encoding="utf8")); item["empirical_validation"]=True
            (target/"comparison.json").write_text(json.dumps(item),encoding="utf8")
            with self.assertRaisesRegex(DataError,"ARTIFACT_MISMATCH"): replay_real_case(target)


if __name__ == "__main__": unittest.main()
