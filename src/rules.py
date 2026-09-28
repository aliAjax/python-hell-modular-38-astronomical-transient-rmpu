from .domain import ConflictError, InvalidTransition, PermissionDenied, ValidationError


# Quality gates applied when an operator submits the follow-up observation result.
# cloud cover is a fraction in [0, 1]; seeing and tracking error are arcseconds;
# the limiting magnitude must be deep enough (larger value = fainter reach).
QUALITY_LIMITS = {
    "cloud_cover": {"constraint": "max", "value": 0.30},
    "seeing_arcsec": {"constraint": "max", "value": 2.0},
    "tracking_error_arcsec": {"constraint": "max", "value": 1.5},
    "limiting_magnitude": {"constraint": "min", "value": 19.0},
}


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _metric_number(data, field, minimum, minimum_inclusive=True, maximum=None):
    try:
        number = float(data.get(field))
    except (TypeError, ValueError):
        raise ValidationError("%s must be numeric" % field)
    if minimum is not None and (number < minimum or (not minimum_inclusive and number == minimum)):
        raise ValidationError("%s must be %s %s" % (field, ">=" if minimum_inclusive else ">", minimum))
    if maximum is not None and number > maximum:
        raise ValidationError("%s must be <= %s" % (field, maximum))
    return number


def evaluate_observation_quality(data):
    """Evaluate operator submitted metrics against the quality gates."""
    metrics = {
        "cloud_cover": _metric_number(data, "cloud_cover", 0.0, maximum=1.0),
        "seeing_arcsec": _metric_number(data, "seeing_arcsec", 0.0, minimum_inclusive=False),
        "tracking_error_arcsec": _metric_number(data, "tracking_error_arcsec", 0.0),
        "limiting_magnitude": _metric_number(data, "limiting_magnitude", 0.0, maximum=30.0),
    }
    reasons = []
    for field, rule in QUALITY_LIMITS.items():
        value = metrics[field]
        exceeded = value > rule["value"] if rule["constraint"] == "max" else value < rule["value"]
        if exceeded:
            relation = "exceeds maximum" if rule["constraint"] == "max" else "is shallower than minimum"
            reasons.append(
                {
                    "metric": field,
                    "value": value,
                    "constraint": rule["constraint"],
                    "limit": rule["value"],
                    "message": "%s=%s %s %s" % (field, value, relation, rule["value"]),
                }
            )
    return {
        "metrics": metrics,
        "limits": {field: rule["value"] for field, rule in QUALITY_LIMITS.items()},
        "reasons": reasons,
        "passed": not reasons,
    }


def calculate_priority(magnitude, transient_type):
    """Return a deterministic observing priority from 0 to 100."""
    try:
        mag = float(magnitude)
    except (TypeError, ValueError):
        raise ValidationError("magnitude must be numeric")
    if mag < -30 or mag > 40:
        raise ValidationError("magnitude is outside the supported range")
    type_weights = {
        "grb": 30,
        "supernova": 22,
        "tde": 20,
        "variable": 8,
        "unknown": 12,
    }
    score = max(0.0, 100.0 - mag * 4.0) + type_weights.get(transient_type, 5)
    return round(min(100.0, score), 2)


def measurements_overlap(first_start, first_end, second_start, second_end):
    return str(first_start) < str(second_end) and str(second_start) < str(first_end)


def _validate_source(actor, data, lookup):
    if len(str(data.get("name", "")).strip()) < 2:
        raise ValidationError("source name is required")
    return {}


def _validate_candidate(actor, data, lookup):
    source_id = data.get("source_id")
    if not _find_one(lookup, "source", "id", source_id):
        raise ValidationError("source does not exist")
    try:
        ra = float(data.get("ra"))
        dec = float(data.get("dec"))
    except (TypeError, ValueError):
        raise ValidationError("ra and dec must be numeric")
    if ra < 0 or ra >= 360 or dec < -90 or dec > 90:
        raise ValidationError("coordinates are outside valid ranges")
    event_key = "%s:%s" % (source_id, data.get("event_id"))
    duplicate = _find_one(lookup, "candidate", "event_key", event_key)
    if duplicate:
        raise ConflictError("candidate already exists for event key " + event_key)
    return {
        "event_key": event_key,
        "measurements": [
            {
                "observed_at": data.get("observed_at"),
                "ra": ra,
                "dec": dec,
                "magnitude": float(data["magnitude"]),
            }
        ],
        "priority_score": calculate_priority(data.get("magnitude"), data.get("transient_type")),
    }


def _validate_telescope(actor, data, lookup):
    if not (0 < float(data.get("aperture_m", 0))):
        raise ValidationError("aperture_m must be positive")
    return {}


def _candidate_has_archived_result(lookup, candidate_id):
    """Return the archived qualified observation for a candidate, if any."""
    if lookup is None or not candidate_id:
        return None
    for observation in lookup("observation", "candidate_id", candidate_id) or []:
        if observation["status"] == "archived":
            return observation
    return None


def _ensure_no_archived_result(lookup, candidate_id):
    archived = _candidate_has_archived_result(lookup, candidate_id)
    if archived:
        raise ConflictError(
            "candidate %s already has a usable archived result %s"
            % (candidate_id, archived["id"])
        )


def _validate_observation(actor, data, lookup):
    candidate_id = data.get("candidate_id")
    if not _find_one(lookup, "candidate", "id", candidate_id):
        raise ValidationError("candidate does not exist")
    if not _find_one(lookup, "telescope", "id", data.get("telescope_id")):
        raise ValidationError("telescope does not exist")
    if str(data.get("start_at")) >= str(data.get("end_at")):
        raise ValidationError("observation end must be after start")
    reobservation_id = data.get("reobservation_id")
    if reobservation_id is not None and not isinstance(reobservation_id, str):
        raise ValidationError("reobservation_id must be a string")
    reobservation_id = (reobservation_id or "").strip() or None
    if reobservation_id:
        todo = _find_one(lookup, "reobservation", "id", reobservation_id)
        if not todo:
            raise ValidationError("reobservation todo does not exist")
        if todo["data"].get("candidate_id") != candidate_id:
            raise ValidationError("reobservation todo belongs to another candidate")
        if todo["status"] in ("resolved", "cancelled"):
            raise InvalidTransition("reobservation todo %s is %s" % (reobservation_id, todo["status"]))
        # A follow-up created for another todo already covers this request.
        follow_up_id = todo["data"].get("follow_up_observation_id")
        if follow_up_id:
            follow_up = _find_one(lookup, "observation", "id", follow_up_id)
            if follow_up and follow_up["status"] not in ("withdrawn",):
                raise ConflictError(
                    "reobservation todo %s already has follow-up observation %s"
                    % (reobservation_id, follow_up_id)
                )
    # A candidate with a qualified archived result must not consume more
    # telescope / observing-team time.
    _ensure_no_archived_result(lookup, candidate_id)
    return {
        "scheduled_team": data.get("team_id"),
        **({"reobservation_id": reobservation_id} if reobservation_id else {}),
    }


def _validate_merge_measurement(actor, entity, data, lookup):
    measurement = data.get("measurement")
    if not isinstance(measurement, dict):
        raise ValidationError("measurement must be an object")
    try:
        ra = float(measurement.get("ra"))
        dec = float(measurement.get("dec"))
        magnitude = float(measurement.get("magnitude"))
    except (TypeError, ValueError):
        raise ValidationError("measurement coordinates and magnitude must be numeric")
    if ra < 0 or ra >= 360 or dec < -90 or dec > 90:
        raise ValidationError("measurement coordinates are outside valid ranges")
    observed_at = str(measurement.get("observed_at", "")).strip()
    if not observed_at:
        raise ValidationError("measurement observed_at is required")
    existing = list(entity["data"].get("measurements") or [])
    if any(item.get("observed_at") == observed_at for item in existing):
        raise ConflictError("measurement timestamp already merged")
    merged = existing + [{"observed_at": observed_at, "ra": ra, "dec": dec, "magnitude": magnitude}]
    latest = sorted(merged, key=lambda item: item["observed_at"])[-1]
    return {
        "measurements": merged,
        "merged_measurement_count": len(merged),
        "latest_magnitude": latest["magnitude"],
        "priority_score": calculate_priority(latest["magnitude"], entity["data"].get("transient_type")),
    }


def _validate_reclassify(actor, entity, data, lookup):
    if data.get("new_type") not in {"grb", "supernova", "tde", "variable", "unknown"}:
        raise ValidationError("unsupported transient type")
    return {"transient_type": data["new_type"], "previous_type": entity["data"].get("transient_type")}


def _validate_correct(actor, entity, data, lookup):
    if not str(data.get("reason", "")).strip():
        raise ValidationError("correction reason is required")
    return {"corrected_by": actor.user_id}


def _validate_schedule(actor, entity, data, lookup):
    candidate_id = entity["data"].get("candidate_id")
    archived = _candidate_has_archived_result(lookup, candidate_id)
    if archived and archived["id"] != entity["id"]:
        raise ConflictError(
            "candidate %s already has a usable archived result %s; refusing to occupy telescope time"
            % (candidate_id, archived["id"])
        )
    observations = lookup("observation", "telescope_id", entity["data"].get("telescope_id")) if lookup else []
    for other in observations:
        if other["id"] == entity["id"] or other["status"] != "scheduled":
            continue
        if measurements_overlap(
            entity["data"].get("start_at"),
            entity["data"].get("end_at"),
            other["data"].get("start_at"),
            other["data"].get("end_at"),
        ):
            raise ConflictError("telescope is already scheduled in this window")
    team_observations = lookup("observation", "team_id", entity["data"].get("team_id")) if lookup else []
    for other in team_observations:
        if other["id"] == entity["id"] or other["status"] != "scheduled":
            continue
        if measurements_overlap(
            entity["data"].get("start_at"),
            entity["data"].get("end_at"),
            other["data"].get("start_at"),
            other["data"].get("end_at"),
        ):
            raise ConflictError("observation team is already committed in this window")
    return {"scheduled_by": actor.user_id}


def _validate_complete(actor, entity, data, lookup):
    evaluation = evaluate_observation_quality(data)
    if evaluation["passed"]:
        return {
            "quality": evaluation,
            "quality_submitted_by": actor.user_id,
            "__next_status__": "review_pending",
        }
    archived = _candidate_has_archived_result(lookup, entity["data"].get("candidate_id"))
    return {
        "quality": evaluation,
        "quality_submitted_by": actor.user_id,
        "quality_archived_result_exists": bool(archived),
        "quality_archived_result_id": archived["id"] if archived else None,
        "__next_status__": "needs_reobservation",
    }


def _validate_review(actor, entity, data, lookup):
    decision = data.get("decision")
    if decision not in ("approve", "reject"):
        raise ValidationError("decision must be approve or reject")
    if decision == "approve" and entity["status"] != "review_pending":
        raise InvalidTransition(
            "only pending reviews with passing quality can be approved for archival"
        )
    patch = {
        "reviewed_by": actor.user_id,
        "review_decision": decision,
        "__next_status__": "archived" if decision == "approve" else "needs_reobservation",
    }
    if decision == "reject":
        if not str(data.get("reason", "")).strip():
            raise ValidationError("rejection reason is required")
        previous_quality = entity["data"].get("quality") or {}
        review_reason = {
            "metric": "review",
            "constraint": "review",
            "value": None,
            "limit": None,
            "message": str(data.get("reason", "")).strip(),
        }
        reasons = list(previous_quality.get("reasons") or [])
        if not any(item.get("metric") == "review" for item in reasons):
            reasons.append(review_reason)
        patch["quality"] = {
            **previous_quality,
            "passed": False,
            "reasons": reasons,
            "review_reason": review_reason["message"],
        }
    return patch


def _validate_reobservation(actor, data, lookup):
    candidate_id = data.get("candidate_id")
    if not _find_one(lookup, "candidate", "id", candidate_id):
        raise ValidationError("candidate does not exist")
    source_observation_id = data.get("source_observation_id")
    if source_observation_id:
        source = _find_one(lookup, "observation", "id", source_observation_id)
        if not source:
            raise ValidationError("source observation does not exist")
        if source["data"].get("candidate_id") != candidate_id:
            raise ValidationError("source observation belongs to another candidate")
    reasons = data.get("reasons")
    if not isinstance(reasons, list) or not reasons:
        raise ValidationError("reobservation reasons are required")
    _ensure_no_archived_result(lookup, candidate_id)
    return {"origin": data.get("origin", "manual"), "created_by_user": actor.user_id}


def _validate_cancel_reobservation(actor, entity, data, lookup):
    if not str(data.get("reason", "")).strip():
        raise ValidationError("cancellation reason is required")
    return {"cancelled_by": actor.user_id}


class RuleEngine:
    ALIASES = {
        "sources": "source",
        "candidates": "candidate",
        "telescopes": "telescope",
        "observations": "observation",
        "reobservations": "reobservation",
    }
    INITIAL_STATUS = {
        "source": "registered",
        "candidate": "detected",
        "telescope": "available",
        "observation": "requested",
        "reobservation": "open",
    }
    TRANSITIONS = {
        "source": {
            "activate": (("registered",), "active"),
            "retire": (("active",), "retired"),
        },
        "candidate": {
            "merge_measurement": (("detected", "triaged"), "triaged"),
            "triage": (("detected",), "triaged"),
            "reclassify": (("triaged",), "triaged"),
            "correct": (("detected", "triaged", "classified"), "triaged"),
            "withdraw": (("detected", "triaged"), "withdrawn"),
            "classify": (("triaged",), "classified"),
        },
        "telescope": {
            "restrict": (("available",), "restricted"),
            "restore": (("restricted",), "available"),
        },
        "observation": {
            "schedule": (("requested",), "scheduled"),
            "complete": (("scheduled",), "__dynamic__"),
            "review": (("review_pending", "needs_reobservation"), "__dynamic__"),
            "withdraw": (("requested", "scheduled"), "withdrawn"),
            "correct": (("requested", "scheduled"), "requested"),
        },
        "reobservation": {
            "mark_scheduled": (("open",), "scheduled"),
            "resolve": (("open", "scheduled"), "resolved"),
            "cancel": (("open", "scheduled"), "cancelled"),
        },
    }
    CREATE_REQUIRED = {
        "source": ("name", "survey_name"),
        "candidate": ("source_id", "event_id", "ra", "dec", "magnitude", "transient_type", "observed_at"),
        "telescope": ("name", "aperture_m", "site_name"),
        "observation": ("candidate_id", "telescope_id", "team_id", "start_at", "end_at", "mode"),
        "reobservation": ("candidate_id", "reasons"),
    }
    ACTION_REQUIRED = {
        ("source", "retire"): ("reason",),
        ("candidate", "merge_measurement"): ("measurement",),
        ("candidate", "triage"): ("reason",),
        ("candidate", "reclassify"): ("new_type", "reason"),
        ("candidate", "correct"): ("reason",),
        ("candidate", "withdraw"): ("reason",),
        ("candidate", "classify"): ("classification",),
        ("telescope", "restrict"): ("reason",),
        ("observation", "schedule"): ("operator_id",),
        ("observation", "complete"): (
            "cloud_cover",
            "seeing_arcsec",
            "limiting_magnitude",
            "tracking_error_arcsec",
        ),
        ("observation", "review"): ("decision",),
        ("observation", "withdraw"): ("reason",),
        ("observation", "correct"): ("reason",),
        ("reobservation", "cancel"): ("reason",),
    }
    CREATE_ROLES = {
        "source": ("analyst", "admin"),
        "candidate": ("analyst", "operator", "admin"),
        "telescope": ("coordinator", "admin"),
        "observation": ("analyst", "coordinator", "admin"),
        "reobservation": ("coordinator", "supervisor", "admin"),
    }
    ROLE_ACTIONS = {
        ("source", "activate"): ("coordinator", "admin"),
        ("source", "retire"): ("coordinator", "admin"),
        ("candidate", "merge_measurement"): ("analyst", "operator", "admin"),
        ("candidate", "triage"): ("analyst", "operator", "admin"),
        ("candidate", "reclassify"): ("analyst", "supervisor", "admin"),
        ("candidate", "correct"): ("analyst", "supervisor", "admin"),
        ("candidate", "withdraw"): ("analyst", "supervisor", "admin"),
        ("candidate", "classify"): ("analyst", "supervisor", "admin"),
        ("telescope", "restrict"): ("coordinator", "admin"),
        ("telescope", "restore"): ("coordinator", "admin"),
        ("observation", "schedule"): ("coordinator", "admin"),
        ("observation", "complete"): ("operator", "coordinator", "admin"),
        ("observation", "review"): ("supervisor", "admin"),
        ("reobservation", "mark_scheduled"): ("coordinator", "operator", "admin"),
        ("reobservation", "resolve"): ("supervisor", "admin"),
        ("reobservation", "cancel"): ("coordinator", "supervisor", "admin"),
    }
    CUSTOM_CREATE = {
        "source": _validate_source,
        "candidate": _validate_candidate,
        "telescope": _validate_telescope,
        "observation": _validate_observation,
        "reobservation": _validate_reobservation,
    }
    CUSTOM_TRANSITIONS = {
        ("candidate", "merge_measurement"): _validate_merge_measurement,
        ("candidate", "reclassify"): _validate_reclassify,
        ("candidate", "correct"): _validate_correct,
        ("observation", "schedule"): _validate_schedule,
        ("observation", "complete"): _validate_complete,
        ("observation", "review"): _validate_review,
        ("observation", "correct"): _validate_correct,
        ("reobservation", "cancel"): _validate_cancel_reobservation,
    }

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    @staticmethod
    def _ensure_role(actor, allowed):
        if actor.role not in allowed:
            raise PermissionDenied("role %s is not allowed here" % actor.role)

    @staticmethod
    def _require(data, fields):
        for field in fields:
            value = data.get(field)
            if value is None or value == "" or value == [] or value == {}:
                raise ValidationError("missing required field: " + field)

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        self._ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        self._require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = self.CUSTOM_CREATE.get(kind)
        return custom(actor, data, lookup) if custom else {}

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition("cannot %s from status %s" % (action, entity["status"]))
        allowed_roles = self.ROLE_ACTIONS.get((kind, action), self.ROLE_ACTIONS.get(action, ("admin",)))
        self._ensure_role(actor, allowed_roles)
        self._require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = self.CUSTOM_TRANSITIONS.get((kind, action))
        extra = custom(actor, entity, data, lookup) if custom else {}
        patch = dict(data)
        if extra:
            dynamic_status = extra.pop("__next_status__", None)
            patch.update(extra)
        else:
            dynamic_status = None
        if next_status == "__dynamic__":
            next_status = dynamic_status
            if not next_status:
                raise InvalidTransition("action %s did not resolve a target status" % action)
        return next_status, patch
