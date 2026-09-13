"""Pure, descriptive evaluation of independently captured pregame context.

Nothing in this module writes observations, invokes a provider, or produces an
adjusted probability.  Input snapshots are an intentionally small ingestion
contract for a separate public-data collection job.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

WEATHER_MAX_AGE_SECONDS = 3 * 60 * 60
INJURY_MAX_AGE_SECONDS = 12 * 60 * 60
ROLE_MAX_AGE_SECONDS = 7 * 24 * 60 * 60


def _utc(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _dimension(label: str, reason: str) -> dict[str, str]:
    return {"label": label, "reason": reason}


def evaluate_context(observation: dict[str, Any], snapshot: dict[str, Any] | None,
                     *, now: datetime | None = None) -> dict[str, Any]:
    """Return a deterministic explanation layer while leaving inputs untouched."""
    now = _utc(now) or datetime.now(timezone.utc)
    edge = abs(float(observation.get("probability_edge_pp") or 0))
    books = int(observation.get("reference_book_coverage") or 0)
    ref_probability = observation.get("same_threshold_reference_probability")
    model_probability = observation.get("model_probability")
    comparable_ref = ((1-float(ref_probability)) if observation.get("side") == "under" else float(ref_probability)) \
        if ref_probability is not None else None
    dims = {
        "model_signal": _dimension("strong" if edge >= 5 else "moderate" if edge >= 2 else "weak",
                                    f"Absolute persisted model edge is {edge:.2f} percentage points."),
        "market_confirmation": _dimension("insufficient" if not books or ref_probability is None else
                                           "confirmed" if abs(comparable_ref-float(model_probability)) <= .05 else "conflicted",
                                           ("No same-threshold independent reference consensus is available." if not books or ref_probability is None else
                                            f"{books} independent book(s); same-threshold probability differs from the model by "
                                            f"{abs(comparable_ref-float(model_probability))*100:.1f} points.")),
    }
    if not snapshot:
        missing = _dimension("unknown", "No external context snapshot was available as of the requested time.")
        dims.update(weather_context=missing.copy(), injury_context=missing.copy(), role_context=missing.copy(),
                    data_freshness=_dimension("missing", "External context has not been captured."))
        return {"available": False, "as_of_utc": None, "collected_at_utc": None,
                "weather": {}, "injuries": {}, "role": {}, "flags": {"missing_external_context": True},
                "dimensions": dims, "evidence_quality": "insufficient_context",
                "evidence_quality_reason": "Weather, injury, and role context are all unavailable.",
                "disclaimer": "Descriptive context only; this is not a probability or a guarantee."}

    weather, injuries, role = (dict(snapshot.get(k) or {}) for k in ("weather", "injuries", "role"))
    as_of = _utc(snapshot.get("as_of_utc")); age = max(0, (now-as_of).total_seconds()) if as_of else None
    venue = str(weather.get("venue_type") or "unknown").lower()
    indoor = venue in {"indoor", "dome", "closed_roof"}
    wind = weather.get("wind_mph"); gust = weather.get("wind_gust_mph")
    precip = float(weather.get("precipitation_probability") or 0)
    temp = weather.get("temperature_f")
    weather_flags = {"indoor_weather_irrelevant": indoor,
        "wind_high": not indoor and ((wind is not None and wind >= 20) or (gust is not None and gust >= 30)),
        "wind_moderate": not indoor and wind is not None and 12 <= wind < 20,
        "precipitation_risk": not indoor and precip >= .30,
        "extreme_temperature": not indoor and temp is not None and (temp <= 20 or temp >= 95)}
    weather_flags["weather_neutral"] = not any(weather_flags.values())
    designation = str(injuries.get("player_designation") or "").lower()
    active = injuries.get("active_status")
    qb = str(injuries.get("qb_status") or "").lower()
    injury_concern = designation in {"questionable", "doubtful", "out", "injured_reserve"} or active == "inactive"
    qb_concern = observation.get("canonical_market") in {"player_reception_yds", "player_receptions"} and qb in {"out", "inactive", "doubtful"}
    role_status = str(role.get("change_status") or "none").lower()
    confirmed_role = role_status == "confirmed" and bool(role.get("material_change"))
    uncertain_role = role_status in {"uncertain", "reported"} or bool(role.get("uncertain"))
    week = role.get("season_week")
    mismatch = bool(role.get("current_team_mismatch") or role.get("prior_season_role_mismatch"))
    stale_role_flag = (week == 1 or week == "1") and bool(role.get("prior_season_usage_dependency")) and (confirmed_role or mismatch or injury_concern)
    flags = {**weather_flags, "injury_concern": injury_concern, "qb_absence_affects_receiver": qb_concern,
             "confirmed_role_change": confirmed_role, "role_uncertainty": uncertain_role,
             "historical_role_may_be_stale": stale_role_flag}
    dims["weather_context"] = _dimension("irrelevant" if indoor else "concern" if any(weather_flags[k] for k in
        ("wind_high", "precipitation_risk", "extreme_temperature")) else "neutral",
        "Venue is indoors/roof closed." if indoor else "Descriptive outdoor conditions evaluated; no betting effect assumed.")
    dims["injury_context"] = _dimension("concern" if injury_concern or qb_concern else "clear" if injuries else "unknown",
        "Player/QB has a material confirmed or designated status." if injury_concern or qb_concern else
        "No material injury designation is present." if injuries else "No injury report was captured.")
    dims["role_context"] = _dimension("confirmed_change" if confirmed_role else "uncertain" if uncertain_role or mismatch else "stable" if role else "unknown",
        "Explicit structured role evidence indicates a material change." if confirmed_role else
        "Role evidence is uncertain or differs from prior-season history." if uncertain_role or mismatch else
        "No material structured role change is present." if role else "No depth-chart context was captured.")
    category_limits = {"weather": WEATHER_MAX_AGE_SECONDS, "injuries": INJURY_MAX_AGE_SECONDS,
                       "role": ROLE_MAX_AGE_SECONDS}
    category_freshness = {name: {"max_age_seconds": limit, "age_seconds": int(age) if age is not None else None,
                                 "stale": age is None or age > limit}
                          for name, limit in category_limits.items() if snapshot.get(name)}
    stale_names = [name for name, value in category_freshness.items() if value["stale"]]
    stale = age is None or bool(stale_names)
    dims["data_freshness"] = _dimension("stale" if stale else "fresh", "Snapshot age is unavailable." if age is None else
        f"Snapshot age is {int(age)} seconds; stale categories: {', '.join(stale_names) or 'none'}.")
    negatives = sum((dims["market_confirmation"]["label"] in {"conflicted", "insufficient"},
                     dims["weather_context"]["label"] == "concern", dims["injury_context"]["label"] == "concern",
                     dims["role_context"]["label"] in {"confirmed_change", "uncertain"}, stale))
    missing = sum(not x for x in (weather, injuries, role))
    quality = "insufficient_context" if missing >= 2 else "weak" if stale or negatives >= 4 else "mixed" if negatives >= 2 else "strong" if edge >= 5 and dims["market_confirmation"]["label"] == "confirmed" else "moderate"
    return {"available": True, "context_id": snapshot.get("context_id"), "as_of_utc": as_of,
            "collected_at_utc": _utc(snapshot.get("collected_at_utc")), "sources": snapshot.get("sources") or {},
            "weather": weather, "injuries": injuries, "role": role, "category_freshness": category_freshness,
            "flags": flags, "dimensions": dims,
            "evidence_quality": quality, "evidence_quality_reason": f"Deterministic assessment: {negatives} concern(s), {missing} missing category/categories.",
            "disclaimer": "Descriptive context only; this is not a probability or a guarantee."}
