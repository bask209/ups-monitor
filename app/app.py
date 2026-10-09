"""UPS monitor: polls NUT (upsd), stores history, serves a LAN dashboard, alerts via Telegram."""
import json
import os
import socket
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

NUT_HOST = os.environ.get("NUT_HOST", "127.0.0.1")
NUT_PORT = int(os.environ.get("NUT_PORT", "3493"))
UPS = os.environ.get("NUT_UPS", "ups")
HTTP_PORT = int(os.environ.get("HTTP_PORT", "8330"))
DB_PATH = os.environ.get("DB_PATH", "/data/ups.db")
POLL = float(os.environ.get("POLL_SECONDS", "1"))
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "365"))  # how long history is kept
RAW_DAYS = int(os.environ.get("RAW_DAYS", "30"))  # full 10 s resolution; older data is rolled up to 1 min
TG_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
WEBAPP_URL = os.environ.get("WEBAPP_URL", "").strip()  # public https URL for the Telegram Mini App
DASH_URL = os.environ.get("DASHBOARD_URL", "").strip()  # LAN URL shown in messages
LOAD_ALERT = float(os.environ.get("LOAD_ALERT_PCT", "80"))
DIGEST_HOUR = int(os.environ.get("DIGEST_HOUR", "8"))
DIGEST_ENABLED = os.environ.get("DIGEST_ENABLED", "false").lower() in ("1", "true", "yes")  # off: alert only on events
KWH_PRICE = os.environ.get("KWH_PRICE", "").strip()
CURRENCY = os.environ.get("CURRENCY", "")

FLAGS = {
    "OL": ("Online (mains OK)", "ok"), "OB": ("ON BATTERY - power failure", "crit"),
    "LB": ("LOW BATTERY", "crit"), "HB": ("High battery", "warn"),
    "RB": ("REPLACE BATTERY", "warn"), "CHRG": ("Charging", "info"),
    "DISCHRG": ("Discharging", "info"), "BYPASS": ("Bypass", "warn"),
    "CAL": ("Calibrating", "info"), "OFF": ("Off", "warn"),
    "OVER": ("OVERLOAD", "crit"), "TRIM": ("Trimming high voltage (AVR)", "info"),
    "BOOST": ("Boosting low voltage (AVR)", "info"), "FSD": ("Forced shutdown", "crit"),
    "ALARM": ("Alarm", "crit"),
}

state = {"ver": 0, "vars": {}, "ok": False, "since": time.time(), "err": "", "ts": 0, "desc": "", "cmds": [], "rw": {}}
lock = threading.Lock()
cond = threading.Condition(lock)  # wakes SSE streams on every poll
dblock = threading.Lock()


# ---------------------------------------------------------------- NUT client
def nut_cmd(sock_file, sock, line):
    sock.sendall((line + "\n").encode())
    out = []
    while True:
        l = sock_file.readline()
        if not l:
            raise ConnectionError("upsd closed connection")
        l = l.strip()
        if l.startswith("ERR"):
            raise RuntimeError(l)
        if l.startswith("BEGIN LIST"):
            continue
        if l.startswith("END LIST"):
            return out
        if line.startswith("LIST"):
            out.append(l)
        else:
            return [l]


def unq(s):
    return s.strip().strip('"').replace('\\"', '"').replace("\\\\", "\\")


def nut_fetch(extra=True):
    with socket.create_connection((NUT_HOST, NUT_PORT), timeout=5) as s:
        s.settimeout(5)
        f = s.makefile("r")
        v = {}
        for l in nut_cmd(f, s, f"LIST VAR {UPS}"):
            p = l.split(" ", 3)
            if len(p) == 4 and p[0] == "VAR":
                v[p[2]] = unq(p[3])
        rw, cmds, desc = {}, [], ""
        if not extra:
            return v, None, None, None
        try:
            for l in nut_cmd(f, s, f"LIST RW {UPS}"):
                p = l.split(" ", 3)
                if len(p) == 4:
                    rw[p[2]] = unq(p[3])
        except Exception:
            pass
        try:
            cmds = [l.split(" ")[2] for l in nut_cmd(f, s, f"LIST CMD {UPS}") if l.startswith("CMD")]
        except Exception:
            pass
        try:
            desc = unq(nut_cmd(f, s, f"GET UPSDESC {UPS}")[0].split(" ", 2)[2])
        except Exception:
            pass
        return v, rw, cmds, desc


def num(v, k):
    try:
        return float(v[k])
    except (KeyError, ValueError, TypeError):
        return None


def derive(v):
    """Computed metrics not reported directly by the UPS."""
    d = {}
    load, nom = num(v, "ups.load"), num(v, "ups.realpower.nominal")
    d["watts"] = round(load * nom / 100, 1) if load is not None and nom else num(v, "ups.realpower")
    return d


# ---------------------------------------------------------------- storage
def db():
    c = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    c.execute("PRAGMA journal_mode=WAL")
    return c


conn = None


def init_db():
    global conn
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS samples(ts INTEGER PRIMARY KEY, charge REAL, runtime REAL, load REAL, watts REAL,
        in_v REAL, out_v REAL, batt_v REAL, freq REAL, temp REAL, onbatt INTEGER, status TEXT);
    CREATE TABLE IF NOT EXISTS samples_1m(ts INTEGER PRIMARY KEY, charge REAL, runtime REAL, load REAL, watts REAL,
        in_v REAL, out_v REAL, batt_v REAL, freq REAL, temp REAL, onbatt INTEGER, in_min REAL, in_max REAL);
    CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, level TEXT, kind TEXT, msg TEXT);
    CREATE TABLE IF NOT EXISTS outages(id INTEGER PRIMARY KEY AUTOINCREMENT, start INTEGER, end INTEGER,
        min_charge REAL, min_runtime REAL, max_load REAL, min_in_v REAL, charge_start REAL, charge_end REAL);
    CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
    CREATE INDEX IF NOT EXISTS ev_ts ON events(ts);
    """)
    conn.commit()


def kv_get(k, default=None):
    with dblock:
        r = conn.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    return r[0] if r else default


def kv_set(k, v):
    with dblock:
        conn.execute("INSERT OR REPLACE INTO kv VALUES(?,?)", (k, str(v)))
        conn.commit()


def add_event(level, kind, msg, notify=True):
    ts = int(time.time())
    with dblock:
        conn.execute("INSERT INTO events(ts,level,kind,msg) VALUES(?,?,?,?)", (ts, level, kind, msg))
        conn.commit()
    print(f"[{level}] {kind}: {msg}", flush=True)
    if notify:
        icon = {"crit": "🚨", "warn": "⚠️", "ok": "✅", "info": "ℹ️"}.get(level, "•")
        tg_send(f"{icon} <b>{kind}</b>\n{msg}")


# ---------------------------------------------------------------- Telegram
def tg_api(method, **params):
    if not TG_TOKEN:
        return None
    data = json.dumps(params).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{TG_TOKEN}/{method}", data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.load(r)


def tg_send(text, chat=None, keyboard=True):
    chat = chat or TG_CHAT
    if not (TG_TOKEN and chat):
        return
    params = {"chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if keyboard and WEBAPP_URL:
        params["reply_markup"] = {"inline_keyboard": [[{"text": "📊 Open dashboard", "web_app": {"url": WEBAPP_URL}}]]}
    for attempt in range(3):
        try:
            tg_api("sendMessage", **params)
            return
        except Exception as e:
            print("telegram send failed:", e, flush=True)
            time.sleep(2 + attempt * 3)


def fmt_dur(s):
    s = int(s)
    h, r = divmod(s, 3600)
    m, sec = divmod(r, 60)
    return (f"{h}h " if h else "") + (f"{m}m " if m or h else "") + f"{sec}s"


def snapshot_text():
    with lock:
        v = dict(state["vars"])
        ok = state["ok"]
    if not ok or not v:
        return "❌ Cannot reach the UPS driver right now."
    d = derive(v)
    st = v.get("ups.status", "?")
    g = lambda k, u="": (v[k] + u) if k in v else "n/a"
    rt = num(v, "battery.runtime")
    lines = [
        f"<b>{v.get('ups.model', v.get('device.model', 'UPS'))}</b>",
        f"Status: <b>{st}</b> - {', '.join(FLAGS[f][0] for f in st.split() if f in FLAGS)}",
        f"🔋 Charge: <b>{g('battery.charge', '%')}</b>   ⏱ Runtime: <b>{fmt_dur(rt) if rt is not None else 'n/a'}</b>",
        f"⚡ Load: <b>{g('ups.load', '%')}</b> ≈ {d['watts'] if d['watts'] is not None else 'n/a'} W",
        f"🔌 Input: {g('input.voltage', ' V')}   Output: {g('output.voltage', ' V')}   Freq: {g('input.frequency', ' Hz')}",
        f"🔧 Battery: {g('battery.voltage', ' V')}",
    ]
    if DASH_URL:
        lines.append(f"\n{DASH_URL}")
    return "\n".join(lines)


def range_stats(since):
    with dblock:
        r = conn.execute("SELECT COUNT(*),MIN(in_v),MAX(in_v),AVG(load),AVG(watts),MAX(load),MIN(charge),MIN(ts),MAX(ts) FROM samples WHERE ts>=?", (since,)).fetchone()
        o = conn.execute("SELECT COUNT(*),COALESCE(SUM(COALESCE(end,?)-start),0) FROM outages WHERE start>=?", (int(time.time()), since)).fetchone()
    n = r[0]
    kwh = (r[4] or 0) * ((r[8] - r[7]) + 10) / 3600 / 1000 if n else 0  # avg watts x recorded span
    return {"samples": n, "min_in": r[1], "max_in": r[2], "avg_load": r[3], "avg_w": r[4], "max_load": r[5],
            "min_charge": r[6], "outages": o[0], "outage_s": o[1], "kwh": kwh}


def report_text(title, since):
    s = range_stats(since)
    if not s["samples"]:
        return f"{title}: no data yet."
    f = lambda x, p=1: "n/a" if x is None else f"{x:.{p}f}"
    t = [f"📈 <b>{title}</b>",
         f"Outages: <b>{s['outages']}</b> ({fmt_dur(s['outage_s'])} on battery)",
         f"Input voltage: {f(s['min_in'], 0)}-{f(s['max_in'], 0)} V",
         f"Load: avg {f(s['avg_load'])}% / peak {f(s['max_load'], 0)}%  (avg {f(s['avg_w'], 0)} W)",
         f"Lowest battery charge: {f(s['min_charge'], 0)}%",
         f"Energy: ≈ {s['kwh']:.2f} kWh"]
    if KWH_PRICE:
        t[-1] += f"  ≈ {s['kwh'] * float(KWH_PRICE):.2f} {CURRENCY}"
    return "\n".join(t)


def events_text(n=10):
    with dblock:
        rows = conn.execute("SELECT ts,kind,msg FROM events ORDER BY id DESC LIMIT ?", (n,)).fetchall()
    if not rows:
        return "No events recorded yet."
    return "<b>Last events</b>\n" + "\n".join(
        f"{datetime.fromtimestamp(t).strftime('%m-%d %H:%M')} <b>{k}</b> {m.splitlines()[0]}" for t, k, m in rows)


def tg_loop():
    if not TG_TOKEN:
        print("Telegram disabled (no TELEGRAM_TOKEN)", flush=True)
        return
    offset = int(kv_get("tg_offset", 0))
    try:
        tg_api("setMyCommands", commands=[
            {"command": "status", "description": "Current UPS status"},
            {"command": "day", "description": "Last 24 h report"},
            {"command": "week", "description": "Last 7 days report"},
            {"command": "events", "description": "Recent events"},
            {"command": "outages", "description": "Recent power outages"},
            {"command": "dashboard", "description": "Dashboard link"}])
        if WEBAPP_URL:
            tg_api("setChatMenuButton", menu_button={"type": "web_app", "text": "UPS", "web_app": {"url": WEBAPP_URL}})
    except Exception as e:
        print("telegram setup failed:", e, flush=True)
    while True:
        try:
            r = tg_api("getUpdates", offset=offset, timeout=30, allowed_updates=["message"])
            for u in r.get("result", []):
                offset = u["update_id"] + 1
                kv_set("tg_offset", offset)
                m = u.get("message")
                if not m or "text" not in m:
                    continue
                chat = str(m["chat"]["id"])
                sender = str(m.get("from", {}).get("id", ""))
                cmd = m["text"].split()[0].split("@")[0].lower()
                # Owner-only: private chat with the configured user. Everyone else is ignored silently.
                if not TG_CHAT or chat != TG_CHAT or sender != TG_CHAT or m["chat"].get("type") != "private":
                    print(f"telegram: ignored message from chat={chat} sender={sender}", flush=True)
                    continue
                if cmd in ("/start", "/status"):
                    tg_send(snapshot_text(), chat)
                elif cmd == "/day":
                    tg_send(report_text("Last 24 hours", int(time.time()) - 86400), chat)
                elif cmd == "/week":
                    tg_send(report_text("Last 7 days", int(time.time()) - 7 * 86400), chat)
                elif cmd == "/events":
                    tg_send(events_text(), chat)
                elif cmd == "/outages":
                    tg_send(outages_text(), chat)
                elif cmd == "/dashboard":
                    tg_send(DASH_URL or "Dashboard URL not configured (set DASHBOARD_URL).", chat)
        except Exception as e:
            print("telegram poll error:", e, flush=True)
            time.sleep(10)


def outages_text(n=8):
    with dblock:
        rows = conn.execute("SELECT start,end,min_charge,max_load FROM outages ORDER BY id DESC LIMIT ?", (n,)).fetchall()
    if not rows:
        return "No outages recorded. 🎉"
    out = ["<b>Recent outages</b>"]
    for s, e, mc, ml in rows:
        dur = fmt_dur((e or time.time()) - s) + ("" if e else " (ongoing)")
        out.append(f"{datetime.fromtimestamp(s).strftime('%m-%d %H:%M')} - {dur}, min charge {mc:.0f}%" if mc is not None
                   else f"{datetime.fromtimestamp(s).strftime('%m-%d %H:%M')} - {dur}")
    return "\n".join(out)


# ---------------------------------------------------------------- poller / event detection
class Tracker:
    def __init__(self):
        self.prev_flags = None
        self.outage_id = None
        self.low_notified = set()
        self.load_high = False
        self.last_test = None
        self.last_rb = False
        self.v_bad = False

    def feed(self, v):
        flags = set(v.get("ups.status", "").split())
        now = int(time.time())
        charge, rt, load, inv = num(v, "battery.charge"), num(v, "battery.runtime"), num(v, "ups.load"), num(v, "input.voltage")
        if self.prev_flags is None:
            add_event("info", "Monitor started", f"Status {' '.join(sorted(flags))}", notify=False)
            if "OB" in flags:
                self.start_outage(now, charge, inv)
        else:
            on_now, on_prev = "OB" in flags, "OB" in self.prev_flags
            if on_now and not on_prev:
                self.start_outage(now, charge, inv)
                add_event("crit", "POWER FAILURE", f"Running on battery.\nCharge {charge}%, est. runtime {fmt_dur(rt or 0)}, load {load}%")
            elif on_prev and not on_now:
                self.end_outage(now, charge)
            for f in sorted(flags - self.prev_flags - {"OL", "OB", "CHRG", "DISCHRG"}):
                lvl = FLAGS.get(f, ("", "info"))[1]
                add_event(lvl, f"Flag {f}", FLAGS.get(f, (f,))[0])
            for f in sorted(self.prev_flags - flags - {"OL", "OB", "CHRG", "DISCHRG"}):
                add_event("ok", f"Cleared {f}", FLAGS.get(f, (f,))[0], notify=f in ("LB", "OVER", "RB", "ALARM"))
        self.prev_flags = flags

        if self.outage_id is not None:
            with dblock:
                conn.execute("""UPDATE outages SET min_charge=MIN(COALESCE(min_charge,999),COALESCE(?,999)),
                    min_runtime=MIN(COALESCE(min_runtime,1e9),COALESCE(?,1e9)), max_load=MAX(COALESCE(max_load,0),COALESCE(?,0)),
                    min_in_v=MIN(COALESCE(min_in_v,1e9),COALESCE(?,1e9)) WHERE id=?""", (charge, rt, load, inv, self.outage_id))
                conn.commit()
            for th in (75, 50, 25, 10):
                if charge is not None and charge <= th and th not in self.low_notified:
                    self.low_notified.add(th)
                    add_event("crit" if th <= 25 else "warn", f"Battery {th}%", f"Charge {charge}%, runtime left {fmt_dur(rt or 0)}")
        else:
            self.low_notified.clear()

        if load is not None:
            if load >= LOAD_ALERT and not self.load_high:
                self.load_high = True
                add_event("warn", "High load", f"Load is {load}% (alert threshold {LOAD_ALERT:.0f}%)")
            elif load < LOAD_ALERT - 10 and self.load_high:
                self.load_high = False
                add_event("ok", "Load normal", f"Load back to {load}%", notify=False)

        # utility voltage outside the UPS transfer window while still on mains
        lo, hi = num(v, "input.transfer.low"), num(v, "input.transfer.high")
        if inv is not None and lo and hi and "OB" not in flags:
            bad = inv < lo + 3 or inv > hi - 3
            if bad and not self.v_bad:
                self.v_bad = True
                add_event("warn", "Mains voltage near limit", f"Input {inv} V (UPS transfers outside {lo:.0f}-{hi:.0f} V)")
            elif not bad and self.v_bad:
                self.v_bad = False
                add_event("ok", "Mains voltage OK", f"Input {inv} V", notify=False)

        tr = v.get("ups.test.result")
        if tr and self.last_test is not None and tr != self.last_test:
            add_event("info", "Self-test result", tr)
        self.last_test = tr if tr else self.last_test

    def start_outage(self, now, charge, inv):
        with dblock:
            cur = conn.execute("INSERT INTO outages(start,charge_start,min_in_v) VALUES(?,?,?)", (now, charge, inv))
            conn.commit()
            self.outage_id = cur.lastrowid

    def end_outage(self, now, charge):
        if self.outage_id is None:
            return
        with dblock:
            start = conn.execute("SELECT start FROM outages WHERE id=?", (self.outage_id,)).fetchone()[0]
            row = conn.execute("SELECT min_charge FROM outages WHERE id=?", (self.outage_id,)).fetchone()
            conn.execute("UPDATE outages SET end=?, charge_end=? WHERE id=?", (now, charge, self.outage_id))
            conn.commit()
        add_event("ok", "Power restored", f"Outage lasted {fmt_dur(now - start)}.\nLowest charge {row[0]}%, now {charge}%.")
        self.outage_id = None


def poller():
    tr = Tracker()
    # resume an unfinished outage after restart
    with dblock:
        r = conn.execute("SELECT id FROM outages WHERE end IS NULL ORDER BY id DESC LIMIT 1").fetchone()
    if r:
        tr.outage_id = r[0]
    fails = 0
    last_store = 0
    last_extra = 0
    while True:
        try:
            want_extra = time.time() - last_extra > 60 or not state["desc"]
            v, rw, cmds, desc = nut_fetch(want_extra)
            if want_extra:
                last_extra = time.time()
            with lock:
                was_ok = state["ok"]
                state.update(vars=v, ok=True, err="", ts=time.time(), ver=state["ver"] + 1)
                if want_extra:
                    state.update(rw=rw, cmds=cmds, desc=desc)
                cond.notify_all()
            if not was_ok and fails >= 3:
                add_event("ok", "UPS link restored", "Communication with the UPS is back.")
            fails = 0
            tr.feed(v)
            now = int(time.time())
            if now - last_store >= 10:
                last_store = now
                d = derive(v)
                flags = v.get("ups.status", "")
                with dblock:
                    conn.execute("INSERT OR REPLACE INTO samples VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (
                        now, num(v, "battery.charge"), num(v, "battery.runtime"), num(v, "ups.load"), d["watts"],
                        num(v, "input.voltage"), num(v, "output.voltage"), num(v, "battery.voltage"),
                        num(v, "input.frequency"), num(v, "ups.temperature"), 1 if "OB" in flags.split() else 0, flags))
                    conn.commit()
        except Exception as e:
            fails += 1
            with lock:
                state.update(ok=False, err=str(e), ver=state["ver"] + 1)
                cond.notify_all()
            if fails == 3:
                add_event("crit", "UPS link lost", f"Cannot read the UPS driver: {e}")
        time.sleep(POLL)


def rollup_and_prune():
    """Keep RAW_DAYS at full resolution, roll older samples into 1-minute rows, drop anything past RETENTION_DAYS."""
    now = int(time.time())
    raw_cut = (now - RAW_DAYS * 86400) // 60 * 60
    with dblock:
        conn.execute("""INSERT OR REPLACE INTO samples_1m SELECT ts/60*60, AVG(charge),AVG(runtime),AVG(load),AVG(watts),
            AVG(in_v),AVG(out_v),AVG(batt_v),AVG(freq),AVG(temp),MAX(onbatt),MIN(in_v),MAX(in_v)
            FROM samples WHERE ts<? GROUP BY ts/60""", (raw_cut,))
        conn.execute("DELETE FROM samples WHERE ts<?", (raw_cut,))
        conn.execute("DELETE FROM samples_1m WHERE ts<?", (now - RETENTION_DAYS * 86400,))
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def maintenance():
    """Daily digest, plus rollup/pruning every 6 hours."""
    while True:
        try:
            now = datetime.now()
            today = now.strftime("%Y-%m-%d")
            if DIGEST_ENABLED and now.hour >= DIGEST_HOUR and kv_get("digest_day") != today:
                kv_set("digest_day", today)
                tg_send(report_text("Daily digest (last 24 h)", int(time.time()) - 86400))
            if time.time() - float(kv_get("maint_ts", 0)) > 6 * 3600:
                rollup_and_prune()
                kv_set("maint_ts", time.time())
        except Exception as e:
            print("maintenance error:", e, flush=True)
        time.sleep(60)


# ---------------------------------------------------------------- HTTP
RANGES = {"1h": 3600, "6h": 6 * 3600, "24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400, "90d": 90 * 86400, "1y": 365 * 86400}


def api_history(rng):
    secs = RANGES.get(rng, 86400)
    since = int(time.time()) - secs
    bucket = max(10, secs // 480)
    with dblock:
        rows = conn.execute("""SELECT (ts/?)*? AS b, AVG(charge),AVG(runtime),AVG(load),AVG(watts),AVG(in_v),MIN(in_min),MAX(in_max),
            AVG(out_v),AVG(batt_v),AVG(freq),AVG(temp),MAX(onbatt) FROM (
              SELECT ts,charge,runtime,load,watts,in_v,in_min,in_max,out_v,batt_v,freq,temp,onbatt FROM samples_1m WHERE ts>=?
              UNION ALL
              SELECT ts,charge,runtime,load,watts,in_v,in_v,in_v,out_v,batt_v,freq,temp,onbatt FROM samples WHERE ts>=?)
            GROUP BY b ORDER BY b""", (bucket, bucket, since, since)).fetchall()
    cols = ["t", "charge", "runtime", "load", "watts", "in_v", "in_min", "in_max", "out_v", "batt_v", "freq", "temp", "onbatt"]
    return {"cols": cols, "rows": [[round(x, 2) if isinstance(x, float) else x for x in r] for r in rows],
            "bucket": bucket, "since": since}


def api_state():
    with lock:
        s = dict(state)
    v = s["vars"]
    d = derive(v) if v else {}
    day = range_stats(int(time.time()) - 86400) if conn else {}
    return {"ok": s["ok"], "err": s["err"], "ts": s["ts"], "vars": v, "rw": s["rw"], "cmds": s["cmds"], "desc": s["desc"],
            "derived": d, "flags": {f: FLAGS[f] for f in v.get("ups.status", "").split() if f in FLAGS},
            "day": day, "now": time.time(), "price": KWH_PRICE, "currency": CURRENCY,
            "telegram": bool(TG_TOKEN and TG_CHAT)}


def api_live():
    with lock:
        s = dict(state)
    v = s["vars"]
    return {"ok": s["ok"], "err": s["err"], "ts": s["ts"], "now": time.time(), "vars": v,
            "derived": derive(v) if v else {},
            "flags": {f: FLAGS[f] for f in v.get("ups.status", "").split() if f in FLAGS}}


def api_events(n=200):
    with dblock:
        ev = conn.execute("SELECT ts,level,kind,msg FROM events ORDER BY id DESC LIMIT ?", (n,)).fetchall()
        out = conn.execute("SELECT start,end,min_charge,min_runtime,max_load,min_in_v,charge_start,charge_end FROM outages ORDER BY id DESC LIMIT 50").fetchall()
    return {"events": ev, "outages": out}


def prom():
    with lock:
        v = dict(state["vars"])
        ok = state["ok"]
    lines = [f"ups_up {1 if ok else 0}"]
    for k, val in v.items():
        try:
            lines.append(f'nut_{k.replace(".", "_")} {float(val)}')
        except ValueError:
            pass
    for f in v.get("ups.status", "").split():
        lines.append(f'ups_status_flag{{flag="{f}"}} 1')
    if v:
        lines.append(f"ups_watts {derive(v)['watts'] or 0}")
    return "\n".join(lines) + "\n"


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, body, ctype="application/json", code=200):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def stream(self):
        """Server-Sent Events: push a fresh reading the moment the poller has one."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        ver = -1
        try:
            while True:
                with cond:
                    cond.wait_for(lambda: state["ver"] != ver, timeout=15)
                    changed = state["ver"] != ver
                    ver = state["ver"]
                if changed:
                    self.wfile.write(f"data: {json.dumps(api_live())}\n\n".encode())
                else:
                    self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        try:
            if u.path in ("/", "/index.html"):
                self.send(open("/app/index.html", "rb").read(), "text/html")
            elif u.path == "/lwc.js":
                self.send(open("/app/lwc.js", "rb").read(), "application/javascript")
            elif u.path == "/api/state":
                self.send(json.dumps(api_state()))
            elif u.path == "/api/stream":
                return self.stream()
            elif u.path == "/api/history":
                self.send(json.dumps(api_history(q.get("range", ["24h"])[0])))
            elif u.path == "/api/events":
                self.send(json.dumps(api_events()))
            elif u.path == "/metrics":
                self.send(prom(), "text/plain")
            elif u.path == "/api/health":
                with lock:
                    ok = state["ok"] and time.time() - state["ts"] < 60
                self.send(json.dumps({"ok": ok}), code=200 if ok else 503)
            else:
                self.send("not found", "text/plain", 404)
        except Exception as e:
            self.send(json.dumps({"error": str(e)}), code=500)


if __name__ == "__main__":
    init_db()
    for fn in (poller, tg_loop, maintenance):
        threading.Thread(target=fn, daemon=True).start()
    print(f"dashboard on :{HTTP_PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), H).serve_forever()
