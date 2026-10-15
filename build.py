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
TECHNICAL = {"HOME", "Migration", "Multiple location", "N/A2", "CBC"}  # hidden from project/hours/worker lists
INTERNAL_TIMESHEETS = {"Naujas cechas", "Kaminera", "Dubysa", "Laivytė"}  # own timesheet systems, not Ragic
UNASSIGNED = "__unassigned__"    # active people at home without a project = unassigned (available) resources


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


# ---------- management mode: labour cost only for holders of MGMT_PASSWORD ----------
# Cost figures are moved out of the page data into an encrypted block. Stdlib only, mirrored in
# template.html with WebCrypto: PBKDF2-SHA256 -> 64 bytes (32 encryption + 32 MAC key),
# keystream = HMAC-SHA256(enc_key, nonce || counter), tag = HMAC-SHA256(mac_key, salt || nonce || ciphertext).
COST_KEYS = {"cost", "cost30", "position_cost", "cost_total", "cost_last30", "cost_model"}
MGMT_ITERATIONS = 200_000


def split_costs(o):
    """Return (data without cost keys, the cost keys in the same shape or None)."""
    if isinstance(o, dict):
        pub, sec = {}, {}
        for k, v in o.items():
            if k in COST_KEYS:
                sec[k] = v
                continue
            pub[k], s = split_costs(v)
            if s is not None:
                sec[k] = s
        return pub, (sec or None)
    if isinstance(o, list):
        parts = [split_costs(v) for v in o]
        secs = [s for _, s in parts]
        return [p for p, _ in parts], (secs if any(s is not None for s in secs) else None)
    return o, None


def encrypt_costs(obj, password):
    salt, nonce = os.urandom(16), os.urandom(16)
    key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, MGMT_ITERATIONS, dklen=64)
    ek, mk = key[:32], key[32:]
    plain = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    stream = b"".join(hmac.new(ek, nonce + i.to_bytes(4, "big"), hashlib.sha256).digest()
                      for i in range((len(plain) + 31) // 32))
    ct = bytes(a ^ b for a, b in zip(plain, stream))
    tag = hmac.new(mk, salt + nonce + ct, hashlib.sha256).digest()
    b64 = lambda b: base64.b64encode(b).decode("ascii")
    return {"s": b64(salt), "n": b64(nonce), "c": b64(ct), "t": b64(tag), "i": MGMT_ITERATIONS}


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


def timesheet_index(rows, today, names):
    """Who booked where: yards with Ragic timesheets (8 weeks), yards per person in the last completed week,
    last booking date per person and yard (for manpower requests)."""
    this_monday = today - dt.timedelta(days=today.weekday())
    lw_start, lw_end = this_monday - dt.timedelta(days=7), this_monday - dt.timedelta(days=1)
    recent_from = (this_monday - dt.timedelta(days=56)).isoformat()
    yards_with_ts, last_week, emp_yard_last = set(), defaultdict(set), defaultdict(dict)
    for row in rows:
        day, hrs = ragic_date(row[4]), num(row[3])
        if not day or not hrs or day > today.isoformat():
            continue
        proj = (row[0] or "").strip() or (row[1] or "").strip()
        if not proj:
            continue
        yard = site_of(proj, names)
        if day >= recent_from:
            yards_with_ts.add(yard)
        emp = ((row[5] if len(row) > 5 else "") or "").strip()
        if not emp:
            continue
        if emp_yard_last[emp].get(yard, "") < day:
            emp_yard_last[emp][yard] = day
        if lw_start.isoformat() <= day <= lw_end.isoformat():
            last_week[emp].add(yard)
    return {"yards_with_ts": yards_with_ts, "last_week": last_week, "emp_yard_last": emp_yard_last,
            "last_week_no": lw_start.isocalendar()[1]}


def aggregate_workers(people, rows, today, ts=None, names=frozenset()):
    """Per-worker hours and flags. Names are used only to join rows to people and are not output."""
    yesterday = today - dt.timedelta(days=1)
    d30, d28, d112 = (today - dt.timedelta(days=n) for n in (30, 28, 112))
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

    by_name = {person_name(p): p for p in people if person_name(p)
               and (p.get("Project") or "").strip() not in TECHNICAL}
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
            "project": (p.get("Project") or "").strip() or (UNASSIGNED if status == "Home" else "(unassigned)"),
            "status": status,
            "sub": (p.get("Subcontractor") or "") == "Yes",
            "weeks": [round(week_h.get(k, 0)) for k in weeks4],
            "h30": round(sum(v for d, v in days.items() if d > d30)),
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
        project = (p.get("Project") or "").strip()
        yard = site_of(project, names) if project else ""
        if ts is not None and p:
            # completed-week check: some supervisors fill the whole week on Saturday
            checkable = (status == "Yes" and project and project not in INTERNAL_TIMESHEETS
                         and yard in ts["yards_with_ts"])
            if checkable and not ts["last_week"].get(name):
                flags.append("no_ts")
                w["no_ts_week"] = ts["last_week_no"]
            other = sorted(y for y in ts["last_week"].get(name, ()) if project and y != yard)
            if other:
                flags.append("mismatch")
                w["mismatch"] = other
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
            "last_week": ts["last_week_no"] if ts else None,
            "flag_counts": dict(Counter(f for w in out for f in w["flags"]))}


# ---------- labour cost ----------
# Net ("in hand") hourly rate -> cost of the workplace = net + taxes.
GPM, SODRA_EMPLOYEE, SODRA_EMPLOYER = 0.20, 0.195, 0.0177       # standard Lithuanian rates
LT_FACTOR = 1 / (1 - GPM - SODRA_EMPLOYEE) * (1 + SODRA_EMPLOYER)  # ~1.68 € cost per 1 € net
TRIP_SALARY_SHARE = 0.5   # ASSUMPTION: on business trips half of the net pay is salary, half tax-free per diem
TRIP_FACTOR = TRIP_SALARY_SHARE * LT_FACTOR + (1 - TRIP_SALARY_SHARE)
SUBCONTRACTOR_RATE = 35.0  # €/h, everything included (subcontractor invoice)
COUNTRY_ALIAS = {"FIN": "FI"}


class LabourCost:
    def __init__(self, raw, projects):
        self.rate = {}
        by_pos = defaultdict(list)
        for q in raw.get("qualification") or []:
            r = num(q.get("Hour rate"))
            if r > 0:
                self.rate[(q.get("Emploee") or "").strip()] = r
                by_pos[(q.get("Position") or "").strip()].append(r)
        self.pos_avg = {k: sum(v) / len(v) for k, v in by_pos.items()}
        allr = [r for v in by_pos.values() for r in v]
        self.all_avg = sum(allr) / len(allr) if allr else 0.0
        self.sub_company = {(c.get("Company") or "").strip(): (c.get("Subcontractor") or "") == "Yes"
                            for c in raw.get("companies") or []}
        self.sub_person = {person_name(p): (p.get("Subcontractor") or "") == "Yes" for p in raw.get("people") or []}
        self.country = {p["name"]: p["country"] for p in projects if p["country"]}
        self.names = set(self.country) | {p["name"] for p in projects}
        self.stats = Counter()  # hours by rate source, for the data-quality note

    def country_of(self, project):
        if project in self.country:
            return self.country[project]
        site = site_of(project, self.names)
        if site in self.country:
            return self.country[site]
        prefix = project.split("-")[0].upper()
        if 2 <= len(prefix) <= 3 and prefix.isalpha() and "-" in project:
            return COUNTRY_ALIAS.get(prefix, prefix)
        return "LT"

    def cost(self, project, position, hours, name, company):
        company = (company or "").strip()
        sub = self.sub_company.get(company) if company in self.sub_company else self.sub_person.get(name, False)
        if sub:
            self.stats["subcontractor"] += hours
            return hours * SUBCONTRACTOR_RATE
        rate = self.rate.get(name)
        if rate:
            self.stats["own_rate"] += hours
        else:
            rate = self.pos_avg.get(position) or self.all_avg
            self.stats["position_avg"] += hours
        trip = self.country_of(project) != "LT"
        self.stats["trip" if trip else "lt"] += hours
        return hours * rate * (TRIP_FACTOR if trip else LT_FACTOR)


def aggregate_hours(rows, today, names, labour=None):
    """Sum 'Hours real' from Task list sub by site -> sub-project and by position (all time, up to today)."""
    today_s, month_ago = today.isoformat(), (today - dt.timedelta(days=30)).isoformat()

    def months_from(first):   # every month from the yard's first booking through the current month
        out, (y, m) = [], (int(first[:4]), int(first[5:7]))
        while f"{y:04d}-{m:02d}" <= today_s[:7]:
            out.append(f"{y:04d}-{m:02d}")
            y, m = (y, m + 1) if m < 12 else (y + 1, 1)
        return out
    groups = {}
    T = Counter()  # total / last30 hours and cost
    for row in rows:
        sub, proj_t, pos, h, day = row[:5]
        day = ragic_date(day)
        hrs = num(h)
        if not day or day > today_s or not hrs:
            continue  # future-dated or empty task rows
        proj = (sub or "").strip() or (proj_t or "").strip() or "(no project)"
        if proj in TECHNICAL or site_of(proj, names) in TECHNICAL:
            continue
        pos = (pos or "").strip() or "Other"
        cost = labour.cost(proj, pos, hrs, (row[5] if len(row) > 5 else "") or "",
                           row[6] if len(row) > 6 else "") if labour else 0.0
        site = site_of(proj, names)
        recent = day > month_ago
        g = groups.setdefault(site, {"name": site, "first": day, "last": day, "t": Counter(),
                                     "subs": defaultdict(Counter), "positions": defaultdict(Counter),
                                     "months": defaultdict(lambda: {"subs": defaultdict(Counter),
                                                                    "positions": defaultdict(Counter)})})
        mm = g["months"][day[:7]]
        for c in (mm["subs"][proj], mm["positions"][pos]):
            c["h"] += hrs
            c["cost"] += cost
        g["first"], g["last"] = min(g["first"], day), max(g["last"], day)
        for c in (g["t"], g["subs"][proj], T):
            c["total"] += hrs
            c["cost"] += cost
            if recent:
                c["last30"] += hrs
                c["cost30"] += cost
        g["positions"][pos]["h"] += hrs
        g["positions"][pos]["cost"] += cost
    figs = lambda c: {"total": round(c["total"]), "last30": round(c["last30"]),
                      "cost": round(c["cost"]), "cost30": round(c["cost30"])}

    def month_figs(m, mm):
        if not mm:
            return {"m": m, "total": 0, "cost": 0, "subs": [], "positions": {}, "position_cost": {}}
        subs = sorted(mm["subs"].items(), key=lambda kv: -kv[1]["h"])
        pos = sorted(mm["positions"].items(), key=lambda kv: -kv[1]["h"])
        return {"m": m, "total": round(sum(v["h"] for _, v in subs)), "cost": round(sum(v["cost"] for _, v in subs)),
                "subs": [{"name": k, "total": round(v["h"]), "cost": round(v["cost"])} for k, v in subs],
                "positions": {k: round(v["h"]) for k, v in pos},
                "position_cost": {k: round(v["cost"]) for k, v in pos}}

    out = []
    for g in sorted(groups.values(), key=lambda g: -g["t"]["total"]):
        out.append({"name": g["name"], **figs(g["t"]), "first": g["first"], "last": g["last"],
                    "subs": [{"name": k, **figs(v)} for k, v in sorted(g["subs"].items(), key=lambda kv: -kv[1]["total"])],
                    "positions": {k: round(v["h"]) for k, v in sorted(g["positions"].items(), key=lambda kv: -kv[1]["h"])},
                    "position_cost": {k: round(v["cost"]) for k, v in g["positions"].items()},
                    "months": [month_figs(m, g["months"].get(m)) for m in months_from(g["first"])]})
    by_name = {}
    for g in out:
        by_name[g["name"]] = {k: g[k] for k in ("total", "last30", "cost", "cost30")}
        for s in g["subs"]:
            if s["name"] != g["name"]:
                by_name.setdefault(s["name"], {k: s[k] for k in ("total", "last30", "cost", "cost30")})
    return {"groups": out, "by_name": by_name, **figs(T)}


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
        proj = (p.get("Project") or "").strip() or (UNASSIGNED if st == "Home" else "(unassigned)")
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
        if not name or (r.get("Active") or "Yes") != "Yes" or name in TECHNICAL:
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
        if name not in seen and name not in TECHNICAL:
            projects.append({"name": name, "country": "", "start": "", "end": "", "used_hours": 0,
                             "internal": True, "status": dict(st), "onsite": st.get("Yes", 0),
                             "active": sum(st.values()), "positions_onsite": dict(pos_onsite.get(name, Counter())),
                             "staffing": None, "not_in_project_list": True, "unassigned": name == UNASSIGNED})
    projects.sort(key=lambda p: (p["internal"], -p["onsite"], -p["active"], p["name"]))

    labour = LabourCost(raw, projects)
    names = {p["name"] for p in projects}
    rows = raw.get("hours") or []
    ts = timesheet_index(rows, today, names)
    hours = aggregate_hours(rows, today, names, labour)
    workers = aggregate_workers(people, rows, today, ts, names)
    for w in workers["workers"]:
        w["group"] = site_of(w["project"], names)  # same yard grouping as the hours table
    for p in projects:
        g = hours["by_name"].get(p["name"])
        p["hours"] = g  # {"total", "last30"} for a site (incl. its sub-projects) or a single project
        # people on site, but this location keeps its timesheets outside Ragic
        p["no_ragic_ts"] = bool(p["onsite"] and not p["internal"] and p["name"] not in INTERNAL_TIMESHEETS
                                and site_of(p["name"], names) not in ts["yards_with_ts"])

    # Additional manpower requests (current year, last 4 weeks and ahead).
    # The request is only the "letter" to Human Rotation; rotation can be changed by hand afterwards
    # (people replaced, moved to another project, sent home). So every selected person is checked
    # against their current Human Rotation row (planing/33): project, At work, Conf., Ready.
    # Note: Conf. is reset to "No" once the person arrives (At work = Yes).
    rot = {}
    for r in raw.get("rotation") or []:
        n = (r.get("F") or "").strip()
        if n and (n not in rot or r.get("Active") == "Yes"):
            rot[n] = r
    have_rot = bool(rot)
    ragic = f"{os.environ.get('RAGIC_BASE', 'https://eu4.ragic.com').rstrip('/')}/{os.environ.get('RAGIC_ACCOUNT', 'SIDC10000')}"
    requests = []
    for r in raw["requests"]:
        y, w = int(num(r.get("Year"))), int(num(r.get("Week")))
        proj = (r.get("Project") or "").strip()
        if y != year or w < week - 4 or proj in TECHNICAL or (r.get("Active") or "Yes") == "No":
            continue
        yard, date = site_of(proj, names), ragic_date(r.get("Date"))
        people_sel = []
        for row in (r.get("_subtable_1015956") or {}).values():
            n = (row.get("First, last names") or row.get("First&#44; last names") or "").strip()
            if not n:
                continue
            q = rot.get(n)
            pos = (row.get("Position") or "").strip()
            if not have_rot:
                state, where, ready = "planned", "", ragic_date(row.get("Ready"))
            elif not q:
                state, where, ready = "not_in_rotation", "", ""
            else:
                rp, at = (q.get("Project") or "").strip(), (q.get("At work") or "").strip()
                where, ready, pos = rp, ragic_date(q.get("Ready")), (q.get("Position") or pos).strip()
                if q.get("Active") == "No" or not rp:
                    state = "unassigned"
                elif site_of(rp, names) != yard:
                    state = "elsewhere"
                elif at == "Yes":
                    state = "working"
                elif (q.get("Conf.") or "").strip() == "Yes":
                    state = "confirmed"
                else:
                    state = "planned"
            people_sel.append({"name": n, "position": pos, "state": state,
                               "where": where if state == "elsewhere" else "", "ready": ready})
        cnt = Counter(s["state"] for s in people_sel)
        requested = int(num(r.get("Number")))
        working, confirmed = cnt["working"], cnt["confirmed"]
        reserved = working + confirmed + cnt["planned"]
        changed = len(people_sel) - reserved
        if reserved < requested:
            status = "open"
        elif working >= requested:
            status = "completed"
        elif working + confirmed >= requested:
            status = "confirmed"
        else:
            status = "unconfirmed"
        rid = r.get("_ragicId")
        requests.append({"id": rid, "url": f"{ragic}/planing/52/{rid}" if rid else "", "project": proj, "yard": yard,
                         "position": (r.get("Position") or "").strip(), "week": w, "date": date,
                         "requested": requested, "selected": len(people_sel), "reserved": reserved,
                         "confirmed": confirmed, "working": working, "changed": changed,
                         "gap": max(0, requested - reserved), "status": status, "people": people_sel})
    requests.sort(key=lambda r: (r["week"], r["project"]))

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
        "unassigned_key": UNASSIGNED,
        "workforce": {"total": sum(status_total.values()), "status": dict(status_total), "own": own, "sub": sub,
                      "unassigned": sum(by_project.get(UNASSIGNED, Counter()).values()),
                      "by_position": {k: dict(v) for k, v in sorted(by_position.items(), key=lambda kv: -sum(kv[1].values()))}},
        "position_balance_now": dict(sorted(pos_balance.items(), key=lambda kv: kv[1])),
        "requests": requests,
        "hours": hours["groups"],
        "hours_total": hours["total"],
        "hours_last30": hours["last30"],
        "cost_total": hours["cost"],
        "cost_last30": hours["cost30"],
        "cost_model": {"lt_factor": round(LT_FACTOR, 2), "trip_factor": round(TRIP_FACTOR, 2),
                       "trip_salary_share": TRIP_SALARY_SHARE, "sub_rate": SUBCONTRACTOR_RATE,
                       "hours_by_source": {k: round(v) for k, v in labour.stats.items()}},
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
    if "unassigned" in pw and cw.get("unassigned") != pw.get("unassigned"):
        add("people", f"Unassigned resources: {pw['unassigned']} → {cw['unassigned']}", cw["unassigned"] - pw["unassigned"])

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
        elif "reserved" in old and old["reserved"] != r["reserved"]:
            add("request", f"Reserved {old['reserved']} → {r['reserved']} of {r['requested']}: {r['project']} {r['position']} (W{r['week']})",
                r["reserved"] - old["reserved"])

    labels = {"no_ts": "Workers without timesheet entries last week", "mismatch": "Workers booking hours at another location",
              "over12": "Workers with days over 12 h",
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
        st, wf = s["workforce"]["status"], s["workforce"]
        series[s["date"]] = {
            "onsite": st.get("Yes", 0), "workshop": st.get("Workshop", 0), "scheduled": st.get("Scheduled", 0),
            "home": st.get("Home", 0), "active": wf["total"], "unassigned": wf.get("unassigned"),
            "below_plan": -sum(p["staffing"]["short"][0] for p in s["projects"]
                               if p.get("staffing") and not p.get("internal")),
            "still_needed": sum(r.get("gap", 0) for r in s.get("requests", []) if r.get("week", 0) >= s["week"]),
            "projects": {p["name"]: p["onsite"] for p in s["projects"]}}
    return [{"date": d, **v} for d, v in sorted(series.items())][-190:]


def main():
    raw_dir, hist_dir, out_html = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
    raw = {n: json.loads((raw_dir / f"{n}.json").read_text(encoding="utf-8"))
           for n in ("projects", "people", "requests", "hours", "qualification", "companies", "rotation")
           if (raw_dir / f"{n}.json").exists()}
    today = dt.date.fromisoformat(os.environ["BUILD_DATE"]) if os.environ.get("BUILD_DATE") else dt.date.today()
    snap = build_snapshot(raw, today)
    history = [h for h in load_history(hist_dir) if h["date"] < snap["date"]]
    prev = history[-1] if history else None
    # per-worker rows are only needed for today's page; history keeps the flag counts
    save_snapshot(hist_dir, dict(snap, workers={"flag_counts": snap["workers"]["flag_counts"]},
                                 requests=[{k: v for k, v in r.items() if k != "people"} for r in snap["requests"]]))

    page = dict(snap)
    page["changes"] = diff(prev, snap)
    page["trend"] = trend(history, snap)
    if os.environ.get("MGMT_PASSWORD"):
        page, costs = split_costs(page)
        page["costs_enc"] = encrypt_costs(costs, os.environ["MGMT_PASSWORD"])
    template = (Path(__file__).parent / "template.html").read_text(encoding="utf-8")
    payload = json.dumps(page, ensure_ascii=False).replace("</", "<\\/")
    out_html.parent.mkdir(parents=True, exist_ok=True)
    out_html.write_text(template.replace("/*__DATA__*/null", payload), encoding="utf-8")
    print(f"built {out_html} for {snap['date']} (W{snap['week']}); previous snapshot: {prev['date'] if prev else 'none'}")


if __name__ == "__main__":
    main()
