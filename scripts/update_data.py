#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Fuel price data auto-updater for the aviation fuel dashboard.

Fetches the latest prices from two public sources and merges them into
data.json (keyed by date / month, so repeated runs are idempotent):

  1. MOPS jet fuel (daily, USD/bbl)
     Source: Ministry of Commerce (price.mofcom.gov.cn)
     Series: seqno=191 -> 航空煤油（新）, 新加坡市场FOB价

  2. US Gulf jet fuel (daily, USD/gal) and Brent crude (daily, USD/bbl)
     US Gulf: U.S. EIA Open Data API v2, series EER_EPJK_PF4_RGC_DPG
     Brent:   FRED fredgraph.csv, series DCOILBRENTEU (primary; mirrors EIA
              RBRTE exactly but is posted ~1 week earlier, verified
              2026-09-03 on 1667 overlapping days with max abs diff 0.0000).
              Falls back to the EIA API v2 series RBRTE when FRED is
              unreachable or disagrees with EIA on the last common day.

Environment:
  EIA_API_KEY   Optional. Free key from https://www.eia.gov/opendata/.
                Falls back to the rate-limited public DEMO_KEY.

Run:
  python3 scripts/update_data.py
"""

import datetime as _dt
import json
import os
import ssl
import sys
import time
import urllib.parse
import urllib.request

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_FILE = os.path.join(BASE_DIR, "data.json")

MOFCOM_URL = (
    "https://price.mofcom.gov.cn/datamofcom/front/price/"
    "pricequotation/priceQueryList"
)
MOFCOM_SEQNO = "191"          # 航空煤油（新）
MOFCOM_LOOKBACK_DAYS = 90     # daily window to refresh

EIA_API = "https://api.eia.gov/v2/petroleum/pri/spt/data/"
EIA_SERIES = {
    "usgulf": "EER_EPJK_PF4_RGC_DPG",
    "brent": "RBRTE",
}
EIA_LOOKBACK_DAYS = 120       # daily window to refresh

# FRED mirrors EIA RBRTE exactly but posts ~1 week earlier; used as the
# primary Brent source so the leading signal stays fresh. No API key needed.
FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DCOILBRENTEU&cosd=%s"
FRED_LOOKBACK_DAYS = 120

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
TIMEOUT = 60
RETRIES = 4


def _log(msg):
    print("[%s] %s" % (_dt.datetime.now(_dt.timezone.utc).strftime("%H:%M:%S"), msg))


def _http(url, data=None, headers=None, timeout=None, retries=None):
    """GET (or POST when data is given) with retries; returns bytes.

    Callers may shorten the retry budget via `timeout` / `retries` when a
    fast failure is preferable to a long wait (e.g. the optional FRED
    mirror, where we would rather fall back to EIA than burn minutes).
    """
    to = TIMEOUT if timeout is None else timeout
    tries = RETRIES if retries is None else retries
    hdrs = {"User-Agent": UA, "Accept": "*/*"}
    if headers:
        hdrs.update(headers)
    ctx = ssl.create_default_context()
    last_err = None
    for attempt in range(1, tries + 1):
        try:
            req = urllib.request.Request(url, data=data, headers=hdrs)
            with urllib.request.urlopen(req, timeout=to, context=ctx) as resp:
                return resp.read()
        except Exception as err:  # noqa: BLE001 - retry on any network error
            last_err = err
            _log("  attempt %d/%d failed: %s" % (attempt, tries, err))
            time.sleep(2 * attempt)
    raise RuntimeError("HTTP request failed after %d attempts: %s" % (tries, last_err))


# ----------------------------------------------------------------------------
# Source 1: MOPS (Ministry of Commerce)
# ----------------------------------------------------------------------------
def fetch_mops():
    """Return list of {"d": "YYYY-MM-DD", "p": float} sorted ascending."""
    end = _dt.date.today()
    start = end - _dt.timedelta(days=MOFCOM_LOOKBACK_DAYS)
    body = urllib.parse.urlencode({
        "seqno": MOFCOM_SEQNO,
        "startTime": start.strftime("%Y%m%d"),
        "endTime": end.strftime("%Y%m%d"),
        "pageNumber": "1",
        "pageSize": "200",
    }).encode("utf-8")
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Referer": "https://price.mofcom.gov.cn/",
    }
    raw = _http(MOFCOM_URL, data=body, headers=headers)
    payload = json.loads(raw.decode("utf-8", "replace"))
    rows = payload.get("rows") or []
    out = []
    for r in rows:
        try:
            d = "%s-%s-%s" % (r["yyyy"], r["mm"], r["dd"])
            p = float(r["price"])
            out.append({"d": d, "p": p})
        except (KeyError, TypeError, ValueError):
            continue
    out.sort(key=lambda x: x["d"])
    return out


# ----------------------------------------------------------------------------
# Source 2: EIA (US Gulf jet fuel + Brent)
# ----------------------------------------------------------------------------
def fetch_eia():
    """Return (usgulf, brent), each a list of {"d": "YYYY-MM-DD", "p": float} ascending."""
    api_key = os.environ.get("EIA_API_KEY", "").strip() or "DEMO_KEY"
    today = _dt.date.today()
    start = (today - _dt.timedelta(days=EIA_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    end = today.strftime("%Y-%m-%d")

    results = {"usgulf": [], "brent": []}
    for name, series in EIA_SERIES.items():
        params = [
            ("api_key", api_key),
            ("frequency", "daily"),
            ("data[0]", "value"),
            ("facets[series][]", series),
            ("sort[0][column]", "period"),
            ("sort[0][direction]", "asc"),
            ("start", start),
            ("end", end),
            ("length", "500"),
        ]
        url = EIA_API + "?" + urllib.parse.urlencode(params)
        raw = _http(url)
        payload = json.loads(raw.decode("utf-8", "replace"))
        rows = (payload.get("response") or {}).get("data") or []
        series_rows = []
        for it in rows:
            period = it.get("period")
            value = it.get("value")
            if not period or value in (None, ""):
                continue
            try:
                series_rows.append({"d": period, "p": float(value)})
            except (TypeError, ValueError):
                continue
        series_rows.sort(key=lambda x: x["d"])
        results[name] = series_rows
        _log("  EIA %s: %d daily points (latest %s)" % (
            name, len(series_rows),
            series_rows[-1]["d"] if series_rows else "-"))
    return results["usgulf"], results["brent"]


# ----------------------------------------------------------------------------
# Source 2b: FRED (Brent, primary; mirrors EIA RBRTE, posts earlier)
# ----------------------------------------------------------------------------
def fetch_fred_brent():
    """Return list of {"d": "YYYY-MM-DD", "p": float} ascending.

    Empty list on any unusable response (caller falls back to EIA).
    """
    start = (_dt.date.today() - _dt.timedelta(days=FRED_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    raw = _http(FRED_CSV % start, timeout=25, retries=2)
    text = raw.decode("utf-8", "replace")
    if "<html" in text[:200].lower():
        _log("  FRED returned non-CSV payload; treating as unavailable")
        return []
    out = []
    for line in text.splitlines()[1:]:
        parts = line.split(",")
        if len(parts) < 2:
            continue
        d, v = parts[0].strip(), parts[1].strip()
        if not d or v in ("", ".", "NaN", "null"):
            continue
        try:
            out.append({"d": d, "p": float(v)})
        except ValueError:
            continue
    out.sort(key=lambda x: x["d"])
    _log("  FRED brent: %d daily points (latest %s)" % (
        len(out), out[-1]["d"] if out else "-"))
    return out


# ----------------------------------------------------------------------------
# Merge + write
# ----------------------------------------------------------------------------
def load_existing():
    if not os.path.exists(DATA_FILE):
        return {"mops": [], "usgulf": [], "brent": []}
    try:
        with open(DATA_FILE, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
        return {
            "mops": data.get("mops") or [],
            "usgulf": data.get("usgulf") or [],
            "brent": data.get("brent") or [],
        }
    except Exception as err:  # noqa: BLE001
        _log("WARN: could not read existing data.json (%s); rebuilding" % err)
        return {"mops": [], "usgulf": [], "brent": []}


def merge_daily(old, new):
    idx = {r["d"]: r["p"] for r in old if isinstance(r, dict) and "d" in r}
    for r in new:
        idx[r["d"]] = r["p"]
    return [{"d": d, "p": idx[d]} for d in sorted(idx)]


def main():
    _log("Starting fuel data update")
    existing = load_existing()

    mops_ok = usgulf_ok = brent_ok = False
    brent_src = "none"
    mops_rows = []
    usgulf_rows = []
    brent_rows = []

    try:
        mops_rows = fetch_mops()
        mops_ok = bool(mops_rows)
        _log("MOPS fetched: %d daily points (latest %s)" % (
            len(mops_rows), mops_rows[-1]["d"] if mops_rows else "-"))
    except Exception as err:  # noqa: BLE001
        _log("ERROR fetching MOPS: %s" % err)

    eia_brent_rows = []
    try:
        usgulf_rows, eia_brent_rows = fetch_eia()
        usgulf_ok = bool(usgulf_rows)
        if eia_brent_rows:
            brent_rows = eia_brent_rows
            brent_ok = True
            brent_src = "eia"
    except Exception as err:  # noqa: BLE001
        _log("ERROR fetching EIA: %s" % err)

    try:
        fred_rows = fetch_fred_brent()
        if fred_rows:
            if eia_brent_rows:
                eia_idx = {r["d"]: r["p"] for r in eia_brent_rows}
                common = [r for r in fred_rows if r["d"] in eia_idx]
                if common:
                    c = common[-1]
                    if abs(c["p"] - eia_idx[c["d"]]) > 0.01:
                        _log("WARN: FRED/EIA divergence at %s (fred=%.2f eia=%.2f); preferring EIA" % (
                            c["d"], c["p"], eia_idx[c["d"]]))
                        fred_rows = []
            if fred_rows:
                brent_rows = fred_rows
                brent_ok = True
                brent_src = "fred"
    except Exception as err:  # noqa: BLE001
        _log("WARN fetching FRED brent failed; keeping EIA brent: %s" % err)

    if not mops_ok and not usgulf_ok and not brent_ok:
        _log("FATAL: all sources failed; keeping existing data.json unchanged")
        return 1

    merged = {
        "mops": merge_daily(existing["mops"], mops_rows) if mops_ok else existing["mops"],
        "usgulf": merge_daily(existing["usgulf"], usgulf_rows) if usgulf_ok else existing["usgulf"],
        "brent": merge_daily(existing["brent"], brent_rows) if brent_ok else existing["brent"],
    }

    def _latest(series, key):
        return series[-1][key] if series else None

    out = {
        "meta": {
            "generated_at": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "generator": "scripts/update_data.py (GitHub Actions)",
            "mops_latest": _latest(merged["mops"], "d"),
            "usgulf_latest": _latest(merged["usgulf"], "d"),
            "brent_latest": _latest(merged["brent"], "d"),
            "brent_source": brent_src,
            "sources": {
                "mops": "https://price.mofcom.gov.cn/ (seqno=%s)" % MOFCOM_SEQNO,
                "usgulf": "https://www.eia.gov/ (%s, daily)" % EIA_SERIES["usgulf"],
                "brent": (
                    "https://fred.stlouisfed.org/ (DCOILBRENTEU, daily; mirrors EIA RBRTE)"
                    if brent_src == "fred" else
                    "https://www.eia.gov/ (%s, daily)" % EIA_SERIES["brent"]
                ),
            },
            "partial": (not mops_ok) or (not usgulf_ok) or (not brent_ok),
        },
        "mops": merged["mops"],
        "usgulf": merged["usgulf"],
        "brent": merged["brent"],
    }

    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, separators=(",", ":"))
        fh.write("\n")
    os.replace(tmp, DATA_FILE)

    _log("WROTE data.json | mops=%d usgulf=%d brent=%d | latest mops=%s usgulf=%s brent=%s" % (
        len(out["mops"]), len(out["usgulf"]), len(out["brent"]),
        out["meta"]["mops_latest"], out["meta"]["usgulf_latest"], out["meta"]["brent_latest"]))
    if out["meta"]["partial"]:
        _log("NOTE: partial update (mops_ok=%s usgulf_ok=%s brent_ok=%s brent_src=%s)" % (
            mops_ok, usgulf_ok, brent_ok, brent_src))
    return 0


if __name__ == "__main__":
    sys.exit(main())
