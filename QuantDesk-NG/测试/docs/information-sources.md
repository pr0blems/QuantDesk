# Information-source pipeline

QuantDesk now treats market information as a quality-gated pipeline rather than
an unversioned collection of headlines and quotes.

## Live source layers

- Binance USDⓈ-M WebSocket: diff depth, aggregate trades, book ticker, mini
  ticker and all-market force orders.
- Binance public REST: exchange rules, mark/funding data, open interest,
  long/short ratios, basis and ADL snapshots. All REST calls share one IP
  governor and honour 429/418 backoff.
- Underlying markets: Yahoo Chart remains the display/fallback source. Its
  status, market timestamp, age, latency and coverage are retained in the
  quote quality JSON.
- Official event feeds: SEC, Federal Reserve, BLS, BEA, EIA and HKEX feeds are
  allowlisted and parsed into structured event fields. News remains
  shadow-only until forward outcomes validate it.

## Quality and history

`data_quality.archive_loop` samples the live upsert tables into 1-minute and
5-minute append-only archives for microstructure, underlying quotes and social
signals. `GET /api/v2/admin/quality` reports source freshness, coverage,
archive volume and quality events.

The battle and two-hour predictors abstain when required inputs are stale,
incomplete or below the quality threshold. This is intentional: an abstention
is safer than converting an old quote or partial order book into a directional
signal.

After changing source adapters or migrations, run:

```powershell
.\.venv\Scripts\alembic.exe upgrade head
.\.venv\Scripts\pytest.exe -q
```

If Binance returns 418, leave the workers running: the shared governor parses
the ban deadline and pauses REST calls while WebSocket market data continues.
