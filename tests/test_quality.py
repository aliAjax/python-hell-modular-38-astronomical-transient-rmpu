import tempfile
import unittest
from pathlib import Path

from src.domain import (
    Actor,
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class QualityReviewTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DomainService(
            SQLiteRepository(Path(self.tmp.name) / "quality.db"),
            RuleEngine(),
        )
        self.analyst = Actor("analyst-1", "analyst")
        self.coordinator = Actor("coordinator-1", "coordinator")
        self.operator = Actor("operator-1", "operator")

    def tearDown(self):
        self.tmp.cleanup()

    def _candidate(self, event_id="AT-Q1"):
        source = self.service.create(
            self.analyst,
            "source",
            {"name": "Survey", "survey_name": "S"},
        )
        return self.service.create(
            self.analyst,
            "candidate",
            {
                "source_id": source["id"],
                "event_id": event_id,
                "ra": 10,
                "dec": 20,
                "magnitude": 18,
                "transient_type": "unknown",
                "observed_at": "2026-09-27T00:00:00Z",
            },
        )

    def _completed_observation(
        self,
        candidate,
        start="2026-09-28T10:00:00Z",
        end="2026-09-28T11:00:00Z",
    ):
        telescope = self.service.create(
            self.coordinator,
            "telescope",
            {"name": "North 2m", "aperture_m": 2.0, "site_name": "NAO"},
        )
        observation = self.service.create(
            self.coordinator,
            "observation",
            {
                "candidate_id": candidate["id"],
                "telescope_id": telescope["id"],
                "team_id": "team-north",
                "start_at": start,
                "end_at": end,
                "mode": "imaging",
            },
        )
        observation = self.service.transition(
            self.coordinator, observation["id"], "schedule", {"operator_id": "op-1"}
        )
        return self.service.transition(self.operator, observation["id"], "complete", {})

    @staticmethod
    def _good_quality():
        return {
            "cloud_cover_pct": 10,
            "seeing_arcsec": 1.2,
            "limiting_magnitude": 19.5,
            "tracking_error_arcsec": 0.4,
        }

    def _reobservation_todos(self, failed_observation_id):
        return [
            item
            for item in self.service.list("observation", status="requested")
            if item["data"].get("reobservation_of") == failed_observation_id
        ]

    def test_quality_pass_then_archive(self):
        candidate = self._candidate()
        observation = self._completed_observation(candidate)
        reviewed = self.service.transition(
            self.operator, observation["id"], "submit_quality", self._good_quality()
        )
        self.assertEqual(reviewed["status"], "quality_passed")
        self.assertEqual(reviewed["data"]["quality"]["cloud_cover_pct"], 10.0)
        self.assertEqual(reviewed["data"]["quality_failures"], [])
        self.assertEqual(reviewed["data"]["quality_reviewed_by"], "operator-1")

        archived = self.service.transition(
            self.coordinator, reviewed["id"], "archive", {}
        )
        self.assertEqual(archived["status"], "archived")
        actions = [entry["action"] for entry in self.service.audit_log(observation["id"])]
        self.assertEqual(actions, ["create", "schedule", "complete", "submit_quality", "archive"])

    def test_quality_failure_keeps_record_and_creates_todo(self):
        candidate = self._candidate()
        observation = self._completed_observation(candidate)
        quality = self._good_quality()
        quality["cloud_cover_pct"] = 85
        quality["seeing_arcsec"] = 4.0
        reviewed = self.service.transition(
            self.operator, observation["id"], "submit_quality", quality
        )
        self.assertEqual(reviewed["status"], "quality_failed")
        failures = reviewed["data"]["quality_failures"]
        self.assertEqual(len(failures), 2)
        self.assertTrue(any("cloud_cover_pct" in item for item in failures))
        self.assertTrue(any("seeing_arcsec" in item for item in failures))

        kept = self.service.get(observation["id"])
        self.assertEqual(kept["status"], "quality_failed")
        self.assertEqual(kept["data"]["quality"]["cloud_cover_pct"], 85.0)

        todos = self._reobservation_todos(observation["id"])
        self.assertEqual(len(todos), 1)
        todo = todos[0]
        self.assertEqual(todo["data"]["candidate_id"], candidate["id"])
        self.assertEqual(todo["data"]["telescope_id"], observation["data"]["telescope_id"])
        self.assertEqual(todo["data"]["team_id"], "team-north")
        self.assertEqual(todo["data"]["reobservation_reason"], failures)
        self.assertEqual(
            self.service.audit_log(todo["id"])[0]["action"], "create"
        )

        with self.assertRaises(InvalidTransition):
            self.service.transition(self.coordinator, observation["id"], "archive", {})

    def test_shallow_limit_and_tracking_error_fail_review(self):
        candidate = self._candidate()
        observation = self._completed_observation(candidate)
        quality = self._good_quality()
        quality["limiting_magnitude"] = 16.0
        quality["tracking_error_arcsec"] = 3.0
        reviewed = self.service.transition(
            self.operator, observation["id"], "submit_quality", quality
        )
        self.assertEqual(reviewed["status"], "quality_failed")
        failures = reviewed["data"]["quality_failures"]
        self.assertTrue(any("limiting_magnitude" in item for item in failures))
        self.assertTrue(any("tracking_error_arcsec" in item for item in failures))
        self.assertEqual(len(self._reobservation_todos(observation["id"])), 1)

    def test_archive_requires_passed_review(self):
        candidate = self._candidate()
        observation = self._completed_observation(candidate)
        with self.assertRaises(InvalidTransition):
            self.service.transition(self.coordinator, observation["id"], "archive", {})

    def test_new_observation_blocked_when_candidate_has_archived_result(self):
        candidate = self._candidate()
        observation = self._completed_observation(candidate)
        reviewed = self.service.transition(
            self.operator, observation["id"], "submit_quality", self._good_quality()
        )
        self.service.transition(self.coordinator, reviewed["id"], "archive", {})

        with self.assertRaises(ConflictError) as ctx:
            self.service.create(
                self.coordinator,
                "observation",
                {
                    "candidate_id": candidate["id"],
                    "telescope_id": observation["data"]["telescope_id"],
                    "team_id": "team-north",
                    "start_at": "2026-09-29T10:00:00Z",
                    "end_at": "2026-09-29T11:00:00Z",
                    "mode": "imaging",
                },
            )
        self.assertIn(observation["id"], str(ctx.exception))

    def test_todo_suppressed_when_archived_result_already_exists(self):
        candidate = self._candidate()
        first = self._completed_observation(
            candidate, "2026-09-28T10:00:00Z", "2026-09-28T11:00:00Z"
        )
        second = self._completed_observation(
            candidate, "2026-09-28T12:00:00Z", "2026-09-28T13:00:00Z"
        )
        passed = self.service.transition(
            self.operator, first["id"], "submit_quality", self._good_quality()
        )
        self.service.transition(self.coordinator, passed["id"], "archive", {})

        quality = self._good_quality()
        quality["cloud_cover_pct"] = 90
        failed = self.service.transition(
            self.operator, second["id"], "submit_quality", quality
        )
        self.assertEqual(failed["status"], "quality_failed")
        self.assertIn(first["id"], failed["data"]["reobservation_suppressed"])
        self.assertEqual(self._reobservation_todos(second["id"]), [])
        actions = [entry["action"] for entry in self.service.audit_log(second["id"])]
        self.assertIn("reobservation_suppressed", actions)

    def test_quality_permissions_and_metric_validation(self):
        candidate = self._candidate()
        observation = self._completed_observation(candidate)
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                Actor("viewer-1", "viewer"),
                observation["id"],
                "submit_quality",
                self._good_quality(),
            )
        bad_cloud = self._good_quality()
        bad_cloud["cloud_cover_pct"] = 120
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.operator, observation["id"], "submit_quality", bad_cloud
            )
        bad_value = self._good_quality()
        bad_value["seeing_arcsec"] = "blurry"
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.operator, observation["id"], "submit_quality", bad_value
            )
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.operator,
                observation["id"],
                "submit_quality",
                {"cloud_cover_pct": 10},
            )

    def test_archive_role_is_enforced_after_review_passes(self):
        candidate = self._candidate()
        observation = self._completed_observation(candidate)
        reviewed = self.service.transition(
            self.operator, observation["id"], "submit_quality", self._good_quality()
        )
        self.assertEqual(reviewed["status"], "quality_passed")
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                Actor("reporter-1", "reporter"),
                observation["id"],
                "archive",
                {},
            )


if __name__ == "__main__":
    unittest.main()
