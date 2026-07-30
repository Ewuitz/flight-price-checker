#!/usr/bin/env python3
"""
Daily flight price checker: FRA / DUS -> HKG roundtrip.

Data source: live Google Flights, fetched through Bright Data's SERP API
with JS rendering (zone "serp_api"), parsed from the rendered markdown.

Departure window : 2026-10-03 .. 2026-10-07
Return window    : 2026-12-02 .. 2026-12-04
Constraint       : max 1 stop per direction (enforced in the query URL)
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
    """Return (min_price, has_nonstop) from the rendered markdown."""
    prices = []
    for raw in re.findall(r"€\s?([\d.,]{3,7})", md):
        digits = re.sub(r"[^\d]", "", raw)
        if not digits:
            continue
        val = int(digits)
        if PRICE_MIN <= val <= PRICE_MAX:
            prices.append(val)
    nonstop = bool(re.search(r"\bnonstop\b", md, re.I))
    return (min(prices) if prices else None), nonstop


def check_one(args):
    origin, dep, ret, token = args
    try:
        url = build_url(origin, dep, ret)
        md = bd_fetch(url, token)
        price, nonstop = parse_prices(md)
        if price is None:
            return {"origin": origin, "dep": dep, "ret": ret, "error": "no_price_in_page"}
        return {"origin": origin, "dep": dep, "ret": ret, "price": price,
                "nonstop": nonstop, "url": url}
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
                        "return", "nonstop", "status"])
        w.writerow(row)


def main():
    today = date.today().isoformat()
    state = load_state()
    token = os.environ.get("BRIGHTDATA_TOKEN")
    if not token:
        state["failure_streak"] = state.get("failure_streak", 0) + 1
        save_state(state)
        append_log([today, "", "", "", "", "", "config_error: no token"])
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
            append_log([today, o, "", "", "", "", "no_results"])
        print(f"RESULT: FAILURE streak={state['failure_streak']}")
        return

    state["failure_streak"] = 0
    save_state(state)

    alerts = []
    for origin in ORIGINS:
        cands = [r for r in ok if r["origin"] == origin]
        if not cands:
            append_log([today, origin, "", "", "", "", "no_results"])
            continue
        # cheapest first; within 20 EUR prefer a nonstop option
        best = sorted(cands, key=lambda c: (round(c["price"] / 20.0),
                                            0 if c["nonstop"] else 1, c["price"]))[0]
        status = "ALERT" if best["price"] < THRESHOLD else "ok"
        append_log([today, origin, best["price"], best["dep"].isoformat(),
                    best["ret"].isoformat(), best["nonstop"], status])
        print(f"{'BELOW 700: ' if status == 'ALERT' else 'best today: '}"
              f"{origin}->HKG EUR{best['price']} | {best['dep']} -> {best['ret']}"
              f"{' | nonstop available' if best['nonstop'] else ''}")
        if status == "ALERT":
            alerts.append(best)

    if alerts:
        lines = ["Live-Preis auf Google Flights gefunden (max. 1 Zwischenstopp):", ""]
        for b in alerts:
            lines += [
                f"## {b['origin']} -> Hongkong: {b['price']} EUR",
                f"- Hinflug: {b['dep'].isoformat()}, Rueckflug: {b['ret'].isoformat()}",
                f"- {'Direktflug verfuegbar' if b['nonstop'] else 'Mit Zwischenstopp'}",
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
