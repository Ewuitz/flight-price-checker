# Flight Price Checker: FRA/DUS -> Hong Kong

Checks daily at ~09:00 Berlin time for roundtrips
(out Oct 3-7, back Dec 2 - Dec 4, max 1 stop, verify price incl. baggage before booking).
Data: live Google Flights prices (via fast-flights). Opens an issue (= sends you an email) only when a fare drops below 700 EUR,
or when the check itself has failed 3 days in a row.

Price history: `price_log.csv`. Built and maintained via Claude.
