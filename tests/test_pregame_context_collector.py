from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import create_engine, insert, select
from sqlalchemy.pool import StaticPool

from app.pregame_context_collector import PublicContextClient, collect_pregame_context
from app.shadow_storage import ShadowStore, metadata, observations, pregame_context_snapshots

NOW = datetime(2026, 9, 13, 12, tzinfo=timezone.utc)


def observation(identifier="obs-1", **changes):
    row = {"observation_id":identifier,"observed_at_utc":NOW-timedelta(minutes=5),"kickoff_utc":NOW+timedelta(hours=5),
        "game_id":"2026_01_BUF_NYG","player_id":"p1","player_name":"Example Player","team":"BUF","opponent":"NYG",
        "canonical_market":"player_receptions","line":4.5,"side":"over","hard_rock_offered_odds":-110,
        "offered_price_break_even_probability":.524,"model_probability":.55,"model_version":"v1",
        "raw_probability_edge_pp":2.6,"hypothetical_expected_return_per_dollar":.05,"edge_bucket":"edge_2_to_5pp",
        "feature_built_at_utc":NOW-timedelta(hours=1),"uncertainty_method":"residual","uncertainty_version":"v1",
        "reference_context":{},"kalshi_context":{},"source_observation_ids":{},"freshness":{},"context":{},
        "confirmation_flags":{},"research_config":{}}
    row.update(changes); return row


def store():
    engine = create_engine("sqlite://", connect_args={"check_same_thread":False}, poolclass=StaticPool)
    metadata.create_all(engine); value = object.__new__(ShadowStore); value.engine = engine
    with engine.begin() as connection: connection.execute(insert(observations), observation())
    return value


class StubProvider:
    def weather(self, home, kickoff):
        assert home == "NYG"
        return ({"availability":"available","venue_type":"outdoor","temperature_f":68,"wind_mph":8,
                 "wind_gust_mph":15,"precipitation_probability":.2,"precipitation_type":"Rain","humidity":.7},
                {"provider":"NOAA/National Weather Service","status":"official","source_timestamp":"2026-09-13T11:00:00Z"})
    def official_player(self, team, player, season, week):
        assert (team, player, season, week) == ("BUF","Example Player",2026,1)
        return ({"availability":"unavailable","player_designation":"unknown","status_confidence":"unavailable"},
                {"availability":"available","current_team":"BUF","depth_chart_position":"unknown","starter":None,
                 "change_status":"none"},
                {"provider":"NFL.com official injury report","status":"official_no_player_entry","source_timestamp":None},
                {"provider":"official team depth chart","status":"official","source_timestamp":None})


def test_cycle_appends_and_same_source_snapshot_is_idempotent():
    database = store()
    first = collect_pregame_context(database, StubProvider(), now=NOW)
    second = collect_pregame_context(database, StubProvider(), now=NOW+timedelta(minutes=1))
    assert first["snapshots_appended"] == 1 and second["snapshots_appended"] == 0
    assert first["odds_api_calls"] == first["kalshi_calls"] == first["model_calls"] == first["wagering_calls"] == 0
    with database.engine.connect() as connection:
        row = dict(connection.execute(select(pregame_context_snapshots)).one()._mapping)
    assert row["weather"]["temperature_f"] == 68
    assert row["injuries"]["status_confidence"] == "unavailable"
    assert row["sources"]["weather"]["provider"] == "NOAA/National Weather Service"


def test_weather_uses_nws_hourly_forecast_and_stadium_timezone():
    def handler(request):
        if "/points/" in str(request.url):
            return httpx.Response(200, json={"properties":{"forecastHourly":"https://api.weather.gov/grid/hourly"}})
        return httpx.Response(200, headers={"Last-Modified":"Sun, 13 Sep 2026 11:00:00 GMT"}, json={"properties":{
            "updateTime":"2026-09-13T11:00:00Z","timeZone":"America/New_York","periods":[{
                "startTime":"2026-09-13T13:00:00-04:00","temperature":72,"windSpeed":"10 to 15 mph",
                "windGust":"25 mph","shortForecast":"Chance Showers","probabilityOfPrecipitation":{"value":40},
                "relativeHumidity":{"value":65}}]}})
    http = httpx.Client(transport=httpx.MockTransport(handler))
    weather, source = PublicContextClient("tests@example.invalid", http).weather("NYG", NOW+timedelta(hours=5))
    assert weather["kickoff_local"].endswith("-04:00")
    assert (weather["temperature_f"], weather["wind_mph"], weather["wind_gust_mph"]) == (72, 15, 25)
    assert weather["precipitation_probability"] == .4 and weather["humidity"] == .65
    assert source["status"] == "official" and source["source_timestamp"] == "2026-09-13T11:00:00Z"


def test_indoor_weather_does_not_make_network_request_or_fabricate_conditions():
    def forbidden(_request):
        raise AssertionError("indoor weather must not be fetched")
    weather, source = PublicContextClient("test", httpx.Client(transport=httpx.MockTransport(forbidden))).weather("DET", NOW)
    assert weather["venue_type"] == "indoor" and "temperature_f" not in weather
    assert source["status"] == "not_applicable_indoor"


def test_weather_failure_does_not_prevent_injury_and_role_snapshot():
    class Partial(StubProvider):
        def weather(self, *_): raise httpx.TimeoutException("offline")
    database = store(); report = collect_pregame_context(database, Partial(), now=NOW)
    assert report["snapshots_appended"] == 1 and report["category_failures"][0]["category"] == "weather"
    with database.engine.connect() as connection:
        row = dict(connection.execute(select(pregame_context_snapshots)).one()._mapping)
    assert row["weather"]["availability"] == "unavailable" and row["role"]["availability"] == "available"


def test_only_upcoming_games_on_current_eastern_date_are_inspected():
    database = store()
    with database.engine.begin() as connection:
        connection.execute(insert(observations), observation("past", kickoff_utc=NOW-timedelta(minutes=1)))
        connection.execute(insert(observations), observation("tomorrow", kickoff_utc=NOW+timedelta(hours=20)))
    report = collect_pregame_context(database, StubProvider(), now=NOW)
    assert report["observations_inspected"] == 1
