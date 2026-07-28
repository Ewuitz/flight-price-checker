#!/usr/bin/env python3
"""
Daily flight price checker: FRA / DUS -> HKG roundtrip.

Data source: Google Flights via the fast-flights library (live prices).

Departure window : 2026-10-03 .. 2026-10-07
Return window    : 2026-11-28 .. 2026-12-02
Constraints      : max 1 stop per direction (direct preferred), EUR
Alert threshold  : < 700 EUR

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
import time
import urllib.parse
from datetime import date, timedelta

from fast_flights import FlightData, Passengers, get_flights

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(BASE_DIR, "price_log.csv")
STATE_PATH = os.path.join(BASE_DIR, "state.json")
ALERT_PATH = os.path.join(BASE_DIR, "alert_body.md")

THRESHOLD = 700.0
ORIGINS = ["FRA", "DUS"]
DEST = "HKG"
DEPARTURES = [date(2026, 10, 3) + timedelta(days=i) for i in range(5)]
RETURNS = [date(2026, 11, 28) + timedelta(days=i) for i in range(5)]
MAX_STOPS = 1
PAUSE = 1.5


def parse_price(price_str):
    """'€714' / '$1,234' / '1.234 €' -> (714.0, '€'). Returns (None, '') on failure."""
    if not price_str:
        return None, ""
    digits = re.sub(r"[^\d]", "", price_str)
    if not digits:
        return None, ""
    cur = "".join(c for c in price_str if c in "€$£¥") or "?"
    return float(digits), cur


def query(origin, dep, ret):
    """One Google Flights round-trip query. Returns list of flight dicts."""
    res = get_flights(
        flight_data=[
            FlightData(date=dep.isoformat(), from_airport=origin, to_airport=DEST),
            FlightData(date=ret.isoformat(), from_airport=DEST, to_airport=origin),
        ],
        trip="round-trip",
        seat="economy",
        passengers=Passengers(adults=1, children=0, infants_in_seat=0, infants_on_lap=0),
        fetch_mode="fallback",
    )
    out = []
    for f in res.flights:
        price, cur = parse_price(getattr(f, "price", None))
        if price is None:
            continue
        stops = getattr(f, "stops", None)
        try:
            stops = int(stops)
        except (TypeError, ValueError):
            stops = 99
        out.append({
            "price": price,
            "currency": cur,
            "stops": stops,
            "airline": (getattr(f, "name", "") or "?")[:40],
            "is_best": bool(getattr(f, "is_best", False)),
        })
    return out


def gflights_link(origin, dep, ret):
    q = f"Flights from {origin} to {DEST} on {dep} through {ret}"
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
            w.writerow(["check_date", "origin", "best_price", "currency", "depart",
                        "return", "airline", "stops", "status"])
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

    best_per_origin = {}
    errors = 0
    total_queries = 0
    for origin in ORIGINS:
        candidates = []
        for dep in DEPARTURES:
            for ret in RETURNS:
                total_queries += 1
                try:
                    flights = query(origin, dep, ret)
                except Exception as e:
                    errors += 1
                    print(f"  warn: {origin} {dep}->{ret}: {type(e).__name__}: {e}",
                          file=sys.stderr)
                    flights = []
                for fl in flights:
                    if fl["stops"] <= MAX_STOPS:
                        fl["depart"] = dep.isoformat()
                        fl["return"] = ret.isoformat()
                        fl["origin"] = origin
                        candidates.append(fl)
                time.sleep(PAUSE)
        if candidates:
            # cheapest first; within ~20 price units prefer fewer stops
            candidates.sort(key=lambda c: (round(c["price"] / 20.0), c["stops"], c["price"]))
            best_per_origin[origin] = candidates[0]

    print(f"queries={total_queries} errors={errors}")
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
        is_alert = b["price"] < THRESHOLD and b["currency"] == "€"
        status = "ALERT" if is_alert else "ok"
        append_log([today, origin, b["price"], b["currency"], b["depart"], b["return"],
                    b["airline"], b["stops"], status])
        print(f"{'BELOW 700: ' if is_alert else 'best today: '}"
              f"{origin}->HKG {b['currency']}{b['price']:.0f} | {b['depart']} -> {b['return']} "
              f"| {b['airline']} | {b['stops']} stop(s)")
        if is_alert:
            alerts.append(b)

    if alerts:
        lines = ["Live-Preis auf Google Flights gesehen:", ""]
        for b in alerts:
            stops_txt = "Direktflug" if b["stops"] == 0 else f"{b['stops']} Stopp(s)"
            link = gflights_link(b["origin"], b["depart"], b["return"])
            lines += [
                f"## {b['origin']} -> Hongkong: {b['price']:.0f} EUR",
                f"- Hinflug: {b['depart']}, Rueckflug: {b['return']}",
                f"- Airline: {b['airline']}, {stops_txt}",
                f"- [Auf Google Flights oeffnen und buchen]({link})",
                "",
            ]
        lines.append("Hinweis: Preis beim Buchen nochmal pruefen (inkl. Gepaeck).")
        lines.append("Preisverlauf: price_log.csv im Repo.")
        with open(ALERT_PATH, "w") as f:
            f.write("\n".join(lines))
        summary = " | ".join(f"{b['origin']} {b['price']:.0f} EUR "
                             f"({b['depart']}->{b['return']})" for b in alerts)
        print(f"RESULT: ALERT {summary}")
    else:
        cheapest = min(best_per_origin.values(), key=lambda b: b["price"])
        print(f"RESULT: OK cheapest today {cheapest['currency']}{cheapest['price']:.0f} "
              f"({cheapest['origin']}, {cheapest['depart']} -> {cheapest['return']})")


if __name__ == "__main__":
    main()
