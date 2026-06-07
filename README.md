# Fractured: US — data pipeline

Generates `snapshot.json` (schema v2) from real sources and publishes it via GitHub Pages.
The iOS app fetches that file. **Full setup: see `../GENUINE-DATA-SETUP.md`.**

## Sources
| Factor | Source | Method |
|---|---|---|
| economy | FRED API (Gini, debt-service, real earnings) — needs `FRED_API_KEY` | live |
| violence | GDELT DOC 2.0 API (US protest/violence coverage, 90d) — **keyless, commercial-OK** | live |
| polarization, distrust, animosity, extremism | `curated.json` | hand-updated |

> Not ACLED: its free license is non-commercial and it moved to OAuth in Sept 2025; GDELT is the
> commercial-safe, keyless choice for a paid app (attribution to GDELT required).

## Run it
```bash
# offline, no keys — prove it works and preview numbers
python3 generate_snapshot.py --mock --backfill --weeks 52

# live incremental (only FRED needs a key; GDELT is keyless)
export FRED_API_KEY=...
python3 generate_snapshot.py            # appends one weekly point to snapshot.json
python3 generate_snapshot.py --backfill # rebuild full history from real FRED + GDELT series
```

## Files
- `generate_snapshot.py` — dependency-free generator (Python 3.9+)
- `config.json` — weights, FRED series ids, GDELT query/params, 0–100 normalization ranges, sigmoid
- `curated.json` — the no-API factors + historical analogues
- `snapshot.json` — the published output **and** the rolling history state (committed by the Action)
- `.github/workflows/update-snapshot.yml` — weekly cron + manual dispatch (with `backfill` input)
