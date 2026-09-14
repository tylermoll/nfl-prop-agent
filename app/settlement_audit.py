"""Read-only audit of persisted evidence for settlement event identity."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from app.historical.normalize import normalize_schedules
from app.identities import canonical_event_identity, teams_from_event_label
from app.settlement_identity import parse_observation_event_id, resolve_settlement_game, utc_datetime


def _iso(value: Any) -> str | None:
    try:
        return utc_datetime(value).isoformat()
    except (TypeError, ValueError):
        return None


def _canonical(value: Any) -> str | None:
    parsed = parse_observation_event_id(value)
    return canonical_event_identity(*parsed) if parsed else None


def _capture_items(details: dict, event_id: str) -> list[dict]:
    items = []
    for section in ("due", "selected", "completed", "deferred", "failed"):
        for item in details.get(section, []) if isinstance(details.get(section), list) else []:
            if isinstance(item, dict) and str(item.get("provider_event_id") or item.get("event_id")) == event_id:
                items.append({"section": section, "kickoff_utc": _iso(item.get("kickoff_utc")),
                              "slot": item.get("slot"), "canonical_event": _canonical(item.get("canonical_event")),
                              "matchup": item.get("matchup") if isinstance(item.get("matchup"), str) else None})
    return items


def _execution_evidence(executions: list[dict], event_id: str,
                        kickoff: str) -> tuple[list[dict], set[str], bool]:
    """Extract only safe identity fields; never return an execution's raw JSON."""
    output, canonical, conflicting = [], set(), False
    for execution in executions:
        details = execution.get("details") if isinstance(execution.get("details"), dict) else {}
        captures = _capture_items(details, event_id)
        matching_captures = [item for item in captures if item["kickoff_utc"] == kickoff]
        for item in matching_captures:
            identity = item["canonical_event"]
            label_teams = teams_from_event_label(item["matchup"])
            label_identity = canonical_event_identity(*label_teams) if label_teams else None
            if identity and label_identity and identity != label_identity:
                identity = None
                conflicting = True
            if identity:
                canonical.add(identity)
            output.append({"execution_id": str(execution["execution_id"]),
                "section": item["section"], "kickoff_utc": item["kickoff_utc"],
                "slot": item["slot"], "canonical_event": identity,
                "matchup_present": item["matchup"] is not None,
                "matchup_agrees_with_canonical_event": (
                    label_identity == identity if label_identity and identity else None)})
        rejected = details.get("rejected_props") if isinstance(details.get("rejected_props"), list) else []
        for item in rejected:
            if not isinstance(item, dict) or str(item.get("provider_event_id")) != event_id:
                continue
            identity = _canonical(item.get("canonical_event"))
            # A rejected prop is bound to a kickoff only when this same persisted
            # execution also contains the provider event in its capture lists.
            bound = bool(matching_captures)
            if identity and bound:
                canonical.add(identity)
            elif bound and item.get("canonical_event") is not None:
                conflicting = True
            output.append({"execution_id": str(execution["execution_id"]),
                "section": "rejected_props", "kickoff_utc": kickoff if bound else None,
                "slot": matching_captures[0]["slot"] if len(matching_captures) == 1 else None,
                "canonical_event": identity if bound else None, "matchup_present": False,
                "matchup_agrees_with_canonical_event": None})
    return output, canonical, conflicting


def _future_observation_identity(rows: list[dict], event_id: str,
                                 kickoff: str) -> tuple[list[dict], set[str], bool]:
    evidence, identities, invalid = [], set(), False
    for row in rows:
        value = row.get("context", {}).get("event_identity") if isinstance(row.get("context"), dict) else None
        if not isinstance(value, dict):
            continue
        provider = str(value.get("provider_event_id", ""))
        canonical = _canonical(value.get("canonical_event"))
        away, home = value.get("away_team"), value.get("home_team")
        pair_identity = canonical_event_identity(away, home)
        evidence_kickoff = _iso(value.get("kickoff_utc"))
        valid = (provider == event_id and evidence_kickoff == kickoff and canonical is not None and
                 canonical == pair_identity)
        if valid:
            identities.add(canonical)
        else:
            invalid = True
        evidence.append({"provider_event_id": provider or None, "canonical_event": canonical if valid else None,
                         "away_team": away, "home_team": home, "kickoff_utc": evidence_kickoff,
                         "evidence_valid": valid})
    return evidence, identities, invalid


def audit_settlement_identities(*, store, nflverse, refresh: bool = True,
                                now: datetime | None = None) -> dict[str, Any]:
    """Audit eligible observations and persisted metadata without any database write."""
    inspected_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    observations, _ = store.settlement_candidates(inspected_at)
    persisted = store.persisted_event_identity_evidence({str(row["game_id"]) for row in observations})
    schedules = normalize_schedules(nflverse.fetch("schedules", refresh=refresh))
    slots_by_event: dict[str, list[dict]] = defaultdict(list)
    for slot in persisted["slots"]:
        slots_by_event[str(slot["event_id"])].append(slot)
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for observation in observations:
        groups[(str(observation["game_id"]), _iso(observation["kickoff_utc"]) or "invalid")].append(observation)

    events = []
    for (event_id, kickoff), rows in sorted(groups.items()):
        execution_evidence, execution_ids, execution_conflict = _execution_evidence(
            persisted["executions"], event_id, kickoff)
        observation_evidence, observation_ids, observation_conflict = _future_observation_identity(
            rows, event_id, kickoff)
        parsed = parse_observation_event_id(event_id)
        parsed_identity = canonical_event_identity(*parsed) if parsed else None
        identities = execution_ids | observation_ids | ({parsed_identity} if parsed_identity else set())
        source_provider_ids = sorted({event_id for row in rows
            if isinstance(row.get("source_observation_ids"), dict) and any(
                isinstance(value, str) and value.startswith(event_id + ":")
                for value in row["source_observation_ids"].values())})
        candidate = None
        failure = None
        method = None
        if execution_conflict or observation_conflict:
            failure = "conflicting_persisted_identity_evidence"
        elif len(identities) > 1:
            failure = "conflicting_persisted_canonical_events"
        elif len(identities) == 0:
            failure = "persisted_team_identity_not_found"
        else:
            canonical = next(iter(identities))
            proposed = {**rows[0], "game_id": canonical}
            resolution = resolve_settlement_game(proposed, schedules)
            if resolution.identity_verified:
                candidate = resolution.game
                method = ("immutable_observation_event_identity" if observation_ids else
                          "persisted_scheduler_canonical_event_and_kickoff" if execution_ids else
                          "canonical_observation_game_id")
            else:
                failure = resolution.failure_reason
        verified = candidate is not None
        slots = [{"event_id": event_id, "slot": row["slot"],
                  "target_time_utc": _iso(row["target_time_utc"]), "status": row["status"]}
                 for row in sorted(slots_by_event[event_id], key=lambda value: (str(value["slot"]), value["target_time_utc"]))]
        events.append({"observation_event_identity": event_id,
            "observation_game_id": event_id,
            "parsed_away_team": parsed[0] if parsed else None,
            "parsed_home_team": parsed[1] if parsed else None,
            "observation_kickoff_utc": kickoff,
            "observation_team_values": sorted({str(row["team"]) for row in rows if row.get("team")}),
            "observation_opponent_values": sorted({str(row["opponent"]) for row in rows if row.get("opponent")}),
            "provider_source_event_ids": source_provider_ids, "capture_slots": slots,
            "scheduler_execution_evidence": execution_evidence,
            "observation_event_identity_evidence": observation_evidence,
            "persisted_canonical_events": sorted(identities),
            "candidate_nflverse_game_id": str(candidate.game_id) if candidate is not None else None,
            "nflverse_away_team": str(candidate.away_team) if candidate is not None else None,
            "nflverse_home_team": str(candidate.home_team) if candidate is not None else None,
            "nflverse_kickoff_utc": _iso(candidate.kickoff) if candidate is not None else None,
            "season": int(candidate.season) if candidate is not None else None,
            "week": int(candidate.week) if candidate is not None else None,
            "match_method": method, "evidence_chain": ([
                "shadow_observations.game_id = persisted provider_event_id",
                "same persisted scheduler execution binds provider_event_id to observation kickoff",
                "persisted canonical_event supplies the exact canonical team pair",
                "canonical team pair plus kickoff uniquely identifies one nflverse game_id",
            ] if method == "persisted_scheduler_canonical_event_and_kickoff" else [
                "immutable observation context binds provider_event_id, canonical teams, and kickoff",
                "canonical team pair plus kickoff uniquely identifies one nflverse game_id",
            ] if method == "immutable_observation_event_identity" else []),
            "identity_verified": verified, "failure_reason": failure, "observation_count": len(rows)})
    return {"read_only": True, "inspected_at_utc": inspected_at.isoformat(),
        "total_unsettled_observations": len(observations),
        "unique_observation_event_identities": len({row["game_id"] for row in observations}),
        "event_kickoff_groups": len(events),
        "verified_event_kickoff_groups": sum(item["identity_verified"] for item in events), "events": events}
