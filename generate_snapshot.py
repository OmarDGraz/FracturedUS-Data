#!/usr/bin/env python3
"""
Fractured: US — snapshot generator.

Produces snapshot.json (schema v2) from real sources:
  - economy  -> FRED API (live)         set FRED_API_KEY   (the ONLY key needed)
  - violence -> GDELT DOC 2.0 API (live, KEYLESS, commercial-OK with attribution)
  - polarization / distrust / animosity / extremism -> curated.json (no public API)

The published snapshot.json IS the history state: each run reads the prior file,
appends one dated point per factor + one composite point, and trims to historyWeeks.

Usage:
  python3 generate_snapshot.py                       # incremental live run (needs FRED_API_KEY)
  python3 generate_snapshot.py --mock                # no network; deterministic raw inputs
  python3 generate_snapshot.py --mock --backfill     # build a full initial history (offline)
  python3 generate_snapshot.py --backfill            # live backfill (real FRED + GDELT history)
  python3 generate_snapshot.py --date 2026-06-06 --out ../Shared/FactorSnapshot.json

Dependency-free (urllib only). Python 3.9+.
"""

import argparse
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))


# ---------- small helpers ----------

def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def normalize(x, lo, hi, invert=False):
    """Map a real-world reading onto 0-100 via a configured reference range."""
    if hi == lo:
        return 0.0
    pct = clamp((x - lo) / (hi - lo), 0.0, 1.0)
    if invert:
        pct = 1.0 - pct
    return round(pct * 100.0, 1)


def sigmoid_probability(score, p):
    import math
    z = (score - p["center"]) / p["scale"]
    sig = 1.0 / (1.0 + math.exp(-z))
    return round(sig * p["span"] + p["floor"], 4)


def http_get_json(url, params, timeout=40):
    q = urllib.parse.urlencode(params)
    full = f"{url}?{q}"
    req = urllib.request.Request(full, headers={"User-Agent": "FracturedUS-pipeline/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        raise RuntimeError(f"HTTP {e.code} :: {body}") from None


def weekly_dates(end_date, weeks):
    """Oldest -> newest, inclusive of end_date, one step per week."""
    return [end_date - dt.timedelta(weeks=(weeks - 1 - i)) for i in range(weeks)]


# ---------- FRED (economy) ----------

def fred_latest(fred_cfg, series_id, api_key, end_date):
    data = http_get_json(fred_cfg["endpoint"], {
        "series_id": series_id, "api_key": api_key, "file_type": "json",
        "sort_order": "desc", "limit": 1, "observation_end": end_date.isoformat(),
    })
    for obs in data.get("observations", []):
        if obs.get("value") not in (".", "", None):
            return float(obs["value"])
    raise RuntimeError(f"FRED {series_id}: no usable observation")


def fred_series(fred_cfg, series_id, api_key, start_date, end_date):
    data = http_get_json(fred_cfg["endpoint"], {
        "series_id": series_id, "api_key": api_key, "file_type": "json",
        "observation_start": start_date.isoformat(), "observation_end": end_date.isoformat(),
    })
    out = []
    for obs in data.get("observations", []):
        if obs.get("value") not in (".", "", None):
            out.append((dt.date.fromisoformat(obs["date"]), float(obs["value"])))
    return out  # ascending


def economy_value(fcfg, mock, api_key, end_date):
    parts = []
    for s in fcfg["series"]:
        try:
            raw = s["mock"] if mock else fred_latest(fcfg, s["id"], api_key, end_date)
        except Exception as e:
            print(f"  WARN: FRED {s['id']} failed ({e}); skipping this indicator")
            continue
        v = normalize(raw, s["normalize"]["lo"], s["normalize"]["hi"], s.get("invert", False))
        if not mock:
            print(f"  FRED {s['id']:16s} raw={raw}  -> {v}/100  ({s['label']})")
        parts.append((v, s["weight"]))
    if not parts:
        raise RuntimeError("all FRED economy series failed (check FRED_API_KEY)")
    return round(sum(v * w for v, w in parts) / sum(w for _, w in parts), 1)


def fred_backfill(fcfg, api_key, dates):
    """Weekly normalized economy values from real FRED series. None on failure."""
    try:
        obs_by_id = {
            s["id"]: fred_series(fcfg, s["id"], api_key,
                                 dates[0] - dt.timedelta(days=400), dates[-1])
            for s in fcfg["series"]
        }
        out = []
        for dte in dates:
            parts = []
            for s in fcfg["series"]:
                val = None
                for (od, ov) in obs_by_id[s["id"]]:
                    if od <= dte:
                        val = ov
                    else:
                        break
                if val is None and obs_by_id[s["id"]]:
                    val = obs_by_id[s["id"]][0][1]
                if val is None:
                    raise ValueError(f"no FRED obs for {s['id']}")
                nv = normalize(val, s["normalize"]["lo"], s["normalize"]["hi"], s.get("invert", False))
                parts.append((nv, s["weight"]))
            out.append(round(sum(v * w for v, w in parts) / sum(w for _, w in parts), 1))
        return out
    except Exception as e:
        print(f"  (FRED backfill failed: {e}; using ramp)")
        return None


# ---------- GDELT (violence) — keyless DOC 2.0 timelinevol ----------

def gdelt_date(s):
    s = (s or "")[:8]
    try:
        return dt.date(int(s[0:4]), int(s[4:6]), int(s[6:8]))
    except Exception:
        return None


def gdelt_series(gcfg, start_date, end_date):
    """Raw [(date, intensity)] from GDELT's volume-intensity timeline.
    GDELT rate-limits to one request / 5s, so retry once after a pause on 429."""
    params = {
        "query": gcfg["query"],
        "mode": gcfg.get("mode", "timelinevol"),
        "format": "json",
        "startdatetime": start_date.strftime("%Y%m%d000000"),
        "enddatetime": end_date.strftime("%Y%m%d000000"),
    }
    data = None
    for attempt in range(3):
        try:
            data = http_get_json(gcfg["endpoint"], params)
            break
        except Exception as e:
            if attempt < 2 and "429" in str(e):
                print("  GDELT rate-limited; waiting 6s and retrying...")
                time.sleep(6)
            else:
                raise
    out = []
    for series in data.get("timeline", []):
        for p in series.get("data", []):
            d = gdelt_date(p.get("date", ""))
            if d is not None:
                out.append((d, float(p.get("value", 0))))
    return out


def violence_value(gcfg, mock, end_date):
    if mock:
        intensity = gcfg["mockIntensity"]
    else:
        series = gdelt_series(gcfg, end_date - dt.timedelta(days=gcfg["windowDays"]), end_date)
        vals = [v for _, v in series]
        intensity = (sum(vals) / len(vals)) if vals else 0.0
        print(f"  GDELT avg intensity over {gcfg['windowDays']}d = {intensity:.4f}"
              f"  (tune gdelt.normalize lo/hi around this)")
    return normalize(intensity, gcfg["normalize"]["lo"], gcfg["normalize"]["hi"])


def gdelt_backfill(gcfg, dates):
    """Weekly normalized violence values from real GDELT history. None on failure.
    Queries in <=80-day chunks: GDELT throttles wide windows as 'larger queries',
    so a single 1-year request gets 429'd. Small chunks + 6s spacing succeed."""
    try:
        start = dates[0] - dt.timedelta(days=7)
        end = dates[-1]
        series = []
        chunk_start = start
        first = True
        while chunk_start <= end:
            chunk_end = min(chunk_start + dt.timedelta(days=80), end)
            if not first:
                time.sleep(6)  # respect GDELT's 1-request / 5s limit between chunks
            series += gdelt_series(gcfg, chunk_start, chunk_end)
            first = False
            chunk_start = chunk_end + dt.timedelta(days=1)
        if not series:
            raise ValueError("empty GDELT timeline")
        smap = {}
        for d, v in series:
            smap[d] = v
        ordered = sorted(smap.items())
        out = []
        for i, dte in enumerate(dates):
            lo = dates[i - 1] if i > 0 else start
            window = [v for (d, v) in ordered if lo < d <= dte]
            intensity = (sum(window) / len(window)) if window else ordered[-1][1]
            out.append(normalize(intensity, gcfg["normalize"]["lo"], gcfg["normalize"]["hi"]))
        return out
    except Exception as e:
        print(f"  (GDELT backfill failed: {e}; using ramp)")
        return None


# ---------- curated factor backfill (interpolate published reference points) ----------

def curated_trajectory(entry, dates):
    """Smooth weekly path for a curated factor: fiveYearMean -> oneYearMean -> currentValue.
    Honest interpolation between the source's sparse published readings, not invented noise."""
    n = len(dates)
    five, one, cur = entry["fiveYearMean"], entry["oneYearMean"], entry["currentValue"]
    out = []
    for i in range(n):
        t = i / (n - 1) if n > 1 else 1.0
        knee = max(0.0, 1.0 - 52.0 / max(n - 1, 1))  # where the 1-year mark sits
        if t <= knee:
            seg = t / knee if knee > 0 else 1.0
            v = five + (one - five) * seg
        else:
            seg = (t - knee) / (1.0 - knee) if knee < 1.0 else 1.0
            v = one + (cur - one) * seg
        out.append(round(clamp(v, 0, 100), 1))
    out[-1] = round(float(cur), 1)
    return out


def live_trajectory_mock(current_value, dates):
    """Deterministic ramp toward current_value (offline backfill / fallback; no RNG)."""
    n = len(dates)
    start = clamp(current_value - 6.0, 0, 100)
    out = []
    for i in range(n):
        t = i / (n - 1) if n > 1 else 1.0
        ripple = 1.2 * (1 if i % 3 == 0 else (-1 if i % 3 == 1 else 0)) * (1 - t)
        out.append(round(clamp(start + (current_value - start) * t + ripple, 0, 100), 1))
    out[-1] = round(float(current_value), 1)
    return out


# ---------- assembly ----------

def build_snapshot(config, curated, prior, args):
    end_date = args.date
    mock = args.mock
    fred_key = os.environ.get("FRED_API_KEY", "")

    if not mock and not fred_key:
        sys.exit("FRED_API_KEY not set (use --mock to run without it). GDELT needs no key.")

    weeks = args.weeks or config["historyWeeks"]
    prior_factors = (prior or {}).get("factors", {})
    prior_composite_hist = (prior or {}).get("composite", {}).get("history", [])

    # --- current factor values ---
    cur_vals = {}
    cur_vals["economy"] = economy_value(config["fred"], mock, fred_key, end_date)
    cur_vals["violence"] = violence_value(config["gdelt"], mock, end_date)
    for fid, entry in curated["factors"].items():
        cur_vals[fid] = round(float(entry["currentValue"]), 1)

    # --- per-factor history ---
    factor_hist = {}
    if args.backfill:
        dates = weekly_dates(end_date, weeks)
        for fid in config["factors"]:
            if fid in curated["factors"]:
                vals = curated_trajectory(curated["factors"][fid], dates)
            elif fid == "economy":
                vals = (None if mock else fred_backfill(config["fred"], fred_key, dates)) \
                       or live_trajectory_mock(cur_vals[fid], dates)
            elif fid == "violence":
                vals = (None if mock else gdelt_backfill(config["gdelt"], dates)) \
                       or live_trajectory_mock(cur_vals[fid], dates)
            else:
                vals = live_trajectory_mock(cur_vals[fid], dates)
            if fid not in curated["factors"]:
                vals[-1] = cur_vals[fid]  # anchor the latest point to the headline value
            factor_hist[fid] = [{"date": d.isoformat(), "value": v} for d, v in zip(dates, vals)]
    else:
        today = end_date.isoformat()
        for fid in config["factors"]:
            h = [p for p in prior_factors.get(fid, {}).get("history", []) if p.get("date") != today]
            h.append({"date": today, "value": cur_vals[fid]})
            factor_hist[fid] = h[-weeks:]

    # --- factor entries (metadata + history) ---
    factors_out = {}
    for fid, fcfg in config["factors"].items():
        if fid in curated["factors"]:
            c = curated["factors"][fid]
            meta = {"asOf": c.get("asOf", end_date.isoformat()),
                    "sourceURL": c.get("sourceURL", ""), "sourceLabel": c.get("sourceLabel", ""),
                    "events": c.get("events", [])}
        elif fcfg.get("source") == "fred":
            meta = {"asOf": end_date.isoformat(), "sourceURL": config["fred"]["sourceURL"],
                    "sourceLabel": config["fred"]["sourceLabel"], "events": []}
        else:  # gdelt
            meta = {"asOf": end_date.isoformat(), "sourceURL": config["gdelt"]["sourceURL"],
                    "sourceLabel": config["gdelt"]["sourceLabel"], "events": []}
        hist = factor_hist[fid]
        vals = [p["value"] for p in hist]
        factors_out[fid] = {
            "currentValue": cur_vals[fid],
            "oneYearMean": round(sum(vals[-52:]) / len(vals[-52:]), 1),
            "fiveYearMean": round(sum(vals) / len(vals), 1),
            "method": fcfg["method"],
            **meta,
            "history": hist,
        }

    # --- composite + its history ---
    weights = {fid: config["factors"][fid]["weight"] for fid in config["factors"]}

    def composite_on(values):
        return round(sum(values[f] * weights[f] for f in weights), 2)

    if args.backfill:
        dates = weekly_dates(end_date, weeks)
        comp_hist = []
        for i, d in enumerate(dates):
            vals = {fid: factor_hist[fid][i]["value"] for fid in weights}
            sc = composite_on(vals)
            comp_hist.append({"date": d.isoformat(), "score": sc,
                              "probability": sigmoid_probability(sc, config["sigmoid"])})
    else:
        today = end_date.isoformat()
        sc = composite_on(cur_vals)
        comp_hist = [p for p in prior_composite_hist if p.get("date") != today]
        comp_hist.append({"date": today, "score": sc,
                          "probability": sigmoid_probability(sc, config["sigmoid"])})
        comp_hist = comp_hist[-weeks:]

    composite = {"score": comp_hist[-1]["score"], "probability": comp_hist[-1]["probability"],
                 "history": comp_hist}

    label = end_date.strftime("%b %-d, %Y") if os.name != "nt" else end_date.strftime("%b %d, %Y")
    return {
        "schemaVersion": 2,
        "asOf": end_date.isoformat(),
        "asOfLabel": label,
        "generatedAt": dt.datetime(end_date.year, end_date.month, end_date.day).isoformat() + "Z",
        "horizonYears": config["horizonYears"],
        "cadenceLabel": config["cadenceLabel"],
        "composite": composite,
        "factors": factors_out,
        "historicalAnalogues": curated.get("historicalAnalogues", []),
    }


def validate(snap):
    assert snap["schemaVersion"] == 2
    c = snap["composite"]
    assert 0.0 <= c["probability"] <= 1.0, "probability out of range"
    assert len(c["history"]) >= 1
    for fid, f in snap["factors"].items():
        assert 0 <= f["currentValue"] <= 100, f"{fid} value out of range"
        assert len(f["history"]) >= 1, f"{fid} empty history"
        for p in f["history"]:
            assert 0 <= p["value"] <= 100
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mock", action="store_true", help="no network; deterministic raw inputs")
    ap.add_argument("--backfill", action="store_true", help="build full initial history")
    ap.add_argument("--date", type=lambda s: dt.date.fromisoformat(s), default=None)
    ap.add_argument("--weeks", type=int, default=None, help="override history length")
    ap.add_argument("--out", default=os.path.join(HERE, "snapshot.json"))
    args = ap.parse_args()
    if args.date is None:
        args.date = dt.date.today()

    config = load_json(os.path.join(HERE, "config.json"))
    curated = load_json(os.path.join(HERE, "curated.json"))
    prior = None
    if not args.backfill and os.path.exists(args.out):
        try:
            prior = load_json(args.out)
        except Exception:
            prior = None

    snap = build_snapshot(config, curated, prior, args)
    validate(snap)

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(snap, f, indent=2)
        f.write("\n")

    c = snap["composite"]
    print(f"wrote {args.out}")
    print(f"  asOf={snap['asOf']}  composite={c['score']}  probability={c['probability']*100:.1f}%"
          f"  history={len(c['history'])} pts")
    for fid, fo in snap["factors"].items():
        print(f"  {fid:13s} {fo['currentValue']:5.1f}  ({fo['method']}, {len(fo['history'])} pts)")


if __name__ == "__main__":
    main()
