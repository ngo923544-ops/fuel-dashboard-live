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
     Source: U.S. EIA Open Data API v2
     Series: EER_EPJK_PF4_RGC_DPG (US Gulf kerosene jet), RBRTE (Brent spot)

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

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
TIMEOUT = 60
RETRIES = 4


def _log(msg):
    print("[%s] %s" % (_dt.datetime.now(_dt.timezone.utc).strftime("%H:%M:%S"), msg))


def _http(url, data=None, headers=None):
    """GET (or POST when data is given) with retries; returns bytes."""
    hdrs = {"User-Agent": UA, "Accept": "*/*"}
    if headers:
        hdrs.update(headers)
    ctx = ssl.create_default_context()
    last_err = None
    for attempt in range(1, RETRIES + 1):
        try:
            req = urllib.request.Request(url, data=data, headers=hdrs)
            with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as resp:
                return resp.read()
        except Exception as err:  # noqa: BLE001 - retry on any network error
            last_err = err
            _log("  attempt %d/%d failed: %s" % (attempt, RETRIES, err))
            time.sleep(2 * attempt)
    raise RuntimeError("HTTP request failed after %d attempts: %s" % (RETRIES, last_err))


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

    mops_ok = eia_ok = False
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

    try:
        usgulf_rows, brent_rows = fetch_eia()
        eia_ok = bool(usgulf_rows) and bool(brent_rows)
    except Exception as err:  # noqa: BLE001
        _log("ERROR fetching EIA: %s" % err)

    if not mops_ok and not eia_ok:
        _log("FATAL: both sources failed; keeping existing data.json unchanged")
        return 1

    merged = {
        "mops": merge_daily(existing["mops"], mops_rows) if mops_ok else existing["mops"],
        "usgulf": merge_daily(existing["usgulf"], usgulf_rows) if eia_ok else existing["usgulf"],
        "brent": merge_daily(existing["brent"], brent_rows) if eia_ok else existing["brent"],
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
            "sources": {
                "mops": "https://price.mofcom.gov.cn/ (seqno=%s)" % MOFCOM_SEQNO,
                "usgulf": "https://www.eia.gov/ (%s, daily)" % EIA_SERIES["usgulf"],
                "brent": "https://www.eia.gov/ (%s, daily)" % EIA_SERIES["brent"],
            },
            "partial": (not mops_ok) or (not eia_ok),
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
        _log("NOTE: partial update (mops_ok=%s eia_ok=%s)" % (mops_ok, eia_ok))
    return 0


if __name__ == "__main__":
    sys.exit(main())
