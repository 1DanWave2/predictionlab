"""Lab 3, stage 2: find Kalshi <-> Polymarket market pairs that resolve on the same outcome.

    python3 match.py [--verify]      # --verify re-checks text pairs with Claude if ANTHROPIC_API_KEY is set

Three matchers run in order; a market taken by an earlier one is not offered to the later ones:
  1. games  - daily game markets (KXNFLGAME-26SEP28PHICHI-PHI <-> nfl-phi-chi-2026-09-28): same
              league, game date within a day, both teams agree (US leagues through the code/nickname
              table in teams.py, soccer/college through club-name tokens); each Kalshi team / tie
              market is then tied to one Polymarket outcome (a moneyline outcome, or the per-team /
              draw market).
  2. groups - templated series: season champions (KXSB <-> "Pro Football: 2027 Champion"), Fed
              decisions, nominees, MVPs, Oscars ... The entity (team, person, bucket) is extracted on
              both sides and matched exactly (US teams, Fed buckets) or by name tokens, mutual-best.
  3. text   - everything else: token Jaccard on titles with dates removed, crude stemming and a few
              synonyms; numbers and discriminator words must agree; deadlines parsed from the text
              must agree; mutual-best; score >= 0.6.
pairs_manual.csv (kalshi_ticker,poly_id,verdict[,note]) overrides anything (accept / reject).
A pair whose two mid prices differ by more than 0.35 at match time is kept but flagged `suspect`
and not marked matched: a wrong pair looks exactly like a huge arbitrage.
Output: <data>/pairs.parquet (+ pairs.csv), <data> = $XVENUE_DATA_DIR or data/labs/xvenue.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from teams import TEAMS, by_code, by_name, club_tokens, norm  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent.parent   # repo root when run from labs/<lab>/, harmless elsewhere
OUT = Path(os.environ.get("XVENUE_DATA_DIR") or ROOT / "data" / "labs" / "xvenue")
TEXT_MIN, GROUP_MIN, GAME_MIN = 0.6, 0.5, 0.45
ET = ZoneInfo("America/New_York")
SUSPECT_GAP = 0.35

MONTHS = {m: i + 1 for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split())}
MON_RE = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
STOP = set("will the a an of to in on by at for and or vs versus win wins won be is are does do this that with from than over under before after end match "
           "game total points yes no market price close closes above below more less least most next first second who which what when how many much "
           "between times happen happens named be been get gets become becomes any all either per its it their his her he she they them announced "
           "announce officially official as up out into".split())
FIRST_WORDS = set("will who what which when where how is are does do can could should would has have new the a an".split())
DISCRIM = set("vice vp emergency ticket runoff primary exact halftime female male women womens men mens co tie draw spread total over under".split())
SYN = {"leaves": "out", "leave": "out", "resign": "out", "resigns": "out", "depart": "out", "departs": "out", "removed": "out", "ousted": "out",
       "acquire": "buy", "acquires": "buy", "purchase": "buy", "purchases": "buy", "race": "election", "governorship": "governor",
       "gubernatorial": "governor", "usa": "us", "america": "us", "american": "us", "nominee": "nomination", "nominated": "nomination",
       "democratics": "democrats", "democratic": "democrats", "republican": "republicans", "gop": "republicans"}


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def ts(s) -> int | None:
    if s is None or (isinstance(s, float) and s != s) or s == "":
        return None
    s = str(s).replace("Z", "+00:00").replace(" ", "T", 1)
    if s.endswith("+00"):
        s = s[:-3] + "+00:00"
    try:
        return int(datetime.fromisoformat(s).timestamp())
    except ValueError:
        return None


def clean(s: str | None) -> str:
    t = norm(s).replace(" u s ", " us ").replace("united states", "us")
    t = re.sub(r"\b\d{1,2}(?:\s*:\s*\d{2})?\s*(?:am|pm)\b(?:\s*(?:et|est|edt|utc|pt|pst|pdt|ct|cst|cdt|gmt))?", " ", t)   # 11:59 pm et
    t = re.sub(r"\b(19|20)\d{2}\s*[-/]\s*\d{2}\b", " ", t)                     # 2026-27 seasons
    t = re.sub(r"\b(19|20)\d{2}\s+\d{2}\s+\d{2}\b", " ", t)                     # 2026 09 19 (ISO dates after norm)
    t = re.sub(r"\b" + MON_RE + r"\s*(\d{1,2}(st|nd|rd|th)?)?\s*((19|20)\d{2})?\b", " ", t)
    t = re.sub(r"\b(19|20)\d{2}\b", " ", t)
    t = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", t)
    t = re.sub(r"\b(\d+(?:\.\d+)?)k\b", lambda m: str(int(float(m.group(1)) * 1000)), t)
    t = re.sub(r"(\d)([a-z])", r"\1 \2", t)
    return t


def words(s: str | None) -> list[str]:
    out = []
    for w in clean(s).split():
        w = SYN.get(w, w)
        if len(w) < 2 and not w.isdigit():
            continue
        if w in STOP:
            continue
        out.append(w)
    return out


def stem(w: str) -> str:
    return w if w.isdigit() else w[:5]


def toks(*parts) -> tuple[set[str], set[str]]:
    ws = [w for p in parts for w in words(p)]
    return {stem(w) for w in ws if not w.isdigit()}, {w for w in ws if w.isdigit()}


def text_score(a: tuple[set, set], b: tuple[set, set]) -> float:
    (aw, an), (bw, bn) = a, b
    if an != bn or not aw or not bw or ((aw ^ bw) & DISCRIM):
        return 0.0
    return len(aw & bw) / len(aw | bw)


def deadline(text: str | None, now: datetime) -> int | None:
    """Deadline stated in a title: 'before Jan 1, 2027', 'in 2026', 'by December 31', 'before October 2026'."""
    t = norm(text)
    if not t:
        return None

    def year_or_guess(y: str | None, month: int, day: int) -> datetime:
        if y:
            return datetime(int(y), month, day, tzinfo=timezone.utc)
        d = datetime(now.year, month, day, tzinfo=timezone.utc)
        return d if d >= now - timedelta(days=45) else datetime(now.year + 1, month, day, tzinfo=timezone.utc)

    m = re.search(r"\b(?P<rel>before|by|on|until|through|prior to|in)\s+(?:the\s+)?(?:end\s+of\s+)?(?P<mon>" + MON_RE + r")\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?\s*,?\s*(?P<year>(?:19|20)\d{2})?\b", t)
    if m:
        try:
            return int(year_or_guess(m.group("year"), MONTHS[m.group("mon")[:3]], int(m.group("day"))).timestamp())
        except ValueError:
            return None
    m = re.search(r"\b(?P<rel>before|by|in|through|until|during|end of)\s+(?:the\s+)?(?:end\s+of\s+)?(?P<mon>" + MON_RE + r")\s+(?P<year>(?:19|20)\d{2})\b", t)
    if m:
        y, mo = int(m.group("year")), MONTHS[m.group("mon")[:3]]
        if m.group("rel") == "before":
            return int(datetime(y, mo, 1, tzinfo=timezone.utc).timestamp())
        nxt = datetime(y + (mo == 12), mo % 12 + 1, 1, tzinfo=timezone.utc)
        return int(nxt.timestamp())
    m = re.search(r"\b(?P<rel>before|by|in|through|until|during|end of)\s+(?:the\s+)?(?P<eoy>end\s+of\s+)?(?P<year>(?:19|20)\d{2})\b", t)
    if m:
        y = int(m.group("year"))
        if m.group("rel") in ("before", "by") and not m.group("eoy"):
            return int(datetime(y, 1, 1, tzinfo=timezone.utc).timestamp())
        return int(datetime(y + 1, 1, 1, tzinfo=timezone.utc).timestamp())
    m = re.search(r"\b(?P<year>(?:19|20)\d{2}) (?P<mon>\d{2}) (?P<day>\d{2})\b", t)
    if m:
        try:
            return int(datetime(int(m.group("year")), int(m.group("mon")), int(m.group("day")), tzinfo=timezone.utc).timestamp())
        except ValueError:
            return None
    return None


def proper(text: str | None) -> set[str]:
    """Stemmed tokens of the capitalised words of a title (not the first word), 4+ chars: names, places, parties."""
    ws = re.findall(r"[^\W\d_][\w'’.-]*", str(text or ""))
    caps = [w for i, w in enumerate(ws) if w[0].isupper() and (i > 0 or w.lower() not in FIRST_WORDS)]
    return {t for t in toks(" ".join(caps))[0] if len(t) >= 4}


def mutual_best(scores: dict[tuple, float], min_score: float) -> list[tuple]:
    """scores: {(k_id, p_id): score} -> pairs where each is the other's unique best and score >= min."""
    best_k: dict = {}
    best_p: dict = {}
    for (k, p), s in scores.items():
        if s < min_score:
            continue
        for best, key, other in ((best_k, k, p), (best_p, p, k)):
            cur = best.get(key)
            if cur is None or s > cur[0]:
                best[key] = (s, other, 1)
            elif s == cur[0]:
                best[key] = (s, cur[1], cur[2] + 1)
    out = []
    for k, (s, p, n) in best_k.items():
        if n == 1 and best_p.get(p, (None, None, 0))[1] == k and best_p[p][2] == 1:
            out.append((k, p, s))
    return out


# ----------------------------------------------------------------------------------------------- rows
def poly_prices(p: pd.Series, idx: int) -> tuple[float | None, float | None]:
    bb, ba = p["best_bid"], p["best_ask"]
    bb = None if bb != bb else bb
    ba = None if ba != ba else ba
    if idx == 0:
        return bb, ba
    return (None if ba is None else round(1 - ba, 4)), (None if bb is None else round(1 - bb, 4))


def mid(b, a, last=None):
    b = None if b is None or b != b else b
    a = None if a is None or a != a else a
    if b is not None and a is not None:
        return (b + a) / 2
    return last if last == last else None


def make_row(k: pd.Series, p: pd.Series, idx: int, group: str, method: str, score: float) -> dict:
    outs = json.loads(p["outcomes"] or "[]")
    tks = json.loads(p["tokens"] or "[]")
    pb, pa = poly_prices(p, idx)
    km, pm = mid(k["yes_bid"], k["yes_ask"], k["last"]), mid(pb, pa)
    gap = None if km is None or pm is None else round(abs(km - pm), 3)
    return {"pair_id": f"{k['ticker']}|{p['market_id']}:{idx}", "group": group, "method": method, "score": round(float(score), 3),
            "kalshi_ticker": k["ticker"], "kalshi_title": k["title"], "kalshi_sub": k["yes_sub_title"], "kalshi_event": k["event_title"],
            "kalshi_close": k["close_time"], "kalshi_yes_bid": k["yes_bid"], "kalshi_yes_ask": k["yes_ask"], "kalshi_oi": k["open_interest"],
            "kalshi_vol24": k["volume_24h"], "poly_id": p["market_id"], "poly_condition_id": p["condition_id"], "poly_question": p["question"],
            "poly_event": p["event_title"], "poly_slug": p["slug"], "poly_outcome": (outs[idx] if idx < len(outs) else None), "poly_outcome_idx": idx,
            "poly_token": (tks[idx] if idx < len(tks) else None), "poly_end": p["end_date"], "poly_game_start": p["game_start"],
            "poly_bid": pb, "poly_ask": pa, "poly_vol24": p["volume_24h"], "price_gap": gap, "suspect": bool(gap is not None and gap > SUSPECT_GAP)}


# ---------------------------------------------------------------------------------------------- games
GAME_SERIES = {"KXNFLGAME": ("nfl", "nfl"), "KXMLBGAME": ("mlb", "mlb"), "KXNBAGAME": ("nba", "nba"), "KXNHLGAME": ("nhl", "nhl"),
               "KXWNBAGAME": ("wnba", "wnba"), "KXNCAAFGAME": ("cfb", "cfb"), "KXEPLGAME": ("epl", "epl"), "KXLALIGAGAME": ("lal", "lal"),
               "KXBUNDESLIGAGAME": ("bun", "bun"), "KXSERIEAGAME": ("sea", "sea"), "KXLIGUE1GAME": ("fl1", "fl1"), "KXMLSGAME": ("mls", "mls"),
               "KXUCLGAME": ("ucl", "ucl")}
PREFIX_LEAGUE = {pf: lg for lg, pf in GAME_SERIES.values()}
SLUG_RE = re.compile(r"^([a-z0-9]+)-([a-z0-9]+)-([a-z0-9]+)-(\d{4}-\d{2}-\d{2})(?:-(.*))?$")
KDATE_RE = re.compile(r"-(\d{2})([A-Z]{3})(\d{2})(\d{4})?(?=[A-Z])")


def kalshi_game_time(event_ticker: str) -> tuple[date | None, datetime | None]:
    """Game date from the event ticker (KXNFLGAME-26SEP28PHICHI) and, when the ticker carries HHMM
    (KXMLBGAME-26SEP191605MILBAL, Eastern time), the UTC start datetime."""
    m = KDATE_RE.search(event_ticker or "")
    if not m:
        return None, None
    try:
        d = date(2000 + int(m.group(1)), MONTHS[m.group(2).lower()], int(m.group(3)))
        dt = None
        if m.group(4):
            dt = datetime(d.year, d.month, d.day, int(m.group(4)[:2]), int(m.group(4)[2:]), tzinfo=ET).astimezone(timezone.utc)
        return d, dt
    except (KeyError, ValueError):
        return None, None


def game_time_score(kd: date, kdt: datetime | None, pd_: date, pgs: int | None) -> float:
    """1.0 when the two games start together (or on the same date when no time is known), 0.9 a day apart."""
    if kdt is not None and pgs:
        h = abs(kdt.timestamp() - pgs) / 3600
        return 1.0 if h <= 3 else (0.9 if h <= 26 else 0.0)
    dd = abs((pd_ - kd).days)
    return 1.0 if dd == 0 else (0.9 if dd == 1 else 0.0)


def poly_team_names(g: pd.DataFrame) -> list[str]:
    ml = g[g["slug_suffix"].isna()]
    if len(ml):
        outs = json.loads(ml.iloc[0]["outcomes"] or "[]")
        if len(outs) == 2 and outs[0].lower() not in ("yes", "no"):
            return outs
    ev = str(g.iloc[0]["event_title"] or "")
    parts = re.split(r"\s+vs\.?\s+", ev, flags=re.I)
    if len(parts) == 2:
        return [re.sub(r"\s*-\s*(More Markets|Exact Score).*$", "", x).strip() for x in parts]
    names = []
    for _, r in g.iterrows():
        m = re.match(r"^Will (.+?) win on \d{4}-\d{2}-\d{2}\?$", str(r["question"] or ""))
        if m:
            names.append(m.group(1))
    return names


def match_games(k: pd.DataFrame, p: pd.DataFrame) -> list[dict]:
    rows: list[dict] = []
    kg = k[k["series"].isin(GAME_SERIES)]
    pp = p[p["slug"].notna()].copy()
    parsed = pp["slug"].str.extract(SLUG_RE)
    pp["prefix"], pp["c1"], pp["c2"], pp["gdate"], pp["slug_suffix"] = parsed[0], parsed[1], parsed[2], parsed[3], parsed[4]
    prefixes = {v[1] for v in GAME_SERIES.values()}
    pp = pp[pp["prefix"].isin(prefixes)]
    pp = pp[pp["slug_suffix"].isna() | (pp["slug_suffix"] == "draw") | (pp["slug_suffix"] == pp["c1"]) | (pp["slug_suffix"] == pp["c2"])]
    pgames = {key: g for key, g in pp.groupby(["prefix", "c1", "c2", "gdate"])}
    pinfo = {}
    for key, g in pgames.items():
        league = PREFIX_LEAGUE[key[0]]
        names = poly_team_names(g)
        canon = set()
        if league in TEAMS:
            canon = {c for c in (by_name(league, n) for n in names) if c} or {c for c in (by_code(league, key[1]), by_code(league, key[2])) if c}
        tk = set().union(*(club_tokens(n) for n in names)) if names else set()
        pinfo[key] = {"league": league, "names": names, "canon": canon, "tokens": tk, "date": datetime.strptime(key[3], "%Y-%m-%d").date(),
                      "gs": ts(g.iloc[0]["game_start"])}
    scores: dict[tuple, float] = {}
    kinfo = {}
    for et, g in kg.groupby("event_ticker"):
        series = g.iloc[0]["series"]
        league, prefix = GAME_SERIES[series]
        kd, kdt = kalshi_game_time(et)
        if kd is None:
            continue
        teams = g[g["ticker"].str.split("-").str[-1] != "TIE"]
        codes = [t.split("-")[-1] for t in teams["ticker"]]
        canon = {c for c in (by_code(league, c) or by_name(league, n) for c, n in zip(codes, teams["yes_sub_title"])) if c} if league in TEAMS else set()
        tk = set().union(*(club_tokens(n) for n in teams["yes_sub_title"])) if len(teams) else set()
        kinfo[et] = {"league": league, "canon": canon, "tokens": tk, "date": kd, "frame": g}
        for key, info in pinfo.items():
            if info["league"] != league or abs((info["date"] - kd).days) > 1:
                continue
            if league in TEAMS:
                s = 1.0 if canon and canon == info["canon"] and len(canon) == 2 else 0.0
            else:
                s = len(tk & info["tokens"]) / len(tk | info["tokens"]) if tk and info["tokens"] else 0.0
            s *= game_time_score(kd, kdt, info["date"], info["gs"])
            if s > 0:
                scores[(et, key)] = s
    for et, key, s in mutual_best(scores, GAME_MIN):
        ki, pi, g = kinfo[et], pinfo[key], pgames[key]
        league = ki["league"]
        ml = g[g["slug_suffix"].isna()]
        for _, km in ki["frame"].iterrows():
            code = km["ticker"].split("-")[-1]
            if code == "TIE":
                dr = g[g["slug_suffix"] == "draw"]
                if len(dr):
                    rows.append(make_row(km, dr.iloc[0], 0, "game:" + league, "draw", s))
                continue
            kt = club_tokens(km["yes_sub_title"])
            kc = by_code(league, code) or by_name(league, km["yes_sub_title"]) if league in TEAMS else None
            if len(ml):                                                        # moneyline with two team outcomes
                pm = ml.iloc[0]
                outs = json.loads(pm["outcomes"] or "[]")
                idx = None
                if kc:
                    hits = [i for i, o in enumerate(outs) if by_name(league, o) == kc]
                    idx = hits[0] if len(hits) == 1 else None
                if idx is None and outs:
                    sc = [len(kt & club_tokens(o)) / max(1, len(kt | club_tokens(o))) for o in outs]
                    if max(sc) > 0 and sc.count(max(sc)) == 1:
                        idx = sc.index(max(sc))
                if idx is not None:
                    rows.append(make_row(km, pm, idx, "game:" + league, "moneyline", s))
                    continue
            per = g[g["slug_suffix"].notna() & (g["slug_suffix"] != "draw")]
            if len(per):
                sc = []
                for _, pr in per.iterrows():
                    m = re.match(r"^Will (.+?) win on \d{4}-\d{2}-\d{2}\?$", str(pr["question"] or ""))
                    nm = m.group(1) if m else str(pr["question"])
                    if kc and by_name(league, nm) == kc:
                        sc.append(1.0)
                    else:
                        sc.append(len(kt & club_tokens(nm)) / max(1, len(kt | club_tokens(nm))))
                if max(sc) > 0 and sc.count(max(sc)) == 1:
                    rows.append(make_row(km, per.iloc[sc.index(max(sc))], 0, "game:" + league, "per-team", s))
    return rows


# --------------------------------------------------------------------------------------------- groups
GROUPS = [
    ("KXSB", r"^Pro Football: \d{4} Champion$|Super Bowl.*(Champion|Winner)", "nfl"),
    ("KXNFLAFCCHAMP", r"^Pro Football: \d{4} AFC Champion", "nfl"), ("KXNFLNFCCHAMP", r"^Pro Football: \d{4} NFC Champion", "nfl"),
    ("KXMLB", r"World Series (Champion|Winner)", "mlb"), ("KXMLBAL", r"^(MLB )?(AL|American League) (Champion|Pennant)", "mlb"),
    ("KXMLBNL", r"^(MLB )?(NL|National League) (Champion|Pennant)", "mlb"),
    ("KXNBA", r"^NBA: \d{4} Champion|NBA (Finals )?Champion", "nba"), ("KXNHL", r"^NHL: \d{4} Champion|Stanley Cup", "nhl"),
    ("KXWNBA", r"^WNBA: \d{4} Champion", "name"),
    ("KXPREMIERLEAGUE", r"^EPL: \d{4} Champion|Premier League.*(Champion|Winner)", "club"),
    ("KXUCL", r"^UEFA Champions League: \d{4} Champion", "club"), ("KXUEL", r"^UEFA Europa League: \d{4} Champion", "club"),
    ("KXLALIGA", r"^LALIGA: \d{4} Champion|La Liga.*(Champion|Winner)", "club"), ("KXBUNDESLIGA", r"^Bundesliga: \d{4} Champion", "club"),
    ("KXSERIEA", r"^Serie A: \d{4} Champion", "club"), ("KXLIGUE1", r"^Ligue 1: \d{4} Champion", "club"), ("KXMLSCUP", r"MLS Cup", "name"),
    ("KXNCAAF", r"College Football.*(Champion|Playoff)", "club"), ("KXMARMAD", r"March Madness|College Basketball.*Champion|NCAA.*Basketball.*Champion", "club"),
    ("KXF1", r"F1 Drivers'? Champion", "name"), ("KXF1CONSTRUCTORS", r"F1 Constructors", "club"),
    ("KXBALLONDOR", r"Ballon d'Or Winner", "name"), ("KXHEISMAN", r"Heisman", "name"),
    ("KXNFLMVP", r"Pro Football: \d{4} MVP|NFL MVP", "name"), ("KXNBAMVP", r"NBA MVP|Pro Basketball.*MVP", "name"),
    ("KXMLBALMVP", r"\bAL MVP", "name"), ("KXMLBNLMVP", r"\bNL MVP", "name"),
    ("KXOSCARPIC", r"Best Picture", "name"), ("KXOSCARACTO", r"Best Actor\b", "name"),
    ("KXPRESPERSON", r"Presidential Election Winner|^2028 Presidential Election", "name"),
    ("KXPRESNOMD", r"Democratic (Presidential )?Nominee", "name"), ("KXPRESNOMR", r"Republican (Presidential )?Nominee", "name"),
    ("KXVPRESNOMD", r"Democratic (VP|Vice Presidential) Nominee", "name"), ("KXVPRESNOMR", r"Republican (VP|Vice Presidential) Nominee", "name"),
    ("KXBRPRES", r"Brazil.*Presidential Election", "name"),
    ("KXFEDDECISION", r"^Fed Decision in \w+\?", "fed"),
]
FED_K = {"cut 25bps": ("cut", 25), "cut >25bps": ("cut", 50), "fed maintains rate": ("none", 0), "hike 25bps": ("hike", 25), "hike >25bps": ("hike", 50)}


def template(texts: list[str]) -> set[str]:
    if len(texts) < 3:
        return set()
    cnt: dict[str, int] = {}
    for t in texts:
        for w in set(words(t)):
            cnt[w] = cnt.get(w, 0) + 1
    return {w for w, c in cnt.items() if c >= 0.5 * len(texts)}


def entity(text: str, tmpl: set[str], kind: str) -> set[str]:
    res = [w for w in words(text) if w not in tmpl]
    return club_tokens(" ".join(res)) if kind == "club" else {stem(w) for w in res}


def year_of(s: str | None) -> int | None:
    m = re.search(r"\b(20\d{2})\b", str(s or ""))
    return int(m.group(1)) if m else None


def kalshi_year(ticker: str) -> int | None:
    parts = ticker.split("-")
    if len(parts) >= 3 and re.fullmatch(r"\d{2}", parts[1]):
        return 2000 + int(parts[1])
    return None


def fed_bucket_p(q: str) -> tuple | None:
    t = norm(q)
    if "no change" in t:
        return ("none", 0)
    m = re.search(r"(increase|decrease).*?(\d+)\s*\+?\s*bps", t)
    if not m:
        return None
    size = 50 if int(m.group(2)) >= 50 else 25
    return ("hike" if m.group(1) == "increase" else "cut", size)


def match_groups(k: pd.DataFrame, p: pd.DataFrame, used_k: set, used_p: set) -> list[dict]:
    rows: list[dict] = []
    for series, ev_re, kind in GROUPS:
        kk = k[(k["series"] == series) & ~k["ticker"].isin(used_k)]
        pp = p[p["event_title"].fillna("").str.contains(re.sub(r"\((?!\?)", "(?:", ev_re), regex=True, case=False) & ~p["market_id"].isin(used_p)]
        if not len(kk) or not len(pp):
            continue
        if kind in TEAMS:
            pidx = pp.set_index("market_id")
            pc = {r["market_id"]: by_name(kind, r["question"]) for _, r in pp.iterrows()}
            for _, kr in kk.iterrows():
                kc = by_code(kind, kr["ticker"].split("-")[-1]) or by_name(kind, kr["yes_sub_title"])
                if not kc:
                    continue
                ky = kalshi_year(kr["ticker"])
                hits = [h for h, c in pc.items() if c == kc and (ky is None or year_of(pidx.loc[h, "event_title"]) in (None, ky))]
                if len(hits) == 1:
                    pr = pidx.loc[hits[0]].copy(); pr["market_id"] = hits[0]
                    rows.append(make_row(kr, pr, 0, "group:" + series, kind, 1.0))
        elif kind == "fed":
            kb = {}
            for _, kr in kk.iterrows():
                b = FED_K.get(norm(kr["yes_sub_title"]))
                m = re.search(r"in (\w{3})\w* (\d{4})", str(kr["event_title"] or ""), re.I)
                if b and m and m.group(1).lower() in MONTHS:
                    kb[(b, MONTHS[m.group(1).lower()], int(m.group(2)))] = kr
            for _, pr in pp.iterrows():
                b = fed_bucket_p(pr["question"])
                m = re.search(r"after the (\w+) (\d{4}) meeting", str(pr["question"] or ""), re.I)
                if b and m and m.group(1)[:3].lower() in MONTHS:
                    kr = kb.get((b, MONTHS[m.group(1)[:3].lower()], int(m.group(2))))
                    if kr is not None:
                        rows.append(make_row(kr, pr, 0, "group:" + series, "fed", 1.0))
        else:
            ptm = template(pp["question"].fillna("").tolist()) | set(words(pp.iloc[0]["event_title"]))
            ktm = template(kk["title"].fillna("").tolist())
            pe = {r["market_id"]: entity(r["question"], ptm, kind) for _, r in pp.iterrows()}
            ke = {}
            for _, kr in kk.iterrows():
                sub = str(kr["yes_sub_title"] or "")
                ke[kr["ticker"]] = entity(sub, set(), kind) if sub and norm(sub) not in ("yes", "no") else entity(kr["title"], ktm, kind)
            scores = {}
            for kt, ks in ke.items():
                for pid, ps in pe.items():
                    if ks and ps:
                        s = len(ks & ps) / len(ks | ps)
                        if s > 0:
                            scores[(kt, pid)] = s
            kidx, pidx = kk.set_index("ticker"), pp.set_index("market_id")
            for kt, pid, s in mutual_best(scores, GROUP_MIN):
                ky, py = kalshi_year(kt), year_of(pidx.loc[pid, "event_title"])
                if ky and py and ky != py:
                    continue
                kr = kidx.loc[kt].copy(); kr["ticker"] = kt
                pr = pidx.loc[pid].copy(); pr["market_id"] = pid
                rows.append(make_row(kr, pr, 0, "group:" + series, kind, s))
    return rows


# ----------------------------------------------------------------------------------------------- text
def match_text(k: pd.DataFrame, p: pd.DataFrame, used_k: set, used_p: set, now: datetime) -> list[dict]:
    kk = k[~k["ticker"].isin(used_k) & (k["open_interest"].fillna(0) >= 500)].copy()
    pp = p[~p["market_id"].isin(used_p) & (p["volume_24h"].fillna(0) >= 5000) & p["sports_market_type"].isna()].copy()
    pp["tk"] = [toks(q, e) for q, e in zip(pp["question"], pp["event_title"])]
    pp["pn"] = [proper(q) for q in pp["question"]]
    pp["dl"] = [deadline(q, now) for q in pp["question"]]
    pp["t_end"] = [ts(g) or ts(e) for g, e in zip(pp["game_start"], pp["end_date"])]
    index: dict[str, list[int]] = {}
    for i, (tw, _) in enumerate(pp["tk"]):
        for w in tw:
            index.setdefault(w, []).append(i)
    scores: dict[tuple, float] = {}
    for _, kr in kk.iterrows():
        sub = str(kr["yes_sub_title"] or "")
        kt = toks(kr["title"], kr["event_title"], sub if norm(sub) not in ("yes", "no") else None)
        if len(kt[0]) < 2:
            continue
        kdl, kc = deadline(kr["title"], now), ts(kr["close_time"])
        kpn = proper(kr["title"])
        counts: dict[int, int] = {}
        for w in kt[0]:
            for i in index.get(w, []):
                counts[i] = counts.get(i, 0) + 1
        for i in counts:
            s = text_score(kt, pp["tk"].iat[i])
            if s < TEXT_MIN:
                continue
            if not kpn <= pp["tk"].iat[i][0] or not pp["pn"].iat[i] <= kt[0]:   # every name on one side must appear on the other
                continue
            pdl, pe = pp["dl"].iat[i], pp["t_end"].iat[i]
            if kdl and pdl:
                if abs(kdl - pdl) > 3 * 86400:
                    continue
            elif kdl or pdl:            # one side states a deadline: the other's close must be after it, but not years after
                stated, other_close = (kdl, pe) if kdl else (pdl, kc)
                if other_close and (other_close < stated - 3 * 86400 or other_close > stated + 400 * 86400):
                    continue
            elif kc and pe and abs(kc - pe) > 30 * 86400:
                continue
            scores[(kr["ticker"], pp["market_id"].iat[i])] = s
    kidx, pidx = kk.set_index("ticker"), pp.set_index("market_id")
    rows = []
    for kt, pid, s in mutual_best(scores, TEXT_MIN):
        kr = kidx.loc[kt].copy(); kr["ticker"] = kt
        pr = pidx.loc[pid].copy(); pr["market_id"] = pid
        rows.append(make_row(kr, pr, 0, "text", "jaccard", s))
    return rows


def verify_llm(df: pd.DataFrame) -> pd.DataFrame:
    """Ask Claude whether each text pair resolves on the same question. Batches of 15."""
    import anthropic
    client = anthropic.Anthropic()
    verdicts = {}
    items = df.reset_index(drop=True)
    for a in range(0, len(items), 15):
        chunk = items.iloc[a:a + 15]
        lines = [f"### {i}\nKALSHI: {r['kalshi_title']} | event: {r['kalshi_event']} | closes {r['kalshi_close']}\n"
                 f"POLYMARKET: {r['poly_question']} | event: {r['poly_event']} | ends {r['poly_end']}" for i, r in chunk.iterrows()]
        prompt = ("For each numbered pair decide whether the two markets resolve YES on the same real-world outcome with the same deadline "
                  "(small wording differences are fine; different thresholds, dates, or subjects are NOT the same). "
                  "Answer one line per pair: `<number> same|different|unsure - <short reason>`.\n\n" + "\n\n".join(lines))
        try:
            resp = client.messages.create(model="claude-opus-5", max_tokens=2000, output_config={"effort": "medium"},
                                          messages=[{"role": "user", "content": prompt}])
            text = next((b.text for b in resp.content if b.type == "text"), "")
        except Exception as e:  # noqa: BLE001
            log(f"llm error: {e}")
            text = ""
        for line in text.splitlines():
            m = re.match(r"\s*(\d+)\s+(same|different|unsure)\b\s*-?\s*(.*)", line, re.I)
            if m:
                verdicts[int(m.group(1))] = (m.group(2).lower(), m.group(3).strip()[:160])
    items["llm"] = [verdicts.get(i, ("", ""))[0] for i in range(len(items))]
    items["llm_reason"] = [verdicts.get(i, ("", ""))[1] for i in range(len(items))]
    return items


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify", action="store_true")
    a = ap.parse_args()
    now = datetime.now(timezone.utc)
    k = pd.read_parquet(OUT / "kalshi.parquet")
    p = pd.read_parquet(OUT / "polymarket.parquet")
    if "series" not in k:
        k["series"] = k["ticker"].str.split("-").str[0]
    for col in ("outcomes", "tokens", "sports_market_type", "slug", "event_sub_title"):
        if col not in p and col != "event_sub_title":
            p[col] = None
    log(f"kalshi {len(k)} x polymarket {len(p)}")
    rows = match_games(k, p)
    used_k, used_p = {r["kalshi_ticker"] for r in rows}, {r["poly_id"] for r in rows}
    log(f"games: {len(rows)} pairs")
    g = match_groups(k, p, used_k, used_p)
    used_k |= {r["kalshi_ticker"] for r in g}; used_p |= {r["poly_id"] for r in g}
    log(f"groups: {len(g)} pairs")
    t = match_text(k, p, used_k, used_p, now)
    log(f"text: {len(t)} pairs")
    df = pd.DataFrame(rows + g + t)
    if not len(df):
        log("no pairs"); return
    df["llm"], df["llm_reason"] = "", ""
    if a.verify and os.getenv("ANTHROPIC_API_KEY") and (df["group"] == "text").any():
        tv = verify_llm(df[df["group"] == "text"])
        df.loc[df["group"] == "text", ["llm", "llm_reason"]] = tv[["llm", "llm_reason"]].values
        log(f"llm verdicts: {tv['llm'].value_counts().to_dict()}")
    df["matched"] = ~df["suspect"] & (df["llm"] != "different")
    df["manual"] = ""
    man = Path(__file__).resolve().parent / "pairs_manual.csv"
    if man.exists():
        mdf = pd.read_csv(man, dtype=str).fillna("")
        for _, r in mdf.iterrows():
            sel = (df["kalshi_ticker"] == r["kalshi_ticker"]) & (df["poly_id"] == r["poly_id"])
            df.loc[sel, "matched"] = r["verdict"].strip().lower() == "accept"
            df.loc[sel, "manual"] = r["verdict"].strip().lower()
    df = df.drop_duplicates("pair_id")
    df["matched_at"] = int(now.timestamp())
    df.to_parquet(OUT / "pairs.parquet", index=False)
    df.sort_values(["group", "score"], ascending=[True, False]).to_csv(OUT / "pairs.csv", index=False)
    summ = df.groupby("group").agg(pairs=("pair_id", "count"), matched=("matched", "sum"), suspect=("suspect", "sum")).sort_values("pairs", ascending=False)
    log(f"matched {int(df['matched'].sum())} of {len(df)} pairs ({int(df['suspect'].sum())} suspect) -> {OUT / 'pairs.parquet'}\n{summ.to_string()}")


if __name__ == "__main__":
    main()
