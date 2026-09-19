"""Team alias tables for cross-venue matching (Lab 3).

Kalshi identifies a team by a ticker code (KXSB-27-KC, KXNFLGAME-26SEP28PHICHI-PHI) and a city in
yes_sub_title ("Kansas City", "New York J"); Polymarket by nickname or city + nickname in the
question / outcomes ("Chiefs", "Kansas City Chiefs") and by a lowercase code in the slug
(nfl-ind-kc-2026-09-21). Both venues mostly use the standard abbreviations; the known deviations
are listed as extra codes. Soccer and college teams have no table: they are matched on name tokens
after stripping club suffixes and a few aliases (club_tokens).
"""
from __future__ import annotations

import re
import unicodedata

# (canonical code, city, nickname, extra codes seen on either venue)
TEAMS: dict[str, list[tuple[str, str, str, list[str]]]] = {
    "nfl": [
        ("ARI", "Arizona", "Cardinals", []), ("ATL", "Atlanta", "Falcons", []), ("BAL", "Baltimore", "Ravens", []),
        ("BUF", "Buffalo", "Bills", []), ("CAR", "Carolina", "Panthers", []), ("CHI", "Chicago", "Bears", []),
        ("CIN", "Cincinnati", "Bengals", []), ("CLE", "Cleveland", "Browns", []), ("DAL", "Dallas", "Cowboys", []),
        ("DEN", "Denver", "Broncos", []), ("DET", "Detroit", "Lions", []), ("GB", "Green Bay", "Packers", ["GNB"]),
        ("HOU", "Houston", "Texans", []), ("IND", "Indianapolis", "Colts", []), ("JAX", "Jacksonville", "Jaguars", ["JAC"]),
        ("KC", "Kansas City", "Chiefs", ["KAN"]), ("LAC", "Los Angeles", "Chargers", []), ("LAR", "Los Angeles", "Rams", ["LA"]),
        ("LV", "Las Vegas", "Raiders", ["LVR"]), ("MIA", "Miami", "Dolphins", []), ("MIN", "Minnesota", "Vikings", []),
        ("NE", "New England", "Patriots", ["NWE"]), ("NO", "New Orleans", "Saints", ["NOR"]), ("NYG", "New York", "Giants", []),
        ("NYJ", "New York", "Jets", []), ("PHI", "Philadelphia", "Eagles", []), ("PIT", "Pittsburgh", "Steelers", []),
        ("SEA", "Seattle", "Seahawks", []), ("SF", "San Francisco", "49ers", ["SFO"]), ("TB", "Tampa Bay", "Buccaneers", ["TAM"]),
        ("TEN", "Tennessee", "Titans", []), ("WAS", "Washington", "Commanders", ["WSH"]),
    ],
    "mlb": [
        ("ARI", "Arizona", "Diamondbacks", ["AZ"]), ("ATL", "Atlanta", "Braves", []), ("BAL", "Baltimore", "Orioles", []),
        ("BOS", "Boston", "Red Sox", []), ("CHC", "Chicago", "Cubs", []), ("CWS", "Chicago", "White Sox", ["CHW"]),
        ("CIN", "Cincinnati", "Reds", []), ("CLE", "Cleveland", "Guardians", []), ("COL", "Colorado", "Rockies", []),
        ("DET", "Detroit", "Tigers", []), ("HOU", "Houston", "Astros", []), ("KC", "Kansas City", "Royals", ["KCR"]),
        ("LAA", "Los Angeles", "Angels", ["ANA"]), ("LAD", "Los Angeles", "Dodgers", []), ("MIA", "Miami", "Marlins", []),
        ("MIL", "Milwaukee", "Brewers", []), ("MIN", "Minnesota", "Twins", []), ("NYM", "New York", "Mets", []),
        ("NYY", "New York", "Yankees", []), ("OAK", "Oakland", "Athletics", ["ATH", "SAC"]), ("PHI", "Philadelphia", "Phillies", []),
        ("PIT", "Pittsburgh", "Pirates", []), ("SD", "San Diego", "Padres", ["SDP"]), ("SF", "San Francisco", "Giants", ["SFG"]),
        ("SEA", "Seattle", "Mariners", []), ("STL", "St. Louis", "Cardinals", []), ("TB", "Tampa Bay", "Rays", ["TBR"]),
        ("TEX", "Texas", "Rangers", []), ("TOR", "Toronto", "Blue Jays", []), ("WSH", "Washington", "Nationals", ["WAS", "WSN"]),
    ],
    "nba": [
        ("ATL", "Atlanta", "Hawks", []), ("BKN", "Brooklyn", "Nets", ["BRK", "BRO"]), ("BOS", "Boston", "Celtics", []),
        ("CHA", "Charlotte", "Hornets", ["CHO"]), ("CHI", "Chicago", "Bulls", []), ("CLE", "Cleveland", "Cavaliers", []),
        ("DAL", "Dallas", "Mavericks", []), ("DEN", "Denver", "Nuggets", []), ("DET", "Detroit", "Pistons", []),
        ("GSW", "Golden State", "Warriors", ["GS"]), ("HOU", "Houston", "Rockets", []), ("IND", "Indiana", "Pacers", []),
        ("LAC", "Los Angeles", "Clippers", []), ("LAL", "Los Angeles", "Lakers", []), ("MEM", "Memphis", "Grizzlies", []),
        ("MIA", "Miami", "Heat", []), ("MIL", "Milwaukee", "Bucks", []), ("MIN", "Minnesota", "Timberwolves", []),
        ("NOP", "New Orleans", "Pelicans", ["NO"]), ("NYK", "New York", "Knicks", ["NY"]), ("OKC", "Oklahoma City", "Thunder", []),
        ("ORL", "Orlando", "Magic", []), ("PHI", "Philadelphia", "76ers", []), ("PHX", "Phoenix", "Suns", ["PHO"]),
        ("POR", "Portland", "Trail Blazers", []), ("SAC", "Sacramento", "Kings", []), ("SAS", "San Antonio", "Spurs", ["SA"]),
        ("TOR", "Toronto", "Raptors", []), ("UTA", "Utah", "Jazz", []), ("WAS", "Washington", "Wizards", ["WSH"]),
    ],
    "nhl": [
        ("ANA", "Anaheim", "Ducks", []), ("BOS", "Boston", "Bruins", []), ("BUF", "Buffalo", "Sabres", []),
        ("CGY", "Calgary", "Flames", []), ("CAR", "Carolina", "Hurricanes", []), ("CHI", "Chicago", "Blackhawks", []),
        ("COL", "Colorado", "Avalanche", []), ("CBJ", "Columbus", "Blue Jackets", []), ("DAL", "Dallas", "Stars", []),
        ("DET", "Detroit", "Red Wings", []), ("EDM", "Edmonton", "Oilers", []), ("FLA", "Florida", "Panthers", []),
        ("LA", "Los Angeles", "Kings", ["LAK"]), ("MIN", "Minnesota", "Wild", []), ("MTL", "Montreal", "Canadiens", ["MON"]),
        ("NSH", "Nashville", "Predators", []), ("NJ", "New Jersey", "Devils", ["NJD"]), ("NYI", "New York", "Islanders", []),
        ("NYR", "New York", "Rangers", []), ("OTT", "Ottawa", "Senators", []), ("PHI", "Philadelphia", "Flyers", []),
        ("PIT", "Pittsburgh", "Penguins", []), ("SJ", "San Jose", "Sharks", ["SJS"]), ("SEA", "Seattle", "Kraken", []),
        ("STL", "St. Louis", "Blues", []), ("TB", "Tampa Bay", "Lightning", ["TBL"]), ("TOR", "Toronto", "Maple Leafs", []),
        ("UTA", "Utah", "Mammoth", ["UTAH"]), ("VAN", "Vancouver", "Canucks", []), ("VGK", "Vegas", "Golden Knights", ["LV"]),
        ("WSH", "Washington", "Capitals", ["WAS"]), ("WPG", "Winnipeg", "Jets", []),
    ],
}


def norm(s: str | None) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", s.lower())).strip()


def by_code(league: str, code: str | None) -> str | None:
    if not code:
        return None
    code = code.upper()
    for canon, _, _, extra in TEAMS.get(league, []):
        if code == canon or code in extra:
            return canon
    return None


def by_name(league: str, text: str | None) -> str | None:
    """Resolve a team from free text: nickname first, then city (with Kalshi's 'New York J'-style letter)."""
    t = f" {norm(text)} "
    teams = TEAMS.get(league, [])
    hits = [x for x in teams if f" {norm(x[2])} " in t]
    if len(hits) == 1:
        return hits[0][0]
    if len(hits) > 1:                                   # 'Giants' exists in NFL only, but 'Kings' etc. are per league; keep unique only
        return None
    hits = [x for x in teams if f" {norm(x[1])} " in t]
    if len(hits) == 1:
        return hits[0][0]
    if len(hits) > 1:
        m = re.search(r"\b(new york|los angeles|chicago)\s+([a-z]{1,2})\b", t)   # Kalshi: 'New York J', 'Chicago WS'
        if m:
            letters = m.group(2)
            narrowed = [x for x in hits if "".join(w[0] for w in norm(x[2]).split()) == letters or norm(x[2]).startswith(letters)]
            if len(narrowed) == 1:
                return narrowed[0][0]
    return None


SOCCER_STRIP = set("fc cf afc sc ac as ss ssc ud cd rcd sd ca rc de club fsv tsg vfb vfl sv bsc us ogc losc rb calcio football futbol "
                   "la le les the cfc fk bk if ik sk 1899 04 07 09 05 1 1860 1904 1907 1909 1910 1913 fbc gnk hnk nk krc kaa kv rsc "
                   "royal sporting sportive deportivo cf sad sa spa ag e v ev tsv 1 fsv fc".split())
SOCCER_ALIASES = {
    "psg": ["paris"], "germain": [], "saint": [], "st": ["state"], "utd": ["united"], "man": ["manchester"],
    "spurs": ["tottenham"], "hotspur": [], "wolves": ["wolverhampton"], "wanderers": [], "internazionale": ["inter"],
    "milano": ["milan"], "munchen": ["munich"], "muenchen": ["munich"], "monchengladbach": ["gladbach"],
    "moenchengladbach": ["gladbach"], "borussia": [], "bayer": ["leverkusen"], "04": [], "olympique": [], "lyonnais": ["lyon"],
    "athletic": ["athletic"], "bilbao": ["athletic"], "atletico": ["atletico"], "balompie": [], "hove": [], "albion": [],
    "forest": ["forest"], "nottingham": ["forest"], "villa": ["villa"], "aston": ["villa"], "palace": ["palace"], "crystal": ["palace"],
    "sociedad": ["sociedad"], "betis": ["betis"], "real": [], "madrid": ["madrid"], "girona": ["girona"], "celta": ["celta"], "vigo": ["celta"],
    "rayo": ["rayo"], "vallecano": ["rayo"], "athletico": ["athletic"], "hamburger": ["hamburg"], "hsv": ["hamburg"],
    "eintracht": [], "frankfurt": ["frankfurt"], "koln": ["cologne"], "koeln": ["cologne"], "cologne": ["cologne"], "mainz": ["mainz"],
    "stuttgart": ["stuttgart"], "werder": ["bremen"], "bremen": ["bremen"], "freiburg": ["freiburg"], "augsburg": ["augsburg"],
    "hoffenheim": ["hoffenheim"], "paderborn": ["paderborn"], "wolfsburg": ["wolfsburg"], "leipzig": ["leipzig"], "union": ["union"],
    "inter": ["inter"], "juventus": ["juventus"], "juve": ["juventus"], "napoli": ["napoli"], "roma": ["roma"], "lazio": ["lazio"],
    "atalanta": ["atalanta"], "fiorentina": ["fiorentina"], "torino": ["torino"], "genoa": ["genoa"], "marseille": ["marseille"],
    "lille": ["lille"], "monaco": ["monaco"], "nice": ["nice"], "rennes": ["rennes"], "lens": ["lens"], "toulouse": ["toulouse"],
    "strasbourg": ["strasbourg"], "nantes": ["nantes"], "brest": ["brest"], "lorient": ["lorient"], "auxerre": ["auxerre"], "metz": ["metz"],
    "le": [], "havre": ["havre"], "angers": ["angers"], "reims": ["reims"], "montpellier": ["montpellier"],
}


def club_tokens(name: str | None) -> set[str]:
    """Tokens that identify a club/college: suffixes stripped, aliases applied, crude 5-char stem."""
    out: set[str] = set()
    for w in norm(name).split():
        if w in SOCCER_ALIASES:
            out.update(SOCCER_ALIASES[w])
            continue
        if w in SOCCER_STRIP or len(w) < 2:
            continue
        out.add(w[:5])
    return out
