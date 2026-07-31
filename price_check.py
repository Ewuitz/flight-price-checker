#!/usr/bin/env python3
"""
Daily flight price checker: FRA / DUS -> HKG roundtrip.

Data source: live Google Flights, fetched through Bright Data's SERP API
with JS rendering (zone "serp_api"), parsed from the rendered markdown.

Departure window : 2026-10-03 .. 2026-10-07
Return window    : 2026-12-02 .. 2026-12-04
Constraints      : max 1 stop per direction (enforced in the query URL),
                   outbound flight duration <= 16 h
Alert threshold  : < 700 EUR

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

THRESHOLD = 700.0
ORIGINS = ["FRA", "DUS"]
DEST = "HKG"
DEPARTURES = [date(2026, 10, 3) + timedelta(days=i) for i in range(5)]
RETURNS = [date(2026, 12, 2) + timedelta(days=i) for i in range(3)]
MAX_STOPS = 1
MAX_DURATION_MIN = 16 * 60
EXCLUDED_AIRLINES = ["air china"]      # substring match, case-insensitive
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
            for hh, mm in re.findall(r"(\d{1,2})\s*hr(?:\s*(\d{1,2})\s*min)?", w, re.I):
                durations.append((int(hh) * 60 + int(mm or 0), wi))
        if not durations:
            continue                      # no duration known -> not alert-worthy
        total, dur_idx = max(durations)
        airline = ""
        for back in range(dur_idx - 1, max(-1, dur_idx - 4), -1):
            cand = window[back].strip(" |*_#")
            if (len(cand) >= 3 and re.search(r"[A-Za-z]{3}", cand)
                    and not re.search(r"\d{1,2}:\d{2}|hr\b|min\b|emission|CO2|stop|–|—", cand, re.I)):
                airline = cand[:40]
                break
        stops = None
        for w in window:
            if re.search(r"\bnonstop\b", w, re.I):
                stops = 0
            sm = re.search(r"\b(\d)\s*stop", w, re.I)
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
                   if e["duration_min"] <= MAX_DURATION_MIN and e["stops"] <= MAX_STOPS
                   and not any(x in e["airline"].lower() for x in EXCLUDED_AIRLINES)]
        teaser = parse_teaser(md)
        if not entries:
            if teaser is not None:
                return {"origin": origin, "dep": dep, "ret": ret, "price": teaser,
                        "duration_min": None, "stops": None, "airline": "",
                        "unverified": True, "url": url}
            return {"origin": origin, "dep": dep, "ret": ret,
                    "error": "no_qualifying_flight"}
        best = min(entries, key=lambda e: e["price"])
        if teaser is not None and teaser < best["price"]:
            return {"origin": origin, "dep": dep, "ret": ret, "price": teaser,
                    "duration_min": None, "stops": None, "airline": "", "unverified": True,
                    "verified_airline": best["airline"],
                    "verified_price": best["price"],
                    "verified_duration_min": best["duration_min"],
                    "verified_stops": best["stops"], "url": url}
        return {"origin": origin, "dep": dep, "ret": ret, "price": best["price"],
                "duration_min": best["duration_min"], "stops": best["stops"],
                "airline": best["airline"], "unverified": False, "url": url}
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
            w.writerow(["check_date", "origin", "best_price_eur", "depart",
                        "return", "duration_h", "stops", "airline", "status"])
        w.writerow(row)


def main():
    today = date.today().isoformat()
    state = load_state()
    token = os.environ.get("BRIGHTDATA_TOKEN")
    if not token:
        state["failure_streak"] = state.get("failure_streak", 0) + 1
        save_state(state)
        append_log([today, "", "", "", "", "", "", "", "config_error: no token"])
        print("BRIGHTDATA_TOKEN missing")
        print(f"RESULT: FAILURE streak={state['failure_streak']}")
        return

    jobs = [(o, d, r, token) for o in ORIGINS for d in DEPARTURES for r in RETURNS]
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        results = list(pool.map(check_one, jobs))

    ok = [r for r in results if "price" in r]
    errs = [r for r in results if "price" not in r]
    print(f"queries={len(results)} ok={len(ok)} failed={len(errs)}")
    for r in errs[:6]:
        print(f"  warn: {r['origin']} {r['dep']}->{r['ret']}: {r['error']}", file=sys.stderr)

    if not ok:
        state["failure_streak"] = state.get("failure_streak", 0) + 1
        save_state(state)
        for o in ORIGINS:
            append_log([today, o, "", "", "", "", "", "", "no_results"])
        print(f"RESULT: FAILURE streak={state['failure_streak']}")
        return

    state["failure_streak"] = 0
    save_state(state)

    alerts = []
    for origin in ORIGINS:
        cands = [r for r in ok if r["origin"] == origin]
        if not cands:
            append_log([today, origin, "", "", "", "", "", "", "no_results"])
            continue
        # cheapest first; within 20 EUR prefer a nonstop option
        best = sorted(cands, key=lambda c: (round(c["price"] / 20.0),
                                            c["stops"] if c["stops"] is not None else 9,
                                            c["price"]))[0]
        status = "ALERT" if best["price"] < THRESHOLD else "ok"
        append_log([today, origin, best["price"], best["dep"].isoformat(),
                    best["ret"].isoformat(),
                    round(best["duration_min"] / 60.0, 1) if best["duration_min"] else "?",
                    best["stops"] if best["stops"] is not None else "?",
                    best.get("airline") or "?", status])
        print(f"{'BELOW 700: ' if status == 'ALERT' else 'best today: '}"
              f"{origin}->HKG EUR{best['price']} | {best['dep']} -> {best['ret']}"
              + (f" | {best['duration_min'] // 60}h{best['duration_min'] % 60:02d}"
                 f" | {'nonstop' if best['stops'] == 0 else str(best['stops']) + ' stop'}"
                 if best["duration_min"] else " | duration unverified"))
        if status == "ALERT":
            alerts.append(best)

    if alerts:
        lines = ["Live-Preis auf Google Flights gefunden (max. 1 Zwischenstopp, max. 16 h):", ""]
        for b in alerts:
            lines += [
                f"## {b['origin']} -> Hongkong: {b['price']} EUR",
                f"- Hinflug: {b['dep'].isoformat()}, Rueckflug: {b['ret'].isoformat()}",
                (f"- Airline: {b['airline']}"
                 + (" -- WUNSCH-AIRLINE!"
                    if any(x in b['airline'].lower() for x in FAVOURITE_AIRLINES) else "")
                 if b.get("airline") else "- Airline: nicht auslesbar"),
                (f"- Flugdauer Hinflug: {b['duration_min'] // 60} h {b['duration_min'] % 60} min"
                 f" ({'Direktflug' if b['stops'] == 0 else str(b['stops']) + ' Zwischenstopp'})"
                 if b["duration_min"] else
                 "- ACHTUNG: Googles Guenstigst-Preis, Flugdauer nicht auslesbar."
                 " Bitte im Link pruefen, ob unter 16 h."
                 + (f" Guenstigster gepruefter Flug (<=16h): {b['verified_price']} EUR"
                    if b.get("verified_price") else "")),
                f"- [Auf Google Flights oeffnen und buchen]({gflights_link(b['origin'], b['dep'], b['ret'])})",
                "",
            ]
        lines += ["Hinweis: Preis und Gepaeck beim Buchen pruefen.",
                  "Preisverlauf: price_log.csv im Repo."]
        with open(ALERT_PATH, "w") as f:
            f.write("\n".join(lines))
        summary = " | ".join(f"{b['origin']} {b['price']} EUR "
                             f"({b['dep']}->{b['ret']})" for b in alerts)
        print(f"RESULT: ALERT {summary}")
    else:
        cheapest = min(ok, key=lambda c: c["price"])
        print(f"RESULT: OK cheapest today {cheapest['price']} EUR "
              f"({cheapest['origin']}, {cheapest['dep']} -> {cheapest['ret']})")


if __name__ == "__main__":
    main()

# verification run: airline extraction
