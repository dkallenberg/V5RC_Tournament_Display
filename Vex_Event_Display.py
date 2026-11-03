#!/usr/bin/env python3
"""
VEX Dual-Division Kiosk (HS + MS) for Raspberry Pi
===================================================
Single file. Python 3.8+ standard library only (no pip installs).

Run:      python3 Vex_Event_Display.py                (serves on port 5000)
          sudo python3 Vex_Event_Display.py --port 80 (serves on port 80)
Kiosk:    http://<pi-ip>:5000/
Admin:    http://<pi-ip>:5000/admin

Chromium kiosk launch (Pi desktop):
  chromium-browser --kiosk --noerrdialogs --disable-infobars \
      --autoplay-policy=no-user-gesture-required http://localhost:5000/
"""
import argparse, json, os, random, re, socket, threading, time
import urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "kiosk_config.json")

# --------------------------------------------------------------------------
# Configuration (server-side source of truth; mirrored to localStorage)
# --------------------------------------------------------------------------
# NOTE: events.vex.com does not publish a documented public API. The URL
# templates below are best-effort candidates. Each is tried in order; any
# JSON (or JSON embedded in a page) that looks like matches / rankings is
# used. Use Admin > "Test connection" to see which ones respond, and edit the
# list there if events.vex.com uses different paths. {sku} is substituted.
DEFAULT_ENDPOINTS = "\n".join([
    "https://events.vex.com/api/events/{sku}",
    "https://events.vex.com/api/events/{sku}/matches",
    "https://events.vex.com/api/events/{sku}/rankings",
    "https://events.vex.com/events/{sku}",
    "https://events.vex.com/events/{sku}/matches",
    "https://events.vex.com/events/{sku}/rankings",
])

DEFAULT_CONFIG = {
    "hs": {"sku": "", "demo": True, "phase": "auto", "fields": 2},
    "ms": {"sku": "", "demo": True, "phase": "auto", "fields": 2},
    "rankWait": 300,   # seconds to wait after the last qual is scored before showing rankings only
    "poll": 15,
    "scroll": 8,
    "endpoints": DEFAULT_ENDPOINTS,
    "rev": 1,
}

CFG_LOCK = threading.Lock()
CONFIG = json.loads(json.dumps(DEFAULT_CONFIG))
COMMAND = {"seq": 0, "cmd": ""}
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
        out[d]["fields"] = clamp(src.get("fields"), 1, 4, 2)
        ph = str(src.get("phase", "auto"))
        out[d]["phase"] = ph if ph in ("auto", "quals", "rankings", "bracket") else "auto"
    out["rankWait"] = clamp(c.get("rankWait"), 0, 3600, 300)
    out["poll"] = clamp(c.get("poll"), 5, 300, 15)
    out["scroll"] = clamp(c.get("scroll"), 3, 120, 8)
    ep = str(c.get("endpoints") or DEFAULT_ENDPOINTS)
    lines = [l.strip() for l in ep.splitlines() if l.strip().startswith("http")]
    out["endpoints"] = "\n".join(lines) if lines else DEFAULT_ENDPOINTS
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
        merged = sanitize_config(new)
        for d in ("hs", "ms"):
            old_e = CONFIG.get(d, {})
            if old_e.get("sku") != merged[d]["sku"] or old_e.get("demo") != merged[d]["demo"]:
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


def looks_match(d):
    if not isinstance(d, dict):
        return False
    has_id = lk(d, "matchnum", "matchnumber", "number", "name") is not None
    has_all = (lk(d, "alliances") is not None or lk(d, "red1", "redscore", "red", "redteams") is not None)
    return has_id and has_all


def looks_rank(d):
    return (isinstance(d, dict) and lk(d, "rank") is not None
            and team_name(lk(d, "team", "teamnumber", "teamname")) is not None)


def is_qual(m):
    rnd = lk(m, "round")
    if isinstance(rnd, bool):
        rnd = None
    if isinstance(rnd, (int, float)):
        return int(rnd) == 2
    if isinstance(rnd, str) and rnd.strip():
        r = rnd.strip().lower()
        return r.startswith("q") or r == "2"
    nm = lk(m, "name")
    if isinstance(nm, str) and nm.strip():
        n = nm.strip().lower()
        if n.startswith(("r16", "r32", "r64", "qf", "sf", "f ", "final", "round of", "p ", "practice", "r ")):
            return False
        return True
    return True


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
    if flag is not None:
        scored = bool(flag) and rs is not None and bs is not None
    else:
        scored = rs is not None and bs is not None and (rs + bs) > 0
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


def looks_alliance(d):
    if not isinstance(d, dict) or looks_match(d) or looks_rank(d):
        return False
    if lk(d, "color", "score", "wins", "wp", "rank") is not None:
        return False
    t = lk(d, "teams")
    return isinstance(t, list) and 2 <= len(t) <= 3 and lk(d, "number", "alliance", "seed") is not None


def parse_alliances(lst):
    out = []
    for i, d in enumerate(lst, 1):
        n = to_num(lk(d, "number", "alliance", "seed")) or i
        out.append({"n": int(n), "teams": alliance_teams(d)})
    out.sort(key=lambda a: a["n"])
    return out


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
    if unmapped and not best:                               # unknown naming: fall back to scheduled order
        unmapped.sort(key=lambda m: (m["rnd"], m["inst"], m["num"]))
        for i, m in enumerate(unmapped[:15], 1):
            m["slot"] = i
            best[i] = m
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


def walk_lists(obj, depth=0):
    if depth > 9:
        return
    if isinstance(obj, list):
        if obj and all(isinstance(x, dict) for x in obj[:5]):
            yield obj
        for x in obj[:400]:
            if isinstance(x, (dict, list)):
                yield from walk_lists(x, depth + 1)
    elif isinstance(obj, dict):
        for v in obj.values():
            if isinstance(v, (dict, list)):
                yield from walk_lists(v, depth + 1)


def extract_name(raw):
    if isinstance(raw, dict):
        ev = lk(raw, "event")
        for src in (ev, raw):
            n = lk(src, "eventname", "name", "title") if isinstance(src, dict) else None
            if isinstance(n, str) and n.strip():
                return n.strip()
    return None


def parse_payload(raw):
    """Return dict(matches, rankings, name) found in an arbitrary JSON tree."""
    best_m, best_r = [], []
    for lst in walk_lists(raw):
        head = lst[:5]
        if sum(looks_match(x) for x in head) >= max(1, len(head) // 2 + 1 if len(head) > 1 else 1):
            if len(lst) > len(best_m):
                best_m = lst
        elif sum(looks_rank(x) for x in head) >= max(1, len(head) // 2 + 1 if len(head) > 1 else 1):
            if len(lst) > len(best_r):
                best_r = lst
    matches, elims = [], []
    for m in best_m:
        p = parse_match(m)
        if p:
            (matches if is_qual(m) else elims).append(p)
    rankings = [x for x in (parse_rank(r) for r in best_r) if x["team"]]
    alliances = []
    for lst in walk_lists(raw):
        head = lst[:8]
        if 2 <= len(lst) <= 16 and all(looks_alliance(x) for x in head):
            alliances = parse_alliances(lst)
            break
    return {"matches": matches, "elims": elims, "alliances": alliances,
            "rankings": rankings, "name": extract_name(raw)}


def embedded_json(html):
    out = []
    for m in re.finditer(r"<script[^>]*(?:id=\"__NEXT_DATA__\"|type=\"application/(?:ld\+)?json\")[^>]*>(.*?)</script>", html, re.S | re.I):
        try:
            out.append(json.loads(m.group(1)))
        except ValueError:
            pass
    return out


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
# Fetching from events.vex.com
# --------------------------------------------------------------------------
SKU_RE = re.compile(r"RE-[A-Z0-9]+-\d{2}-\d{4}", re.I)
UA = "Mozilla/5.0 (X11; Linux armv7l) VexKiosk/1.0"


def http_get(url, timeout=8):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "application/json,text/html;q=0.9,*/*;q=0.5"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read(6 * 1024 * 1024).decode("utf-8", "replace")
        return r.status, body, int((time.time() - t0) * 1000)


def candidate_urls(entry, templates):
    val = entry["sku"].strip()
    urls = []
    if val.lower().startswith("http"):
        urls.append(val)
    m = SKU_RE.search(val)
    sku = m.group(0).upper() if m else (val if val and not val.lower().startswith("http") else "")
    if sku:
        for t in templates.splitlines():
            u = t.strip().replace("{sku}", sku)
            if u and u not in urls:
                urls.append(u)
    return sku, urls


def fetch_live(entry, templates):
    sku, urls = candidate_urls(entry, templates)
    diag = []
    result = {"matches": [], "rankings": [], "name": None, "elims": [], "alliances": []}
    if not urls:
        return None, diag, "No event SKU/URL configured"
    for url in urls:
        rec = {"url": url, "status": None, "ms": None, "note": ""}
        try:
            status, body, ms = http_get(url)
            rec["status"], rec["ms"] = status, ms
            payloads = []
            try:
                payloads.append(json.loads(body))
            except ValueError:
                payloads.extend(embedded_json(body))
                tm = re.search(r"<title>(.*?)</title>", body, re.S | re.I)
                if tm and not result["name"]:
                    result["name"] = re.sub(r"\s+", " ", tm.group(1)).strip().split(" | ")[0]
            found = []
            for p in payloads:
                part = parse_payload(p)
                if part["matches"] and not result["matches"]:
                    result["matches"] = part["matches"]; found.append("%d matches" % len(part["matches"]))
                if part["rankings"] and not result["rankings"]:
                    result["rankings"] = part["rankings"]; found.append("%d rankings" % len(part["rankings"]))
                if part["elims"] and not result["elims"]:
                    result["elims"] = part["elims"]; found.append("%d elim matches" % len(part["elims"]))
                if part["alliances"] and not result["alliances"]:
                    result["alliances"] = part["alliances"]; found.append("%d alliances" % len(part["alliances"]))
                if part["name"] and not result["name"]:
                    result["name"] = part["name"]; found.append("name")
            rec["note"] = ", ".join(found) if found else "responded, no usable data recognised"
        except urllib.error.HTTPError as e:
            rec["status"], rec["note"] = e.code, "HTTP %d" % e.code
        except Exception as e:  # network, DNS, timeout, SSL
            rec["note"] = "%s: %s" % (type(e).__name__, e)
        diag.append(rec)
        if result["matches"] and result["rankings"] and result["name"]:
            break
    if not result["matches"] and not result["rankings"]:
        if all(d["status"] is None for d in diag):
            return None, diag, "Could not reach events.vex.com (check the Pi's internet connection)"
        return None, diag, "events.vex.com responded, but no recognisable match/ranking data was found (see diagnostics)"
    result["matches"].sort(key=lambda m: (m["num"], m["inst"]))
    assign_slots(result["elims"])
    result["rankings"].sort(key=lambda r: r["rank"] or 9999)
    fill_averages(result["rankings"], result["matches"])
    result["name"] = result["name"] or sku
    result["sku"] = sku
    return result, diag, ""


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


def demo_data(div):
    """Loops through: quals -> all scored -> rankings only -> bracket filling in (about 10 min)."""
    rnd = random.Random(7 if div == "hs" else 11)
    n_teams = 32
    teams = ["%d%s" % (1000 + i * 41 + (0 if div == "hs" else 9000), "ABCDEFG"[i % 7]) for i in range(n_teams)]
    fields = ["RED 1", "BLUE 1", "RED 2", "BLUE 2"]
    matches = []
    for n in range(1, 61):
        four = rnd.sample(teams, 4)
        matches.append({"num": n, "inst": 1, "field": fields[(n - 1) % 4], "red": four[:2], "blue": four[2:],
                        "_rs": rnd.randint(18, 96), "_bs": rnd.randint(18, 96)})
    step = int(time.time() // 4) % 150
    done = 60 if step >= 60 else max(14, step)
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
    if step >= 72:
        alliances = [{"n": i + 1, "teams": [order[i], order[31 - i]]} for i in range(16)]
        elims = sim_bracket(alliances, max(0, (step - 76) // 3), random.Random(5))
    name = "Demo %s Championship" % ("High School" if div == "hs" else "Middle School")
    return {"name": name, "sku": "DEMO", "matches": matches, "rankings": rankings,
            "elims": elims, "alliances": alliances}


# --------------------------------------------------------------------------
# Division data service with cache + last-good fallback
# --------------------------------------------------------------------------
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
    return ph, left


def get_division(div, force=False):
    with CFG_LOCK:
        cfg = json.loads(json.dumps(CONFIG))
    entry = cfg[div]
    now = time.time()
    hit = CACHE.get(div)
    if hit and not force and now - hit["t"] < 4:
        return hit["data"]
    err, diag = "", []
    if entry["demo"]:
        res = demo_data(div)
    else:
        res, diag, err = fetch_live(entry, cfg["endpoints"])
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
    wait = 12 if entry["demo"] else cfg["rankWait"]
    out["phase"], out["rankingsIn"] = compute_phase(div, entry, out, wait, now)
    out["rankWait"] = wait
    out["diag"] = diag
    CACHE[div] = {"t": now, "data": out}
    return out


# --------------------------------------------------------------------------
# HTTP server
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "VexKiosk/1.0"

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
                    self._json(CONFIG)
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
                    "alliances": len(d.get("alliances", [])), "top": d["rankings"][:5],
                    "next": [m for m in d["matches"] if not m["scored"]][:3], "diag": d["diag"]})
            elif path == "/favicon.ico":
                self._send(204, b"", "image/x-icon")
            else:
                self._send(404, "Not found", "text/plain")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def do_POST(self):
        path = self.path.split("?")[0].rstrip("/")
        try:
            if path == "/api/config":
                self._json(save_config(self._body()))
            elif path == "/api/command":
                cmd = str(self._body().get("cmd", ""))
                if cmd not in ("refresh", "nextpage", "reload", "clearcache"):
                    return self._json({"error": "unknown command"}, 400)
                with CFG_LOCK:
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
            self._json({"error": str(e)}, 500)


# --------------------------------------------------------------------------
# KIOSK PAGE
# --------------------------------------------------------------------------
KIOSK_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>VEX Kiosk</title>
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
#panes{flex:1;min-height:0;display:grid;grid-template-columns:1fr 1fr;gap:.9rem;padding:.9rem .9rem .5rem}
.pane{min-width:0;min-height:0;display:flex;flex-direction:column;background:var(--panel);border:.14rem solid var(--line);border-radius:.8rem;overflow:hidden}
.phead{display:flex;align-items:center;justify-content:space-between;gap:1rem;padding:.7rem 1.2rem;background:var(--panel2);border-bottom:.14rem solid var(--line)}
.badge{font-weight:800;font-size:2.1rem;letter-spacing:.02em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.badge .div{color:var(--gold)}
.api{display:flex;align-items:center;gap:.6rem;font-weight:700;font-size:1.5rem;white-space:nowrap}
.dot{width:1.2rem;height:1.2rem;border-radius:50%;background:var(--green);box-shadow:0 0 .8rem var(--green)}
.dot.warn{background:var(--amber);box-shadow:0 0 .8rem var(--amber)}
.dot.off{background:var(--red);box-shadow:0 0 .8rem var(--red)}
.pbody{flex:1;min-height:0;display:grid;grid-template-columns:44% 56%}
.matches{display:flex;flex-direction:column;min-height:0;border-right:.14rem solid var(--line);padding:.7rem .8rem;gap:.6rem}
.sec-title{font-weight:800;font-size:1.45rem;color:var(--dim);letter-spacing:.08em;padding:0 .2rem .3rem}
.sec{display:flex;flex-direction:column;gap:.45rem;min-height:0}
.sec.last{flex:0 0 auto}
.sec.next{flex:1}
.card{background:var(--bg);border:.12rem solid var(--line);border-radius:.6rem;padding:.4rem .7rem;display:flex;flex-direction:column;justify-content:center;gap:.25rem;min-height:0}
.sec.next .card{flex:1}
.card.upnext{border-color:var(--gold);box-shadow:inset 0 0 0 .1rem var(--gold)}
.row1{display:flex;align-items:center;gap:.6rem;font-size:1.35rem;font-weight:700}
.qbadge{background:var(--text);color:var(--bg);border-radius:.4rem;padding:0 .6rem;font-weight:800;font-size:1.5rem}
.field{color:var(--dim)}
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
td{font-size:1.75rem;font-weight:700;text-align:right;padding:0 .4rem;height:3.7rem;border-bottom:.08rem solid #1b2738}
th:first-child,td:first-child,th:nth-child(2),td:nth-child(2){text-align:left}
td:nth-child(2){font-weight:800}
td.rk{color:var(--gold);font-weight:800}
tr.top3 td:nth-child(2){color:var(--gold)}
td.avg{color:var(--green)}
.sec.last{margin-bottom:.2rem}
.queue-title{color:var(--gold)}
.card.queuing{border-color:var(--gold);background:#2a2208;box-shadow:inset 0 0 0 .1rem var(--gold),0 0 1rem rgba(255,200,61,.35)}
.flash{animation:flash 1s linear infinite}
@keyframes flash{0%,49%{opacity:1}50%,100%{opacity:.1}}
@keyframes softpulse{0%,100%{opacity:1}50%{opacity:.45}}
@media (prefers-reduced-motion:reduce){.flash{animation:softpulse 2s ease-in-out infinite}}
.matches.dense{gap:.35rem}
.matches.dense .card{padding:.15rem .6rem;gap:.1rem}
.matches.dense .qbadge{font-size:1.15rem}
.matches.dense .row1{font-size:1.1rem}
.matches.dense .alli{font-size:1.4rem}
.matches.dense .alli .score{font-size:1.8rem}
/* phases: quals (default) | rankings (rankings only) | bracket */
.bracket{display:none;min-height:0;min-width:0;padding:.7rem .8rem;grid-template-columns:repeat(6,1fr);grid-template-rows:auto repeat(8,minmax(0,1fr));column-gap:0;row-gap:.3rem}
.pane.ph-bracket .bracket{display:grid}
.pane.ph-bracket .matches,.pane.ph-bracket .ranks,.pane.ph-rankings .matches{display:none}
.pane.ph-bracket .pbody,.pane.ph-rankings .pbody{grid-template-columns:1fr}
.pane.ph-rankings .ranks{padding:.9rem 2rem}
.pane.ph-rankings td{font-size:2.4rem;height:4.4rem}
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
.bl.lose{opacity:.45}
.bcell.fin .bm{flex:0 1 auto;max-height:none;padding:.35rem .45rem}
#foot{flex:0 0 auto;display:flex;justify-content:space-between;align-items:center;gap:1.5rem;padding:.55rem 1.4rem;background:#070a0f;border-top:.14rem solid var(--line);font-size:1.3rem;font-weight:700;color:var(--dim);white-space:nowrap}
#foot b{color:var(--text)}
#foot .ok{color:var(--green)}
#foot .bad{color:var(--red)}
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
<div id="foot">
  <span id="f-sys">PI SYSTEM STATUS: <span class="ok">ACTIVE</span> (IP: <b id="f-ip">--</b>)</span>
  <span>DATA INGEST: <b>EVENTS.VEX.COM</b></span>
  <span>POLLING EVERY <b id="f-poll">15</b>s</span>
  <span>NEXT PAGE IN <b id="f-next" class="num">8</b>s</span>
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
const DEFAULT_CFG = {hs:{sku:'',demo:true},ms:{sku:'',demo:true},poll:15,scroll:8,rev:0};
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
      '<div class="matches">'+
        '<div class="sec last"><div id="last-'+div+'" class="sec"></div></div>'+
        '<div class="sec next"><div class="sec-title">ON FIELD NOW</div><div id="next-'+div+'" class="sec next" style="flex:1"></div></div>'+
        '<div class="sec next"><div class="sec-title queue-title">NOW QUEUING</div><div id="queue-'+div+'" class="sec next" style="flex:1"></div></div>'+
      '</div>'+
      '<div class="ranks"><div class="rhead"><div class="sec-title" id="rt-'+div+'" style="padding:0">TEAM RANKINGS</div><div class="pageind num" id="pi-'+div+'"></div></div>'+
        '<div class="rwrap" id="rw-'+div+'"></div></div>'+
      '<div class="bracket" id="bk-'+div+'"></div>'+
    '</div>';
}

/* ---------- renderers ---------- */
function teamsHtml(a, cls){ return a.map(t=>'<span'+(cls?' class="'+cls+'"':'')+'>'+esc(t)+'</span>').join(''); }

function lastCard(m){
  const rw = m.rs>m.bs, bw = m.bs>m.rs;
  const win = rw ? '<span class="win red">RED WINS</span>' : bw ? '<span class="win blue">BLUE WINS</span>' : '<span class="win tie">TIE</span>';
  return '<div class="card"><div class="row1"><span class="qbadge num">Q '+m.num+'</span>'+(m.field?'<span class="field">Field: '+esc(m.field)+'</span>':'')+win+'</div>'+
    '<div class="row2"><div class="alli red'+(bw?' lose':'')+'"><span class="teams">'+teamsHtml(m.red)+'</span><span class="score num">'+m.rs+'</span></div>'+
    '<div class="alli blue'+(rw?' lose':'')+'"><span class="teams">'+teamsHtml(m.blue)+'</span><span class="score num">'+m.bs+'</span></div></div></div>';
}
function nextCard(m, queuing){
  const c = queuing ? 'flash' : '';
  return '<div class="card'+(queuing?' queuing':'')+'"><div class="row1"><span class="qbadge num">Q '+m.num+'</span>'+(m.field?'<span class="field">Field: '+esc(m.field)+'</span>':'')+'</div>'+
    '<div class="row2"><div class="alli red"><span class="teams">'+teamsHtml(m.red,c)+'</span></div><div class="alli blue"><span class="teams">'+teamsHtml(m.blue,c)+'</span></div></div></div>';
}

function pageSize(div){
  const wrap = document.getElementById('rw-'+div);
  const rem = parseFloat(getComputedStyle(document.documentElement).fontSize);
  const big = document.getElementById('pane-'+div).classList.contains('ph-rankings');
  const rowH = (big ? 4.4 : 3.7)*rem + 1.3;           /* td height + border */
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
  const n = Math.max(1, Math.min(4, +(cfg[div] && cfg[div].fields) || 2));   /* number of fields */
  last.closest('.matches').classList.toggle('dense', n >= 3);
  const scored = d.matches.filter(m=>m.scored).slice(-2).reverse();
  const upcoming = d.matches.filter(m=>!m.scored);
  const onField = upcoming.slice(0, n), queuing = upcoming.slice(n, 2*n);
  last.innerHTML = scored.length ? scored.map(lastCard).join('') : '<div class="empty">No matches scored yet</div>';
  nxt.innerHTML = onField.length ? onField.map(m=>nextCard(m,false)).join('') : '<div class="empty">No matches on the fields</div>';
  que.innerHTML = queuing.length ? queuing.map(m=>nextCard(m,true)).join('') : '<div class="empty">No matches queuing</div>';
}

function renderRanks(div){
  const d = S[div].data, wrap = document.getElementById('rw-'+div), pi = document.getElementById('pi-'+div);
  if (!d || !d.rankings.length){ wrap.innerHTML='<div class="empty">'+(d? 'No rankings published yet':'Waiting for data…')+'</div>'; pi.textContent=''; return; }
  const size = pageSize(div), pages = Math.max(1, Math.ceil(d.rankings.length/size)), p = page % pages;
  const rows = d.rankings.slice(p*size, p*size+size);
  pi.textContent = 'PAGE '+(p+1)+' / '+pages;
  wrap.innerHTML = '<table><thead><tr><th>#</th><th>TEAM</th><th>W-L-T</th><th>WP</th><th>AP</th><th>SP</th><th>AVG</th></tr></thead><tbody>'+
    rows.map(r=>'<tr class="'+(r.rank<=3?'top3':'')+' num"><td class="rk">'+r.rank+'</td><td>'+esc(r.team)+'</td><td>'+r.w+'-'+r.l+'-'+r.t+'</td><td>'+r.wp+'</td><td>'+r.ap+'</td><td>'+r.sp+'</td><td class="avg">'+(+r.avg).toFixed(1)+'</td></tr>').join('')+
    '</tbody></table>';
}

function renderHeader(div){
  const d = S[div].data;
  const name = d ? d.name : (cfg[div].sku || 'Not configured');
  document.getElementById('b-'+div).innerHTML = '<span class="div">'+div.toUpperCase()+':</span> '+esc(name);
  renderStatus(div);
}
function renderDiv(d){ applyPhase(d); renderHeader(d); renderMatches(d); renderBracket(d); renderRanks(d); }
function renderAll(){ ['hs','ms'].forEach(renderDiv); renderFoot(); }

function renderFoot(){
  document.getElementById('f-ip').textContent = ip;
  document.getElementById('f-poll').textContent = cfg.poll;
  document.getElementById('f-next').textContent = countdown;
}

/* ---------- phases: qualification view -> rankings only -> single-elimination bracket ---------- */
function applyPhase(div){
  const d = S[div].data, p = document.getElementById('pane-'+div);
  const ph = d && d.phase ? d.phase : 'quals';
  p.classList.toggle('ph-rankings', ph==='rankings');
  p.classList.toggle('ph-bracket', ph==='bracket');
  document.getElementById('rt-'+div).textContent = ph==='rankings' ? 'FINAL QUALIFICATION RANKINGS' : 'TEAM RANKINGS';
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
  /* red side = first feeder, blue side = second feeder (matches VEX's own bracket) */
  function side(slot, i){
    const m = by[slot]; let seed=null, teams=null, ph='';
    if (slot<=8){ seed=SEED[slot][i]; teams=(al[seed]&&al[seed].length)?al[seed]:null; ph='ALLIANCE '+seed; }
    else { const f=FEED[slot][i]; ph='W '+slotName(f); const w=winner(f); if (w){ seed=w.seed; teams=w.teams; } }
    const own = m ? (i ? m.blue : m.red) : null;
    if (own && own.length) teams = own;
    return {seed:seed, teams:teams, ph:ph};
  }
  function winner(slot){
    const m = by[slot]; if (!m || !m.scored || m.rs===m.bs) return null;
    return side(slot, m.rs>m.bs ? 0 : 1);
  }
  const line = (cls,x,score,win,lose) => '<div class="bl '+cls+(win?' win':'')+(lose?' lose':'')+'"><span class="sd num">'+(x.seed==null?'':x.seed)+'</span><span class="bt'+(x.teams?'':' ph')+'">'+esc(x.teams?x.teams.join(' '):x.ph)+'</span><span class="bs num">'+(score==null?'':score)+'</span></div>';
  function card(slot){
    const m = by[slot], a = side(slot,0), b = side(slot,1);
    const sc = !!(m&&m.scored), rw = sc&&m.rs>m.bs, bw = sc&&m.bs>m.rs;
    return '<div class="bm'+(sc?' done':'')+'"><div class="bh">'+slotName(slot)+'</div>'+line('red',a,sc?m.rs:null,rw,bw)+line('blue',b,sc?m.bs:null,bw,rw)+'</div>';
  }
  let h = '';
  ['ROUND OF 16','QUARTERFINALS','SEMIFINALS','FINALS'].forEach((t,i)=>{ h += '<div class="ctitle" style="grid-column:'+[1,3,4,5][i]+';grid-row:1;overflow:visible">'+t+'</div>'; });
  for (let s=1; s<=15; s++){
    const col = s<=8?1 : s<=12?3 : s<=14?4 : 5;   /* each round starts halfway across the previous one */
    const z = s<=8?4 : s<=12?3 : s<=14?2 : 1;     /* later rounds tuck underneath earlier ones */
    const span = s<=8?1 : s<=12?2 : s<=14?4 : 8;
    const start = s<=8 ? s : s<=12 ? 2*(s-9)+1 : s<=14 ? 4*(s-13)+1 : 1;
    h += '<div class="bwrap" style="grid-column:'+col+' / span 2;grid-row:'+(start+1)+' / span '+span+';z-index:'+z+(s<=8?';padding-right:.9rem':'')+'">'+card(s)+'</div>';
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
  try {
    const d = await fetchJson('/api/data/'+div, 12000);
    s.data = d; s.fails = d.online ? 0 : Math.min(s.fails+1, 2);
  } catch(e){ s.fails++; }
  renderDiv(div);
  const delay = s.fails ? Math.min(cfg.poll*1000*Math.pow(2, s.fails-1), 60000) : cfg.poll*1000;
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
      if (c.cmd==='refresh') pollAll();
      else if (c.cmd==='nextpage'){ page++; countdown = cfg.scroll; renderAll(); }
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
  if (countdown <= 0){ page++; countdown = cfg.scroll; ['hs','ms'].forEach(renderRanks); }
  document.getElementById('f-next').textContent = Math.max(countdown,0);
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
<title>VEX Kiosk Admin</title>
<script src="https://cdn.tailwindcss.com"></script>
<style>
  body{background:#0a0e14;color:#eef3f9;font-family:"Barlow","Segoe UI","DejaVu Sans",Arial,sans-serif}
  .card{background:#111823;border:1px solid #26364a;border-radius:.75rem;padding:1.25rem}
  label{display:block;font-size:.85rem;color:#8da2ba;margin-bottom:.25rem}
  input[type=text],input[type=number],textarea{width:100%;background:#0a0e14;border:1px solid #26364a;border-radius:.5rem;padding:.55rem .7rem;color:#eef3f9}
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
      <h1 class="text-2xl font-bold">VEX Kiosk Setup</h1>
      <p class="text-sm" style="color:#8da2ba">Kiosk: <a class="underline" id="kiosk-link" href="/">/</a> &nbsp; Admin: <span id="admin-link"></span></p>
    </div>
    <div class="flex gap-2">
      <a class="btn" href="/" target="_blank"><span data-i="monitor"></span>Open kiosk</a>
      <button class="btn primary" id="save"><span data-i="save"></span>Save settings</button>
    </div>
  </header>

  <div id="toast" class="hidden card text-sm"></div>

  <section class="grid md:grid-cols-2 gap-6">
    <div class="card space-y-3">
      <h2 class="font-bold text-lg">High School event</h2>
      <div><label for="hs-sku">Event SKU or events.vex.com URL</label><input type="text" id="hs-sku" placeholder="RE-V5RC-XX-XXXX"></div>
      <label class="flex items-center gap-2" style="color:#eef3f9"><input type="checkbox" id="hs-demo"> Use demo data (no internet needed)</label>
      <div><label for="hs-fields">Number of fields (1-4): matches on the fields now / queuing</label><input type="number" id="hs-fields" min="1" max="4"></div>
      <div><label for="hs-phase">Display mode</label><select id="hs-phase"><option value="auto">Automatic (quals, then rankings, then bracket)</option><option value="quals">Always show qualification matches</option><option value="rankings">Always show rankings only</option><option value="bracket">Always show bracket</option></select></div>
      <div class="flex gap-2"><button class="btn" data-test="hs"><span data-i="activity"></span>Test HS connection</button></div>
    </div>
    <div class="card space-y-3">
      <h2 class="font-bold text-lg">Middle School event</h2>
      <div><label for="ms-sku">Event SKU or events.vex.com URL</label><input type="text" id="ms-sku" placeholder="RE-V5RC-XX-XXXX"></div>
      <label class="flex items-center gap-2" style="color:#eef3f9"><input type="checkbox" id="ms-demo"> Use demo data (no internet needed)</label>
      <div><label for="ms-fields">Number of fields (1-4): matches on the fields now / queuing</label><input type="number" id="ms-fields" min="1" max="4"></div>
      <div><label for="ms-phase">Display mode</label><select id="ms-phase"><option value="auto">Automatic (quals, then rankings, then bracket)</option><option value="quals">Always show qualification matches</option><option value="rankings">Always show rankings only</option><option value="bracket">Always show bracket</option></select></div>
      <div class="flex gap-2"><button class="btn" data-test="ms"><span data-i="activity"></span>Test MS connection</button></div>
    </div>
  </section>

  <section class="card grid sm:grid-cols-2 gap-4">
    <div><label for="poll">Polling / refresh interval (seconds, 5–300)</label><input type="number" id="poll" min="5" max="300"></div>
    <div><label for="scroll">Ranking page cycle (seconds, 3–120)</label><input type="number" id="scroll" min="3" max="120"></div>
    <div><label for="rankWait">Wait after last qual is scored before showing rankings only (seconds, default 300)</label><input type="number" id="rankWait" min="0" max="3600"></div>
  </section>

  <section class="card space-y-2">
    <h2 class="font-bold text-lg">events.vex.com endpoint templates</h2>
    <p class="text-sm" style="color:#8da2ba">One URL per line; <code>{sku}</code> is replaced with the event SKU. Each is tried in order and any JSON (or JSON embedded in the page) containing matches or rankings is used. events.vex.com has no documented public API, so these defaults are best guesses: run a connection test, check the results below, and edit this list to match whatever paths respond with data.</p>
    <textarea id="endpoints" rows="6" spellcheck="false" class="font-mono text-sm"></textarea>
  </section>

  <section class="card space-y-3">
    <h2 class="font-bold text-lg">Kiosk controls</h2>
    <div class="flex flex-wrap gap-2">
      <button class="btn" data-cmd="refresh"><span data-i="refresh"></span>Force data refresh</button>
      <button class="btn" data-cmd="nextpage"><span data-i="next"></span>Cycle rank page</button>
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
  $('hs-sku').value = cfg.hs.sku; $('hs-demo').checked = !!cfg.hs.demo; $('hs-phase').value = cfg.hs.phase || 'auto'; $('hs-fields').value = cfg.hs.fields || 2;
  $('ms-sku').value = cfg.ms.sku; $('ms-demo').checked = !!cfg.ms.demo; $('ms-phase').value = cfg.ms.phase || 'auto'; $('ms-fields').value = cfg.ms.fields || 2;
  $('rankWait').value = cfg.rankWait == null ? 300 : cfg.rankWait;
  $('poll').value = cfg.poll; $('scroll').value = cfg.scroll; $('endpoints').value = cfg.endpoints || '';
}
function read(){
  return {
    hs:{sku:$('hs-sku').value.trim(), demo:$('hs-demo').checked, phase:$('hs-phase').value, fields:+$('hs-fields').value||2},
    ms:{sku:$('ms-sku').value.trim(), demo:$('ms-demo').checked, phase:$('ms-phase').value, fields:+$('ms-fields').value||2},
    rankWait: $('rankWait').value === '' ? 300 : +$('rankWait').value,
    poll:+$('poll').value||15, scroll:+$('scroll').value||8, endpoints:$('endpoints').value, rev:cfg?cfg.rev:1
  };
}
async function load(){
  try { const r = await fetch('/api/config',{cache:'no-store'}); cfg = await r.json(); localStorage.setItem(LS_KEY, JSON.stringify(cfg)); }
  catch(e){ try { cfg = JSON.parse(localStorage.getItem(LS_KEY)); } catch(_){} if(!cfg) cfg = {hs:{sku:'',demo:true},ms:{sku:'',demo:true},poll:15,scroll:8,endpoints:'',rev:1}; toast('Server unreachable: showing locally saved settings', false); }
  fill();
}
async function save(){
  try {
    const r = await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(read())});
    if(!r.ok) throw new Error('HTTP '+r.status);
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
      '<div>Event: <b style="color:#eef3f9">'+esc(d.name)+'</b></div>'+
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
    print("VEX Kiosk running")
    print("  Kiosk : http://%s:%d/" % (ip, args.port))
    print("  Admin : http://%s:%d/admin" % (ip, args.port))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
