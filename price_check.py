#!/usr/bin/env python3
"""
Daily flight price checker: FRA / DUS -> HKG roundtrip.

Departure window : 2026-10-03 .. 2026-10-07
Return window    : 2026-11-28 .. 2026-12-02
Constraints      : max 1 stop per direction (direct preferred),
                   checked bag included, price in EUR.
Alert threshold  : < 700 EUR.

Reads credentials from config.json (same folder):
    {"api_key": "...", "api_secret": "...", "env": "test"}
"env" is "test" (test.api.amadeus.com) or "production" (api.amadeus.com).

Outputs:
  - appends one row per origin per day to price_log.csv
  - maintains state.json (consecutive failure streak)
  - prints a human-readable summary; final line is one of:
        RESULT: ALERT <details>
        RESULT: OK <details>
        RESULT: FAILURE streak=<n>
Exit code is always 0 unless the script itself crashes unexpectedly.
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
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
LOG_PATH = os.path.join(BASE_DIR, "price_log.csv")
STATE_PATH = os.path.join(BASE_DIR, "state.json")

THRESHOLD_EUR = 700.0
ORIGINS = ["FRA", "DUS"]
DEST = "HKG"
DEPARTURES = [date(2026, 10, 3) + timedelta(days=i) for i in range(5)]
RETURNS = [date(2026, 11, 28) + timedelta(days=i) for i in range(5)]
MAX_STOPS = 1          # per direction
PAUSE_BETWEEN_CALLS = 0.6   # seconds; stay well under rate limits
FAILURE_ALERT_AT = 3   # consecutive failed days before warning


def load_config():
    # Preferred: environment variables (GitHub Actions secrets)
    key = os.environ.get("AMADEUS_API_KEY")
    secret = os.environ.get("AMADEUS_API_SECRET")
    env = os.environ.get("AMADEUS_ENV", "test")
    if not (key and secret):
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
        key, secret, env = cfg["api_key"], cfg["api_secret"], cfg.get("env", "test")
    host = "test.api.amadeus.com" if env == "test" else "api.amadeus.com"
    return key, secret, host


def get_token(api_key, api_secret, host):
    data = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": api_key,
        "client_secret": api_secret,
    }).encode()
    req = urllib.request.Request(
        f"https://{host}/v1/security/oauth2/token",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)["access_token"]


def search_offers(token, host, origin, dep, ret):
    """One Flight Offers Search call for a single origin/date pair."""
    params = urllib.parse.urlencode({
        "originLocationCode": origin,
        "destinationLocationCode": DEST,
        "departureDate": dep.isoformat(),
        "returnDate": ret.isoformat(),
        "adults": 1,
        "currencyCode": "EUR",
        "includedCheckedBagsOnly": "true",
        "max": 10,
    })
    req = urllib.request.Request(
        f"https://{host}/v2/shopping/flight-offers?{params}",
        headers={"Authorization": f"Bearer {token}"},
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r).get("data", [])
        except urllib.error.HTTPError as e:
            if e.code == 429:               # rate limited -> back off and retry
                time.sleep(2 * (attempt + 1))
                continue
            if e.code in (400, 404):        # no offers / bad combo -> treat as empty
                return []
            raise
        except (urllib.error.URLError, TimeoutError):
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))
    return []


def offer_stops(offer):
    """Max number of stops across the two itineraries; also returns per-direction stops."""
    stops = [len(it["segments"]) - 1 for it in offer["itineraries"]]
    return max(stops), stops


def summarize_offer(offer, origin, dep, ret):
    price = float(offer["price"]["grandTotal"])
    _, stops = offer_stops(offer)
    carriers = sorted({s["carrierCode"] for it in offer["itineraries"] for s in it["segments"]})
    # connection airports (for context in the alert)
    vias = []
    for it in offer["itineraries"]:
        for s in it["segments"][:-1]:
            vias.append(s["arrival"]["iataCode"])
    return {
        "origin": origin,
        "price_eur": round(price, 2),
        "depart": dep.isoformat(),
        "return": ret.isoformat(),
        "airlines": "/".join(carriers),
        "stops_out": stops[0],
        "stops_back": stops[1] if len(stops) > 1 else None,
        "via": "/".join(sorted(set(vias))) if vias else "direct",
    }


def rank_key(s):
    """Cheapest first; on near-equal price (within 20 EUR) prefer fewer total stops."""
    total_stops = s["stops_out"] + (s["stops_back"] or 0)
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
                        "airlines", "stops_out", "stops_back", "via", "status"])
        w.writerow(row)


def main():
    today = date.today().isoformat()
    state = load_state()
    try:
        api_key, api_secret, host = load_config()
        token = get_token(api_key, api_secret, host)
    except Exception as e:
        state["failure_streak"] = state.get("failure_streak", 0) + 1
        save_state(state)
        for o in ORIGINS:
            append_log([today, o, "", "", "", "", "", "", "", f"auth_error: {e}"])
        print(f"Could not authenticate with Amadeus: {e}")
        print(f"RESULT: FAILURE streak={state['failure_streak']}")
        return

    best_per_origin = {}
    errors = 0
    calls = 0
    for origin in ORIGINS:
        candidates = []
        for dep in DEPARTURES:
            for ret in RETURNS:
                try:
                    offers = search_offers(token, host, origin, dep, ret)
                    calls += 1
                except Exception as e:
                    errors += 1
                    calls += 1
                    print(f"  warn: {origin} {dep}->{ret} failed: {e}", file=sys.stderr)
                    offers = []
                for off in offers:
                    mx, _ = offer_stops(off)
                    if mx <= MAX_STOPS:
                        candidates.append(summarize_offer(off, origin, dep, ret))
                time.sleep(PAUSE_BETWEEN_CALLS)
        if candidates:
            best_per_origin[origin] = sorted(candidates, key=rank_key)[0]

    if not best_per_origin:
        state["failure_streak"] = state.get("failure_streak", 0) + 1
        save_state(state)
        for o in ORIGINS:
            append_log([today, o, "", "", "", "", "", "", "", "no_results"])
        print(f"No qualifying offers found (calls={calls}, errors={errors}).")
        print(f"RESULT: FAILURE streak={state['failure_streak']}")
        return

    # success -> reset failure streak
    state["failure_streak"] = 0
    save_state(state)

    alerts = []
    for origin in ORIGINS:
        b = best_per_origin.get(origin)
        if not b:
            append_log([today, origin, "", "", "", "", "", "", "", "no_results"])
            continue
        status = "ALERT" if b["price_eur"] < THRESHOLD_EUR else "ok"
        append_log([today, origin, b["price_eur"], b["depart"], b["return"],
                    b["airlines"], b["stops_out"], b["stops_back"], b["via"], status])
        line = (f"{origin}->HKG: {b['price_eur']:.2f} EUR | out {b['depart']}, "
                f"back {b['return']} | {b['airlines']} | "
                f"stops {b['stops_out']}/{b['stops_back']} via {b['via']}")
        print(("BELOW 700: " if status == "ALERT" else "best today: ") + line)
        if status == "ALERT":
            alerts.append(line)

    if alerts:
        print("RESULT: ALERT " + " || ".join(alerts))
    else:
        cheapest = min(best_per_origin.values(), key=lambda b: b["price_eur"])
        print(f"RESULT: OK cheapest today {cheapest['price_eur']:.2f} EUR "
              f"({cheapest['origin']}, {cheapest['depart']} -> {cheapest['return']})")


if __name__ == "__main__":
    main()
