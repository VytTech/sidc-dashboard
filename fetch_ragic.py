"""Fetch the Ragic sheets the dashboard needs and save them as raw JSON lists.

Environment:
  RAGIC_API_KEY  - API key of a read-only Ragic service account (required)
  RAGIC_BASE     - server URL, e.g. https://www.ragic.com (default)
  RAGIC_ACCOUNT  - Ragic account / apname (default SIDC10000)

Raw files never leave the build machine: only aggregated, name-free numbers
end up in the published page (see build.py).
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

# Field ids are listed for documentation and server-side filtering; records are
# returned keyed by field label, which is what build.py reads.
SHEETS = {
    # Projects with staffing subtables (_subtable_1016490 = required(+/-balance) per position/week)
    "projects": {
        "path": "planing/3",
        "params": {"subtables": "1", "where": "1009297,eq,Yes"},
        "keep": ["Project", "Active", "Loc", "Start date", "End date", "Used hours",
                 "_subtable_1016490"],
    },
    # People: only status fields, no personal data is requested
    "people": {
        "path": "planing/19",
        "params": {"subtables": "0", "where": "1001432,eq,Yes",
                   "fetchDomainIds": ["1001432", "1001370", "1000119", "1000764", "1009378", "1000917"]},
        "keep": ["Active", "Project", "Position", "At work", "Subcontractor", "Type"],
    },
    # Additional manpower requests
    "requests": {
        "path": "planing/52",
        "params": {"subtables": "0"},
        "keep": ["Project", "Year", "Position", "Date", "Number", "People", "Week", "Status"],
    },
}


def trim_record(rec, keep):
    if not keep:
        return rec
    return {k: rec.get(k) for k in keep if k in rec}


def fetch_sheet(base, account, key, cfg):
    records, offset, limit = [], 0, 1000
    while True:
        params = [("api", ""), ("v", "3"), ("APIKey", key), ("limit", str(limit)), ("offset", str(offset))]
        for k, v in cfg["params"].items():
            for item in (v if isinstance(v, list) else [v]):
                params.append((k, item))
        url = f"{base}/{account}/{cfg['path']}?" + urllib.parse.urlencode(params)
        for attempt in range(4):
            try:
                with urllib.request.urlopen(url, timeout=120) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                break
            except Exception as exc:  # network hiccup: retry with backoff
                if attempt == 3:
                    raise
                print(f"  retry {cfg['path']} ({exc})", file=sys.stderr)
                time.sleep(5 * (attempt + 1))
        if isinstance(data, dict) and "status" in data and data.get("status") != "SUCCESS" and not any(
                k.isdigit() for k in data):
            raise RuntimeError(f"Ragic error for {cfg['path']}: {str(data)[:300]}")
        page = [r for k, r in data.items() if k.isdigit()]
        records.extend(trim_record(r, cfg.get("keep")) for r in page)
        if len(page) < limit:
            return records
        offset += limit


def main():
    key = os.environ.get("RAGIC_API_KEY")
    if not key:
        sys.exit("RAGIC_API_KEY is not set")
    base = os.environ.get("RAGIC_BASE", "https://www.ragic.com").rstrip("/")
    account = os.environ.get("RAGIC_ACCOUNT", "SIDC10000")
    out_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "raw")
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, cfg in SHEETS.items():
        recs = fetch_sheet(base, account, key, cfg)
        (out_dir / f"{name}.json").write_text(json.dumps(recs, ensure_ascii=False), encoding="utf-8")
        sample_keys = sorted(recs[0].keys()) if recs else []
        print(f"{name}: {len(recs)} records; fields: {sample_keys}")


if __name__ == "__main__":
    main()
