from uuid import uuid4

from .audit import AuditTrail
from .domain import Actor, ConflictError, NotFoundError
from .rules import RuleEngine

SYSTEM_ACTOR = Actor("system", "admin")


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    # ------------------------------------------------------------------
    # Creation
    # ------------------------------------------------------------------
    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        validated = self.rules.validate_create(actor, kind, payload, self._lookup)
        if validated:
            payload.update(validated)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    # ------------------------------------------------------------------
    # Transitions with quality review / reobservation orchestration
    # ------------------------------------------------------------------
    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )

        # Side effects that must be validated before the main record changes.
        created_reobservation = None
        if entity["kind"] == "observation" and next_status == "needs_reobservation":
            created_reobservation = self._open_reobservation(
                entity, patch["quality"]["reasons"]
            )

        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": self._auditable_patch(patch), "reobservation_id": created_reobservation},
        )

        if entity["kind"] == "observation":
            if next_status == "archived":
                self._after_observation_archived(actor, updated)
            elif action == "schedule":
                self._after_observation_scheduled(actor, updated)
        return updated

    @staticmethod
    def _auditable_patch(patch):
        # Keep the audit row compact and free of the internal status marker.
        clean = {key: value for key, value in patch.items() if key != "__next_status__"}
        return clean

    def _find_open_reobservation(self, candidate_id):
        for todo in self.repository.list_entities(kind="reobservation"):
            if todo["status"] not in ("open", "scheduled"):
                continue
            if todo["data"].get("candidate_id") == candidate_id:
                return todo
        return None

    def _open_reobservation(self, observation, reasons):
        candidate_id = observation["data"].get("candidate_id")
        # The candidate is already covered by a qualified result: do not queue more time.
        archived = self._archived_observation(candidate_id, exclude_id=observation["id"])
        if archived:
            self.audit.record(
                observation["id"],
                SYSTEM_ACTOR,
                "reobservation_skipped",
                observation["status"],
                observation["status"],
                {
                    "candidate_id": candidate_id,
                    "archived_result_id": archived["id"],
                    "reasons": reasons,
                },
            )
            return None
        # A repeated failed completion (including on the follow-up) must not
        # pile up duplicate todos: refresh the existing one with latest reasons.
        existing = self._find_open_reobservation(candidate_id)
        if existing:
            data = dict(existing["data"])
            data["reasons"] = reasons
            data["source_observation_id"] = observation["id"]
            if existing["status"] == "scheduled" and data.get("follow_up_observation_id") == observation["id"]:
                # The follow-up itself failed: it needs scheduling again.
                data.pop("follow_up_observation_id", None)
                refreshed = self.repository.update_entity(
                    existing["id"], existing["version"], "open", data
                )
                self.audit.record(
                    existing["id"], SYSTEM_ACTOR, "reopen",
                    existing["status"], "open", {"source_observation_id": observation["id"]},
                )
                return refreshed["id"]
            refreshed = self.repository.update_entity(
                existing["id"], existing["version"], existing["status"], data
            )
            self.audit.record(
                existing["id"], SYSTEM_ACTOR, "refresh",
                existing["status"], existing["status"],
                {"source_observation_id": observation["id"]},
            )
            return refreshed["id"]
        payload = {
            "candidate_id": candidate_id,
            "source_observation_id": observation["id"],
            "reasons": reasons,
            "origin": "quality_gate",
            "telescope_id": observation["data"].get("telescope_id"),
            "team_id": observation["data"].get("team_id"),
        }
        todo = self.repository.create_entity(
            str(uuid4()), "reobservation", "open", payload, "system"
        )
        self.audit.record(
            todo["id"], SYSTEM_ACTOR, "create", None, "open",
            {"kind": "reobservation", "source_observation_id": observation["id"]},
        )
        return todo["id"]

    def _archived_observation(self, candidate_id, exclude_id=None):
        for observation in self.repository.list_entities(kind="observation"):
            if observation["status"] != "archived":
                continue
            if observation["data"].get("candidate_id") != candidate_id:
                continue
            if exclude_id and observation["id"] == exclude_id:
                continue
            return observation
        return None

    def _after_observation_scheduled(self, actor, observation):
        reobservation_id = observation["data"].get("reobservation_id")
        if not reobservation_id:
            return
        todo = self.repository.get_entity(reobservation_id)
        if todo and todo["status"] == "open":
            data = dict(todo["data"])
            data["follow_up_observation_id"] = observation["id"]
            self._move_reobservation(
                todo,
                "scheduled",
                actor,
                data,
                "mark_scheduled",
            )

    def _after_observation_archived(self, actor, observation):
        candidate_id = observation["data"].get("candidate_id")
        for todo in self.repository.list_entities(kind="reobservation"):
            if todo["status"] not in ("open", "scheduled"):
                continue
            if todo["data"].get("candidate_id") != candidate_id:
                continue
            follow_up_id = todo["data"].get("follow_up_observation_id")
            reason = "candidate %s now has usable archived result %s" % (
                candidate_id,
                observation["id"],
            )
            if follow_up_id == observation["id"]:
                # This archived observation was the follow-up for that todo.
                data = dict(todo["data"])
                data["resolution_reason"] = reason
                self._move_reobservation(todo, "resolved", actor, data, "resolve")
            else:
                data = dict(todo["data"])
                data["cancellation_reason"] = reason
                self._move_reobservation(todo, "cancelled", actor, data, "cancel")

    def _move_reobservation(self, todo, next_status, actor, new_data, action):
        updated = self.repository.update_entity(
            todo["id"], todo["version"], next_status, new_data
        )
        self.audit.record(
            todo["id"], actor, action, todo["status"], next_status, {"systematic": True}
        )
        return updated

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------
    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
