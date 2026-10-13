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
    # Projects with the staffing subtables the Ragic "Balance" JS uses:
    # 1003224 = people on the project (week of departure/arrival/ready), 1003520 = required per position/week.
    "projects": {
        "path": "planing/3",
        "params": {"subtables": "1", "where": "1009297,eq,Yes"},
        "keep": ["Project", "Active", "Loc", "Start date", "End date", "Used hours", "Year",
                 "_subtable_1003224", "_subtable_1003520"],
        "sub_keep": {"_subtable_1003224": ["Year", "Position", "At work", "Dep week", "Ariv week", "Read week"]},
    },
    # People: only status fields, no personal data is requested
    "people": {
        "path": "planing/19",
        "params": {"subtables": "0", "where": "1001432,eq,Yes",
                   "fetchDomainIds": ["1001432", "1001370", "1000119", "1000764", "1009378", "1000917"]},
        "keep": ["Active", "Project", "Position", "At work", "Subcontractor", "Type"],
    },
    # Booked hours (Task list sub): one row per worker/day/task. Only the fields needed for sums.
    "hours": {
        "path": "sub/20",
        "params": {"subtables": "0",
                   "fetchDomainIds": ["1003457", "1009449", "1009445", "1000166", "1000180"]},
        "keep": ["Project-sub", "Project-T", "Position", "Hours real", "Data task sub"],
        "compact": True,
    },
    # Additional manpower requests
    "requests": {
        "path": "planing/52",
        "params": {"subtables": "0"},
        "keep": ["Project", "Year", "Position", "Date", "Number", "People", "Week", "Status"],
    },
}


def trim_record(rec, keep, sub_keep=None):
    """Keep only the listed fields; subtable rows can be trimmed too (drops names)."""
    if not keep:
        return rec
    out = {k: rec.get(k) for k in keep if k in rec}
    for sub, fields in (sub_keep or {}).items():
        if isinstance(out.get(sub), dict):
            out[sub] = {rid: {f: row.get(f) for f in fields if f in row} for rid, row in out[sub].items()}
    return out


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
        for r in page:
            r = trim_record(r, cfg.get("keep"), cfg.get("sub_keep"))
            # large sheets: store rows as lists in "keep" order to keep the raw file small
            records.append([r.get(k) for k in cfg["keep"]] if cfg.get("compact") else r)
        if len(page) < limit:
            return records
        offset += limit
        if offset % 20000 == 0:
            print(f"  {cfg['path']}: {offset} records so far", flush=True)


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
        sample_keys = cfg["keep"] if cfg.get("compact") else (sorted(recs[0].keys()) if recs else [])
        print(f"{name}: {len(recs)} records; fields: {sample_keys}")


if __name__ == "__main__":
    main()
