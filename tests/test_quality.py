import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, InvalidTransition, PermissionDenied, ValidationError
from src.repository import SQLiteRepository
from src.rules import QUALITY_LIMITS, RuleEngine
from src.service import DomainService

GOOD_QUALITY = {
    "cloud_cover": 0.1,
    "seeing_arcsec": 1.1,
    "limiting_magnitude": 20.5,
    "tracking_error_arcsec": 0.4,
}


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
        self.supervisor = Actor("supervisor-1", "supervisor")

    def tearDown(self):
        self.tmp.cleanup()

    def _setup(self):
        source = self.service.create(
            self.analyst, "source", {"name": "Survey", "survey_name": "S"}
        )
        candidate = self.service.create(
            self.analyst,
            "candidate",
            {
                "source_id": source["id"],
                "event_id": "AT-Q1",
                "ra": 10,
                "dec": 20,
                "magnitude": 18,
                "transient_type": "unknown",
                "observed_at": "2026-09-27T00:00:00Z",
            },
        )
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
                "start_at": "2026-09-28T10:00:00Z",
                "end_at": "2026-09-28T11:00:00Z",
                "mode": "imaging",
            },
        )
        observation = self.service.transition(
            self.coordinator, observation["id"], "schedule", {"operator_id": "op-1"}
        )
        return candidate, telescope, observation

    def test_quality_gate_classifies_each_metric(self):
        from src.rules import evaluate_observation_quality

        result = evaluate_observation_quality(GOOD_QUALITY)
        self.assertTrue(result["passed"])
        self.assertEqual(result["reasons"], [])

        bad = evaluate_observation_quality(
            {
                "cloud_cover": 0.9,
                "seeing_arcsec": 3.0,
                "limiting_magnitude": 17.0,
                "tracking_error_arcsec": 2.0,
            }
        )
        self.assertFalse(bad["passed"])
        self.assertEqual(
            {item["metric"] for item in bad["reasons"]},
            {"cloud_cover", "seeing_arcsec", "limiting_magnitude", "tracking_error_arcsec"},
        )
        self.assertEqual(QUALITY_LIMITS["cloud_cover"]["value"], 0.30)

    def test_missing_or_non_numeric_metrics_are_rejected(self):
        _, _, observation = self._setup()
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.operator,
                observation["id"],
                "complete",
                {**GOOD_QUALITY, "seeing_arcsec": "blurry"},
            )
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.operator,
                observation["id"],
                "complete",
                {key: value for key, value in GOOD_QUALITY.items() if key != "cloud_cover"},
            )

    def test_passing_metrics_wait_for_review_and_cannot_archive_directly(self):
        _, _, observation = self._setup()
        observation = self.service.transition(
            self.operator, observation["id"], "complete", GOOD_QUALITY
        )
        self.assertEqual(observation["status"], "review_pending")
        self.assertTrue(observation["data"]["quality"]["passed"])
        # The original observation is preserved without an archived result.
        self.assertNotEqual(observation["status"], "archived")
        with self.assertRaises(PermissionDenied):
            # Operators may complete observations but only supervisors review them.
            self.service.transition(
                self.operator, observation["id"], "review", {"decision": "approve"}
            )
        with self.assertRaises(InvalidTransition):
            # Archiving is not a standalone action: review approval is required.
            self.service.transition(
                self.supervisor, observation["id"], "archive", {}
            )

    def test_supervisor_approves_and_archives(self):
        _, _, observation = self._setup()
        observation = self.service.transition(
            self.operator, observation["id"], "complete", GOOD_QUALITY
        )
        archived = self.service.transition(
            self.supervisor, observation["id"], "review", {"decision": "approve"}
        )
        self.assertEqual(archived["status"], "archived")
        self.assertEqual(archived["data"]["review_decision"], "approve")

    def test_exceeding_limit_keeps_record_and_creates_reobservation_todo(self):
        candidate, telescope, observation = self._setup()
        failed = self.service.transition(
            self.operator,
            observation["id"],
            "complete",
            {
                "cloud_cover": 0.8,
                "seeing_arcsec": 1.1,
                "limiting_magnitude": 20.5,
                "tracking_error_arcsec": 0.4,
            },
        )
        # Original record is preserved (status and quality reasons retained).
        self.assertEqual(failed["status"], "needs_reobservation")
        self.assertEqual(
            failed["data"]["quality"]["reasons"][0]["metric"], "cloud_cover"
        )

        todos = self.service.list("reobservation")
        self.assertEqual(len(todos), 1)
        todo = todos[0]
        self.assertEqual(todo["status"], "open")
        self.assertEqual(todo["data"]["candidate_id"], candidate["id"])
        self.assertEqual(todo["data"]["source_observation_id"], observation["id"])
        self.assertEqual(todo["data"]["reasons"][0]["metric"], "cloud_cover")

        audit_actions = [row["action"] for row in self.service.audit_log(observation["id"])]
        self.assertIn("complete", audit_actions)

    def test_repeated_failed_completion_does_not_duplicate_todo(self):
        follow_up, todo = self._fail_then_schedule_follow_up()[2:4]
        self.service.transition(
            self.operator,
            follow_up["id"],
            "complete",
            {
                "cloud_cover": 0.8,
                "seeing_arcsec": 1.1,
                "limiting_magnitude": 20.5,
                "tracking_error_arcsec": 0.4,
            },
        )
        # Same todo is reused instead of creating a second one.
        self.assertEqual(len(self.service.list("reobservation")), 1)

    def test_follow_up_links_todo_and_schedule_marks_it_scheduled(self):
        follow_up, todo = self._fail_then_schedule_follow_up()[2:4]
        self.assertEqual(follow_up["data"]["reobservation_id"], todo["id"])
        refreshed = self.service.get(todo["id"])
        self.assertEqual(refreshed["status"], "scheduled")
        self.assertEqual(refreshed["data"]["follow_up_observation_id"], follow_up["id"])

    def _fail_then_schedule_follow_up(self):
        candidate, telescope, first = self._setup()
        first = self.service.transition(
            self.operator,
            first["id"],
            "complete",
            {
                "cloud_cover": 0.8,
                "seeing_arcsec": 1.1,
                "limiting_magnitude": 20.5,
                "tracking_error_arcsec": 0.4,
            },
        )
        todo = self.service.list("reobservation")[0]
        follow_up = self.service.create(
            self.coordinator,
            "observation",
            {
                "candidate_id": candidate["id"],
                "telescope_id": telescope["id"],
                "team_id": "team-south",
                "start_at": "2026-09-29T10:00:00Z",
                "end_at": "2026-09-29T11:00:00Z",
                "mode": "imaging",
                "reobservation_id": todo["id"],
            },
        )
        follow_up = self.service.transition(
            self.coordinator, follow_up["id"], "schedule", {"operator_id": "op-1"}
        )
        todo = self.service.get(todo["id"])
        return candidate, telescope, follow_up, todo

    def test_archived_follow_up_resolves_todo_and_blocks_further_observations(self):
        candidate, telescope, follow_up, _ = self._fail_then_schedule_follow_up()
        follow_up = self.service.transition(
            self.operator, follow_up["id"], "complete", GOOD_QUALITY
        )
        follow_up = self.service.transition(
            self.supervisor, follow_up["id"], "review", {"decision": "approve"}
        )
        self.assertEqual(follow_up["status"], "archived")
        todos = self.service.list("reobservation")
        self.assertEqual(todos[0]["status"], "resolved")

        # No further observation may occupy telescope/team time for this candidate.
        with self.assertRaises(ConflictError):
            self.service.create(
                self.coordinator,
                "observation",
                {
                    "candidate_id": candidate["id"],
                    "telescope_id": telescope["id"],
                    "team_id": "team-north",
                    "start_at": "2026-09-30T10:00:00Z",
                    "end_at": "2026-09-30T11:00:00Z",
                    "mode": "imaging",
                },
            )

    def test_creating_todo_manually_is_blocked_when_result_exists(self):
        candidate, telescope, follow_up, _todo = self._fail_then_schedule_follow_up()
        follow_up = self.service.transition(
            self.operator, follow_up["id"], "complete", GOOD_QUALITY
        )
        self.service.transition(
            self.supervisor, follow_up["id"], "review", {"decision": "approve"}
        )
        with self.assertRaises(ConflictError):
            self.service.create(
                self.coordinator,
                "reobservation",
                {
                    "candidate_id": candidate["id"],
                    "reasons": [{"metric": "manual", "message": "one more night"}],
                },
            )

    def test_failed_quality_after_archived_result_skips_new_todo(self):
        candidate, telescope, follow_up, _todo = self._fail_then_schedule_follow_up()
        follow_up = self.service.transition(
            self.operator, follow_up["id"], "complete", GOOD_QUALITY
        )
        self.service.transition(
            self.supervisor, follow_up["id"], "review", {"decision": "approve"}
        )
        # A new follow-up for the same candidate cannot even be created ...
        with self.assertRaises(ConflictError):
            self.service.create(
                self.coordinator,
                "observation",
                {
                    "candidate_id": candidate["id"],
                    "telescope_id": telescope["id"],
                    "team_id": "team-late",
                    "start_at": "2026-10-01T10:00:00Z",
                    "end_at": "2026-10-01T11:00:00Z",
                    "mode": "imaging",
                },
            )
        # ... and the originally failed observation keeps no extra open todo.
        todos = self.service.list("reobservation")
        self.assertEqual(len(todos), 1)
        self.assertEqual(todos[0]["status"], "resolved")

    def test_supervisor_rejection_requires_reason_and_reopens_reobservation(self):
        _, _, observation = self._setup()
        observation = self.service.transition(
            self.operator, observation["id"], "complete", GOOD_QUALITY
        )
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.supervisor, observation["id"], "review", {"decision": "reject"}
            )
        rejected = self.service.transition(
            self.supervisor,
            observation["id"],
            "review",
            {"decision": "reject", "reason": "suspect tracking on the stacked frames"},
        )
        self.assertEqual(rejected["status"], "needs_reobservation")
        todos = self.service.list("reobservation")
        self.assertEqual(len(todos), 1)
        self.assertEqual(todos[0]["data"]["reasons"][0]["metric"], "review")

    def test_scheduling_follow_up_for_resolved_todo_is_invalid(self):
        candidate, telescope, follow_up, _todo = self._fail_then_schedule_follow_up()
        todo = self.service.list("reobservation")[0]
        follow_up = self.service.transition(
            self.operator, follow_up["id"], "complete", GOOD_QUALITY
        )
        self.service.transition(
            self.supervisor, follow_up["id"], "review", {"decision": "approve"}
        )
        with self.assertRaises(InvalidTransition):
            self.service.create(
                self.coordinator,
                "observation",
                {
                    "candidate_id": candidate["id"],
                    "telescope_id": telescope["id"],
                    "team_id": "team-x",
                    "start_at": "2026-10-02T10:00:00Z",
                    "end_at": "2026-10-02T11:00:00Z",
                    "mode": "imaging",
                    "reobservation_id": todo["id"],
                },
            )

    def test_failed_observation_cannot_be_approved_directly(self):
        _, _, observation = self._setup()
        observation = self.service.transition(
            self.operator,
            observation["id"],
            "complete",
            {
                "cloud_cover": 0.8,
                "seeing_arcsec": 1.1,
                "limiting_magnitude": 20.5,
                "tracking_error_arcsec": 0.4,
            },
        )
        self.assertEqual(observation["status"], "needs_reobservation")
        with self.assertRaises(InvalidTransition):
            self.service.transition(
                self.supervisor, observation["id"], "review", {"decision": "approve"}
            )

    def test_manually_cancelled_todo_requires_reason(self):
        _, _, observation = self._setup()
        self.service.transition(
            self.operator,
            observation["id"],
            "complete",
            {
                "cloud_cover": 0.8,
                "seeing_arcsec": 1.1,
                "limiting_magnitude": 20.5,
                "tracking_error_arcsec": 0.4,
            },
        )
        todo = self.service.list("reobservation")[0]
        with self.assertRaises(ValidationError):
            self.service.transition(self.coordinator, todo["id"], "cancel", {})
        cancelled = self.service.transition(
            self.coordinator, todo["id"], "cancel", {"reason": "target behind Sun"}
        )
        self.assertEqual(cancelled["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
