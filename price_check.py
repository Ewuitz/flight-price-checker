#!/usr/bin/env python3
"""
Daily flight price checker: FRA / DUS -> HKG roundtrip.

Data source: live Google Flights, fetched through Bright Data's SERP API
with JS rendering (zone "serp_api"), parsed from the rendered markdown.

Departure window : 2026-10-03 .. 2026-10-07
Return window    : 2026-12-02 .. 2026-12-04
Constraints      : max 1 stop per direction (enforced in the query URL),
                   outbound flight duration <= 16 h
Alert threshold  : < 650 EUR

Credentials: env var BRIGHTDATA_TOKEN (GitHub Actions secret).

Outputs:
  - appends rows to price_log.csv
  - maintains state.json (consecutive failure streak)
  - prints summary; final line starts with RESULT: ALERT / OK / FAILURE
  - on ALERT, writes alert_body.md (issue/email body with booking links)
"""

import csv
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import fast_flights as ff

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(BASE_DIR, "price_log.csv")
STATE_PATH = os.path.join(BASE_DIR, "state.json")
ALERT_PATH = os.path.join(BASE_DIR, "alert_body.md")
TITLE_PATH = os.path.join(BASE_DIR, "alert_title.txt")

THRESHOLD = 650.0
ORIGINS = ["FRA", "DUS"]
DEST = "HKG"
DEPARTURES = [date(2026, 10, 3) + timedelta(days=i) for i in range(5)]
RETURNS = [date(2026, 12, 2) + timedelta(days=i) for i in range(3)]
MAX_STOPS = 1
MAX_DURATION_MIN = 16 * 60
DISPREFERRED_AIRLINES = ["china"]      # not excluded, only ranked last
NOT_AIRLINE = ("round trip", "select flight", "cheapest", "best", "top departing",
               "prices include", "ranked based", "travel update", "search results",
               "sorted by", "separate tickets", "self transfer", "climate", "avg",
               "price", "date", "flight", "results returned")
FAVOURITE_AIRLINES = ["emirates"]      # highlighted in the alert
WORKERS = 5
ZONE = "serp_api"
PRICE_MIN, PRICE_MAX = 150, 9000   # sanity bounds for a FRA/DUS-HKG roundtrip


def build_url(origin, dep, ret):
    """Google Flights URL with the search encoded (max 1 stop, EUR)."""
    q = ff.create_query(
        flights=[
            ff.FlightQuery(date=dep.isoformat(), from_airport=origin,
                           to_airport=DEST, max_stops=MAX_STOPS),
            ff.FlightQuery(date=ret.isoformat(), from_airport=DEST,
                           to_airport=origin, max_stops=MAX_STOPS),
        ],
        trip="round-trip", seat="economy", currency="EUR",
    )
    url = getattr(q, "url", None)
    if callable(url):
        url = url()
    if not url:
        raise RuntimeError("could not build query URL")
    for extra in ("hl=en", "curr=EUR", "gl=de"):
        if extra.split("=")[0] + "=" not in url:
            url += ("&" if "?" in url else "?") + extra
    return url


def bd_fetch(url, token, timeout=200):
    payload = {"zone": ZONE, "url": url, "format": "raw",
               "render": True, "data_format": "markdown"}
    req = urllib.request.Request(
        "https://api.brightdata.com/request",
        data=json.dumps(payload).encode(),
        headers={"Authorization": "Bearer " + token,
                 "Content-Type": "application/json"},
        method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode(errors="replace")


def parse_prices(md):
    """Parse itinerary blocks from the rendered page.

    Each result block ends with: ... <total duration> <route> <stops> ... <price>.
    We anchor on price lines and look back only as far as the previous price,
    taking the LARGEST duration in that window (a layover is always shorter
    than the total), so layover times cannot be mistaken for flight time.
    Entries without a duration (e.g. the "from EUR x" teaser) are skipped.
    Returns list of dicts: price, duration_min, stops.
    """
    lines = [l.strip() for l in md.split("\n")]
    price_at = []
    for i, l in enumerate(lines):
        m = re.search(r"€\s?([\d.,]{3,7})", l)
        if not m:
            continue
        digits = re.sub(r"[^\d]", "", m.group(1))
        if not digits:
            continue
        val = int(digits)
        if PRICE_MIN <= val <= PRICE_MAX:
            price_at.append((i, val))

    out = []
    prev = 0
    for idx, val in price_at:
        window = lines[prev:idx]
        prev = idx
        durations = []                    # (minutes, index_in_window)
        for wi, w in enumerate(window):
            for hh, mm in re.findall(r"(\d{1,2})\s*(?:hr|h|std)\b\.?(?:\s*(\d{1,2})\s*m(?:in)?)?", w, re.I):
                durations.append((int(hh) * 60 + int(mm or 0), wi))
        if not durations:
            continue                      # no duration known -> not alert-worthy
        total, dur_idx = max(durations)
        airline = ""
        for back in range(dur_idx - 1, max(-1, dur_idx - 4), -1):
            cand = window[back].strip(" |*_#")
            low = cand.lower()
            if (len(cand) >= 3 and re.search(r"[A-Za-z]{3}", cand)
                    and not re.search(r"\d{1,2}:\d{2}|hr\b|min\b|emission|CO2|stop|–|—", cand, re.I)
                    and not any(bad in low for bad in NOT_AIRLINE)):
                airline = cand[:40]
                break
        stops = None
        for w in window:
            if re.search(r"\b(?:nonstop|non-stop|direkt|sem escalas|directo)\b", w, re.I):
                stops = 0
            sm = re.search(r"\b(\d)\s*(?:stop|escala|parada|zwischenstopp|scalo)", w, re.I)
            if sm:
                stops = int(sm.group(1))
        out.append({"price": val, "duration_min": total, "airline": airline,
                    "stops": stops if stops is not None else MAX_STOPS})
    return out


def parse_teaser(md):
    """Google's own "Cheapest from EUR x" figure (no duration shown)."""
    vals = []
    for raw in re.findall(r"from\s*€\s?([\d.,]{3,7})", md, re.I):
        digits = re.sub(r"[^\d]", "", raw)
        if digits and PRICE_MIN <= int(digits) <= PRICE_MAX:
            vals.append(int(digits))
    return min(vals) if vals else None


def check_one(args):
    origin, dep, ret, token = args
    try:
        url = build_url(origin, dep, ret)
        md = bd_fetch(url, token)
        entries = [e for e in parse_prices(md)
                   if e["duration_min"] <= MAX_DURATION_MIN and e["stops"] <= MAX_STOPS]
        teaser = parse_teaser(md)
        bags = sorted({l.strip()[:90] for l in md.split("\n")
                       if re.search(r"bag|carry.on|checked|gep[aä]ck", l, re.I)
                       and len(l.strip()) > 3})[:12]
        return {"origin": origin, "dep": dep, "ret": ret, "url": url,
                "entries": entries, "teaser": teaser, "bags": bags, "md": md}
    except Exception as e:
        return {"origin": origin, "dep": dep, "ret": ret,
                "error": f"{type(e).__name__}: {str(e)[:80]}"}


def gflights_link(origin, dep, ret):
    q = f"Flights from {origin} to {DEST} on {dep.isoformat()} through {ret.isoformat()}"
    return "https://www.google.com/travel/flights?q=" + urllib.parse.quote(q)


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            return json.load(f)
    return {"failure_streak": 0}


def save_state(state):
    with open(STATE_PATH, "w") as f:
        json.dump(state, f)


def append_log(row):
    exists = os.path.exists(LOG_PATH)
    with open(LOG_PATH, "a", newline="") as f:
        w = csv.writer(f)
        if not exists:
            w.writerow(["check_date", "origin", "price_eur", "depart", "return",
                        "duration_h", "stops", "airline", "tier", "status"])
        w.writerow(row)


def is_dispreferred(airline):
    return any(x in (airline or "").lower() for x in DISPREFERRED_AIRLINES)


def is_favourite(airline):
    return any(x in (airline or "").lower() for x in FAVOURITE_AIRLINES)


def collect(results, origin):
    """Best options for one origin: preferred airline, dispreferred, and teaser."""
    pool = []
    for r in results:
        if r["origin"] != origin or "entries" not in r:
            continue
        for e in r["entries"]:
            pool.append({**e, "dep": r["dep"], "ret": r["ret"], "url": r["url"],
                         "tier": "verified"})
    preferred = [e for e in pool if not is_dispreferred(e["airline"])]
    dispreferred = [e for e in pool if is_dispreferred(e["airline"])]
    teasers = [{"price": r["teaser"], "dep": r["dep"], "ret": r["ret"], "url": r["url"],
                "airline": "", "duration_min": None, "stops": None, "tier": "teaser"}
               for r in results
               if r["origin"] == origin and r.get("teaser") is not None]
    pick = lambda xs: min(xs, key=lambda e: e["price"]) if xs else None
    return pick(preferred), pick(dispreferred), pick(teasers)


def describe(e):
    bits = [f"{e['price']} EUR", f"{e['dep'].isoformat()} -> {e['ret'].isoformat()}"]
    if e["tier"] == "verified":
        bits.append(f"{e['duration_min'] // 60} h {e['duration_min'] % 60} min")
        bits.append("Direktflug" if e["stops"] == 0 else f"{e['stops']} Stopp")
        bits.append(e["airline"] or "Airline unbekannt")
    else:
        bits.append("Googles Guenstigst-Preis, Dauer/Airline nicht auslesbar")
    return " | ".join(bits)


def main():
    today = date.today().isoformat()
    state = load_state()
    token = os.environ.get("BRIGHTDATA_TOKEN")
    if not token:
        state["failure_streak"] = state.get("failure_streak", 0) + 1
        save_state(state)
        append_log([today, "", "", "", "", "", "", "", "", "config_error: no token"])
        print("BRIGHTDATA_TOKEN missing")
        print(f"RESULT: FAILURE streak={state['failure_streak']}")
        return

    jobs = [(o, d, r, token) for o in ORIGINS for d in DEPARTURES for r in RETURNS]
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        results = list(pool.map(check_one, jobs))

    ok = [r for r in results if "entries" in r]
    try:
        sample = next((r for r in ok if r.get("md")), None)
        if sample:
            L = [x.strip() for x in sample["md"].split("\n")]
            i = next((k for k, x in enumerate(L) if "\u20ac" in x), 0)
            with open(os.path.join(BASE_DIR, "parse_debug.md"), "w") as fh:
                fh.write("\n".join(L[max(0, i - 30):i + 10]))
    except Exception:
        pass
    errs = [r for r in results if "entries" not in r]
    print(f"queries={len(results)} ok={len(ok)} failed={len(errs)}")
    for r in errs[:6]:
        print(f"  warn: {r['origin']} {r['dep']}->{r['ret']}: {r['error']}", file=sys.stderr)

    if not ok:
        state["failure_streak"] = state.get("failure_streak", 0) + 1
        save_state(state)
        for o in ORIGINS:
            append_log([today, o, "", "", "", "", "", "", "", "no_results"])
        print(f"RESULT: FAILURE streak={state['failure_streak']}")
        return

    state["failure_streak"] = 0
    save_state(state)

    alert_blocks = []
    headline = []
    for origin in ORIGINS:
        pref, dispref, teaser = collect(ok, origin)
        shown = [e for e in (pref, dispref, teaser) if e]
        if not shown:
            append_log([today, origin, "", "", "", "", "", "", "", "no_results"])
            continue
        primary = min(shown, key=lambda e: e["price"])
        under = [e for e in shown
                 if e["price"] < THRESHOLD and e["tier"] == "verified"]
        status = "ALERT" if under else "ok"
        append_log([today, origin, primary["price"], primary["dep"].isoformat(),
                    primary["ret"].isoformat(),
                    round(primary["duration_min"] / 60.0, 1) if primary["duration_min"] else "?",
                    primary["stops"] if primary["stops"] is not None else "?",
                    primary["airline"] or "?", primary["tier"], status])
        print(f"{'BELOW ' + str(int(THRESHOLD)) + ': ' if under else 'best today: '}"
              f"{origin} -> {describe(primary)}")
        if under:
            # preferred airlines first, dispreferred (China) last, teaser in between
            order = []
            if pref and pref["price"] < THRESHOLD:
                order.append((pref, ""))
            if dispref and dispref["price"] < THRESHOLD:
                order.append((dispref, " (China-Airline, nachrangig)"))
            if not order:
                order = [(min(under, key=lambda e: e["price"]), "")]
            lines = [f"## {origin} -> Hongkong"]
            for e, note in order:
                fav = " -- WUNSCH-AIRLINE!" if is_favourite(e["airline"]) else ""
                lines.append(f"- {describe(e)}{note}{fav}")
                lines.append(f"  [Auf Google Flights oeffnen]"
                             f"({gflights_link(origin, e['dep'], e['ret'])})")
            alert_blocks.append("\n".join(lines))
            names = []
            for e, _ in order:                      # order already puts China last
                n = (e["airline"] or "").strip()
                n = n.split("Operated by")[0].strip() or "Airline unbekannt"
                if n not in names:
                    names.append(n)
            headline.append(f"{origin} {min(e['price'] for e, _ in order)} EUR"
                            + (f" ({', '.join(names)})" if names else ""))

    if alert_blocks:
        body = ([f"Preis unter {int(THRESHOLD)} EUR gefunden (max. 1 Stopp, max. 16 h):", ""]
                + alert_blocks
                + ["", "Hinweis: Preis und Gepaeck beim Buchen pruefen.",
                   "Preisverlauf: price_log.csv im Repo."])
        with open(ALERT_PATH, "w") as f:
            f.write("\n".join(body))
        ctx = ["# Gepaeck-Hinweise aus den gerenderten Seiten", ""]
        for r in ok:
            if r.get("bags"):
                ctx.append(f"## {r['origin']} {r['dep']} -> {r['ret']}")
                ctx += [f"- {b}" for b in r["bags"]]
                ctx.append("")
        with open(os.path.join(BASE_DIR, "alert_context.md"), "w") as f:
            f.write("\n".join(ctx))
        title = "Flug-Deal: " + " | ".join(headline)
        if len(title) > 110:
            title = title[:107] + "..."
        with open(TITLE_PATH, "w") as f:
            f.write(title)
        print("TITLE: " + title)
        print("RESULT: ALERT " + " | ".join(headline))
    else:
        print("RESULT: OK nothing below threshold today")


if __name__ == "__main__":
    main()
