"""Build the director dashboard from raw Ragic JSON.

  py -I build.py <raw_dir> <history_dir> <out_html>

* Aggregates raw records into a name-free daily snapshot.
* Stores the snapshot in <history_dir> (encrypted when DATA_KEY / PAGE_PASSWORD is set,
  so the public repository never holds readable company data).
* Compares with the previous snapshot and renders template.html -> <out_html>.
"""
import base64
import datetime as dt
import hashlib
import hmac
import json
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

HORIZON = 9  # current week + 8 weeks ahead
STATUS_ORDER = ["Yes", "Workshop", "Scheduled", "Home"]
INTERNAL = {"HOME", "Migration"}  # bookkeeping "projects", shown separately


# ---------- helpers ----------

def num(v, default=0.0):
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return default


def ragic_date(s):
    s = (s or "").strip()
    for fmt in ("%Y/%m/%d", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            pass
    return ""


def week_val(v):
    """parseInt-like week number; 53 counts as 0 (same as the Ragic Balance JS)."""
    m = re.match(r"\s*(\d+)", str(v or ""))
    n = int(m.group(1)) if m else 0
    return 0 if n == 53 else n


def year_of(v):
    m = re.search(r"\d{4}", str(v or ""))
    return m.group(0) if m else ""


def staffing_balance(rec, year):
    """Port of the Ragic 'Balance' action (balance(id) JS on planing/3).

    Returns {position: (required[52], available[52])} for the given year.
    """
    required = {}
    for row in (rec.get("_subtable_1003520") or {}).values():
        pos = (row.get("Position") or "").strip()
        if year_of(row.get("Year")) != year or not pos:
            continue
        required[pos] = [week_val(row.get(str(w))) for w in range(1, 53)]

    available = {}
    for row in (rec.get("_subtable_1003224") or {}).values():
        pos = (row.get("Position") or "").strip()
        status = (row.get("At work") or "").strip()
        if year_of(row.get("Year")) != year or not pos or status in ("Terminate", "Workshop"):
            continue
        dep, ariv, read = (week_val(row.get(k)) for k in ("Dep week", "Ariv week", "Read week"))
        avail = available.setdefault(pos, [0] * 52)
        for w in range(1, 53):
            work = False
            if status == "Yes" and dep > 0 and w >= dep:
                work = True
            elif status == "Scheduled" and dep > 0 and w >= dep:
                work = True
            elif status == "Scheduled" and dep == 0 and read > 0 and w >= read:
                work = True
            if ariv and w >= ariv and (not dep or w < dep):
                work = False
            if work:
                avail[w - 1] += 1

    return {pos: (required.get(pos, [0] * 52), available.get(pos, [0] * 52))
            for pos in list(required) + [p for p in available if p not in required]}


# ---------- history storage ----------

def _fernet():
    secret = os.environ.get("DATA_KEY") or os.environ.get("PAGE_PASSWORD")
    if not secret:
        return None
    from cryptography.fernet import Fernet
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()))


def load_history(hdir):
    f = _fernet()
    snaps = []
    for p in sorted(hdir.glob("*.json*")):
        try:
            raw = p.read_bytes()
            if p.suffix == ".enc":
                if not f:
                    continue
                raw = f.decrypt(raw)
            snaps.append(json.loads(raw.decode("utf-8")))
        except Exception as exc:
            print(f"skip history file {p.name}: {exc}", file=sys.stderr)
    return snaps


def save_snapshot(hdir, snap):
    hdir.mkdir(parents=True, exist_ok=True)
    f = _fernet()
    data = json.dumps(snap, ensure_ascii=False).encode("utf-8")
    if f:
        (hdir / f"{snap['date']}.json.enc").write_bytes(f.encrypt(data))
    else:
        (hdir / f"{snap['date']}.json").write_bytes(data)


# ---------- aggregation ----------

def site_of(project, names):
    """'NL-SPG-716' -> 'SPG' when SPG is itself a project (a yard running several projects)."""
    parts = project.split("-")
    if len(parts) >= 3 and parts[1] in names and parts[1] != project:
        return parts[1]
    return project


MAX_DAY, MAX_WEEK, MAX_AVG16 = 12, 60, 48   # NL Working Hours Act limits
MISSING_DAYS = 2                             # working days (Mon-Sat) on site without booked hours


def person_name(p):
    return ((p.get("First, Last names") or p.get("First&#44; Last names") or "").strip())


def worker_code(position, key):
    """Stable pseudonym such as 'Welder · 3F9A' (keyed hash, the name cannot be recovered)."""
    secret = (os.environ.get("DATA_KEY") or os.environ.get("PAGE_PASSWORD") or "local-preview").encode()
    tag = hmac.new(secret, str(key).encode(), hashlib.sha256).hexdigest()[:4].upper()
    return f"{position or 'Worker'} · {tag}"


def working_days_between(after, until):
    """Mon-Sat days in (after, until]."""
    n, d = 0, after + dt.timedelta(days=1)
    while d <= until:
        n += d.weekday() != 6
        d += dt.timedelta(days=1)
    return n


def aggregate_workers(people, rows, today):
    """Per-worker hours and flags. Names are used only to join rows to people and are not output."""
    yesterday = today - dt.timedelta(days=1)
    d7, d28, d112 = (today - dt.timedelta(days=n) for n in (7, 28, 112))
    weeks4 = [(today - dt.timedelta(weeks=i)).isocalendar()[:2] for i in range(3, -1, -1)]

    daily = defaultdict(lambda: defaultdict(float))  # name -> date -> hours
    total = defaultdict(float)
    for row in rows:
        if len(row) < 6 or not row[5]:
            continue
        day, hrs = ragic_date(row[4]), num(row[3])
        if not day or not hrs or day > today.isoformat():
            continue
        name = row[5].strip()
        total[name] += hrs
        if day > d112.isoformat():
            daily[name][day] += hrs

    by_name = {person_name(p): p for p in people if person_name(p)}
    out = []
    for name in set(by_name) | {n for n, days in daily.items() if any(d > d28.isoformat() for d in days)}:
        p = by_name.get(name, {})
        status = (p.get("At work") or "").strip() or ("—" if p else "Not in active HR")
        if status == "Terminate":
            continue
        pos = (p.get("Position") or "").strip()
        days = {dt.date.fromisoformat(k): v for k, v in daily.get(name, {}).items()}
        week_h = defaultdict(float)
        for d, v in days.items():
            week_h[d.isocalendar()[:2]] += v
        last = max(days) if days else None
        recent = {d: v for d, v in days.items() if d > d28}
        w = {
            "code": worker_code(pos, p.get("_ragicId") or name),
            "rid": p.get("_ragicId"),
            "position": pos or "—",
            "project": (p.get("Project") or "").strip() or "(unassigned)",
            "status": status,
            "sub": (p.get("Subcontractor") or "") == "Yes",
            "weeks": [round(week_h.get(k, 0)) for k in weeks4],
            "h7": round(sum(v for d, v in days.items() if d > d7)),
            "h28": round(sum(recent.values())),
            "days28": len(recent),
            "max_day": round(max(recent.values()), 1) if recent else 0,
            "days_over": sum(1 for v in recent.values() if v > MAX_DAY),
            "weeks_over": sum(1 for v in week_h.values() if v > MAX_WEEK),
            "avg16": round(sum(days.values()) / 16, 1),
            "total": round(total.get(name, 0)),
            "last": last.isoformat() if last else "",
        }
        flags = []
        if status == "Yes":
            gap = working_days_between(last, yesterday) if last else None
            w["missing"] = gap
            if gap is None or gap >= MISSING_DAYS:
                flags.append("missing")
        if w["days_over"]:
            flags.append("over12")
        if w["weeks_over"]:
            flags.append("over60")
        if w["avg16"] > MAX_AVG16:
            flags.append("avg48")
        w["flags"] = flags
        out.append(w)
    out.sort(key=lambda w: (not w["flags"], w["project"], w["position"], w["code"]))
    return {"weeks": [f"W{wk}" for _, wk in weeks4], "workers": out,
            "flag_counts": dict(Counter(f for w in out for f in w["flags"]))}


def aggregate_hours(rows, today, names):
    """Sum 'Hours real' from Task list sub by site -> sub-project and by position (all time, up to today)."""
    today_s, week_ago = today.isoformat(), (today - dt.timedelta(days=7)).isoformat()
    groups = {}
    total = last7 = 0.0
    for row in rows:
        sub, proj_t, pos, h, day = row[:5]
        day = ragic_date(day)
        hrs = num(h)
        if not day or day > today_s or not hrs:
            continue  # future-dated or empty task rows
        proj = (sub or "").strip() or (proj_t or "").strip() or "(no project)"
        site = site_of(proj, names)
        recent = day > week_ago
        g = groups.setdefault(site, {"name": site, "total": 0.0, "last7": 0.0, "first": day, "last": day,
                                     "subs": defaultdict(lambda: [0.0, 0.0]), "positions": defaultdict(float)})
        g["total"] += hrs
        g["first"], g["last"] = min(g["first"], day), max(g["last"], day)
        g["subs"][proj][0] += hrs
        g["positions"][(pos or "").strip() or "Other"] += hrs
        total += hrs
        if recent:
            g["last7"] += hrs
            g["subs"][proj][1] += hrs
            last7 += hrs
    out = []
    for g in sorted(groups.values(), key=lambda g: -g["total"]):
        out.append({"name": g["name"], "total": round(g["total"]), "last7": round(g["last7"]),
                    "first": g["first"], "last": g["last"],
                    "subs": [{"name": k, "total": round(v[0]), "last7": round(v[1])}
                             for k, v in sorted(g["subs"].items(), key=lambda kv: -kv[1][0])],
                    "positions": {k: round(v) for k, v in sorted(g["positions"].items(), key=lambda kv: -kv[1])}})
    by_name = {}
    for g in out:
        by_name[g["name"]] = {"total": g["total"], "last7": g["last7"]}
        for s in g["subs"]:
            if s["name"] != g["name"]:
                by_name.setdefault(s["name"], {"total": s["total"], "last7": s["last7"]})
    return {"groups": out, "by_name": by_name, "total": round(total), "last7": round(last7)}


def build_snapshot(raw, today):
    year, week, _ = today.isocalendar()
    weeks = [w for w in range(week, week + HORIZON) if w <= 52]

    # People (status only, no names)
    people = [p for p in raw["people"] if (p.get("Active") or "").strip() == "Yes"]
    by_project = defaultdict(Counter)
    pos_onsite = defaultdict(Counter)
    by_position = defaultdict(Counter)
    status_total = Counter()
    own = sub = 0
    for p in people:
        st = (p.get("At work") or "").strip()
        if st == "Terminate":
            continue
        st = st if st in STATUS_ORDER else "Other"
        proj = (p.get("Project") or "").strip() or "(unassigned)"
        pos = (p.get("Position") or "").strip() or "Other"
        by_project[proj][st] += 1
        by_position[pos][st] += 1
        status_total[st] += 1
        if st == "Yes":
            pos_onsite[proj][pos] += 1
        if (p.get("Subcontractor") or "").strip() == "Yes":
            sub += 1
        else:
            own += 1

    # Projects + staffing plan
    projects = []
    seen = set()
    for r in raw["projects"]:
        name = (r.get("Project") or "").strip()
        if not name or (r.get("Active") or "Yes") != "Yes":
            continue
        seen.add(name)
        staffing = None
        by_pos = []
        plan = staffing_balance(r, str(year))
        if not any(any(req) for req, _ in plan.values()):
            plan = {}  # no staffing plan for this year -> nothing to compare against
        for pos, (req52, avail52) in plan.items():
            req = [req52[w - 1] for w in weeks]
            bal = [avail52[w - 1] - req52[w - 1] for w in weeks]
            if any(req) or any(bal):
                by_pos.append({"position": pos, "required": req, "balance": bal})
        if by_pos:
            by_pos.sort(key=lambda x: -max(x["required"] or [0]))
            staffing = {
                "required": [sum(p["required"][i] for p in by_pos) for i in range(len(weeks))],
                "balance": [sum(p["balance"][i] for p in by_pos) for i in range(len(weeks))],
                "short": [sum(min(0, p["balance"][i]) for p in by_pos) for i in range(len(weeks))],
                "by_position": by_pos,
            }
        status = dict(by_project.get(name, Counter()))
        projects.append({
            "name": name,
            "country": (r.get("Loc") or "").strip(),
            "start": ragic_date(r.get("Start date")),
            "end": ragic_date(r.get("End date")),
            "used_hours": round(num(r.get("Used hours"))),
            "internal": name in INTERNAL,
            "status": status,
            "onsite": status.get("Yes", 0),
            "active": sum(status.values()),
            "positions_onsite": dict(pos_onsite.get(name, Counter()).most_common()),
            "staffing": staffing,
        })
    # people assigned to projects that are not in the active project list
    for name, st in by_project.items():
        if name not in seen:
            projects.append({"name": name, "country": "", "start": "", "end": "", "used_hours": 0,
                             "internal": True, "status": dict(st), "onsite": st.get("Yes", 0),
                             "active": sum(st.values()), "positions_onsite": dict(pos_onsite.get(name, Counter())),
                             "staffing": None, "not_in_project_list": True})
    projects.sort(key=lambda p: (p["internal"], -p["active"], p["name"]))

    # Additional manpower requests (current year, last 4 weeks and ahead)
    requests = []
    for r in raw["requests"]:
        y, w = int(num(r.get("Year"))), int(num(r.get("Week")))
        if y != year or w < week - 4:
            continue
        req_n, filled = int(num(r.get("Number"))), max(0, int(num(r.get("People"))))
        requests.append({"project": (r.get("Project") or "").strip(), "position": (r.get("Position") or "").strip(),
                         "week": w, "date": ragic_date(r.get("Date")), "requested": req_n, "filled": filled,
                         "gap": max(0, req_n - filled)})
    requests.sort(key=lambda r: (r["week"], r["project"]))

    hours = aggregate_hours(raw.get("hours") or [], today, {p["name"] for p in projects})
    workers = aggregate_workers(people, raw.get("hours") or [], today)
    for p in projects:
        g = hours["by_name"].get(p["name"])
        p["hours"] = g  # {"total", "last7"} for a site (incl. its sub-projects) or a single project

    # company-wide staffing balance per position (current week)
    pos_balance = Counter()
    for p in projects:
        for row in (p["staffing"] or {}).get("by_position", []):
            pos_balance[row["position"]] += row["balance"][0] if row["balance"] else 0

    return {
        "date": today.isoformat(),
        "generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes"),
        "year": year, "week": week, "weeks": weeks,
        "week_starts": [dt.date.fromisocalendar(year, w, 1).isoformat() for w in weeks],
        "projects": projects,
        "workforce": {"total": sum(status_total.values()), "status": dict(status_total), "own": own, "sub": sub,
                      "by_position": {k: dict(v) for k, v in sorted(by_position.items(), key=lambda kv: -sum(kv[1].values()))}},
        "position_balance_now": dict(sorted(pos_balance.items(), key=lambda kv: kv[1])),
        "requests": requests,
        "hours": hours["groups"],
        "hours_total": hours["total"],
        "hours_last7": hours["last7"],
        "workers": workers,
        "ragic_people_url": f"{os.environ.get('RAGIC_BASE', 'https://eu4.ragic.com').rstrip('/')}/"
                            f"{os.environ.get('RAGIC_ACCOUNT', 'SIDC10000')}/planing/19/",
    }


# ---------- day-over-day changes ----------

def diff(prev, cur):
    if not prev:
        return {"since": None, "items": []}
    items = []

    def add(kind, text, delta=0):
        items.append({"kind": kind, "text": text, "delta": delta})

    pw, cw = prev["workforce"], cur["workforce"]
    d = cw["status"].get("Yes", 0) - pw["status"].get("Yes", 0)
    if d:
        add("people", f"People on site: {pw['status'].get('Yes', 0)} → {cw['status'].get('Yes', 0)}", d)
    d = cw["total"] - pw["total"]
    if d:
        add("people", f"Active workforce: {pw['total']} → {cw['total']}", d)

    pp = {p["name"]: p for p in prev["projects"]}
    cp = {p["name"]: p for p in cur["projects"]}
    for name in cp:
        if name not in pp and not cp[name].get("not_in_project_list"):
            add("project", f"New active project: {name}", 1)
    for name in pp:
        if name not in cp and not pp[name].get("not_in_project_list"):
            add("project", f"Project no longer active: {name}", -1)
    for name, p in cp.items():
        q = pp.get(name)
        if not q:
            continue
        if p["onsite"] != q["onsite"]:
            add("people", f"{name}: on site {q['onsite']} → {p['onsite']}", p["onsite"] - q["onsite"])
        # staffing balance for the current week
        def bal(s, snap):
            if not s:
                return None
            weeks = snap["weeks"]
            return s["balance"][weeks.index(cur["week"])] if cur["week"] in weeks else None
        b0, b1 = bal(q["staffing"], prev), bal(p["staffing"], cur)
        if b0 is not None and b1 is not None and b0 != b1:
            add("staffing", f"{name}: staffing balance W{cur['week']} {b0:+d} → {b1:+d}", b1 - b0)

    key = lambda r: (r["project"], r["position"], r["week"], r["date"])
    prev_req = {key(r): r for r in prev["requests"]}
    for r in cur["requests"]:
        old = prev_req.get(key(r))
        if not old:
            add("request", f"New manpower request: {r['project']} – {r['requested']}× {r['position']} (W{r['week']})", r["requested"])
        elif old["filled"] != r["filled"]:
            add("request", f"Request filled {old['filled']} → {r['filled']} of {r['requested']}: {r['project']} {r['position']} (W{r['week']})",
                r["filled"] - old["filled"])

    labels = {"missing": "Workers on site without booked hours", "over12": "Workers with days over 12 h",
              "over60": "Workers with weeks over 60 h", "avg48": "Workers averaging over 48 h/week"}
    pf = (prev.get("workers") or {}).get("flag_counts")
    if pf is not None:
        cf = cur["workers"]["flag_counts"]
        for k, label in labels.items():
            if cf.get(k, 0) != pf.get(k, 0):
                add("hours", f"{label}: {pf.get(k, 0)} → {cf.get(k, 0)}", cf.get(k, 0) - pf.get(k, 0))
    return {"since": prev["date"], "items": items}


def trend(history, cur):
    series = {}
    for s in history + [cur]:
        series[s["date"]] = {"onsite": s["workforce"]["status"].get("Yes", 0), "active": s["workforce"]["total"],
                             "projects": {p["name"]: p["onsite"] for p in s["projects"]}}
    return [{"date": d, **v} for d, v in sorted(series.items())][-120:]


def main():
    raw_dir, hist_dir, out_html = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
    raw = {n: json.loads((raw_dir / f"{n}.json").read_text(encoding="utf-8"))
           for n in ("projects", "people", "requests", "hours") if (raw_dir / f"{n}.json").exists()}
    today = dt.date.fromisoformat(os.environ["BUILD_DATE"]) if os.environ.get("BUILD_DATE") else dt.date.today()
    snap = build_snapshot(raw, today)
    history = [h for h in load_history(hist_dir) if h["date"] < snap["date"]]
    prev = history[-1] if history else None
    # per-worker rows are only needed for today's page; history keeps the flag counts
    save_snapshot(hist_dir, dict(snap, workers={"flag_counts": snap["workers"]["flag_counts"]}))

    page = dict(snap)
    page["changes"] = diff(prev, snap)
    page["trend"] = trend(history, snap)
    template = (Path(__file__).parent / "template.html").read_text(encoding="utf-8")
    payload = json.dumps(page, ensure_ascii=False).replace("</", "<\\/")
    out_html.parent.mkdir(parents=True, exist_ok=True)
    out_html.write_text(template.replace("/*__DATA__*/null", payload), encoding="utf-8")
    print(f"built {out_html} for {snap['date']} (W{snap['week']}); previous snapshot: {prev['date'] if prev else 'none'}")


if __name__ == "__main__":
    main()
