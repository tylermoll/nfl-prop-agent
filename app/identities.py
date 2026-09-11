"""Strict, provider-independent identities used for cross-market joins."""

import re
import unicodedata


# Canonical franchise IDs deliberately exclude ambiguous short forms such as
# "LA" and "NY".  Alternate provider abbreviations are accepted only where
# they identify exactly one NFL franchise.
NFL_TEAMS = {
    "ari": ("Arizona Cardinals", ("ARI", "ARZ")),
    "atl": ("Atlanta Falcons", ("ATL",)),
    "bal": ("Baltimore Ravens", ("BAL",)),
    "buf": ("Buffalo Bills", ("BUF",)),
    "car": ("Carolina Panthers", ("CAR",)),
    "chi": ("Chicago Bears", ("CHI",)),
    "cin": ("Cincinnati Bengals", ("CIN",)),
    "cle": ("Cleveland Browns", ("CLE",)),
    "dal": ("Dallas Cowboys", ("DAL",)),
    "den": ("Denver Broncos", ("DEN",)),
    "det": ("Detroit Lions", ("DET",)),
    "gb": ("Green Bay Packers", ("GB", "GBP")),
    "hou": ("Houston Texans", ("HOU",)),
    "ind": ("Indianapolis Colts", ("IND",)),
    "jax": ("Jacksonville Jaguars", ("JAX", "JAC")),
    "kc": ("Kansas City Chiefs", ("KC", "KCC")),
    "lv": ("Las Vegas Raiders", ("LV", "LVR")),
    "lac": ("Los Angeles Chargers", ("LAC",)),
    "lar": ("Los Angeles Rams", ("LAR",)),
    "mia": ("Miami Dolphins", ("MIA",)),
    "min": ("Minnesota Vikings", ("MIN",)),
    "ne": ("New England Patriots", ("NE", "NEP")),
    "no": ("New Orleans Saints", ("NO", "NOS")),
    "nyg": ("New York Giants", ("NYG",)),
    "nyj": ("New York Jets", ("NYJ",)),
    "phi": ("Philadelphia Eagles", ("PHI",)),
    "pit": ("Pittsburgh Steelers", ("PIT",)),
    "sea": ("Seattle Seahawks", ("SEA",)),
    "sf": ("San Francisco 49ers", ("SF", "SFO")),
    "tb": ("Tampa Bay Buccaneers", ("TB", "TBB")),
    "ten": ("Tennessee Titans", ("TEN",)),
    "was": ("Washington Commanders", ("WAS", "WSH")),
}


def normalize_player_name(name: str) -> str:
    """Normalize typography, not identity; no spelling or nickname guessing."""
    value = unicodedata.normalize("NFKC", name).casefold()
    value = value.replace("\u2018", "'").replace("\u2019", "'")
    value = value.replace(".", "")
    return " ".join(value.split())


def _text_key(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


_TEAM_TEXT = {
    _text_key(display): team_id
    for team_id, (display, _) in NFL_TEAMS.items()
}
_TEAM_CODES = {
    code: team_id
    for team_id, (_, codes) in NFL_TEAMS.items()
    for code in codes
}


def canonical_team(value: str | None) -> str | None:
    """Resolve an exact full team name or unambiguous provider abbreviation."""
    if not isinstance(value, str):
        return None
    return _TEAM_TEXT.get(_text_key(value)) or _TEAM_CODES.get(value.strip().upper())


def canonical_event_identity(team_a: str | None, team_b: str | None) -> str | None:
    """Return an orientation-independent NFL matchup after resolving both teams."""
    first, second = canonical_team(team_a), canonical_team(team_b)
    if first is None or second is None or first == second:
        return None
    return "nfl:" + ":".join(sorted((first, second)))


def teams_from_event_label(label: str | None) -> tuple[str, str] | None:
    """Extract only explicit ``away at/vs home``-style matchup labels."""
    if not isinstance(label, str):
        return None
    parts = re.split(r"\s+(?:at|vs\.?|versus|@)\s+", label.strip(), flags=re.I)
    if len(parts) != 2 or canonical_event_identity(parts[0], parts[1]) is None:
        return None
    return parts[0], parts[1]


def teams_from_kalshi_ticker(ticker: str | None) -> tuple[str, str] | None:
    """Resolve the unique pair of concatenated team codes at a ticker suffix."""
    if not isinstance(ticker, str):
        return None
    compact = re.sub(r"[^A-Z0-9]", "", ticker.upper())
    matches: set[tuple[str, str]] = set()
    for first_code, first in _TEAM_CODES.items():
        for second_code, second in _TEAM_CODES.items():
            if first != second and compact.endswith(first_code + second_code):
                matches.add((first, second))
    # Aliases can yield the same canonical ordered pair; reject genuinely
    # ambiguous parses rather than guessing.
    if len(matches) != 1:
        return None
    first, second = next(iter(matches))
    return NFL_TEAMS[first][0], NFL_TEAMS[second][0]


def event_identity(*, home_team: str | None = None, away_team: str | None = None,
                   event_name: str | None = None, event_ticker: str | None = None) -> str | None:
    """Build a canonical matchup from the strongest unambiguous representation."""
    identity = canonical_event_identity(away_team, home_team)
    if identity:
        return identity
    label_teams = teams_from_event_label(event_name)
    if label_teams:
        return canonical_event_identity(*label_teams)
    ticker_teams = teams_from_kalshi_ticker(event_ticker)
    if ticker_teams:
        return canonical_event_identity(*ticker_teams)
    return None
