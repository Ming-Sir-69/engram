import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from engram.maintenance_evidence import (
    MAX_INPUT,
    AccessDenied,
    FixedEvidenceAccess,
    MaintenanceEvidence,
    safe_projection,
)


class MaintenanceEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        # Resolve /var -> /private/var before making exact-path fixtures.
        self.root = Path(self.temp.name).resolve()
        self.project = self.root / "engram"
        self.home = self.root / "owner"
        self.evidence = MaintenanceEvidence(
            FixedEvidenceAccess(), self.project, self.home
        )

    def put(self, resource, value):
        _, path, _ = self.evidence._resolve(resource)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value if isinstance(value, str) else json.dumps(value))
        return path

    def test_exact_resource_handles_reject_paths_traversal_and_candidates(self):
        for resource in (
            "/etc/passwd",
            "../state.json",
            "source.report:../../private.json",
            "source.report:.env",
            "content.candidates",
            "source.report:2026-10-01T01.json/other",
        ):
            with self.assertRaises(AccessDenied):
                self.evidence.read(resource)

    def test_runtime_projection_omits_raw_output_and_private_fields(self):
        self.put(
            "content.runs",
            json.dumps(
                {
                    "event": "maintained",
                    "executor": "gpt",
                    "body": "PRIVATE_RECORD",
                    "credentials": "PRIVATE_KEY",
                    "attempts": [
                        {
                            "route": "cloud_gpt",
                            "exit": 0,
                            "result": "RAW_MODEL_OUTPUT",
                            "summary": "authorization=synthetic-private-secret",
                        }
                    ],
                }
            )
            + "\n",
        )
        page = self.evidence.read("content.runs")
        for private in (
            "PRIVATE_RECORD",
            "PRIVATE_KEY",
            "RAW_MODEL_OUTPUT",
            "synthetic-private-secret",
        ):
            self.assertNotIn(private, page["text"])
        self.assertIn("cloud_gpt", page["text"])
        self.assertIn("REDACTED", page["text"])

    def test_nested_unknown_state_maps_do_not_escape_projection(self):
        result = safe_projection(
            {
                "last_result": {
                    "status": "inconclusive",
                    "before_fingerprint": {"credentials/file": "private-hash"},
                    "api_key": "synthetic-private-secret",
                    "next_step": "diagnose",
                },
                "quota": {"provider": "private-provider"},
            }
        )
        self.assertEqual(
            result, {"last_result": {"status": "inconclusive", "next_step": "diagnose"}}
        )

    def test_source_redaction_happens_before_page_boundaries(self):
        source = '标题\napi_key = "synthetic-private-secret"\n' + "中文😀" * 20
        path = self.put("source.runner", source)
        pages = []
        offset = 0
        sha = None
        while True:
            page = self.evidence.read("source.runner", offset, 5, sha)
            pages.append(page["text"])
            sha = page["view_sha256"]
            if page["next_offset"] is None:
                break
            offset = page["next_offset"]
        self.assertNotIn("synthetic-private-secret", "".join(pages))
        self.assertIn("中文😀", "".join(pages))
        self.assertEqual(path.read_text(), source)
        path.write_text("changed")
        changed = self.evidence.read("source.runner", 5, 5, sha)
        self.assertEqual(changed["status"], "source_changed")
        self.assertIsNone(changed["text"])

    def test_registered_symlink_cannot_read_another_allowed_file(self):
        target = self.root / "ordinary.txt"
        target.write_text("WRONG_RESOURCE")
        _, path, _ = self.evidence._resolve("source.runner")
        path.parent.mkdir(parents=True)
        path.symlink_to(target)
        with self.assertRaises(AccessDenied):
            self.evidence.read("source.runner")

    def test_denied_root_still_applies_to_maintenance_lane(self):
        self.put("policy", {"mode": "cloud_gpt"})
        evidence = MaintenanceEvidence(
            FixedEvidenceAccess(deny_paths=[self.project]), self.project, self.home
        )
        with self.assertRaises(AccessDenied):
            evidence.read("policy")

    def test_catalog_paginates_only_fixed_resources_and_dated_json_receipts(self):
        self.put("source.report:2026-10-01T01-00-00Z.json", {"status": "published"})
        self.put("source.report:2026-09-01T01-00-00Z.json", {"status": "failed"})
        (self.evidence.reports / "private-candidates.json").write_text("PRIVATE")
        (self.evidence.reports / "2026-10-02T01-00-00Z.json").symlink_to(
            self.root / "missing"
        )
        resources = []
        offset = 0
        while True:
            result = self.evidence.catalog("source", offset, 3)
            resources.extend(row["resource"] for row in result["resources"])
            if result["next_offset"] is None:
                break
            offset = result["next_offset"]
        self.assertNotIn("source.report:private-candidates.json", resources)
        self.assertNotIn("source.report:2026-10-02T01-00-00Z.json", resources)
        self.assertEqual(
            resources[-2:],
            [
                "source.report:2026-10-01T01-00-00Z.json",
                "source.report:2026-09-01T01-00-00Z.json",
            ],
        )

    def test_status_distinguishes_waiting_snapshot_and_previous_receipt(self):
        self.put(
            "source.status",
            {"status": "waiting", "next_retry_at": "2026-10-02T00:00:00Z"},
        )
        self.put(
            "source.report:2026-10-01T01-00-00Z.json",
            {"status": "failed", "stage": "pre_publish"},
        )
        self.put("policy", {"mode": "cloud_gpt", "claude": {"mode": "manual_only"}})
        with patch.object(self.evidence, "_health", return_value={"ok": True}):
            result = self.evidence.status()
        self.assertEqual(
            result["source_improvement"]["status"]["data"]["status"], "waiting"
        )
        self.assertEqual(
            result["source_improvement"]["latest_receipt"]["data"]["status"], "failed"
        )
        self.assertEqual(result["policy"]["data"]["claude"]["mode"], "manual_only")
        self.assertEqual(
            result["content_maintenance"]["state"]["status"], "not_applicable"
        )

    def test_run_history_is_bounded_and_sampling_is_explicit(self):
        self.put(
            "content.runs",
            "".join(
                json.dumps({"event": "run", "count": n}) + "\n" for n in range(120)
            ),
        )
        page = self.evidence.read("content.runs", length=24000)
        rows = json.loads(page["text"])
        self.assertEqual(
            (len(rows), rows[0]["count"], rows[-1]["count"]), (100, 20, 119)
        )
        self.assertTrue(page["source_sampled"])

    def test_cloud_source_receipt_is_projected_and_catalogued(self):
        self.put(
            "cloud_source.status",
            {
                "status": "validated",
                "mode": "cloud_gpt",
                "claude": {"mode": "manual_only"},
                "body": "PRIVATE_KNOWLEDGE",
                "next_step": "review",
                "api_key": "synthetic-private-credential",
            },
        )
        with patch.object(self.evidence, "_health", return_value={"ok": True}):
            result = self.evidence.status()
        view = result["cloud_source"]["status"]["data"]
        self.assertEqual(view["mode"], "cloud_gpt")
        self.assertEqual(view["claude"]["mode"], "manual_only")
        self.assertNotIn("PRIVATE", json.dumps(view))
        handles = [r["resource"] for r in self.evidence.catalog("source")["resources"]]
        self.assertIn("cloud_source.status", handles)
        page = self.evidence.read("cloud_source.status")
        self.assertEqual(json.loads(page["text"]), view)

    def test_cloud_source_required_receipt_fields_survive_without_test_output(self):
        receipt = {
            "status": "validated",
            "request_id": "synthetic",
            "changes": "bounded",
            "reason": "checked",
            "blocked_reason": None,
            "required_evidence": ["regression"],
            "regression": {
                "exit_code": 0,
                "passed": True,
                "stdout": "PRIVATE_STDOUT",
                "stderr": "PRIVATE_STDERR",
            },
            "verification": {
                "returncode": 0,
                "ok": True,
                "summary": "checks passed",
                "output": "PRIVATE_TEST_OUTPUT",
            },
            "review_limitation": "tests are not independent review",
            "memory_benefit": "unknown",
            "deployment": "not_published",
            "started_epoch": 1.0,
            "worker_pid": 42,
            "finished_at": "2026-10-01",
        }
        self.put("cloud_source.status", receipt)
        projected = json.loads(self.evidence.read("cloud_source.status")["text"])
        self.assertEqual(set(projected), set(receipt))
        self.assertEqual(projected["regression"], {"exit_code": 0, "passed": True})
        self.assertEqual(
            projected["verification"],
            {"returncode": 0, "ok": True, "summary": "checks passed"},
        )
        self.assertNotIn("PRIVATE_", json.dumps(projected))

    def test_cloud_mode_uses_current_daily_receipt_without_missing_legacy_files(self):
        self.put("policy", {"mode": "cloud_gpt", "claude": {"mode": "manual_only"}})
        self.put(
            "cloud.receipts",
            {
                "schedule_definitions": {"cloud_daily": [-1, 9, 15]},
                "events": [
                    {
                        "task": "cloud_daily",
                        "phase": "start",
                        "run_id": "run1",
                        "at": "T1",
                    },
                    {
                        "task": "cloud_daily",
                        "phase": "success",
                        "run_id": "run1",
                        "at": "T2",
                        "slot": None,
                        "detail": "applied=4; api_key=synthetic-private-secret",
                    },
                    {"task": "manual", "phase": "start", "at": "T3"},
                ],
            },
        )
        with (
            patch.object(self.evidence, "_health", return_value={"ok": True}),
            patch.object(
                self.evidence, "_snapshot", wraps=self.evidence._snapshot
            ) as reads,
        ):
            result = self.evidence.status()
        resources_read = [call.args[0] for call in reads.call_args_list]
        self.assertNotIn("content.status", resources_read)
        self.assertNotIn("content.state", resources_read)
        content = result["content_maintenance"]
        self.assertEqual(content["status"]["status"], "success")
        self.assertEqual(content["status"]["data"]["run_id"], "run1")
        self.assertNotIn("error_type", content["status"])
        self.assertEqual(content["state"]["status"], "not_applicable")
        self.assertFalse(result["source_improvement"]["active"])
        self.assertEqual(result["cloud_source"]["status"]["status"], "not_started")
        handles = [r["resource"] for r in self.evidence.catalog("content")["resources"]]
        self.assertIn("cloud.receipts", handles)
        self.assertNotIn("content.status", handles)
        self.assertNotIn("content.state", handles)
        page = self.evidence.read("cloud.receipts")
        self.assertNotIn("synthetic-private-secret", page["text"])
        self.assertEqual(
            json.loads(page["text"])["schedule_definitions"]["cloud_daily"], [-1, 9, 15]
        )

    def test_cloud_receipt_tail_keeps_newest_hundred_events(self):
        self.put(
            "cloud.receipts",
            {
                "events": [
                    {"task": "cloud_daily", "phase": "success", "run_id": str(i)}
                    for i in range(105)
                ]
            },
        )
        page = self.evidence.read("cloud.receipts", length=24000)
        events = json.loads(page["text"])["events"]
        self.assertTrue(page["source_sampled"])
        self.assertEqual(
            (len(events), events[0]["run_id"], events[-1]["run_id"]), (100, "5", "104")
        )

    def test_missing_cloud_source_status_with_existing_request_is_unknown(self):
        (self.evidence.cloud_source_state / "requests/existing-stage").mkdir(
            parents=True
        )
        result = self.evidence._cloud_source_snapshot()
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["reason"], "request_exists_but_status_unavailable")

    def test_budget_is_enforced_and_health_redirects_are_not_followed(self):
        self.put("source.runner", "x" * (MAX_INPUT + 1))
        with self.assertRaises(ValueError):
            self.evidence.read("source.runner")
        with self.assertRaises(ValueError):
            self.evidence.read("source.runner", length=24001)
        from engram.maintenance_evidence import _NoRedirect

        with self.assertRaises(AccessDenied):
            _NoRedirect().redirect_request(
                None, None, 302, None, None, "http://example.com"
            )


if __name__ == "__main__":
    unittest.main()
