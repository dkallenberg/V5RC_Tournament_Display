#!/usr/bin/env python3
"""
VEX Event Display (HS + MS) for Raspberry Pi
============================================
Single file. Python 3.8+ standard library only (no pip installs).
Data comes from the events.vex.com v2 API (needs a bearer token, set in Admin).

Run:      python3 Vex_Event_Display.py                (serves on port 5000)
          sudo python3 Vex_Event_Display.py --port 80 (serves on port 80)
Kiosk:    http://<pi-ip>:5000/
Admin:    http://<pi-ip>:5000/admin

Chromium kiosk launch (Pi desktop):
  chromium-browser --kiosk --noerrdialogs --disable-infobars \
      --autoplay-policy=no-user-gesture-required http://localhost:5000/
"""
import argparse, base64, datetime, json, os, random, re, socket, threading, time
import traceback
import urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "kiosk_config.json")

# --------------------------------------------------------------------------
# Configuration (server-side source of truth; mirrored to localStorage)
# --------------------------------------------------------------------------
API_BASE_DEFAULT = "https://events.vex.com/api/v2"
# Bearer token taken from the working vex_matches.html. Change it any time in Admin > events.vex.com connection.
DEFAULT_TOKEN = "eyJ0eXAiOiJKV1QiLCJhbGciOiJSUzI1NiJ9.eyJhdWQiOiIzIiwianRpIjoiNzMzODkyNTBjNjA3MTg1NGI1YTA4Njg5MDNmYTkwMmM2MjM3YjBkYTQzZmNmODQzOTc0MjA5NmM5MmE3MzcwNGExZmI2ZmE0YWNmZjUyYmYiLCJpYXQiOjE3OTM0MDYzOTIuMzYxMjYwOSwibmJmIjoxNzkzNDA2MzkyLjM2MTI2MywiZXhwIjoyNzQwMTc3NTkyLjM1NjY5MjgsInN1YiI6IjEyMzUzOSIsInNjb3BlcyI6W119.BZpXY1KL9WZHW_O-igw3qa1d4gBhW0efci3RfeCH5eHf_66T1zYQx0uuhBjpmRjFPBaEu2V_8bR0wQs4H7j1cb4oz3_Vwd_zle8VWBcdL7ScjfGL1rF69Mpv8W8Tja1RH6Pe3Hus3RHlf-MN4Kh1osLJSu-kIsKaPpZBIwtFTVPHGK7L5V7d0j9Nr5AHudovAiSYk8Xzl8kM7F1OhkpwrsyBeK59XzaLu5VeKWt0gKtBj444U53YJlMuxqdu7g2mO5ZssXZJHLhBwY1pfXxMu8V-Pt_vvlmNF1YqXpMLx8AhrVhhwlVCnH2VrPsu4xqTKi431Lld3WcQTsJZvQbdZbU5B7gfMTgE0HoWjkTXZDjzJI8mhD8UmQG1DyRpiLmtIL5lBcIyoKntfhR8uvdnlEG1u6LnUN2kaTg6amUAUDMAT5KI7iRd4C-6jIiIIB_Yo3R8D_no-Sqmg3UixFwyqC7W1dRi18X1NYgEMtxEwvPjc1r_0RS3HLy3oHah0-FFj5FpXYu0oZ1SgyZaZYCR5mqiIHwG7kfgYrn5c2SSFLqgzs6eZRTZ8F1qirWBe83oMZYw1wACP3rK4yHXT5aWUPDEprnQBbmBGyU8bgLczJU48SlF9Yxc7G0bEhvXjjKd04iMiuIkLxpalniq-m-aqgII86qNsyjogWECWspRrSo"

DEFAULT_CONFIG = {
    "view": "single",  # single = one tournament full screen (HS settings) | double = HS left + MS right
    "hs": {"sku": "", "demo": False, "phase": "auto", "fields": 3, "division": "", "lunchAfter": 0, "lunchMins": 0, "lunchResume": ""},
    "ms": {"sku": "", "demo": False, "phase": "auto", "fields": 2, "division": "", "lunchAfter": 0, "lunchMins": 0, "lunchResume": ""},
    "rankWait": 300,   # seconds to wait after the last qual is scored before showing rankings only
    "poll": 15,
    "scroll": 8,
    "apiBase": API_BASE_DEFAULT,
    "token": DEFAULT_TOKEN,
    "rev": 1,
}

CFG_LOCK = threading.Lock()
CONFIG = json.loads(json.dumps(DEFAULT_CONFIG))
COMMAND = {"seq": 0, "cmd": ""}
SERVER_START = time.time()
DEMO_START = {"hs": SERVER_START, "ms": SERVER_START}   # when each division's demo timeline began
CACHE = {}          # division -> {"t": ts, "data": normalized}
LAST_GOOD = {}      # division -> normalized data from last successful fetch
STATE_PATH = os.path.join(HERE, "kiosk_state.json")
STATE = {"hs": {}, "ms": {}}   # per division: {"done_at": time the last qual was first seen scored}


def load_state():
    global STATE
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            st = json.load(f)
        STATE = {"hs": dict(st.get("hs", {})), "ms": dict(st.get("ms", {}))}
    except (OSError, ValueError, AttributeError):
        STATE = {"hs": {}, "ms": {}}


def save_state():
    try:
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(STATE, f)
        os.replace(tmp, STATE_PATH)
    except OSError:
        pass


def clamp(v, lo, hi, default):
    try:
        v = int(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def sanitize_config(c):
    out = json.loads(json.dumps(DEFAULT_CONFIG))
    for d in ("hs", "ms"):
        src = c.get(d, {}) if isinstance(c.get(d), dict) else {}
        out[d]["sku"] = str(src.get("sku", "")).strip()[:500]
        out[d]["demo"] = bool(src.get("demo", False))
        out[d]["fields"] = clamp(src.get("fields"), 1, 4, 3 if d == "hs" else 2)
        out[d]["division"] = str(src.get("division", "")).strip()[:60]
        out[d]["lunchAfter"] = clamp(src.get("lunchAfter"), 0, 999, 0)       # lunch break after this Q (0 = none)
        out[d]["lunchMins"] = clamp(src.get("lunchMins"), 0, 600, 0)         # ... lasting this many minutes
        rs = str(src.get("lunchResume", "")).strip()                          # ... or until this clock time (HH:MM)
        out[d]["lunchResume"] = rs if re.match(r"^\d{1,2}:\d{2}$", rs) else ""
        ph = str(src.get("phase", "auto"))
        out[d]["phase"] = ph if ph in ("auto", "quals", "rankings", "bracket") else "auto"
    out["rankWait"] = clamp(c.get("rankWait"), 0, 3600, 300)
    out["poll"] = clamp(c.get("poll"), 5, 300, 15)
    out["scroll"] = clamp(c.get("scroll"), 3, 120, 8)
    v = str(c.get("view", "single"))
    out["view"] = "double" if v in ("double", "both") else "single"
    base = str(c.get("apiBase") or API_BASE_DEFAULT).strip().rstrip("/")
    out["apiBase"] = base if base.startswith("http") else API_BASE_DEFAULT
    out["token"] = str(c.get("token") or DEFAULT_TOKEN).strip()
    out["rev"] = clamp(c.get("rev"), 1, 10**9, 1)
    return out


def load_config():
    global CONFIG
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            CONFIG = sanitize_config(json.load(f))
    except (OSError, ValueError):
        CONFIG = sanitize_config(DEFAULT_CONFIG)


def save_config(new):
    global CONFIG
    with CFG_LOCK:
        prev_rev = CONFIG.get("rev", 1)
        new = dict(new)
        new["token"] = str(new.get("token") or "").strip() or CONFIG.get("token") or DEFAULT_TOKEN  # blank = keep
        merged = sanitize_config(new)
        if merged["view"] == "single":     # single-tournament mode: the MS settings are locked
            merged["ms"] = json.loads(json.dumps(CONFIG.get("ms") or DEFAULT_CONFIG["ms"]))
        for d in ("hs", "ms"):
            old_e = CONFIG.get(d, {})
            if merged[d]["demo"] and not old_e.get("demo"):
                DEMO_START[d] = time.time()         # demo timeline starts when demo mode is selected
            if old_e.get("sku") != merged[d]["sku"] or old_e.get("demo") != merged[d]["demo"] or old_e.get("division") != merged[d]["division"]:
                STATE[d] = {}     # new event: forget when quals finished
        save_state()
        merged["rev"] = prev_rev + 1
        CONFIG = merged
        CACHE.clear()
        try:
            tmp = CONFIG_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(CONFIG, f, indent=2)
            os.replace(tmp, CONFIG_PATH)
        except OSError as e:
            print("Could not persist config:", e)
    return CONFIG


def token_exp(tok):
    try:
        part = tok.split(".")[1]
        part += "=" * (-len(part) % 4)
        exp = json.loads(base64.urlsafe_b64decode(part)).get("exp")
        return float(exp) if exp else None
    except (IndexError, ValueError, TypeError, OverflowError):
        return None


def fmt_date(ts):
    """YYYY-MM-DD for a Unix time. Pure Python, so it also works past 2038 on 32-bit Raspberry Pi OS."""
    try:
        return (datetime.datetime(1970, 1, 1) + datetime.timedelta(seconds=ts)).strftime("%Y-%m-%d")
    except (OverflowError, ValueError, OSError, TypeError):
        return ""


def public_config(cfg):
    """Config as sent to browsers: the token itself is never exposed, only a hint."""
    out = {k: v for k, v in cfg.items() if k != "token"}
    tok = cfg.get("token") or ""
    exp = token_exp(tok)
    out["tokenSet"] = bool(tok)
    out["tokenHint"] = tok[-6:] if tok else ""
    out["tokenExp"] = fmt_date(exp) if exp else ""
    out["tokenExpired"] = bool(exp and exp < time.time())
    return out


def local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "127.0.0.1"
    finally:
        s.close()


# --------------------------------------------------------------------------
# Tolerant JSON normalization (handles RobotEvents-style and flat shapes)
# --------------------------------------------------------------------------
def lk(d, *names):
    """Case/underscore-insensitive dict lookup returning first non-None."""
    if not isinstance(d, dict):
        return None
    low = {str(k).lower().replace("_", ""): k for k in d}
    for n in names:
        k = low.get(n.lower().replace("_", ""))
        if k is not None and d[k] is not None:
            return d[k]
    return None


def team_name(v):
    if v is None:
        return None
    if isinstance(v, (str, int)) and not isinstance(v, bool):
        s = str(v).strip()
        return s or None
    if isinstance(v, dict):
        for k in ("number", "teamNumber", "name", "team"):
            x = lk(v, k)
            if x is not None:
                r = team_name(x)
                if r:
                    return r
    return None


def alliance_teams(a):
    if isinstance(a, dict):
        a = lk(a, "teams", "members") or []
    out = []
    if isinstance(a, list):
        for t in a:
            if isinstance(t, dict) and lk(t, "sitting") in (True, 1):
                continue
            n = team_name(t)
            if n:
                out.append(n)
    return out


def to_num(v):
    try:
        if v is None or v == "":
            return None
        f = float(v)
        return int(f) if f == int(f) else round(f, 2)
    except (TypeError, ValueError):
        return None


def match_kind(m):
    """'qual', 'elim' or 'skip' (practice / unknown) for one raw API match."""
    rnd = lk(m, "round")
    if isinstance(rnd, bool):
        rnd = None
    if isinstance(rnd, (int, float)):
        r = int(rnd)                      # VEX rounds: 1 practice, 2 qual, 3 QF, 4 SF, 5 Final, 6 R16
        return "skip" if r <= 1 else ("qual" if r == 2 else "elim")
    nm = str(lk(m, "name") or "").strip().lower()
    if nm.startswith(("p ", "practice")):
        return "skip"
    if nm.startswith("qf"):
        return "elim"
    if nm.startswith("q"):
        return "qual"
    return "elim" if nm else "qual"


def parse_match(m):
    red = blue = None
    rs = bs = None
    al = lk(m, "alliances")
    if isinstance(al, list):
        for a in al:
            c = str(lk(a, "color") or "").lower()
            if c == "red":
                red, rs = alliance_teams(a), lk(a, "score")
            elif c == "blue":
                blue, bs = alliance_teams(a), lk(a, "score")
    if red is None:
        r = lk(m, "red", "redalliance", "redteams")
        if r is not None:
            red = alliance_teams(r)
            rs = lk(r, "score") if isinstance(r, dict) else None
    if blue is None:
        b = lk(m, "blue", "bluealliance", "blueteams")
        if b is not None:
            blue = alliance_teams(b)
            bs = lk(b, "score") if isinstance(b, dict) else None
    if red is None:
        red = [n for n in (team_name(lk(m, k)) for k in ("red1", "red2", "red3")) if n]
    if blue is None:
        blue = [n for n in (team_name(lk(m, k)) for k in ("blue1", "blue2", "blue3")) if n]
    if rs is None:
        rs = lk(m, "redscore")
    if bs is None:
        bs = lk(m, "bluescore")
    rs, bs = to_num(rs), to_num(bs)
    num = to_num(lk(m, "matchnum", "matchnumber", "number"))
    if num is None:
        mm = re.search(r"(\d+)\s*$", str(lk(m, "name") or ""))
        num = int(mm.group(1)) if mm else None
    if num is None:
        return None
    flag = lk(m, "scored", "finished", "completed", "played")
    has = rs is not None and bs is not None
    # scored if the API says so, or if the alliances carry real scores
    scored = has and (bool(flag) or (rs + bs) > 0)
    fld = lk(m, "field", "fieldname")
    if isinstance(fld, dict):
        fld = lk(fld, "name")
    return {
        "num": int(num), "inst": to_num(lk(m, "instance")) or 1,
        "field": str(fld) if fld is not None else "",
        "red": red, "blue": blue,
        "rs": rs if scored else None, "bs": bs if scored else None,
        "scored": scored,
        "rnd": str(lk(m, "round") or ""), "label": str(lk(m, "name") or ""),
    }


ELIM_RE = re.compile(r"\b(R16|QF|SF|FINALS?|F)\b\s*#?\s*(\d+)?(?:\s*-\s*(\d+))?", re.I)
KIND_BASE = {"R16": 0, "QF": 8, "SF": 12, "FINAL": 14}
KIND_MAX = {"R16": 8, "QF": 4, "SF": 2, "FINAL": 1}
RND_KIND = {6: "R16", 3: "QF", 4: "SF", 5: "FINAL"}


def elim_slot(m):
    """Bracket slot 1-15 for an elimination match: R16 #1-8 = 1-8, QF #1-4 = 9-12, SF #1-2 = 13-14, Final = 15."""
    kind = n = inst = None
    mm = ELIM_RE.search(m.get("label") or "")
    if mm:
        k = mm.group(1).upper()
        kind = "FINAL" if k.startswith("F") else k
        n, inst = int(mm.group(2) or 1), int(mm.group(3) or m["inst"])
    else:
        try:
            kind = RND_KIND.get(int(float(m["rnd"])))
        except (TypeError, ValueError):
            kind = None
        if kind:
            n, inst = m["num"], m["inst"]
    if kind is None:
        return None
    if kind == "FINAL":
        n = 1
    if not 1 <= n <= KIND_MAX[kind]:
        return None
    return KIND_BASE[kind] + n, inst


def assign_slots(elims):
    best, unmapped = {}, []
    for m in elims:
        r = elim_slot(m)
        if r is None:
            unmapped.append(m)
            continue
        m["slot"], m["inst"] = r
        if r[0] not in best or r[1] >= best[r[0]]["inst"]:   # later instance (replay) wins
            best[r[0]] = m
    elims[:] = sorted(best.values(), key=lambda m: m["slot"])


def parse_rank(r):
    team = team_name(lk(r, "team", "teamnumber", "teamname"))
    return {
        "rank": to_num(lk(r, "rank")) or 0, "team": team,
        "w": to_num(lk(r, "wins", "w")) or 0,
        "l": to_num(lk(r, "losses", "l")) or 0,
        "t": to_num(lk(r, "ties", "t")) or 0,
        "wp": to_num(lk(r, "wp")), "ap": to_num(lk(r, "ap")), "sp": to_num(lk(r, "sp")),
        "avg": to_num(lk(r, "averagepoints", "avgscore", "averagescore", "avg", "avgpoints")),
    }


def fill_averages(rankings, matches):
    sums, cnt = {}, {}
    for m in matches:
        if not m["scored"]:
            continue
        for teams, sc in ((m["red"], m["rs"]), (m["blue"], m["bs"])):
            for t in teams:
                sums[t] = sums.get(t, 0) + (sc or 0)
                cnt[t] = cnt.get(t, 0) + 1
    for r in rankings:
        if r["avg"] is None:
            r["avg"] = round(sums[r["team"]] / cnt[r["team"]], 1) if cnt.get(r["team"]) else 0
        for k in ("wp", "ap", "sp"):
            if r[k] is None:
                r[k] = 0


# --------------------------------------------------------------------------
# events.vex.com v2 API (bearer token): /events, /events/{id}/divisions/{div}/matches|rankings
# --------------------------------------------------------------------------
SKU_RE = re.compile(r"RE-[A-Z0-9]+-\d{2}-\d{4}", re.I)
UA = "Mozilla/5.0 (X11; Linux armv7l) VexEventDisplay/1.0"
EVENT_CACHE = {}   # sku -> (time, event object); event id/name/divisions rarely change
RATE_BLOCK = {}    # division -> time until which API calls pause after an HTTP 429


class ApiError(Exception):
    pass


def api_get(base, token, path, diag, timeout=12):
    url = base.rstrip("/") + path
    rec = {"url": url, "status": None, "ms": None, "note": ""}
    diag.append(rec)
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + token, "Accept": "application/json", "User-Agent": UA})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read(12 * 1024 * 1024).decode("utf-8", "replace")
            rec["status"], rec["ms"] = r.status, int((time.time() - t0) * 1000)
        data = json.loads(body)
        rec["note"] = "OK"
        return data
    except urllib.error.HTTPError as e:
        rec["status"] = e.code
        bad = "API token rejected (invalid or expired): paste a fresh token in Admin"
        rec["note"] = {401: bad, 403: bad, 404: "Not found", 429: "Rate limited by events.vex.com"}.get(e.code, "HTTP %d" % e.code)
        raise ApiError(rec["note"])
    except ValueError:
        rec["note"] = "Response was not valid JSON"
        raise ApiError(rec["note"])
    except Exception as e:  # DNS, timeout, SSL, connection reset
        rec["note"] = "%s: %s" % (type(e).__name__, e)
        raise ApiError("Could not reach events.vex.com (check the Pi's internet connection)")


def api_all(base, token, path, diag):
    rows, page, last = [], 1, 1
    while page <= last and page <= 20:
        j = api_get(base, token, "%s%sper_page=250&page=%d" % (path, "&" if "?" in path else "?", page), diag)
        rows += (j.get("data") or []) if isinstance(j, dict) else []
        last = int(((j.get("meta") or {}).get("last_page") or 1)) if isinstance(j, dict) else 1
        page += 1
    return rows


def pick_division(divs, want):
    want = str(want or "").strip().lower()
    if want:
        for d in divs:
            if want in (str(d.get("id", "")).lower(), str(d.get("name", "")).lower(), str(d.get("order", "")).lower()):
                return d
    return sorted(divs, key=lambda d: d.get("order") if isinstance(d.get("order"), (int, float)) else 0)[0]


def fetch_live(entry, cfg):
    diag = []
    m = SKU_RE.search(entry["sku"] or "")
    if not m:
        return None, diag, "Enter an event SKU such as RE-V5RC-26-1234"
    sku = m.group(0).upper()
    token, base = cfg.get("token") or "", cfg.get("apiBase") or API_BASE_DEFAULT
    if not token:
        return None, diag, "No API token set (Admin > events.vex.com connection)"
    now = time.time()
    try:
        hit = EVENT_CACHE.get(sku)
        if hit and now - hit[0] < 600:
            evo = hit[1]
        else:
            ev = api_get(base, token, "/events?sku[]=" + sku, diag)
            data = ev.get("data") if isinstance(ev, dict) else None
            if not data:
                raise ApiError("No event found for SKU " + sku)
            evo = data[0]
            EVENT_CACHE[sku] = (now, evo)
        divs = evo.get("divisions") or [{"id": 1, "name": "Division 1"}]
        div = pick_division(divs, entry.get("division"))
        root = "/events/%s/divisions/%s" % (evo["id"], div["id"])
        raw_matches = api_all(base, token, root + "/matches", diag)
        try:
            raw_ranks = api_all(base, token, root + "/rankings", diag)
        except ApiError:
            if diag and diag[-1]["status"] == 404:
                raw_ranks = []          # rankings not published yet
            else:
                raise
    except ApiError as e:
        return None, diag, str(e)
    except (KeyError, TypeError, AttributeError) as e:
        return None, diag, "Unexpected response shape from events.vex.com (%s)" % e
    quals, elims = [], []
    for rm in raw_matches:
        kind = match_kind(rm)
        if kind == "skip":
            continue
        pm = parse_match(rm)
        if pm:
            (quals if kind == "qual" else elims).append(pm)
    rankings = [x for x in (parse_rank(r) for r in raw_ranks) if x["team"]]
    quals.sort(key=lambda x: (x["num"], x["inst"]))
    rankings.sort(key=lambda r: r["rank"] or 9999)
    assign_slots(elims)
    fill_averages(rankings, quals)
    return {"name": evo.get("name") or sku, "sku": sku, "matches": quals, "elims": elims, "alliances": [],
            "rankings": rankings, "divisions": [{"id": d.get("id"), "name": d.get("name", "")} for d in divs],
            "division": div.get("name") or div.get("id")}, diag, ""


# --------------------------------------------------------------------------
# Demo data (lets you verify layout with no event / no internet)
# --------------------------------------------------------------------------
SEEDS16 = {1: (1, 16), 2: (8, 9), 3: (4, 13), 4: (5, 12), 5: (2, 15), 6: (7, 10), 7: (3, 14), 8: (6, 11)}
FEEDS16 = {9: (1, 2), 10: (3, 4), 11: (5, 6), 12: (7, 8), 13: (9, 10), 14: (11, 12), 15: (13, 14)}


def sim_bracket(alliances, nscored, rnd):
    """16-alliance single elimination: R16 #1-8, QF #1-4, SF #1-2, Final."""
    A = {a["n"]: a["teams"] for a in alliances}
    res, out = {}, []
    for slot in range(1, 16):
        sa, sb = rnd.randint(60, 220), rnd.randint(60, 220)
        if sa == sb:
            sa += 1
        if slot <= 8:
            red, blue = A.get(SEEDS16[slot][0]), A.get(SEEDS16[slot][1])
        else:
            f = FEEDS16[slot]
            red = res[f[0]] if f[0] in res else None
            blue = res[f[1]] if f[1] in res else None
        if not red or not blue:
            continue
        sc = len(res) < nscored
        if sc:
            res[slot] = red if sa > sb else blue
        label = ("R16 #%d-1" % slot if slot <= 8 else "QF #%d-1" % (slot - 8) if slot <= 12
                 else "SF #%d-1" % (slot - 12) if slot <= 14 else "Final #1-1")
        out.append({"num": slot, "inst": 1, "field": "", "red": red, "blue": blue,
                    "rs": sa if sc else None, "bs": sb if sc else None, "scored": sc,
                    "rnd": "elim", "label": label, "slot": slot})
    return out


# Demo timeline (seconds): quals advance 1 match / 3 s; 15 s on the final rankings (alliance selection);
# bracket shown for 5 s; then 1 bracket match / 3 s; 15 s hold on the finished bracket; then it starts over.
DEMO_LUNCH_AFTER = 30    # demo lunch break after Q30, lasting 45 s (rankings 20 s, large queue cards 20 s, 5 s normal)
DEMO_LUNCH_LEN = 45
DEMO_QUAL_END = 3 * (60 - 2) + DEMO_LUNCH_LEN
DEMO_CYCLE = DEMO_QUAL_END + 15 + 5 + 3 * 15 + 15


def demo_data(div, now):
    """Demo event whose state depends on how long demo mode has been running (see timeline above)."""
    rnd = random.Random(7 if div == "hs" else 11)
    n_teams = 50 if div == "hs" else 32
    teams = ["%d%s" % (1000 + i * 41 + (0 if div == "hs" else 9000), "ABCDEFG"[i % 7]) for i in range(n_teams)]
    fields = ["Field 1", "Field 2", "Field 3"] if div == "hs" else ["Field 1", "Field 2"]
    matches = []
    for n in range(1, 61):
        four = rnd.sample(teams, 4)
        matches.append({"num": n, "inst": 1, "field": fields[(n - 1) % len(fields)], "red": four[:2], "blue": four[2:],
                        "_rs": rnd.randint(18, 96), "_bs": rnd.randint(18, 96)})
    clock = (now - DEMO_START.get(div, SERVER_START)) % DEMO_CYCLE
    lunch_at = 3 * (DEMO_LUNCH_AFTER - 2)                  # when the last match before lunch is scored
    if clock < lunch_at:
        done = 2 + int(clock // 3)
    elif clock < lunch_at + DEMO_LUNCH_LEN:
        done = DEMO_LUNCH_AFTER                            # lunch break: no matches scored
    else:
        done = min(60, DEMO_LUNCH_AFTER + int((clock - lunch_at - DEMO_LUNCH_LEN) // 3))
    for m in matches:
        sc = m["num"] <= done
        rs, bs = m.pop("_rs"), m.pop("_bs")
        m["scored"] = sc
        m["rs"], m["bs"] = (rs, bs) if sc else (None, None)
        m["rnd"], m["label"] = "2", "Q %d" % m["num"]
    stats = {t: {"w": 0, "l": 0, "t": 0, "wp": 0, "ap": 0, "sp": 0, "pts": 0, "n": 0} for t in teams}
    for m in matches:
        if not m["scored"]:
            continue
        for side, own, opp in (("red", m["rs"], m["bs"]), ("blue", m["bs"], m["rs"])):
            for t in m[side]:
                st = stats[t]
                st["n"] += 1; st["pts"] += own
                st["ap"] += own // 6; st["sp"] += own // 3
                if own > opp: st["w"] += 1; st["wp"] += 2
                elif own < opp: st["l"] += 1
                else: st["t"] += 1; st["wp"] += 1
    order = sorted(teams, key=lambda t: (-stats[t]["wp"], -stats[t]["ap"], -stats[t]["sp"], t))
    rankings = []
    for i, t in enumerate(order, 1):
        st = stats[t]
        rankings.append({"rank": i, "team": t, "w": st["w"], "l": st["l"], "t": st["t"], "wp": st["wp"],
                         "ap": st["ap"], "sp": st["sp"], "avg": round(st["pts"] / st["n"], 1) if st["n"] else 0})
    alliances, elims = [], []
    if clock >= DEMO_QUAL_END + 15:
        alliances = [{"n": i + 1, "teams": [order[i], order[31 - i]]} for i in range(16)]
        since = clock - DEMO_QUAL_END - 15 - 5          # bracket sits unscored for its first 5 s
        elims = sim_bracket(alliances, 0 if since < 0 else min(15, int(since // 3) + 1), random.Random(5))
    name = "Demo %s Championship" % ("High School" if div == "hs" else "Middle School")
    return {"name": name, "sku": "DEMO", "matches": matches, "rankings": rankings,
            "elims": elims, "alliances": alliances, "fields": 3 if div == "hs" else 2}   # HS demo runs 3 fields, MS demo 2


# --------------------------------------------------------------------------
# Division data service with cache + last-good fallback
# --------------------------------------------------------------------------
def lunch_end(entry, start):
    """When play resumes: the 'resume at' clock time if given, otherwise start + the break length."""
    mm = re.match(r"^(\d{1,2}):(\d{2})$", str(entry.get("lunchResume") or ""))
    if mm and int(mm.group(1)) < 24 and int(mm.group(2)) < 60:
        dt = datetime.datetime.fromtimestamp(start).replace(hour=int(mm.group(1)), minute=int(mm.group(2)), second=0, microsecond=0)
        return dt.timestamp()
    return start + 60 * int(entry.get("lunchMins") or 0)


def lunch_state(div, entry, out, now):
    """None, or {"state": "rank" | "queue", "end": epoch} while a lunch break is under way.
    The break starts when the chosen Q is scored. Until 5 min before it ends: rankings only. From 5 min until
    30 s before: large NOW QUEUING cards. Then the normal screen (those matches are on the fields)."""
    demo = bool(entry.get("demo"))
    la = DEMO_LUNCH_AFTER if demo else int(entry.get("lunchAfter") or 0)
    if la <= 0:
        return None
    quals = out.get("matches") or []
    at = next((m for m in quals if m["num"] == la), None)
    st = STATE.setdefault(div, {})
    resumed = any(m["scored"] for m in quals if m["num"] > la)
    if not (at and at["scored"]) or resumed:           # break point not reached yet, or play has already resumed
        if st.get("lunch_start") and not (at and at["scored"]):
            st["lunch_start"] = 0
            save_state()
        return None
    if not st.get("lunch_start"):
        st["lunch_start"] = now
        save_state()
    start = st["lunch_start"]
    end = start + DEMO_LUNCH_LEN if demo else lunch_end(entry, start)
    left = end - now
    if end <= start or left <= 0:
        return None
    t_rank, t_queue = (25, 5) if demo else (300, 30)
    if left > t_rank:
        return {"state": "rank", "end": end}
    if left > t_queue:
        return {"state": "queue", "end": end}
    return None


def compute_phase(div, entry, out, wait, now):
    """Returns (phase, seconds_until_rankings). phase: quals | rankings | bracket."""
    quals = out.get("matches") or []
    elims = out.get("elims") or []
    all_done = bool(quals) and all(m["scored"] for m in quals)
    st = STATE.setdefault(div, {})
    if all_done:
        if not st.get("done_at"):
            st["done_at"] = now          # first time we saw the last qual scored
            save_state()
    elif quals and st.get("done_at"):
        st["done_at"] = 0                # a qual is unscored again (replay / re-queue)
        save_state()
    left = max(0, int(st.get("done_at", 0) + wait - now)) if all_done else None
    bracket_ready = (all_done and len(out.get("alliances") or []) >= 2) or any(m["red"] or m["blue"] for m in elims)
    ov = entry.get("phase", "auto")
    if ov in ("quals", "rankings", "bracket"):
        ph = ov
    elif bracket_ready:
        ph = "bracket"
    elif all_done and left == 0:
        ph = "rankings"
    else:
        ph = "quals"
        lu = lunch_state(div, entry, out, now)
        if lu:
            ph = "lunch_" + lu["state"]
            out["lunchEnd"] = lu["end"]
    return ph, left


def get_division(div, force=False):
    with CFG_LOCK:
        cfg = json.loads(json.dumps(CONFIG))
    entry = cfg[div]
    now = time.time()
    hit = CACHE.get(div)
    if hit and not force and not entry["demo"] and now - hit["t"] < 4:
        return hit["data"]
    err, diag = "", []
    if entry["demo"]:
        res = demo_data(div, now)
    else:
        if RATE_BLOCK.get(div, 0) > now:
            res, err = None, "Rate limited by events.vex.com; retrying shortly"
        else:
            res, diag, err = fetch_live(entry, cfg)
            if "Rate limited" in err:
                RATE_BLOCK[div] = now + 60
    if res:
        out = dict(res)
        out.update({"online": True, "error": "", "fetchedAt": int(now), "stale": False, "demo": entry["demo"]})
        LAST_GOOD[div] = out
    elif div in LAST_GOOD:
        out = dict(LAST_GOOD[div])
        out.update({"online": False, "error": err, "stale": True})
    else:
        out = {"name": "Not configured" if not entry["sku"] else entry["sku"], "sku": entry["sku"],
               "matches": [], "rankings": [], "elims": [], "alliances": [], "online": False, "error": err,
               "fetchedAt": int(now), "stale": False, "demo": False}
    wait = 0 if entry["demo"] else cfg["rankWait"]
    out["phase"], out["rankingsIn"] = compute_phase(div, entry, out, wait, now)
    if not entry["demo"] and entry.get("phase", "auto") == "auto":
        if not SKU_RE.search(entry["sku"] or "") or not cfg.get("token"):
            out["phase"] = "setup"                   # nothing configured yet
        elif out.get("online") and not out["matches"] and not out["elims"]:
            out["phase"] = "nosched"                 # event found, no schedule published yet
    out["rankWait"] = wait
    out["diag"] = diag
    CACHE[div] = {"t": now, "data": out}
    return out


# --------------------------------------------------------------------------
# HTTP server
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "VexEventDisplay/1.0"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if not isinstance(body, (bytes, bytearray)):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj))

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(min(n, 200000)) or b"{}")
        except (ValueError, OSError):
            return {}

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/") or "/"
        try:
            if path == "/":
                self._send(200, KIOSK_HTML, "text/html; charset=utf-8")
            elif path == "/admin":
                self._send(200, ADMIN_HTML, "text/html; charset=utf-8")
            elif path == "/api/config":
                with CFG_LOCK:
                    self._json(public_config(CONFIG))
            elif path == "/api/sysinfo":
                self._json({"ip": local_ip(), "port": self.server.server_address[1], "time": int(time.time())})
            elif path == "/api/command":
                with CFG_LOCK:
                    self._json({"seq": COMMAND["seq"], "cmd": COMMAND["cmd"], "rev": CONFIG["rev"]})
            elif path in ("/api/data/hs", "/api/data/ms"):
                self._json(get_division(path[-2:]))
            elif path in ("/api/test/hs", "/api/test/ms"):
                d = get_division(path[-2:], force=True)
                self._json({
                    "ok": d["online"], "name": d["name"], "error": d["error"], "demo": d.get("demo", False),
                    "matches": len(d["matches"]), "scored": sum(1 for m in d["matches"] if m["scored"]),
                    "teams": len(d["rankings"]), "phase": d.get("phase"),
                    "rankingsIn": d.get("rankingsIn"), "elims": len(d.get("elims", [])),
                    "alliances": len(d.get("alliances", [])), "divisions": d.get("divisions", []),
                    "division": d.get("division"), "top": d["rankings"][:5],
                    "next": [m for m in d["matches"] if not m["scored"]][:3], "diag": d["diag"]})
            elif path == "/favicon.ico":
                self._send(204, b"", "image/x-icon")
            else:
                self._send(404, "Not found", "text/plain")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            traceback.print_exc()
            self._json({"error": "%s: %s" % (type(e).__name__, e)}, 500)

    def do_POST(self):
        path = self.path.split("?")[0].rstrip("/")
        try:
            if path == "/api/config":
                self._json(public_config(save_config(self._body())))
            elif path == "/api/command":
                cmd = str(self._body().get("cmd", ""))
                if cmd not in ("refresh", "nextpage", "reload", "clearcache", "restartdemo"):
                    return self._json({"error": "unknown command"}, 400)
                with CFG_LOCK:
                    if cmd == "restartdemo":
                        for dv in DEMO_START:
                            DEMO_START[dv] = time.time()
                    COMMAND["seq"] += 1
                    COMMAND["cmd"] = cmd
                    if cmd in ("refresh", "reload", "clearcache"):
                        CACHE.clear()
                    self._json(COMMAND)
            else:
                self._send(404, "Not found", "text/plain")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            traceback.print_exc()
            self._json({"error": "%s: %s" % (type(e).__name__, e)}, 500)


# --------------------------------------------------------------------------
# KIOSK PAGE
# --------------------------------------------------------------------------
KIOSK_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>VEX Event Display</title>
<script src="https://cdn.tailwindcss.com"></script>
<style>
/* All kiosk layout lives in this stylesheet so the display still works if the Tailwind CDN is unreachable. */
:root{
  --bg:#0a0e14; --panel:#111823; --panel2:#17212f; --line:#26364a; --text:#eef3f9; --dim:#8da2ba;
  --red:#ff4d5e; --redbg:#3a1820; --blue:#4da3ff; --bluebg:#142a45; --gold:#ffc83d; --green:#2ee66b; --amber:#ffb020;
}
*{box-sizing:border-box;margin:0;padding:0}
html{font-size:calc(.5vw + .5vh);height:100%;background:var(--bg);overflow:hidden}
body{height:100%;background:var(--bg);color:var(--text);font-family:"Barlow Condensed","Roboto Condensed","DejaVu Sans Condensed","Liberation Sans Narrow",Arial,sans-serif;
  display:flex;flex-direction:column;cursor:none;user-select:none}
.num{font-variant-numeric:tabular-nums}
#panes{flex:1;min-height:0;display:grid;grid-template-columns:1fr 1fr;gap:.9rem;padding:.9rem}
.pane{min-width:0;min-height:0;display:flex;flex-direction:column;background:var(--panel);border:.14rem solid var(--line);border-radius:.8rem;overflow:hidden}
.phead{display:flex;align-items:center;justify-content:space-between;gap:1rem;padding:.7rem 1.2rem;background:var(--panel2);border-bottom:.14rem solid var(--line)}
.badge{font-weight:800;font-size:2.1rem;letter-spacing:.02em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.badge .div{color:var(--gold)}
.api{display:flex;align-items:center;gap:.6rem;font-weight:700;font-size:1.5rem;white-space:nowrap}
.dot{width:1.2rem;height:1.2rem;border-radius:50%;background:var(--green);box-shadow:0 0 .8rem var(--green)}
.dot.warn{background:var(--amber);box-shadow:0 0 .8rem var(--amber)}
.dot.off{background:var(--red);box-shadow:0 0 .8rem var(--red)}
.pbody{flex:1;min-height:0;display:grid;grid-template-columns:minmax(0,56fr) minmax(0,44fr)}   /* rankings left, matches right */
.matches{display:flex;flex-direction:column;min-height:0;border-left:.14rem solid var(--line);padding:.7rem .8rem;gap:.6rem}
.sec-title{font-weight:800;font-size:1.45rem;color:var(--dim);letter-spacing:.08em;padding:0 .2rem .3rem}
.sec{display:flex;flex-direction:column;gap:.45rem;min-height:0}
.sec.last{flex:0 0 auto}
.sec.next{flex:1}
.card{background:var(--bg);border:.12rem solid var(--line);border-radius:.6rem;padding:.3rem .7rem;display:flex;flex-direction:column;justify-content:center;gap:.2rem;min-height:0}
.sec.next .card{flex:1}
.card.upnext{border-color:var(--gold);box-shadow:inset 0 0 0 .1rem var(--gold)}
.row1{display:flex;align-items:center;gap:.6rem;font-size:1.25rem;font-weight:700}
.qbadge{background:var(--text);color:var(--bg);border-radius:.4rem;padding:0 .6rem;font-weight:800;font-size:1.3rem}
.field{color:var(--dim);margin-left:auto}
.nextflag{margin-left:auto;background:var(--gold);color:#1a1300;font-weight:800;border-radius:.4rem;padding:0 .6rem;font-size:1.3rem}
.win{margin-left:auto;font-weight:800;border-radius:.4rem;padding:0 .6rem;font-size:1.3rem}
.win.red{background:var(--red);color:#fff}.win.blue{background:var(--blue);color:#021428}.win.tie{background:var(--dim);color:#0a0e14}
.row2{display:grid;grid-template-columns:1fr 1fr;gap:.5rem}
.alli{display:flex;align-items:center;justify-content:space-between;gap:.4rem;border-radius:.45rem;padding:.15rem .5rem;font-weight:800;font-size:1.7rem;min-width:0}
.alli.red{background:var(--redbg);color:var(--red)}.alli.blue{background:var(--bluebg);color:var(--blue)}
.alli .teams{display:flex;gap:.6rem;min-width:0;white-space:nowrap;overflow:hidden}
.alli .score{font-size:2.3rem;color:#fff}
.alli.lose{opacity:.55}
.empty{color:var(--dim);font-size:1.5rem;padding:1rem .4rem}
.ranks{display:flex;flex-direction:column;min-height:0;padding:.7rem .8rem}
.rhead{display:flex;justify-content:space-between;align-items:baseline;padding:0 .2rem .3rem}
.pageind{font-weight:700;font-size:1.3rem;color:var(--dim)}
.rwrap{flex:1;min-height:0;overflow:hidden}
table{width:100%;border-collapse:collapse}
th{font-size:1.25rem;color:var(--dim);font-weight:800;text-align:right;padding:.3rem .4rem;border-bottom:.12rem solid var(--line);letter-spacing:.05em}
td{font-size:1.75rem;font-weight:700;text-align:right;padding:0 .4rem;height:calc(var(--rowh,3.7)*1rem);border-bottom:.08rem solid #1b2738}
th:first-child,td:first-child,th:nth-child(2),td:nth-child(2){text-align:left}
td:nth-child(2){font-weight:800}
td.rk{color:var(--gold);font-weight:800}
td:nth-child(2){color:#fff}
.tn{display:inline-block;padding:0 .5rem;margin-left:-.5rem}
tr.qteam .tn{background:var(--gold);color:#1a1300;border-radius:.35rem}   /* only the team number, for teams headed to the queuing tables */
td.avg{color:var(--green)}
.sec.last{margin-bottom:.2rem}
.queue-sec{background:var(--gold);border-radius:.7rem;padding:.35rem .5rem}
.queue-title{color:#1a1300}
.card.queuing{border-color:#1a1300}
/* the first NOW QUEUING card is steady but brighter than every other match card */
.card.first{background:#46607f;border-color:#fff;box-shadow:0 0 1.2rem rgba(255,255,255,.6)}
.card.first .field{color:#fff}
.matches.dense{gap:.35rem}
.matches.dense .card{padding:.15rem .6rem;gap:.1rem}
.matches.dense .qbadge{font-size:1.05rem}
.matches.dense .row1{font-size:1rem}
.matches.dense .card .bl{font-size:1.1rem}
.card .bl{font-size:1.35rem;line-height:1.15;padding:.05rem .6rem}
.card .bl.win{margin:0 -.7rem;padding:.05rem 1.3rem}   /* winner's block runs the full width of the card */
/* alliance line: a fixed score column (room for 199), the rest split in two halves with one team centred in each */
.card .bl{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr) 3ch;column-gap:.4rem;align-items:center}
.card .bl .bt{display:contents}
.card .bl .bt>span{text-align:center;min-width:0;overflow:hidden;white-space:nowrap}
.card .bl .bs{text-align:right}
.card.blank .bl{opacity:.3}
/* single (full screen): rankings use the left 3/4 as several columns, matches keep the right 1/4 */
#panes.single .pane:not(.ph-bracket):not(.ph-rankings) .pbody{grid-template-columns:minmax(0,3fr) minmax(0,1fr)}
#panes.single .pane{--rowh:3.5}
#panes.single td{font-size:1.4rem;padding:0 .25rem}
#panes.single th{font-size:1.05rem;padding:.3rem .25rem}
.rcols{display:grid;gap:1.4rem;height:100%}
.rview{overflow:hidden;height:100%}
.rtrack{display:flex;height:100%}
.rcol{flex:0 0 auto;box-sizing:border-box;padding:0 .7rem}
.bl.red.win{background:#ff2d45;color:#fff;box-shadow:0 0 .7rem rgba(255,45,69,.75)}
.bl.blue.win{background:#2f7bff;color:#fff;box-shadow:0 0 .7rem rgba(47,123,255,.75)}
.bl.win .sd{color:#fff}
#panes.single .bl{font-size:1.5rem}
#panes.single .ctitle{font-size:1.3rem}
#panes.single .bh{font-size:1.15rem}
#panes.single .sd{font-size:1.2rem;flex-basis:2rem}
.lunchq{display:none;flex:1;min-height:0;flex-direction:column;gap:1rem;background:var(--gold);margin:.8rem;border-radius:.8rem;padding:1rem 1.3rem}
.pane.ph-lunchq .pbody{display:none}
.pane.ph-lunchq .lunchq{display:flex}
.lq-head{display:flex;justify-content:space-between;align-items:baseline;gap:1rem;color:#1a1300}
.lq-title{font-weight:800;font-size:3.6rem;letter-spacing:.04em}
.lq-sub{font-weight:800;font-size:2.2rem}
.lq-cards{flex:1;min-height:0;display:grid;gap:1.1rem}
.lq-cards .card{padding:.8rem 1.2rem;gap:.7rem}
.lq-cards .qbadge{font-size:3rem;padding:0 1rem}
.lq-cards .row1{font-size:2.4rem}
.lq-cards .card .bl{font-size:3.4rem;padding:.2rem 1rem}
.msgbox{display:none;flex:1;min-height:0;flex-direction:column;align-items:center;justify-content:center;gap:1.4rem;text-align:center;padding:2rem}
.pane.ph-msg .pbody{display:none}
.pane.ph-msg .msgbox{display:flex}
.msg-big{font-weight:800;font-size:3.4rem;color:var(--gold);letter-spacing:.03em;line-height:1.1}
.msg-sub{font-size:1.9rem;color:var(--dim)}
.msg-url{font-size:2.2rem;font-weight:700;color:var(--text)}
#panes.single .msg-big{font-size:5.5rem}
#panes.single .msg-sub{font-size:2.4rem}
#panes.single .msg-url{font-size:3rem}
#panes.single .bracket{grid-template-columns:repeat(4,1fr)}   /* full screen: each round is a quarter of the screen */
/* phases: quals (default) | rankings (rankings only) | bracket */
.bracket{display:none;min-height:0;min-width:0;padding:.7rem .8rem;grid-template-columns:repeat(6,1fr);grid-template-rows:auto repeat(8,minmax(0,1fr));column-gap:0;row-gap:.3rem}
.pane.ph-bracket .bracket{display:grid}
.pane.ph-bracket .matches,.pane.ph-bracket .ranks,.pane.ph-rankings .matches{display:none}
.pane.ph-bracket .pbody,.pane.ph-rankings .pbody{grid-template-columns:1fr}
.pane.ph-rankings .ranks{padding:.9rem 2rem}
.pane{--rowh:3.7}
.pane.ph-rankings{--rowh:4.4}
.pane.ph-rankings td{font-size:2.4rem}
.pane.ph-rankings th{font-size:1.5rem}
.bcell{display:flex;flex-direction:column;justify-content:center;gap:.4rem;min-height:0;min-width:0}
.ctitle{font-weight:800;font-size:1.05rem;color:var(--dim);letter-spacing:.05em;white-space:nowrap;overflow:hidden}
.blabel{align-self:center;text-align:center;font-weight:800;font-size:1.9rem;color:#2a3d58}
.bwrap{display:flex;align-items:center;min-height:0;min-width:0}
.bm{width:100%;min-width:0;background:var(--bg);border:.12rem solid var(--line);border-radius:.5rem;padding:.2rem .35rem;display:flex;flex-direction:column;gap:.12rem}
.sd{flex:0 0 1.5rem;color:var(--dim);font-size:1rem;font-weight:700}
.bl .bt{flex:1}
.bm.done{border-color:#3b5372}
.bh{font-size:.95rem;font-weight:800;color:var(--dim);white-space:nowrap}
.bl{display:flex;align-items:center;gap:.3rem;font-weight:800;font-size:1.3rem;border-radius:.3rem;padding:0 .3rem;min-width:0}
.bl.red{background:var(--redbg);color:var(--red)}.bl.blue{background:var(--bluebg);color:var(--blue)}
.bl .bt{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.bl .bt.ph{font-weight:600;font-style:italic;opacity:.5}
.bl.win .bs{color:#fff}
.bm .bl.win{margin:0 -.35rem;padding:0 .65rem}   /* winner's block runs the full width of the card */
.bl .bt{display:flex;gap:.9rem}                   /* team numbers a bit further apart */
.bcell.fin .bm{flex:0 1 auto;max-height:none;padding:.35rem .45rem}
#gear{position:fixed;top:.4rem;right:.5rem;width:2.4rem;height:2.4rem;color:var(--dim);opacity:.12;cursor:pointer;z-index:10;transition:opacity .2s}
#gear:hover{opacity:1}
#gear svg{width:100%;height:100%}
</style>
</head>
<body>
<a id="gear" href="/admin" title="Admin setup" aria-label="Admin setup"></a>
<div id="panes">
  <section class="pane" id="pane-hs"></section>
  <section class="pane" id="pane-ms"></section>
</div>
<script>
/* ---------- tiny Lucide-style inline icons ---------- */
const ICONS = {
  settings:'<circle cx="12" cy="12" r="3"/><path d="M12 1v3M12 20v3M4.2 4.2l2.1 2.1M17.7 17.7l2.1 2.1M1 12h3M20 12h3M4.2 19.8l2.1-2.1M17.7 6.3l2.1-2.1"/>'
};
function icon(n){return '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'+ICONS[n]+'</svg>';}
document.getElementById('gear').innerHTML = icon('settings');

/* ---------- config (server is source of truth; localStorage is the offline mirror) ---------- */
const LS_KEY = 'vexKioskCfg';
const DEFAULT_CFG = {view:'single',hs:{sku:'',demo:false,fields:3},ms:{sku:'',demo:false,fields:2},poll:15,scroll:8,rev:0};
let cfg = (function(){ try { return Object.assign({}, DEFAULT_CFG, JSON.parse(localStorage.getItem(LS_KEY)||'{}')); } catch(e){ return Object.assign({}, DEFAULT_CFG); } })();
function saveLocal(){ try { localStorage.setItem(LS_KEY, JSON.stringify(cfg)); } catch(e){} }

const esc = s => String(s==null?'':s).replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const S = { hs:{data:null, fails:0, timer:null}, ms:{data:null, fails:0, timer:null} };
let page = 0, countdown = cfg.scroll, lastSeq = null, ip = location.hostname;

/* ---------- pane skeleton ---------- */
function buildPane(div){
  document.getElementById('pane-'+div).innerHTML =
    '<div class="phead"><div class="badge" id="b-'+div+'"></div><div class="api" id="a-'+div+'"></div></div>'+
    '<div class="pbody">'+
      '<div class="ranks"><div class="rhead"><div class="sec-title" id="rt-'+div+'" style="padding:0">TEAM RANKINGS</div><div class="pageind num" id="pi-'+div+'"></div></div>'+
        '<div class="rwrap" id="rw-'+div+'"></div></div>'+
      '<div class="matches">'+
        '<div class="sec last"><div id="last-'+div+'" class="sec"></div></div>'+
        '<div class="sec next"><div class="sec-title">ON FIELD NOW</div><div id="next-'+div+'" class="sec next" style="flex:1"></div></div>'+
        '<div class="sec next queue-sec"><div class="sec-title queue-title">NOW QUEUING</div><div id="queue-'+div+'" class="sec next" style="flex:1"></div></div>'+
      '</div>'+
      '<div class="bracket" id="bk-'+div+'"></div>'+
    '</div>'+
    '<div class="msgbox" id="mg-'+div+'"></div>'+
    '<div class="lunchq" id="lq-'+div+'"></div>';
}

/* ---------- renderers ---------- */
function teamsHtml(a, cls){ return a.map(t=>'<span'+(cls?' class="'+cls+'"':'')+'>'+esc(t)+'</span>').join(''); }

/* Qualification match card, laid out like the elimination cards: Q badge (white) with the field name on the right,
   then a red bar and a blue bar. The field name is dropped once the match is scored; the winner's bar lights up. */
function bar(cls, teams, score, win){
  return '<div class="bl '+cls+(win?' win':'')+'"><span class="bt">'+teamsHtml(teams)+'</span><span class="bs num">'+(score==null?'':score)+'</span></div>';
}
function matchCard(m, mode){   /* mode: 'scored' | 'field' | 'queue' | 'first' (first queuing card is shown brighter) */
  const sc = !!m.scored, rw = sc && m.rs>m.bs, bw = sc && m.bs>m.rs;
  const cls = mode==='first' ? ' queuing first' : mode==='queue' ? ' queuing' : '';
  return '<div class="card'+cls+'"><div class="row1"><span class="qbadge num">Q '+m.num+'</span>'+((!sc && m.field)?'<span class="field">'+esc(m.field)+'</span>':'')+'</div>'+
    bar('red', m.red, sc?m.rs:null, rw)+bar('blue', m.blue, sc?m.bs:null, bw)+'</div>';
}
function blankCard(){   /* empty card closing each table: no Q number, no teams */
  return '<div class="card blank"><div class="row1"><span class="qbadge" style="visibility:hidden">Q</span></div><div class="bl red"><span class="bt">&nbsp;</span></div><div class="bl blue"><span class="bt">&nbsp;</span></div></div>';
}
function matchGroups(div){
  const d = S[div].data;
  let n = Math.max(1, Math.min(4, +(cfg[div] && cfg[div].fields) || (div==='hs' ? 3 : 2)));   /* number of fields */
  if (d && d.demo && d.fields) n = Math.max(1, Math.min(4, +d.fields));     /* the demo brings its own field count */
  if (!d) return {n:n, scored:[], onField:[], queuing:[]};
  const upcoming = d.matches.filter(m=>!m.scored);
  return {n:n, scored:d.matches.filter(m=>m.scored).slice(-2).reverse(), onField:upcoming.slice(0,n), queuing:upcoming.slice(n,2*n)};
}

function pageSize(div){
  const wrap = document.getElementById('rw-'+div);
  const rem = parseFloat(getComputedStyle(document.documentElement).fontSize);
  const rowv = parseFloat(getComputedStyle(document.getElementById('pane-'+div)).getPropertyValue('--rowh')) || 3.7;
  const rowH = rowv*rem + 1.3;                          /* td height + border */
  const headH = 2.6*rem;
  return Math.max(3, Math.floor((wrap.clientHeight - headH) / rowH));
}

function renderStatus(div){
  const s = S[div], d = s.data;
  let cls='', txt='API: ONLINE';
  if (!d){ cls='off'; txt = s.fails>0 ? 'API: OFFLINE' : 'API: CONNECTING'; if(s.fails===0) cls='warn'; }
  else if (s.fails>=3){ cls='off'; txt='API: OFFLINE'; }
  else if (s.fails>0 || !d.online){ cls='warn'; txt='API: RECONNECTING'; }
  document.getElementById('a-'+div).innerHTML = '<span class="dot '+cls+'"></span><span>'+txt+'</span>';
}

function renderMatches(div){
  const d = S[div].data;
  const last = document.getElementById('last-'+div), nxt = document.getElementById('next-'+div), que = document.getElementById('queue-'+div);
  if (!d){ last.innerHTML = '<div class="empty">Waiting for data…</div>'; nxt.innerHTML=''; que.innerHTML=''; return; }
  const g = matchGroups(div);
  last.closest('.matches').classList.toggle('dense', g.n >= 4);   /* only 4 fields get the compact size */
  last.innerHTML = g.scored.length ? g.scored.map(m=>matchCard(m,'scored')).join('') : '<div class="empty">No matches scored yet</div>';
  /* one slot per field in each table; a slot with no match left stays an empty card (no Q number, no teams) */
  const fill = (arr, mk) => arr.map(mk).join('') + new Array(Math.max(0, g.n - arr.length)).fill(blankCard()).join('');
  nxt.innerHTML = fill(g.onField, m=>matchCard(m,'field'));
  que.innerHTML = fill(g.queuing, (m,i)=>matchCard(m, i===0 ? 'first' : 'queue'));
}

/* number of ranking columns: 1 in double mode; in single (full screen) 3 beside the matches, 4 when rankings-only */
function rankCols(div){
  if (isDouble()) return 1;
  return document.getElementById('pane-'+div).classList.contains('ph-rankings') ? 4 : 3;
}
function renderRanks(div){
  if (!visible(div)) return;
  const s = S[div], d = s.data, wrap = document.getElementById('rw-'+div), pi = document.getElementById('pi-'+div);
  if (!d || !d.rankings.length){ wrap.innerHTML='<div class="empty">'+(d? 'No rankings published yet':'Waiting for data…')+'</div>'; pi.textContent=''; wrap._sig=''; return; }
  const qt = new Set(); matchGroups(div).queuing.forEach(m=>m.red.concat(m.blue).forEach(t=>qt.add(t)));   /* teams at the queuing tables */
  const K = rankCols(div), size = pageSize(div);
  const table = rs => '<table><thead><tr><th>#</th><th>TEAM</th><th>W-L-T</th><th>WP</th><th>AP</th><th>SP</th><th>AVG</th></tr></thead><tbody>'+
    rs.map(r=>'<tr class="'+(qt.has(r.team)?'qteam ':'')+'num"><td class="rk">'+r.rank+'</td><td><span class="tn">'+esc(r.team)+'</span></td><td>'+r.w+'-'+r.l+'-'+r.t+'</td><td>'+r.wp+'</td><td>'+r.ap+'</td><td>'+r.sp+'</td><td class="avg">'+(+r.avg).toFixed(1)+'</td></tr>').join('')+
    '</tbody></table>';
  if (isDouble()){                                     /* half-screen: one column, flips page by page */
    const pages = Math.max(1, Math.ceil(d.rankings.length/size)), p = page % pages;
    pi.textContent = 'PAGE '+(p+1)+' / '+pages;
    wrap.innerHTML = '<div class="rcols" style="grid-template-columns:minmax(0,1fr)"><div>'+table(d.rankings.slice(p*size, p*size+size))+'</div></div>';
    wrap._sig = '';
    return;
  }
  /* full screen: K columns side by side. When there are more columns than fit, the whole strip slides left one
     column per step and the column that leaves on the left comes back in on the right (see advanceRanks). */
  const C = Math.max(1, Math.ceil(d.rankings.length/size));
  const cols = []; for (let c=0; c<C; c++) cols.push(table(d.rankings.slice(c*size, (c+1)*size)));
  const loop = C > K, track = loop ? cols.concat(cols.slice(0, K)) : cols, L = track.length;   /* first K columns repeated at the end */
  pi.textContent = '';
  const inner = track.map(h=>'<div class="rcol" style="width:'+(100/L)+'%">'+h+'</div>').join('');
  const sig = K+'|'+size+'|'+(loop?C:0)+'|'+inner;
  if (wrap._sig === sig) return;                       /* nothing changed: leave the running animation alone */
  wrap._sig = sig;
  const pos = loop ? (s.rpos||0) % C : 0;
  wrap.innerHTML = '<div class="rview"><div class="rtrack" data-c="'+(loop?C:0)+'" style="width:'+(L*100/K)+'%;transform:translateX(-'+(pos*100/L)+'%)">'+inner+'</div></div>';
}
function advanceRanks(div){
  const s = S[div]; if (!s.data || !visible(div)) return;
  const tr = document.getElementById('rw-'+div).querySelector('.rtrack');
  const C = tr ? +tr.dataset.c : 0; if (!C) return;    /* everything fits: nothing to scroll */
  const L = C + rankCols(div);
  s.rpos = ((s.rpos||0) + 1);
  tr.style.transition = 'transform .9s ease-in-out';
  tr.style.transform = 'translateX(-'+(s.rpos*100/L)+'%)';
  if (s.rpos >= C){                                     /* now showing the repeated columns: jump back to the start unseen */
    setTimeout(function(){ tr.style.transition = 'none'; tr.style.transform = 'translateX(0)'; s.rpos = 0; }, 1000);
  }
}
function renderHeader(div){
  const d = S[div].data, ph = (d && d.phase) || 'quals';
  const lbl = isDouble() ? '<span class="div">'+div.toUpperCase()+'</span> ' : '';
  let txt;
  if (ph==='quals' || ph==='nosched') txt = 'Quals';
  else if (ph==='lunch_rank' || ph==='lunch_queue') txt = 'Lunch Break';
  else if (ph==='bracket') txt = 'Finals';
  else if (ph==='setup') txt = 'Setup';
  else txt = d ? d.name : (cfg[div].sku || 'Not configured');      /* rankings only: event name */
  document.getElementById('b-'+div).innerHTML = lbl + esc(txt);
  renderStatus(div);
}
function resumeTime(d){ return d && d.lunchEnd ? new Date(d.lunchEnd*1000).toLocaleTimeString([], {hour:'numeric', minute:'2-digit'}) : ''; }
/* lunch break, last minutes: the matches that go on the fields next, shown as large NOW QUEUING cards */
function renderLunchQ(div){
  const d = S[div].data, box = document.getElementById('lq-'+div);
  if (!d || d.phase !== 'lunch_queue'){ box.innerHTML = ''; return; }
  const g = matchGroups(div), n = Math.max(1, g.onField.length);
  box.innerHTML = '<div class="lq-head"><div class="lq-title">NOW QUEUING</div><div class="lq-sub">MATCHES RESUME '+esc(resumeTime(d))+'</div></div>'+
    '<div class="lq-cards" style="'+(isDouble() ? 'grid-template-rows:repeat('+n+',minmax(0,1fr))' : 'grid-template-columns:repeat('+n+',minmax(0,1fr))')+'">'+
    g.onField.map((m,i)=>matchCard(m, i===0 ? 'first' : 'queue')).join('')+'</div>';
}
function adminUrl(){ return 'http://'+ip+(location.port ? ':'+location.port : '')+'/admin'; }
function renderMsg(div){
  const d = S[div].data, box = document.getElementById('mg-'+div), ph = d && d.phase;
  if (ph==='setup') box.innerHTML = '<div class="msg-big">SETUP NEEDS TO BE COMPLETED</div><div class="msg-sub">Open this address on any device on the network and enter the event details</div><div class="msg-url">'+esc(adminUrl())+'</div>';
  else if (ph==='nosched') box.innerHTML = '<div class="msg-big">MATCH SCHEDULE NOT FINALIZED</div>'+(d.name?'<div class="msg-sub">'+esc(d.name)+'</div>':'')+'<div class="msg-sub">Matches will appear here automatically once they are published</div>';
  else box.innerHTML = '';
}
function isDouble(){ return (cfg.view || 'single') === 'double'; }
function visible(div){ return isDouble() || div === 'hs'; }   /* single mode shows the HS tournament only */
function applyView(){
  const pn = document.getElementById('panes');
  ['hs','ms'].forEach(d=>{ document.getElementById('pane-'+d).style.display = visible(d) ? '' : 'none'; });
  pn.style.gridTemplateColumns = isDouble() ? '1fr 1fr' : '1fr';
  pn.classList.toggle('single', !isDouble());
}
function renderDiv(d){ if (!visible(d)) return; applyPhase(d); renderHeader(d); renderMsg(d); renderLunchQ(d); renderMatches(d); renderBracket(d); renderRanks(d); }
function renderAll(){ applyView(); ['hs','ms'].forEach(renderDiv); }

/* ---------- phases: qualification view -> rankings only -> single-elimination bracket ---------- */
function applyPhase(div){
  const d = S[div].data, p = document.getElementById('pane-'+div);
  const ph = d && d.phase ? d.phase : 'quals';
  p.classList.toggle('ph-rankings', ph==='rankings' || ph==='lunch_rank');
  p.classList.toggle('ph-lunchq', ph==='lunch_queue');
  p.classList.toggle('ph-bracket', ph==='bracket');
  p.classList.toggle('ph-msg', ph==='setup' || ph==='nosched');
  document.getElementById('rt-'+div).textContent = ph==='rankings' ? 'FINAL QUALIFICATION RANKINGS' : ph==='lunch_rank' ? 'LUNCH BREAK - MATCHES RESUME '+resumeTime(d) : 'TEAM RANKINGS';
}

/* 16-alliance single elimination: R16 #1-8 (slots 1-8) -> QF #1-4 (9-12) -> SF #1-2 (13-14) -> Final (15) */
const SEED = {1:[1,16],2:[8,9],3:[4,13],4:[5,12],5:[2,15],6:[7,10],7:[3,14],8:[6,11]};
const FEED = {9:[1,2],10:[3,4],11:[5,6],12:[7,8],13:[9,10],14:[11,12],15:[13,14]};
const slotName = s => s<=8 ? 'R16 #'+s : s<=12 ? 'QF #'+(s-8) : s<=14 ? 'SF #'+(s-12) : 'Final';

function renderBracket(div){
  const d = S[div].data, el = document.getElementById('bk-'+div);
  if (!d || d.phase!=='bracket'){ el.innerHTML=''; return; }
  const by = {}; (d.elims||[]).forEach(m=>{ by[m.slot]=m; });
  const al = {}; (d.alliances||[]).forEach(a=>{ al[a.n]=a.teams; });
  /* seed numbers for any team list that appears in the Round of 16 */
  const seedOf = {};
  for (let s=1; s<=8; s++){ const m = by[s]; if (m){ if (m.red.length) seedOf[m.red.join(' ')] = SEED[s][0]; if (m.blue.length) seedOf[m.blue.join(' ')] = SEED[s][1]; } }
  Object.keys(al).forEach(n=>{ if (al[n] && al[n].length) seedOf[al[n].join(' ')] = +n; });
  /* red side = first feeder, blue side = second feeder (matches VEX's own bracket).
     The moment a match is scored its winner appears in the next round's block. */
  function side(slot, i){
    const m = by[slot]; let seed = null, teams = null, ph = '';
    if (slot<=8){ seed = SEED[slot][i]; ph = 'ALLIANCE '+seed; if (al[seed] && al[seed].length) teams = al[seed]; }
    else { const f = FEED[slot][i]; ph = 'W '+slotName(f); const w = winner(f); if (w){ seed = w.seed; teams = w.teams; } }
    const own = m ? (i ? m.blue : m.red) : null;
    if (own && own.length) teams = own;
    if (teams && seed===null && seedOf[teams.join(' ')] !== undefined) seed = seedOf[teams.join(' ')];
    return {seed:seed, teams:teams, ph:ph};
  }
  function winner(slot){
    const m = by[slot]; if (!m || !m.scored || m.rs===m.bs) return null;
    return side(slot, m.rs>m.bs ? 0 : 1);
  }
  const line = (cls,x,score,win,lose) => '<div class="bl '+cls+(win?' win':'')+(lose?' lose':'')+'"><span class="sd num">'+(x.seed==null?'':x.seed)+'</span><span class="bt'+(x.teams?'':' ph')+'">'+(x.teams ? x.teams.map(t=>'<span>'+esc(t)+'</span>').join('') : esc(x.ph))+'</span><span class="bs num">'+(score==null?'':score)+'</span></div>';
  function card(slot){
    const m = by[slot], a = side(slot,0), b = side(slot,1);
    const sc = !!(m&&m.scored), rw = sc&&m.rs>m.bs, bw = sc&&m.bs>m.rs;
    return '<div class="bm'+(sc?' done':'')+'"><div class="bh">'+slotName(slot)+'</div>'+line('red',a,sc?m.rs:null,rw,bw)+line('blue',b,sc?m.bs:null,bw,rw)+'</div>';
  }
  let h = '';
  ['ROUND OF 16','QUARTERFINALS','SEMIFINALS','FINALS'].forEach((t,i)=>{ h += '<div class="ctitle" style="grid-column:'+(isDouble() ? [1,3,4,5] : [1,2,3,4])[i]+';grid-row:1;overflow:visible">'+t+'</div>'; });
  for (let s=1; s<=15; s++){
    const sgl = !isDouble();
    const col = sgl ? (s<=8?1 : s<=12?2 : s<=14?3 : 4)          /* full screen: one quarter per round */
                    : (s<=8?1 : s<=12?3 : s<=14?4 : 5);          /* double: each round starts halfway across the previous one */
    const z = s<=8?4 : s<=12?3 : s<=14?2 : 1;     /* later rounds tuck underneath earlier ones */
    const span = s<=8?1 : s<=12?2 : s<=14?4 : 8;
    const start = s<=8 ? s : s<=12 ? 2*(s-9)+1 : s<=14 ? 4*(s-13)+1 : 1;
    h += '<div class="bwrap" style="grid-column:'+col+' / span '+(sgl?1:2)+';grid-row:'+(start+1)+' / span '+span+';z-index:'+z+((sgl ? s<15 : s<=8)?';padding-right:.9rem':'')+'">'+card(s)+'</div>';
  }
  el.innerHTML = h;
}

/* ---------- polling with backoff ---------- */
async function fetchJson(url, ms){
  const ctl = new AbortController(), t = setTimeout(()=>ctl.abort(), ms||10000);
  try { const r = await fetch(url, {signal:ctl.signal, cache:'no-store'}); if(!r.ok) throw new Error('HTTP '+r.status); return await r.json(); }
  finally { clearTimeout(t); }
}
async function poll(div){
  const s = S[div]; clearTimeout(s.timer);
  if (!visible(div)) return;
  try {
    const d = await fetchJson('/api/data/'+div, 12000);
    s.data = d; s.fails = d.online ? 0 : Math.min(s.fails+1, 2);
  } catch(e){ s.fails++; }
  renderDiv(div);
  const delay = s.fails ? Math.min(cfg.poll*1000*Math.pow(2, s.fails-1), 60000) : ((s.data && s.data.demo) ? 2000 : cfg.poll*1000);
  s.timer = setTimeout(()=>poll(div), delay);
}
function pollAll(){ poll('hs'); poll('ms'); }

/* ---------- config + remote command channel ---------- */
async function syncControl(){
  try {
    const c = await fetchJson('/api/command', 4000);
    if (lastSeq === null){ lastSeq = c.seq; }
    else if (c.seq !== lastSeq){
      lastSeq = c.seq;
      if (c.cmd==='refresh' || c.cmd==='restartdemo') pollAll();
      else if (c.cmd==='nextpage'){ page++; countdown = cfg.scroll; ['hs','ms'].forEach(d => isDouble() ? renderRanks(d) : advanceRanks(d)); }
      else if (c.cmd==='reload') location.reload();
      else if (c.cmd==='clearcache') clearEverything();
    }
    if (c.rev !== cfg.rev){
      const nc = await fetchJson('/api/config', 4000);
      cfg = nc; saveLocal(); countdown = Math.min(countdown, cfg.scroll); pollAll(); renderAll();
    }
  } catch(e){ /* server unreachable: keep running on cached cfg + data */ }
}
async function clearEverything(){
  try { localStorage.clear(); sessionStorage.clear(); } catch(e){}
  try { if (window.caches){ const ks = await caches.keys(); await Promise.all(ks.map(k=>caches.delete(k))); } } catch(e){}
  location.reload();
}

/* ---------- 1s tick: page cycling ---------- */
setInterval(function(){
  countdown--;
  if (countdown <= 0){ page++; countdown = cfg.scroll; ['hs','ms'].forEach(d => isDouble() ? renderRanks(d) : advanceRanks(d)); }
}, 1000);
window.addEventListener('resize', ()=>['hs','ms'].forEach(renderRanks));
window.addEventListener('online', pollAll);

/* ---------- boot ---------- */
(async function(){
  buildPane('hs'); buildPane('ms'); renderAll();
  try { const si = await fetchJson('/api/sysinfo', 4000); ip = si.ip; } catch(e){}
  try { cfg = await fetchJson('/api/config', 4000); saveLocal(); } catch(e){}
  countdown = cfg.scroll; renderAll(); pollAll();
  await syncControl();
  setInterval(syncControl, 3000);
  /* nightly safety reload keeps long-running kiosks healthy */
  setTimeout(()=>location.reload(), 6*3600*1000);
})();
</script>
</body>
</html>
"""

# --------------------------------------------------------------------------
# ADMIN PAGE
# --------------------------------------------------------------------------
ADMIN_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>VEX Event Display Admin</title>
<script src="https://cdn.tailwindcss.com"></script>
<style>
  body{background:#0a0e14;color:#eef3f9;font-family:"Barlow","Segoe UI","DejaVu Sans",Arial,sans-serif}
  .card{background:#111823;border:1px solid #26364a;border-radius:.75rem;padding:1.25rem}
  label{display:block;font-size:.85rem;color:#8da2ba;margin-bottom:.25rem}
  input[type=text],input[type=number],input[type=time],textarea{width:100%;background:#0a0e14;border:1px solid #26364a;border-radius:.5rem;padding:.55rem .7rem;color:#eef3f9}
  select{width:100%;background:#0a0e14;border:1px solid #26364a;border-radius:.5rem;padding:.55rem .7rem;color:#eef3f9}
  input:focus,textarea:focus,select:focus,button:focus-visible{outline:2px solid #ffc83d;outline-offset:1px}
  .btn{display:inline-flex;align-items:center;gap:.4rem;background:#1c2a3d;border:1px solid #2f4560;border-radius:.5rem;padding:.5rem .9rem;font-weight:600;cursor:pointer}
  .btn:hover{background:#25374f}
  .btn.primary{background:#ffc83d;color:#1a1300;border-color:#ffc83d}
  .btn.primary:hover{background:#ffd76b}
  .btn.danger{border-color:#ff4d5e;color:#ff7d89}
  .btn svg{width:1.1rem;height:1.1rem}
  pre{background:#070a0f;border:1px solid #1b2738;border-radius:.5rem;padding:.75rem;overflow:auto;font-size:.8rem;max-height:20rem}
  .ok{color:#2ee66b}.bad{color:#ff4d5e}.warn{color:#ffb020}
</style>
</head>
<body class="min-h-screen">
<main class="max-w-5xl mx-auto p-4 md:p-8 space-y-6">
  <header class="flex flex-wrap items-center justify-between gap-3">
    <div>
      <h1 class="text-2xl font-bold">VEX Event Display Setup</h1>
      <p class="text-sm" style="color:#8da2ba">Kiosk: <a class="underline" id="kiosk-link" href="/">/</a> &nbsp; Admin: <span id="admin-link"></span></p>
    </div>
    <div class="flex gap-2">
      <a class="btn" href="/" target="_blank"><span data-i="monitor"></span>Open display</a>
      <button class="btn primary" id="save"><span data-i="save"></span>Save settings</button>
    </div>
  </header>

  <div id="toast" class="hidden card text-sm"></div>

  <section class="card space-y-2">
    <h2 class="font-bold text-lg">Tournaments shown on the kiosk</h2>
    <select id="view"><option value="single">1. Single tournament (FULL SCREEN)</option><option value="double">2. Double tournament: HS (left) and MS (right)</option></select>
    <p class="text-xs" style="color:#8da2ba">Single uses the High School settings below and locks the Middle School settings. Only the tournament(s) shown are polled from events.vex.com.</p>
    <p class="text-xs" style="color:#8da2ba">Demo mode plays a whole event: one qualification match every 3 seconds, 15 seconds on the final rankings (alliance selection), the bracket for 5 seconds, then one bracket match every 3 seconds, then it starts over.</p>
  </section>

  <section class="grid md:grid-cols-2 gap-6">
    <div class="card space-y-3" id="hs-card">
      <h2 class="font-bold text-lg">High School event</h2>
      <div><label for="hs-sku">Event SKU or events.vex.com URL</label><input type="text" id="hs-sku" placeholder="RE-V5RC-XX-XXXX"></div>
      <label class="flex items-center gap-2" style="color:#eef3f9"><input type="checkbox" id="hs-demo"> Use demo data (no internet needed)</label>
      <div><label for="hs-div">Division (optional: id or name, blank = first division of the event)</label><input type="text" id="hs-div"></div>
      <div><label for="hs-fields">Number of fields (1-4): matches on the fields now / queuing</label><input type="number" id="hs-fields" min="1" max="4"></div>
      <div class="grid grid-cols-3 gap-2">
        <div><label for="hs-lunchq">Lunch break after Q # (blank = none)</label><input type="number" id="hs-lunchq" min="0" max="999"></div>
        <div><label for="hs-lunchm">Break length (minutes)</label><input type="number" id="hs-lunchm" min="0" max="600"></div>
        <div><label for="hs-lunchat">or play resumes at</label><input type="time" id="hs-lunchat"></div>
      </div>
      <p class="text-xs" style="color:#8da2ba">A resume time overrides the break length. At the break the screen shows only the rankings; 5 minutes before play resumes it shows the next matches as large NOW QUEUING cards; 30 seconds before, those matches move to ON FIELD NOW on the normal screen.</p>
      <div><label for="hs-phase">Display mode</label><select id="hs-phase"><option value="auto">Automatic (quals, then rankings, then bracket)</option><option value="quals">Always show qualification matches</option><option value="rankings">Always show rankings only</option><option value="bracket">Always show bracket</option></select></div>
      <div class="flex gap-2"><button class="btn" data-test="hs"><span data-i="activity"></span>Test HS connection</button></div>
    </div>
    <div class="card space-y-3" id="ms-card">
      <h2 class="font-bold text-lg">Middle School event</h2>
      <p id="ms-lock" class="text-sm warn hidden">Locked: single-tournament mode shows only the High School tournament.</p>
      <div><label for="ms-sku">Event SKU or events.vex.com URL</label><input type="text" id="ms-sku" placeholder="RE-V5RC-XX-XXXX"></div>
      <label class="flex items-center gap-2" style="color:#eef3f9"><input type="checkbox" id="ms-demo"> Use demo data (no internet needed)</label>
      <div><label for="ms-div">Division (optional: id or name, blank = first division of the event)</label><input type="text" id="ms-div"></div>
      <div><label for="ms-fields">Number of fields (1-4): matches on the fields now / queuing</label><input type="number" id="ms-fields" min="1" max="4"></div>
      <div class="grid grid-cols-3 gap-2">
        <div><label for="ms-lunchq">Lunch break after Q # (blank = none)</label><input type="number" id="ms-lunchq" min="0" max="999"></div>
        <div><label for="ms-lunchm">Break length (minutes)</label><input type="number" id="ms-lunchm" min="0" max="600"></div>
        <div><label for="ms-lunchat">or play resumes at</label><input type="time" id="ms-lunchat"></div>
      </div>
      <p class="text-xs" style="color:#8da2ba">A resume time overrides the break length. At the break the screen shows only the rankings; 5 minutes before play resumes it shows the next matches as large NOW QUEUING cards; 30 seconds before, those matches move to ON FIELD NOW on the normal screen.</p>
      <div><label for="ms-phase">Display mode</label><select id="ms-phase"><option value="auto">Automatic (quals, then rankings, then bracket)</option><option value="quals">Always show qualification matches</option><option value="rankings">Always show rankings only</option><option value="bracket">Always show bracket</option></select></div>
      <div class="flex gap-2"><button class="btn" data-test="ms"><span data-i="activity"></span>Test MS connection</button></div>
    </div>
  </section>

  <section class="card grid sm:grid-cols-2 gap-4">
    <div><label for="poll">Polling / refresh interval (seconds, 5–300)</label><input type="number" id="poll" min="5" max="300"></div>
    <div><label for="scroll">Ranking page cycle (seconds, 3–120)</label><input type="number" id="scroll" min="3" max="120"></div>
    <div><label for="rankWait">Wait after last qual is scored before showing rankings only (seconds, default 300)</label><input type="number" id="rankWait" min="0" max="3600"></div>
  </section>

  <section class="card space-y-3">
    <h2 class="font-bold text-lg">events.vex.com connection</h2>
    <div><label for="token">API bearer token</label><input type="password" id="token" name="vex-api-token" autocomplete="new-password" placeholder="leave blank to keep the saved token">
      <p id="token-hint" class="text-xs mt-1" style="color:#8da2ba"></p></div>
    <div><label for="apiBase">API base URL</label><input type="text" id="apiBase"></div>
    <p class="text-xs" style="color:#8da2ba">Each tournament makes 2 requests per poll (matches + rankings). Intervals under 15 s on both tournaments may hit rate limits; the kiosk pauses and keeps showing the last data if that happens.</p>
  </section>

  <section class="card space-y-3">
    <h2 class="font-bold text-lg">Kiosk controls</h2>
    <div class="flex flex-wrap gap-2">
      <button class="btn" data-cmd="refresh"><span data-i="refresh"></span>Force data refresh</button>
      <button class="btn" data-cmd="nextpage"><span data-i="next"></span>Cycle rank page</button>
      <button class="btn" data-cmd="restartdemo"><span data-i="refresh"></span>Restart demo</button>
      <button class="btn" data-cmd="reload"><span data-i="refresh"></span>Reload kiosk page</button>
      <button class="btn danger" data-cmd="clearcache"><span data-i="trash"></span>Clear kiosk browser cache</button>
    </div>
  </section>

  <section class="card space-y-3">
    <h2 class="font-bold text-lg">Live preview &amp; diagnostics</h2>
    <div id="preview" class="text-sm" style="color:#8da2ba">Run a connection test to see parsed data here.</div>
    <pre id="diag" class="hidden"></pre>
  </section>
</main>

<script>
const ICONS = {
  monitor:'<rect width="20" height="14" x="2" y="3" rx="2"/><line x1="8" x2="16" y1="21" y2="21"/><line x1="12" x2="12" y1="17" y2="21"/>',
  save:'<path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><path d="M17 21v-8H7v8"/><path d="M7 3v5h8"/>',
  activity:'<path d="M22 12h-2.48a2 2 0 0 0-1.93 1.46l-2.35 8.36a.25.25 0 0 1-.48 0L9.24 2.18a.25.25 0 0 0-.48 0l-2.35 8.36A2 2 0 0 1 4.48 12H2"/>',
  refresh:'<path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/><path d="M8 16H3v5"/>',
  next:'<path d="m6 17 5-5-5-5"/><path d="m13 17 5-5-5-5"/>',
  trash:'<path d="M3 6h18"/><path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6"/><path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2"/><line x1="10" x2="10" y1="11" y2="17"/><line x1="14" x2="14" y1="11" y2="17"/>'
};
document.querySelectorAll('[data-i]').forEach(el=>{
  el.innerHTML = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:1.1rem;height:1.1rem">'+ICONS[el.dataset.i]+'</svg>';
});
const $ = id => document.getElementById(id);
const esc = s => String(s==null?'':s).replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const LS_KEY = 'vexKioskCfg';
let cfg = null;

function toast(msg, ok){
  const t = $('toast'); t.className = 'card text-sm ' + (ok===false?'bad':'ok'); t.textContent = msg;
  clearTimeout(toast.t); toast.t = setTimeout(()=>t.classList.add('hidden'), 4000);
}
function fill(){
  $('hs-sku').value = cfg.hs.sku; $('hs-demo').checked = !!cfg.hs.demo; $('hs-div').value = cfg.hs.division || ''; $('hs-phase').value = cfg.hs.phase || 'auto'; $('hs-fields').value = cfg.hs.fields || 3; $('hs-lunchq').value = cfg.hs.lunchAfter || ''; $('hs-lunchm').value = cfg.hs.lunchMins || ''; $('hs-lunchat').value = cfg.hs.lunchResume || '';
  $('ms-sku').value = cfg.ms.sku; $('ms-demo').checked = !!cfg.ms.demo; $('ms-div').value = cfg.ms.division || ''; $('ms-phase').value = cfg.ms.phase || 'auto'; $('ms-fields').value = cfg.ms.fields || 2; $('ms-lunchq').value = cfg.ms.lunchAfter || ''; $('ms-lunchm').value = cfg.ms.lunchMins || ''; $('ms-lunchat').value = cfg.ms.lunchResume || '';
  $('rankWait').value = cfg.rankWait == null ? 300 : cfg.rankWait;
  $('poll').value = cfg.poll; $('scroll').value = cfg.scroll;
  $('view').value = cfg.view === 'double' ? 'double' : 'single'; applyLock(); $('apiBase').value = cfg.apiBase || ''; $('token').value = '';
  $('token-hint').textContent = cfg.tokenSet ? ('A token is saved (ends ' + cfg.tokenHint + ')' + (cfg.tokenExpired ? ', EXPIRED' : cfg.tokenExp ? ', valid until ' + cfg.tokenExp : '') + '. Paste a new one here only to replace it.') : 'No token saved yet.';
}
function read(){
  return {
    hs:{sku:$('hs-sku').value.trim(), demo:$('hs-demo').checked, phase:$('hs-phase').value, division:$('hs-div').value.trim(), fields:+$('hs-fields').value||3, lunchAfter:+$('hs-lunchq').value||0, lunchMins:+$('hs-lunchm').value||0, lunchResume:$('hs-lunchat').value},
    ms:{sku:$('ms-sku').value.trim(), demo:$('ms-demo').checked, phase:$('ms-phase').value, division:$('ms-div').value.trim(), fields:+$('ms-fields').value||2, lunchAfter:+$('ms-lunchq').value||0, lunchMins:+$('ms-lunchm').value||0, lunchResume:$('ms-lunchat').value},
    rankWait: $('rankWait').value === '' ? 300 : +$('rankWait').value,
    poll:+$('poll').value||15, scroll:+$('scroll').value||8, view:$('view').value, apiBase:$('apiBase').value.trim(), token:$('token').value.trim(), rev:cfg?cfg.rev:1
  };
}
async function load(){
  try { const r = await fetch('/api/config',{cache:'no-store'}); cfg = await r.json(); localStorage.setItem(LS_KEY, JSON.stringify(cfg)); }
  catch(e){ try { cfg = JSON.parse(localStorage.getItem(LS_KEY)); } catch(_){} if(!cfg) cfg = {view:'single',hs:{sku:'',demo:false,fields:3},ms:{sku:'',demo:false,fields:2},poll:15,scroll:8,rev:1}; toast('Server unreachable: showing locally saved settings', false); }
  fill();
}
function applyLock(){
  const single = $('view').value === 'single';
  document.querySelectorAll('#ms-card input, #ms-card select, #ms-card button').forEach(el => { el.disabled = single; });
  $('ms-card').style.opacity = single ? '.45' : '1';
  $('ms-lock').classList.toggle('hidden', !single);
}
async function save(){
  try {
    const r = await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(read())});
    if(!r.ok){ let m = ''; try { m = (await r.json()).error || ''; } catch(_){} throw new Error('HTTP '+r.status+(m ? ' - '+m : '')); }
    cfg = await r.json(); localStorage.setItem(LS_KEY, JSON.stringify(cfg)); fill();
    toast('Saved. The kiosk picks up changes within a few seconds.');
  } catch(e){ toast('Save failed: '+e.message, false); }
}
async function test(div){
  /* save first so the test uses what is on screen */
  await save();
  $('preview').innerHTML = '<span class="warn">Testing '+div.toUpperCase()+'…</span>';
  try {
    const r = await fetch('/api/test/'+div,{cache:'no-store'}); const d = await r.json();
    const top = (d.top||[]).map(t=>'#'+t.rank+' '+esc(t.team)+' ('+t.w+'-'+t.l+'-'+t.t+')').join(', ') || 'none';
    const nxt = (d.next||[]).map(m=>'Q'+m.num+' '+esc(m.red.join('/'))+' vs '+esc(m.blue.join('/'))).join('; ') || 'none';
    $('preview').innerHTML =
      '<div class="'+(d.ok?'ok':'bad')+' font-semibold">'+div.toUpperCase()+': '+(d.ok?'DATA STREAM OK':'NO DATA')+(d.demo?' (demo)':'')+'</div>'+
      '<div>Event: <b style="color:#eef3f9">'+esc(d.name)+'</b>'+(d.division?' &middot; Division: '+esc(d.division):'')+'</div>'+
      (d.divisions&&d.divisions.length>1 ? '<div>Divisions in this event: '+d.divisions.map(x=>esc(x.name||x.id)+' (id '+esc(x.id)+')').join(', ')+'</div>' : '')+
      '<div>Qualification matches: '+d.matches+' ('+d.scored+' scored) &middot; Teams ranked: '+d.teams+'</div>'+
      '<div>Display phase: <b style="color:#eef3f9">'+esc(d.phase)+'</b>'+(d.rankingsIn!=null&&d.phase==='quals'?' (rankings-only in '+d.rankingsIn+'s)':'')+' &middot; Elimination matches: '+d.elims+' &middot; Alliances found: '+d.alliances+'</div>'+
      '<div>Top 5: '+top+'</div><div>Next up: '+nxt+'</div>'+(d.error?'<div class="bad">'+esc(d.error)+'</div>':'');
    const pre = $('diag');
    if (d.diag && d.diag.length){
      pre.classList.remove('hidden');
      pre.textContent = d.diag.map(x=>(x.status||'ERR')+'  '+(x.ms!=null?x.ms+'ms':'--')+'  '+x.url+'\n      '+x.note).join('\n');
    } else pre.classList.add('hidden');
  } catch(e){ $('preview').innerHTML = '<span class="bad">Test failed: '+esc(e.message)+'</span>'; }
}
async function cmd(c){
  try {
    const r = await fetch('/api/command',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({cmd:c})});
    if(!r.ok) throw new Error('HTTP '+r.status);
    toast('Sent "'+c+'" to kiosk.');
  } catch(e){ toast('Command failed: '+e.message, false); }
}
$('save').onclick = save;
$('view').onchange = applyLock;
document.querySelectorAll('[data-test]').forEach(b=>b.onclick=()=>test(b.dataset.test));
document.querySelectorAll('[data-cmd]').forEach(b=>b.onclick=()=>cmd(b.dataset.cmd));
fetch('/api/sysinfo').then(r=>r.json()).then(s=>{
  const base = 'http://'+s.ip+(s.port===80?'':':'+s.port);
  $('kiosk-link').textContent = base+'/'; $('kiosk-link').href = '/'; $('admin-link').textContent = base+'/admin';
}).catch(()=>{});
load();
</script>
</body>
</html>
"""


def main():
    ap = argparse.ArgumentParser(description="VEX dual-division kiosk server")
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument("--host", default="0.0.0.0")
    args = ap.parse_args()
    load_config()
    load_state()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True
    ip = local_ip()
    print("VEX Event Display running")
    print("  Kiosk : http://%s:%d/" % (ip, args.port))
    print("  Admin : http://%s:%d/admin" % (ip, args.port))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
