# Flight Price Checker: FRA/DUS -> Hong Kong

Checks 3x daily (08:00, 14:00, 20:00 Berlin) for roundtrips
(out Oct 3-7, back Dec 2 - Dec 4, max 1 stop, verify price incl. baggage before booking).
Data: live Google Flights via Bright Data SERP API (JS-rendered). Opens an issue (= sends you an email) only when a fare drops below 650 EUR,
or when the check itself has failed 3 days in a row.

Price history: `price_log.csv`. Built and maintained via Claude.
