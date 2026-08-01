#!/usr/bin/env python3
"""
Pulls the latest readings for The Saver's Almanac from FRED and rewrites
the hardcoded values inside index.html in place. Designed to be run daily
by a GitHub Action.

Data source:
- Preferred: FRED's official API (api.stlouisfed.org), using a free API
  key stored in the FRED_API_KEY repo secret. This is what FRED actually
  wants automated/CI traffic to use, and is far more reliable from shared
  cloud IP ranges (like GitHub Actions runners) than the CSV endpoint.
- Fallback: the public fredgraph.csv download endpoint, used only if no
  FRED_API_KEY is set. This endpoint is meant for interactive browser use
  and has been observed to time out or get throttled from GitHub Actions'
  IP ranges, so it's a degraded-mode fallback, not the primary path.

How it works:
- Each data point in index.html is tagged with a marker comment right after
  its `value:` field, e.g.  value:4.48 }, /*@id:DGS10*/
- This script fetches the relevant FRED series, finds the latest
  non-blank observation, and replaces that number in the HTML.
- For the two YoY (year-over-year) series (headline CPI, core PCE), it
  computes the % change from the index level 12 months prior rather than
  reading a level directly.
- The file is only rewritten if something actually changed.
"""

import re
import sys
import time
import urllib.request
import urllib.error
import datetime
import csv
import io
import json
import os

HTML_PATH = "index.html"
STATE_PATH = "data/zone-state.json"
ALERTS_PATH = "data/alerts.json"

FRED_API_KEY = os.environ.get("FRED_API_KEY", "").strip()
FRED_API_URL = (
    "https://api.stlouisfed.org/fred/series/observations"
    "?series_id={series}&api_key={key}&file_type=json"
    "&sort_order=asc&observation_start={start}"
)
FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}"

MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 5
REQUEST_TIMEOUT = 45

# Simple level series: just take the latest non-blank observation.
LEVEL_SERIES = {
    "DGS10": "DGS10",      # US 10-Year Treasury (constant maturity)
    "T10YIE": "T10YIE",    # 10-Yr TIPS Breakeven inflation rate
    "SOFR": "SOFR",        # Secured Overnight Financing Rate
    "DFF": "DFF",          # Effective Federal Funds Rate (daily)
}

# YoY series: computed as (latest / value ~12mo ago - 1) * 100 from a price index.
YOY_SERIES = {
    "CPI_YOY": "CPIAUCSL",       # headline CPI index -> YoY %
    "PCE_CORE_YOY": "PCEPILFE",  # core PCE price index -> YoY %
}

# Which markers also get their trailing "Mon D, YYYY" date in `meta` updated.
# (Skipped for DFF since its meta describes the FOMC target range, not a
# daily observation date, and for the YoY ids since their meta describes
# the reporting month / release date, which needs more care than a simple
# date swap.)
UPDATE_META_DATE = {"DGS10", "T10YIE", "SOFR"}

# The three duration-matched pairs shown on the site, and the series ids
# that feed each side of the equation. Keep in sync with index.html.
PAIRS = {
    "10y":   {"label": "10-Year Look",            "nom": "DGS10", "inf": "T10YIE"},
    "short": {"label": "Short-Term Look",          "nom": "SOFR",  "inf": "CPI_YOY"},
    "fed":   {"label": "Fed Policy & Liquidity",   "nom": "DFF",   "inf": "PCE_CORE_YOY"},
}

# Same boundaries as the gauge in index.html. Order matters: first match wins.
ZONES = [
    (-2.0, "Borrowers' Market (strong)"),
    (-0.5, "Borrowers' Market"),
    (0.5, "Neutral"),
    (2.0, "Savers' Market"),
    (float("inf"), "Savers' Market (strong)"),
]


def classify_zone(real_rate):
    for upper, name in ZONES:
        if real_rate <= upper:
            return name
    return ZONES[-1][1]


def _with_retries(fn, series_id):
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return fn(series_id)
        except Exception as e:
            last_err = e
            print(
                f"WARN: attempt {attempt}/{MAX_RETRIES} failed for {series_id}: {e}",
                file=sys.stderr,
            )
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
    raise last_err


def fetch_api(series_id):
    start = (datetime.date.today() - datetime.timedelta(days=800)).isoformat()
    url = FRED_API_URL.format(series=series_id, key=FRED_API_KEY, start=start)
    req = urllib.request.Request(url, headers={"User-Agent": "saver-almanac-bot/1.0"})
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    out = []
    for obs in payload.get("observations", []):
        if obs.get("value") in (".", "", None):
            continue
        out.append((datetime.date.fromisoformat(obs["date"]), float(obs["value"])))
    return out


def fetch_csv(series_id):
    url = FRED_CSV.format(series=series_id)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        text = resp.read().decode("utf-8")
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    out = []
    for row in rows[1:]:
        if len(row) < 2 or row[1] == "." or row[1] == "":
            continue
        date = datetime.date.fromisoformat(row[0])
        out.append((date, float(row[1])))
    return out


def fetch_series(series_id):
    """Prefer the official API (needs FRED_API_KEY); fall back to CSV scrape."""
    if FRED_API_KEY:
        return _with_retries(fetch_api, series_id)
    print(
        f"WARN: FRED_API_KEY not set — falling back to CSV scrape for {series_id} "
        f"(less reliable from CI). Set the FRED_API_KEY repo secret to fix this.",
        file=sys.stderr,
    )
    return _with_retries(fetch_csv, series_id)


def latest_level(series_id):
    obs = fetch_series(series_id)
    if not obs:
        raise ValueError(f"No observations for {series_id}")
    date, value = obs[-1]
    return date, value


def latest_yoy(series_id):
    obs = fetch_series(series_id)
    if len(obs) < 13:
        raise ValueError(f"Not enough history for {series_id}")
    latest_date, latest_val = obs[-1]
    # find the observation closest to 12 months before latest_date
    target = latest_date.replace(year=latest_date.year - 1)
    prior = min(obs[:-1], key=lambda o: abs((o[0] - target).days))
    yoy = (latest_val / prior[1] - 1) * 100
    return latest_date, yoy


def fmt_date(d):
    return d.strftime("%b {}, %Y").format(d.day)


def load_state():
    if not os.path.exists(STATE_PATH):
        return {}
    with open(STATE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def send_alert(event):
    """
    Stub for the paid-tier zone-change alert. Called once per crossing with
    a dict like:
      {"pair": "10y", "label": "10-Year Look", "from_zone": "Neutral",
       "to_zone": "Savers' Market", "real_rate": 2.31, "date": "2026-07-09"}

    Nothing is wired up yet — this just appends to data/alerts.json so the
    crossing is logged and visible in git history / the Action's diff.
    When ready to actually notify people, the cheapest options are:
      - A Zapier/Make.com "Webhooks by Zapier" trigger: POST `event` as JSON
        to a webhook URL (store the URL in a GitHub secret, read via
        os.environ), then have Zapier fan it out to an email list or a
        Substack broadcast via their integrations.
      - A transactional email API (Postmark, SendGrid, Resend): send
        directly from this script to a stored subscriber list.
      - Posting to Twitter/X via their API, for a public (not paid-gated)
        version of the same alert.
    """
    existing = []
    if os.path.exists(ALERTS_PATH):
        with open(ALERTS_PATH, "r", encoding="utf-8") as f:
            existing = json.load(f)
    existing.append(event)
    os.makedirs(os.path.dirname(ALERTS_PATH), exist_ok=True)
    with open(ALERTS_PATH, "w", encoding="utf-8") as f:
        json.dump(existing, f, indent=2)
    print(f"ALERT: {event['label']} moved {event['from_zone']} -> {event['to_zone']}")

    webhook_url = os.environ.get("ALERT_WEBHOOK_URL")
    if webhook_url:
        try:
            req = urllib.request.Request(
                webhook_url,
                data=json.dumps(event).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=15)
        except Exception as e:
            print(f"WARN: failed to POST alert webhook: {e}", file=sys.stderr)


def main():
    with open(HTML_PATH, "r", encoding="utf-8") as f:
        html = f.read()

    lines = html.split("\n")
    changed = False
    today_str = fmt_date(datetime.date.today())
    values = {}  # series id -> latest numeric value, collected for zone tracking

    for sid in LEVEL_SERIES:
        try:
            date, value = latest_level(sid)
        except Exception as e:
            print(f"WARN: failed to fetch {sid}: {e}", file=sys.stderr)
            continue
        values[sid] = value
        marker = f"/*@id:{sid}*/"
        date_str = fmt_date(date)
        found_marker = False
        for i, line in enumerate(lines):
            if marker not in line:
                continue
            found_marker = True
            new_line = re.sub(r"value:[\d.]+(?=\s*\},)", f"value:{value:.2f}", line)
            if sid in UPDATE_META_DATE:
                new_line = re.sub(
                    r"([A-Za-z]{3} \d{1,2}, \d{4})(?=')", date_str, new_line
                )
            if new_line != line:
                print(f"CHANGED {sid}: fetched {value:.2f} ({date_str})")
                changed = True
                lines[i] = new_line
            else:
                print(f"UNCHANGED {sid}: fetched {value:.2f} ({date_str}), matches existing")
        if not found_marker:
            print(f"WARN: marker /*@id:{sid}*/ not found anywhere in {HTML_PATH} — check the file wasn't altered", file=sys.stderr)

    for sid in YOY_SERIES:
        try:
            date, value = latest_yoy(YOY_SERIES[sid])
        except Exception as e:
            print(f"WARN: failed to fetch {sid}: {e}", file=sys.stderr)
            continue
        values[sid] = value
        marker = f"/*@id:{sid}*/"
        found_marker = False
        for i, line in enumerate(lines):
            if marker not in line:
                continue
            found_marker = True
            new_line = re.sub(r"value:[\d.]+(?=\s*\},)", f"value:{value:.2f}", line)
            if new_line != line:
                print(f"CHANGED {sid}: fetched {value:.2f}")
                changed = True
                lines[i] = new_line
            else:
                print(f"UNCHANGED {sid}: fetched {value:.2f}, matches existing")
        if not found_marker:
            print(f"WARN: marker /*@id:{sid}*/ not found anywhere in {HTML_PATH} — check the file wasn't altered", file=sys.stderr)

    asof_marker_found = False
    for i, line in enumerate(lines):
        if "<!--@id:ASOF-->" in line:
            asof_marker_found = True
            new_line = re.sub(
                r"as of [A-Za-z]{3}&nbsp;\d{1,2},&nbsp;\d{4}",
                f"as of {today_str.replace(' ', '&nbsp;').replace(',', ',&nbsp;').replace('&nbsp;&nbsp;', '&nbsp;')}",
                line,
            )
            if new_line != line:
                print(f"CHANGED asof line to {today_str}")
                changed = True
                lines[i] = new_line
            else:
                print(f"UNCHANGED asof line (already {today_str} or regex didn't match)")
    if not asof_marker_found:
        print("WARN: <!--@id:ASOF--> marker not found anywhere in the file", file=sys.stderr)

    # ---- zone-change detection (foundation for the future paid-tier alert) ----
    state = load_state()
    today_iso = datetime.date.today().isoformat()
    for pair_id, pair in PAIRS.items():
        nom_val = values.get(pair["nom"])
        inf_val = values.get(pair["inf"])
        if nom_val is None or inf_val is None:
            continue  # one of the series failed to fetch today; skip rather than guess
        real_rate = round(nom_val - inf_val, 2)
        new_zone = classify_zone(real_rate)
        old_zone = state.get(pair_id, {}).get("zone")
        if old_zone is not None and old_zone != new_zone:
            send_alert({
                "pair": pair_id,
                "label": pair["label"],
                "from_zone": old_zone,
                "to_zone": new_zone,
                "real_rate": real_rate,
                "date": today_iso,
            })
        state[pair_id] = {"zone": new_zone, "real_rate": real_rate, "date": today_iso}
    save_state(state)

    if not changed:
        print("No changes.")
        return

    with open(HTML_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print("index.html updated.")


if __name__ == "__main__":
    main()
