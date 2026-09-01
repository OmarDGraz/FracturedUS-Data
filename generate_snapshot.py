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


def http_get_json(url, params, timeout=90):
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
    for attempt in range(6):
        try:
            data = http_get_json(gcfg["endpoint"], params)
            break
        except Exception as e:
            # A throttled GDELT request usually manifests as a TIMEOUT, not as a
            # 429. Retrying only on the literal "429" meant the common case fell
            # straight through to `raise`, and the caller silently substituted a
            # synthetic ramp. Back off on anything.
            if attempt < 5:
                wait = 20 * (attempt + 1)
                print(f"  GDELT retry {attempt + 1}/6 in {wait}s ({type(e).__name__}: {str(e)[:70]})")
                time.sleep(wait)
            else:
                raise
    out = []
    for series in data.get("timeline", []):
        for p in series.get("data", []):
            d = gdelt_date(p.get("date", ""))
            if d is not None:
                out.append((d, float(p.get("value", 0))))
    return out


def violence_value(gcfg, mock, end_date, fallback=None):
    """Returns (value, observed). `observed` is False when the value was carried
    forward, and must not be inferred by comparing against the prior reading:
    a real observation that happens to repeat last week's number is still an
    observation, and inferring made the backfill publish a live GDELT fetch as
    method="stale"."""
    lo, hi = gcfg["normalize"]["lo"], gcfg["normalize"]["hi"]
    if mock:
        return normalize(gcfg["mockIntensity"], lo, hi), True
    try:
        series = gdelt_series(gcfg, end_date - dt.timedelta(days=gcfg["windowDays"]), end_date)
        vals = [v for _, v in series]
        if not vals:
            raise ValueError("empty GDELT series")
        intensity = sum(vals) / len(vals)
        print(f"  GDELT avg intensity over {gcfg['windowDays']}d = {intensity:.4f}"
              f"  (tune gdelt.normalize lo/hi around this)")
        return normalize(intensity, lo, hi), True
    except Exception as e:
        print(f"  WARN: GDELT current fetch failed ({e})")
        if fallback is not None:
            print(f"  carrying forward prior violence value {fallback}")
            return float(fallback), False
        sys.exit("  violence has no prior value to carry forward — refusing to publish "
                 "a midpoint placeholder as a GDELT measurement.")


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
                # Measured: GDELT answers in 20-35s and 429s aggressively at
                # roughly one request per 6s. 25s between chunks completes a
                # 104-week backfill; 6s does not.
                time.sleep(25)
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



# ---------- polarization (Voteview DW-NOMINATE) ----------

def _congress_for(d):
    """Congress in session on a date. The Nth Congress convenes 3 Jan of the
    odd year 1789 + (N-1)*2 and sits two years."""
    year = d.year if (d.month, d.day) >= (1, 3) else d.year - 1
    if year % 2 == 0:
        year -= 1
    return (year - 1789) // 2 + 1


def _voteview_distances(vcfg):
    """{congress: |R median - D median|} on the first DW-NOMINATE dimension."""
    import csv, io
    req = urllib.request.Request(vcfg["endpoint"],
                                 headers={"User-Agent": "FracturedUS-pipeline/1.0"})
    with urllib.request.urlopen(req, timeout=90) as resp:
        text = resp.read().decode("utf-8")
    medians = {}
    for row in csv.DictReader(io.StringIO(text)):
        if row.get("chamber") != vcfg.get("chamber", "House"):
            continue
        if row.get("party_code") not in ("100", "200"):
            continue
        value = row.get("nominate_dim1_median")
        if not value:
            continue
        medians.setdefault(int(row["congress"]), {})[row["party_code"]] = float(value)
    out = {c: abs(v["200"] - v["100"])
           for c, v in medians.items() if "100" in v and "200" in v}
    if not out:
        raise ValueError("no usable party medians in the Voteview table")
    return out


def polarization_value(vcfg, mock, end_date, fallback=None):
    """Party-median ideological distance, normalized to 0-100.

    Replaces a hand-typed constant with the published measure the app already
    claims this factor is. It changes per Congress, not per week — the factor's
    own asOf carries the real observation date so the UI can say so.
    """
    n = vcfg["normalize"]
    if mock:
        return normalize(vcfg["mockDistance"], n["lo"], n["hi"]), True
    try:
        dist = _voteview_distances(vcfg)
        congress = _congress_for(end_date)
        while congress not in dist and congress > 1:
            congress -= 1          # newest Congress present at or before this date
        return normalize(dist[congress], n["lo"], n["hi"]), True
    except Exception as e:
        print(f"  WARN: polarization failed ({e})")
        if fallback is not None:
            print(f"  carrying forward prior polarization value {fallback}")
            return float(fallback), False
        raise


def polarization_backfill(vcfg, dates):
    """Real per-Congress history: a step function, because that is what it is."""
    n = vcfg["normalize"]
    try:
        dist = _voteview_distances(vcfg)
    except Exception as e:
        print(f"  (polarization backfill failed: {e})")
        return None
    out = []
    for d in dates:
        c = _congress_for(d)
        while c not in dist and c > 1:
            c -= 1
        out.append(normalize(dist[c], n["lo"], n["hi"]))
    return out


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

    # --- current factor values (resilient: carry forward prior value on source failure) ---
    def prior_val(fid):
        return (prior or {}).get("factors", {}).get(fid, {}).get("currentValue")

    cur_vals = {}
    # Tracks whether each live factor's CURRENT value came from its named source
    # on this run. A value that was carried forward is not an observation, and
    # must not be published wearing the source's label.
    observed = {}
    try:
        cur_vals["economy"] = economy_value(config["fred"], mock, fred_key, end_date)
        observed["economy"] = True
    except Exception as e:
        pv = prior_val("economy")
        print(f"  WARN: economy failed ({e}); carrying forward {pv}")
        if pv is None:
            sys.exit("  economy has no prior value to carry forward — refusing to "
                     "publish a placeholder as a Federal Reserve measurement.")
        cur_vals["economy"] = float(pv)
        observed["economy"] = False
    cur_vals["violence"], observed["violence"] = violence_value(
        config["gdelt"], mock, end_date, fallback=prior_val("violence"))
    cur_vals["polarization"], observed["polarization"] = polarization_value(
        config["voteview"], mock, end_date, fallback=prior_val("polarization"))
    for fid, entry in curated["factors"].items():
        if fid in cur_vals:
            continue          # a live source already produced this one
        cur_vals[fid] = round(float(entry["currentValue"]), 1)

    # --- per-factor history ---
    factor_hist = {}
    if args.backfill:
        dates = weekly_dates(end_date, weeks)
        for fid in config["factors"]:
            if fid == "polarization":
                fetched = (None if mock else polarization_backfill(config["voteview"], dates))
                if fetched is None:
                    if mock:
                        vals = [cur_vals[fid]] * len(dates)
                    else:
                        sys.exit("  polarization backfill could not reach Voteview. Re-run "
                                 "when it responds — this factor's history is a published "
                                 "series, not something to interpolate.")
                else:
                    vals = fetched
            elif fid in curated["factors"]:
                vals = curated_trajectory(curated["factors"][fid], dates)
            elif fid in ("economy", "violence"):
                fetched = None
                if not mock:
                    fetched = (fred_backfill(config["fred"], fred_key, dates)
                               if fid == "economy"
                               else gdelt_backfill(config["gdelt"], dates))
                if fetched is None:
                    if mock:
                        vals = live_trajectory_mock(cur_vals[fid], dates)
                    else:
                        # This is how 92 of 104 published violence points became a
                        # straight line from current-6.0 wearing a GDELT label.
                        # A backfill that cannot reach its source has nothing to
                        # publish; say so and stop rather than invent a trend.
                        sys.exit(f"  {fid} backfill could not reach its source. Re-run when it "
                                 f"responds — a synthesized ramp must not ship as {fid} history.")
                else:
                    vals = fetched
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
        # A configured live source wins over any leftover curated entry. Checking
        # curated first meant polarization was fetched from Voteview and then
        # published crediting Pew, because its old curated block still existed.
        if fcfg.get("source") == "voteview":
            v = config["voteview"]
            meta = {"asOf": end_date.isoformat(), "sourceURL": v["sourceURL"],
                    "sourceLabel": v["sourceLabel"], "events": []}
        elif fcfg.get("source") == "fred":
            meta = {"asOf": end_date.isoformat(), "sourceURL": config["fred"]["sourceURL"],
                    "sourceLabel": config["fred"]["sourceLabel"], "events": []}
        elif fid in curated["factors"]:
            c = curated["factors"][fid]
            meta = {"asOf": c.get("asOf", end_date.isoformat()),
                    "sourceURL": c.get("sourceURL", ""), "sourceLabel": c.get("sourceLabel", ""),
                    "events": c.get("events", [])}
        else:  # gdelt
            meta = {"asOf": end_date.isoformat(), "sourceURL": config["gdelt"]["sourceURL"],
                    "sourceLabel": config["gdelt"]["sourceLabel"], "events": []}
        hist = factor_hist[fid]
        vals = [p["value"] for p in hist]
        # For a curated factor the published means come from the source. Taking
        # the mean of our own monotone interpolation overwrote them and drifted
        # further from the source with every weekly append.
        c_entry = curated["factors"].get(fid, {})
        one_year = c_entry.get("oneYearMean") if fid in curated["factors"] else None
        long_run = c_entry.get("fiveYearMean") if fid in curated["factors"] else None
        factors_out[fid] = {
            "currentValue": cur_vals[fid],
            "oneYearMean": one_year if one_year is not None
                           else round(sum(vals[-52:]) / len(vals[-52:]), 1),
            "fiveYearMean": long_run if long_run is not None
                            else round(sum(vals) / len(vals), 1),
            "method": ("live" if observed.get(fid, True) else "stale")
                      if fcfg["method"] == "live" else fcfg["method"],
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
    # Load prior snapshot for incremental history AND for carry-forward fallbacks
    # (kept even in --backfill so a failed live source falls back to the last value).
    prior = None
    if os.path.exists(args.out):
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
