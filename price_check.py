#!/usr/bin/env python3
"""
Daily flight price checker: FRA / DUS -> HKG roundtrip.

Data source: Travelpayouts / Aviasales Data API (cached fares from recent
searches -- treat alerts as "go verify now", not guaranteed bookable prices).

Departure window : 2026-10-03 .. 2026-10-07
Return window    : 2026-11-28 .. 2026-12-02
Constraints      : max 1 stop (per API's transfer count), price in EUR
Alert threshold  : < 700 EUR

Credentials: env var TRAVELPAYOUTS_TOKEN (GitHub Actions secret).

Outputs:
  - appends rows to price_log.csv
  - maintains state.json (consecutive failure streak)
  - prints summary; final line starts with RESULT: ALERT / OK / FAILURE
  - on ALERT, writes alert_body.md (email/issue body with booking links)
"""

import csv
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
from datetime import date, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(BASE_DIR, "price_log.csv")
STATE_PATH = os.path.join(BASE_DIR, "state.json")
ALERT_PATH = os.path.join(BASE_DIR, "alert_body.md")

THRESHOLD_EUR = 700.0
ORIGINS = ["FRA", "DUS"]
DEST = "HKG"
DEPARTURES = [date(2026, 10, 3) + timedelta(days=i) for i in range(5)]
RETURNS = [date(2026, 11, 28) + timedelta(days=i) for i in range(5)]
MAX_STOPS = 1
PAUSE_BETWEEN_CALLS = 0.35
API_HOST = "api.travelpayouts.com"


def get_token():
    tok = os.environ.get("TRAVELPAYOUTS_TOKEN")
    if not tok:
        raise RuntimeError("TRAVELPAYOUTS_TOKEN env var is not set")
    return tok


def fetch_offers(token, origin, dep, ret):
    """One prices_for_dates call for a single origin/date pair. Returns list."""
    params = urllib.parse.urlencode({
        "origin": origin,
        "destination": DEST,
        "departure_at": dep.isoformat(),
        "return_at": ret.isoformat(),
        "currency": "eur",
        "one_way": "false",
        "limit": 30,
        "sorting": "price",
        "token": token,
    })
    url = f"https://{API_HOST}/aviasales/v3/prices_for_dates?{params}"
    req = urllib.request.Request(url, headers={"Accept-Encoding": "identity"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                payload = json.load(r)
                if not payload.get("success", True):
                    raise RuntimeError(f"API error: {payload}")
                return payload.get("data", [])
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(3 * (attempt + 1))
                continue
            if e.code in (400, 404):
                return []
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))
        except (urllib.error.URLError, TimeoutError):
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))
    return []


def gflights_link(origin, dep, ret):
    q = f"Flights from {origin} to {DEST} on {dep} through {ret}"
    return "https://www.google.com/travel/flights?q=" + urllib.parse.quote(q)


def aviasales_link(origin, dep, ret):
    d1 = f"{dep[8:10]}{dep[5:7]}"
    d2 = f"{ret[8:10]}{ret[5:7]}"
    return f"https://www.aviasales.com/search/{origin}{d1}{DEST}{d2}1"


def summarize(offer, origin):
    dep = offer["departure_at"][:10]
    ret = offer["return_at"][:10]
    return {
        "origin": origin,
        "price_eur": round(float(offer["price"]), 2),
        "depart": dep,
        "return": ret,
        "airline": offer.get("airline", "?"),
        "transfers": offer.get("transfers", 0),
        "return_transfers": offer.get("return_transfers", 0),
        "gflights": gflights_link(origin, dep, ret),
        "aviasales": aviasales_link(origin, dep, ret),
    }


def rank_key(s):
    """Cheapest first; within ~20 EUR prefer fewer total stops (direct wins)."""
    total_stops = s["transfers"] + s["return_transfers"]
    return (round(s["price_eur"] / 20.0), total_stops, s["price_eur"])


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
            w.writerow(["check_date", "origin", "best_price_eur", "depart", "return",
                        "airline", "stops_out", "stops_back", "status"])
        w.writerow(row)


def fail(state, note):
    state["failure_streak"] = state.get("failure_streak", 0) + 1
    save_state(state)
    today = date.today().isoformat()
    for o in ORIGINS:
        append_log([today, o, "", "", "", "", "", "", note])
    print(note)
    print(f"RESULT: FAILURE streak={state['failure_streak']}")


def main():
    today = date.today().isoformat()
    state = load_state()
    try:
        token = get_token()
    except Exception as e:
        fail(state, f"config_error: {e}")
        return

    best_per_origin = {}
    errors = 0
    for origin in ORIGINS:
        candidates = []
        for dep in DEPARTURES:
            for ret in RETURNS:
                try:
                    offers = fetch_offers(token, origin, dep, ret)
                except Exception as e:
                    errors += 1
                    print(f"  warn: {origin} {dep}->{ret} failed: {e}", file=sys.stderr)
                    offers = []
                for off in offers:
                    if (off.get("transfers", 0) <= MAX_STOPS
                            and off.get("return_transfers", 0) <= MAX_STOPS):
                        candidates.append(summarize(off, origin))
                time.sleep(PAUSE_BETWEEN_CALLS)
        if candidates:
            best_per_origin[origin] = sorted(candidates, key=rank_key)[0]

    if not best_per_origin:
        fail(state, f"no_results (errors={errors})")
        return

    state["failure_streak"] = 0
    save_state(state)

    alerts = []
    for origin in ORIGINS:
        b = best_per_origin.get(origin)
        if not b:
            append_log([today, origin, "", "", "", "", "", "", "no_results"])
            continue
        status = "ALERT" if b["price_eur"] < THRESHOLD_EUR else "ok"
        append_log([today, origin, b["price_eur"], b["depart"], b["return"],
                    b["airline"], b["transfers"], b["return_transfers"], status])
        stops_txt = ("direct" if b["transfers"] + b["return_transfers"] == 0
                     else f"{b['transfers']} stop(s) out / {b['return_transfers']} back")
        print(f"{'BELOW 700: ' if status == 'ALERT' else 'best today: '}"
              f"{origin}->HKG {b['price_eur']:.2f} EUR | {b['depart']} -> {b['return']} "
              f"| {b['airline']} | {stops_txt}")
        if status == "ALERT":
            alerts.append(b)

    if alerts:
        lines = ["Gesehener Preis (Aviasales-Cache, bitte sofort live pruefen):", ""]
        for b in alerts:
            stops_txt = ("Direktflug" if b["transfers"] + b["return_transfers"] == 0
                         else f"{b['transfers']} Stopp(s) hin / {b['return_transfers']} zurueck")
            lines += [
                f"## {b['origin']} -> Hongkong: {b['price_eur']:.2f} EUR",
                f"- Hinflug: {b['depart']}, Rueckflug: {b['return']}",
                f"- Airline: {b['airline']}, {stops_txt}",
                f"- [Auf Google Flights pruefen]({b['gflights']})",
                f"- [Auf Aviasales pruefen]({b['aviasales']})",
                "",
            ]
        lines.append("Preisverlauf: price_log.csv im Repo.")
        with open(ALERT_PATH, "w") as f:
            f.write("\n".join(lines))
        summary = " | ".join(f"{b['origin']} {b['price_eur']:.0f} EUR "
                             f"({b['depart']}->{b['return']})" for b in alerts)
        print(f"RESULT: ALERT {summary}")
    else:
        cheapest = min(best_per_origin.values(), key=lambda b: b["price_eur"])
        print(f"RESULT: OK cheapest today {cheapest['price_eur']:.2f} EUR "
              f"({cheapest['origin']}, {cheapest['depart']} -> {cheapest['return']})")


if __name__ == "__main__":
    main()
