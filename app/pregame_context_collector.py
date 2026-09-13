"""Public, read-only pregame context collection for existing observations."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select

from app.shadow_storage import observations

# Coordinates describe the playing venue, not a team office.  Roof status is a
# venue property; retractable venues remain explicitly retractable unless an
# official roof state is available (we never guess that state).
STADIUMS = {
    "ARI": ("State Farm Stadium", 33.5276, -112.2626, "retractable"),
    "ATL": ("Mercedes-Benz Stadium", 33.7554, -84.4008, "retractable"),
    "BAL": ("M&T Bank Stadium", 39.2780, -76.6227, "outdoor"),
    "BUF": ("Highmark Stadium", 42.7738, -78.7870, "outdoor"),
    "CAR": ("Bank of America Stadium", 35.2258, -80.8528, "outdoor"),
    "CHI": ("Soldier Field", 41.8623, -87.6167, "outdoor"),
    "CIN": ("Paycor Stadium", 39.0955, -84.5161, "outdoor"),
    "CLE": ("Huntington Bank Field", 41.5061, -81.6995, "outdoor"),
    "DAL": ("AT&T Stadium", 32.7473, -97.0945, "retractable"),
    "DEN": ("Empower Field at Mile High", 39.7439, -105.0201, "outdoor"),
    "DET": ("Ford Field", 42.3400, -83.0456, "indoor"),
    "GB": ("Lambeau Field", 44.5013, -88.0622, "outdoor"),
    "HOU": ("NRG Stadium", 29.6847, -95.4107, "retractable"),
    "IND": ("Lucas Oil Stadium", 39.7601, -86.1639, "retractable"),
    "JAX": ("EverBank Stadium", 30.3239, -81.6373, "outdoor"),
    "KC": ("Arrowhead Stadium", 39.0489, -94.4839, "outdoor"),
    "LV": ("Allegiant Stadium", 36.0909, -115.1833, "indoor"),
    "LAC": ("SoFi Stadium", 33.9535, -118.3392, "indoor"),
    "LAR": ("SoFi Stadium", 33.9535, -118.3392, "indoor"),
    "MIA": ("Hard Rock Stadium", 25.9580, -80.2389, "outdoor"),
    "MIN": ("U.S. Bank Stadium", 44.9736, -93.2575, "indoor"),
    "NE": ("Gillette Stadium", 42.0909, -71.2643, "outdoor"),
    "NO": ("Caesars Superdome", 29.9511, -90.0812, "indoor"),
    "NYG": ("MetLife Stadium", 40.8135, -74.0745, "outdoor"),
    "NYJ": ("MetLife Stadium", 40.8135, -74.0745, "outdoor"),
    "PHI": ("Lincoln Financial Field", 39.9008, -75.1675, "outdoor"),
    "PIT": ("Acrisure Stadium", 40.4468, -80.0158, "outdoor"),
    "SEA": ("Lumen Field", 47.5952, -122.3316, "outdoor"),
    "SF": ("Levi's Stadium", 37.4030, -121.9700, "outdoor"),
    "TB": ("Raymond James Stadium", 27.9759, -82.5033, "outdoor"),
    "TEN": ("Nissan Stadium", 36.1665, -86.7713, "outdoor"),
    "WAS": ("Northwest Stadium", 38.9077, -76.8645, "outdoor"),
}

TEAM_DOMAINS = {"ARI":"azcardinals.com","ATL":"atlantafalcons.com","BAL":"baltimoreravens.com","BUF":"buffalobills.com",
    "CAR":"panthers.com","CHI":"chicagobears.com","CIN":"bengals.com","CLE":"clevelandbrowns.com",
    "DAL":"dallascowboys.com","DEN":"denverbroncos.com","DET":"detroitlions.com","GB":"packers.com",
    "HOU":"houstontexans.com","IND":"colts.com","JAX":"jaguars.com","KC":"chiefs.com","LV":"raiders.com",
    "LAC":"chargers.com","LAR":"therams.com","MIA":"miamidolphins.com","MIN":"vikings.com","NE":"patriots.com",
    "NO":"neworleanssaints.com","NYG":"giants.com","NYJ":"newyorkjets.com","PHI":"philadelphiaeagles.com",
    "PIT":"steelers.com","SEA":"seahawks.com","SF":"49ers.com","TB":"buccaneers.com",
    "TEN":"tennesseetitans.com","WAS":"commanders.com"}


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _home_team(game_id: str, team: str, opponent: str | None) -> str | None:
    """nflverse game IDs end in AWAY_HOME; safely fail if identity differs."""
    parts = game_id.upper().split("_")
    if len(parts) >= 4 and parts[-1] in STADIUMS:
        return parts[-1]
    return team if team in STADIUMS and not opponent else None


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _wind(value: str | None) -> float | None:
    numbers = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", value or "")]
    return max(numbers) if numbers else None


class _TableText(HTMLParser):
    def __init__(self):
        super().__init__(); self.rows: list[list[str]] = []; self._row: list[str] | None = None; self._cell: list[str] | None = None
    def handle_starttag(self, tag, attrs):
        if tag == "tr": self._row = []
        elif tag in {"td", "th"} and self._row is not None: self._cell = []
    def handle_data(self, data):
        if self._cell is not None: self._cell.append(data)
    def handle_endtag(self, tag):
        if tag in {"td", "th"} and self._cell is not None and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split())); self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row: self.rows.append(self._row)
            self._row = None


@dataclass
class PublicContextClient:
    user_agent: str
    client: httpx.Client | None = None
    _page_cache: dict[str, tuple[list[list[str]], str | None]] = field(default_factory=dict, init=False)

    def __post_init__(self):
        self.client = self.client or httpx.Client(timeout=12, follow_redirects=True,
            headers={"User-Agent": self.user_agent, "Accept": "application/geo+json, application/json, text/html"})

    def weather(self, home: str, kickoff: datetime) -> tuple[dict, dict]:
        venue = STADIUMS.get(home)
        if not venue:
            return {"availability":"unavailable","venue_type":"unknown","reason":"stadium_not_resolved"}, {"status":"unavailable"}
        name, lat, lon, venue_type = venue
        base = {"availability":"available","stadium":name,"latitude":lat,"longitude":lon,"venue_type":venue_type,
                "roof_status":"unknown" if venue_type == "retractable" else ("closed" if venue_type == "indoor" else "open")}
        if venue_type == "indoor":
            return base, {"provider":"NOAA/National Weather Service","status":"not_applicable_indoor","source_timestamp":None}
        points_url = f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}"
        points = self.client.get(points_url); points.raise_for_status()
        hourly_url = points.json()["properties"]["forecastHourly"]
        response = self.client.get(hourly_url); response.raise_for_status(); payload = response.json()
        periods = payload.get("properties", {}).get("periods", [])
        when = _utc(kickoff)
        def distance(period):
            return abs((_utc(datetime.fromisoformat(period["startTime"])) - when).total_seconds())
        period = min(periods, key=distance) if periods else None
        updated = payload.get("properties", {}).get("updateTime") or response.headers.get("Last-Modified")
        if not period:
            return {**base,"availability":"unavailable","reason":"kickoff_forecast_unavailable"}, {
                "provider":"NOAA/National Weather Service","url":hourly_url,"status":"unavailable","source_timestamp":updated}
        precip = _number((period.get("probabilityOfPrecipitation") or {}).get("value"))
        humidity = _number((period.get("relativeHumidity") or {}).get("value"))
        forecast_zone = ZoneInfo(payload.get("properties", {}).get("timeZone") or "UTC")
        forecast = {**base,"kickoff_local":when.astimezone(forecast_zone).isoformat(),
            "forecast_period_start":period.get("startTime"),"temperature_f":_number(period.get("temperature")),
            "wind_mph":_wind(period.get("windSpeed")),"wind_gust_mph":_wind(period.get("windGust")),
            "precipitation_probability":precip / 100 if precip is not None else None,
            "precipitation_type":period.get("shortForecast"),"humidity":humidity / 100 if humidity is not None else None}
        return forecast, {"provider":"NOAA/National Weather Service","url":hourly_url,"status":"official",
                          "source_timestamp":updated,"retrieved_from":points_url}

    def official_player(self, team: str, player_name: str, season: int, week: int) -> tuple[dict, dict, dict, dict]:
        injury_url = f"https://www.nfl.com/injuries/league/{season}/reg{week}"
        domain = TEAM_DOMAINS.get(team)
        depth_url = f"https://www.{domain}/team/depth-chart" if domain else None
        injury = {"availability":"unavailable","player_designation":"unknown","practice_participation":"unknown",
                  "active_status":"unknown","qb_status":"unknown","skill_position_absences":[],"status_confidence":"unavailable"}
        role = {"availability":"unavailable","current_team":team,"depth_chart_position":"unknown","starter":None,
                "change_status":"unknown","rookie_status":"unknown","new_team_status":"unknown"}
        sources: dict[str, Any] = {}
        try:
            rows, stamp = self._official_rows(injury_url)
            match = next((row for row in rows if any(cell.casefold() == player_name.casefold() for cell in row)), None)
            sources["injuries"] = {"provider":"NFL.com official injury report","url":injury_url,
                "status":"official" if match else "official_no_player_entry","source_timestamp":stamp}
            if match:
                text = " | ".join(match); designations = ("Out","Doubtful","Questionable","Injured Reserve")
                participation = ("Did Not Participate","Limited Participation","Full Participation")
                injury.update(availability="available", player_designation=next((x.lower().replace(" ","_") for x in designations if x.lower() in text.lower()), "none"),
                    practice_participation=next((x.lower().replace(" ","_") for x in participation if x.lower() in text.lower()), "unknown"), status_confidence="confirmed")
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            sources["injuries"] = {"provider":"NFL.com official injury report","url":injury_url,"status":"unavailable","error_type":type(exc).__name__,"source_timestamp":None}
        try:
            if not depth_url: raise ValueError("team_not_resolved")
            rows, stamp = self._official_rows(depth_url)
            match = next((row for row in rows if any(player_name.casefold() == cell.casefold() for cell in row)), None)
            sources["role"] = {"provider":"official team depth chart","url":depth_url,
                "status":"official" if match else "official_no_player_entry","source_timestamp":stamp}
            if match:
                player_index = next(i for i, cell in enumerate(match) if cell.casefold() == player_name.casefold())
                position = match[0] if player_index and match[0].casefold() != player_name.casefold() else "unknown"
                role.update(availability="available", depth_chart_position=position,
                    starter=(player_index == 1) if position != "unknown" else None,
                    official_depth_row=match, change_status="none")
        except (httpx.HTTPError, ValueError) as exc:
            sources["role"] = {"provider":"official team depth chart","url":depth_url,"status":"unavailable","error_type":type(exc).__name__,"source_timestamp":None}
        return injury, role, sources.get("injuries", {}), sources.get("role", {})

    def _official_rows(self, url: str) -> tuple[list[list[str]], str | None]:
        """Fetch each official document once per run, then match exact players."""
        if url not in self._page_cache:
            response = self.client.get(url); response.raise_for_status()
            parser = _TableText(); parser.feed(response.text)
            self._page_cache[url] = (parser.rows, response.headers.get("Last-Modified"))
        return self._page_cache[url]


def upcoming_observations(engine, now: datetime) -> list[dict]:
    now = _utc(now); local = now.astimezone(ZoneInfo("America/New_York")); end_local = datetime.combine(local.date()+timedelta(days=1), datetime.min.time(), ZoneInfo("America/New_York"))
    with engine.connect() as connection:
        rows = connection.execute(select(observations).where(observations.c.kickoff_utc > now,
            observations.c.kickoff_utc < end_local.astimezone(timezone.utc))).all()
    # One context snapshot per observation is required by the existing overlay.
    return [dict(row._mapping) for row in rows]


def collect_pregame_context(store, provider: PublicContextClient, *, now: datetime | None = None) -> dict:
    now = _utc(now or datetime.now(timezone.utc)); candidates = upcoming_observations(store.engine, now)
    games: dict[str, dict] = {}; snapshots = []; failures = []
    for observation in candidates:
        game_id = observation["game_id"]
        home = _home_team(game_id, observation.get("team"), observation.get("opponent"))
        game_key = f"{game_id}:{observation['kickoff_utc']}"
        if game_key not in games:
            try:
                weather, weather_source = provider.weather(home or "", observation["kickoff_utc"])
            except (httpx.HTTPError, KeyError, ValueError) as exc:
                weather = {"availability":"unavailable","venue_type":STADIUMS.get(home, (None,None,None,"unknown"))[3],"reason":"weather_fetch_failed"}
                weather_source = {"provider":"NOAA/National Weather Service","status":"unavailable","error_type":type(exc).__name__,"source_timestamp":None}
                failures.append({"game_id":game_id,"category":"weather","error_type":type(exc).__name__})
            games[game_key] = {"weather":weather,"source":weather_source}
        season, week = 0, 0
        parts = game_id.split("_")
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit(): season, week = int(parts[0]), int(parts[1])
        injury, role, injury_source, role_source = provider.official_player(observation.get("team") or "", observation["player_name"], season, week)
        sources = {"weather":games[game_key]["source"],"injuries":injury_source,"role":role_source}
        fingerprint = json.dumps({"observation_id":observation["observation_id"],"weather":games[game_key]["weather"],
            "injuries":injury,"role":role,"sources":sources}, sort_keys=True, default=str, separators=(",",":"))
        context_id = str(UUID(hashlib.sha256(fingerprint.encode()).hexdigest()[:32]))
        as_of = now
        snapshots.append({"context_id":context_id,"observation_id":observation["observation_id"],"as_of_utc":as_of,
            "collected_at_utc":now,"weather":games[game_key]["weather"],"injuries":injury,"role":role,"sources":sources})
    inserted = store.append_context_snapshots(snapshots)
    return {"started_at_utc":now.isoformat(),"scope":"upcoming games before midnight America/New_York",
        "observations_inspected":len(candidates),"games_represented":len(games),"snapshots_prepared":len(snapshots),
        "snapshots_appended":inserted,"idempotent_duplicates":len(snapshots)-inserted,"category_failures":failures,
        "network_sources":["api.weather.gov","www.nfl.com"],"odds_api_calls":0,"kalshi_calls":0,"model_calls":0,"wagering_calls":0}
