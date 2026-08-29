import gc
import json
import os
import sqlite3
import sys
import threading
import time
from datetime import datetime

import requests as _requests
import tinytuya
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, session, redirect, url_for

import switchbot

load_dotenv()

# --- Tuya Setup ---
ACCESS_ID      = os.getenv("TUYA_ACCESS_ID")
ACCESS_SECRET  = os.getenv("TUYA_ACCESS_SECRET")
DEVICE_MASTER  = os.getenv("TUYA_DEVICE_ID_MASTER")
DEVICE_GAESTE  = os.getenv("TUYA_DEVICE_ID_GAESTE")
AC_TARGET_TEMP = 20  # Immer 20°C auf der Klimaanlage im Automatikmodus
LEVEL_MAP      = {1: "low", 2: "middle", 3: "high"}

# tinytuya internally uses requests.get()/requests.post() without a Session,
# creating a full TCP+SSL connection per API call. Over days, the transient
# Session/Pool/Connection objects accumulate faster than Python's cyclic GC
# frees them, exhausting the OS file descriptor limit. Replacing the module's
# requests reference with a Session-backed wrapper keeps ~2 persistent
# connections instead of creating thousands of short-lived ones.
_tuya_session = _requests.Session()

class _SessionBackedRequests:
    def __getattr__(self, name):
        return getattr(_requests, name)
    def get(self, *a, **kw):
        kw.setdefault("timeout", 30)
        return _tuya_session.get(*a, **kw)
    def post(self, *a, **kw):
        kw.setdefault("timeout", 30)
        return _tuya_session.post(*a, **kw)

sys.modules["tinytuya.Cloud"].requests = _SessionBackedRequests()

cloud = tinytuya.Cloud(
    apiRegion="eu",
    apiKey=ACCESS_ID,
    apiSecret=ACCESS_SECRET,
)

# --- Settings ---
SETTINGS_FILE = os.path.join(os.path.dirname(__file__), "settings.json")
DB_FILE       = os.path.join(os.path.dirname(__file__), "history.db")
USERS_FILE    = os.path.join(os.path.dirname(__file__), "users.json")

# Per-AC state tracking (remote detection + PI controller)
state = {
    "master": {"expected_switch": None, "expected_level": None, "expected_temp": None,
               "integral": 0.0, "current_fan": 0},
    "gaeste": {"expected_switch": None, "expected_level": None, "expected_temp": None,
               "integral": 0.0, "current_fan": 0},
}

# Cached status — updated once per automation cycle, served to the frontend
cache = {
    "ac_master": {},
    "ac_gaeste": {},
    "sensors": {},
    "last_manual_fetch": {"master": 0, "gaeste": 0},
}
MANUAL_POLL_INTERVAL = 300  # 5 minutes

# PI fan speed control defaults (overridable via settings.json)
PI_DEFAULTS = {
    "fan_up":      [1.2, 2.2],   # control thresholds to step UP   (1→2, 2→3)
    "fan_down":    [0.8, 1.8],   # control thresholds to step DOWN  (2→1, 3→2)
    "anti_windup": 15.0,
}


def load_settings():
    with open(SETTINGS_FILE) as f:
        data = json.load(f)
    # Ensure both AC sections exist
    data.setdefault("master", {"mode": "auto", "target_temp": 22})
    data.setdefault("gaeste", {"mode": "auto", "target_temp": 22})
    data.setdefault("hysteresis", 0.2)
    data.setdefault("time_from", "20:00")
    data.setdefault("time_to", "08:00")
    return data


def save_settings(settings):
    with open(SETTINGS_FILE, "w") as f:
        json.dump(settings, f, indent=2)


# --- History DB ---
def init_db():
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS history (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          INTEGER NOT NULL,
                temp_schlaf REAL,
                temp_gaeste REAL,
                temp_wohn   REAL,
                master_on   INTEGER,
                master_mode TEXT,
                master_fan  TEXT,
                gaeste_on   INTEGER,
                gaeste_mode TEXT,
                gaeste_fan  TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ts ON history(ts)")


def log_status(schlaf_temp, gaeste_temp, wohn_temp, ac_master, ac_gaeste, settings):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            conn.execute("""
                INSERT INTO history
                    (ts, temp_schlaf, temp_gaeste, temp_wohn,
                     master_on, master_mode, master_fan,
                     gaeste_on, gaeste_mode, gaeste_fan)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                int(time.time()),
                schlaf_temp, gaeste_temp, wohn_temp,
                1 if ac_master.get("switch") else 0,
                settings["master"].get("mode", "manual"),
                ac_master.get("level", "low"),
                1 if ac_gaeste.get("switch") else 0,
                settings["gaeste"].get("mode", "manual"),
                ac_gaeste.get("level", "low"),
            ))
    except Exception as e:
        print(f"[DB Error] {e}")


# --- AC Control ---
def send_ac(device_id, commands):
    return cloud.cloudrequest(
        f"v1.0/devices/{device_id}/commands",
        action="POST",
        post={"commands": commands},
    )


def get_ac_status(device_id):
    try:
        result = cloud.cloudrequest(f"v1.0/devices/{device_id}/status")
        if result and result.get("success"):
            return {item["code"]: item["value"] for item in result.get("result", [])}
    except Exception as e:
        print(f"[Tuya Error] {e}")
    return None


def ac_turn_on(ac_key, device_id):
    send_ac(device_id, [{"code": "switch", "value": True}])
    state[ac_key]["expected_switch"] = True


def ac_turn_off(ac_key, device_id):
    send_ac(device_id, [{"code": "switch", "value": False}])
    state[ac_key]["expected_switch"] = False


def ac_set_temperature(ac_key, device_id, degrees):
    send_ac(device_id, [{"code": "temp_set", "value": int(degrees)}])
    state[ac_key]["expected_temp"] = int(degrees)


def ac_set_fan_speed(ac_key, device_id, speed):
    send_ac(device_id, [{"code": "level", "value": LEVEL_MAP[speed]}])
    state[ac_key]["expected_level"] = LEVEL_MAP[speed]


def pi_fan_level(control, current_fan, fan_up, fan_down):
    """Apply hysteresis: step up at fan_up thresholds, step down at fan_down thresholds."""
    if current_fan <= 1:
        if control >= fan_up[0]: return 2
        return 1
    elif current_fan == 2:
        if control >= fan_up[1]: return 3
        if control < fan_down[0]: return 1
        return 2
    else:  # 3
        if control < fan_down[1]: return 2
        return 3


# --- Time Window ---
def is_in_time_window(time_from_str, time_to_str):
    now = datetime.now().time()
    fmt = "%H:%M"
    t_from = datetime.strptime(time_from_str, fmt).time()
    t_to   = datetime.strptime(time_to_str,   fmt).time()
    if t_from > t_to:
        return now >= t_from or now < t_to
    return t_from <= now < t_to


# --- Single AC automation step ---
def run_ac_automation(ac_key, device_id, sensor_temp, settings):
    ac_settings = settings.get(ac_key, {})
    cache_key = f"ac_{ac_key}"

    if ac_settings.get("mode") != "auto":
        now = time.time()
        if now - cache["last_manual_fetch"].get(ac_key, 0) >= MANUAL_POLL_INTERVAL:
            ac = get_ac_status(device_id)
            if ac is not None:
                cache[cache_key] = ac
            cache["last_manual_fetch"][ac_key] = now
        return

    st = state[ac_key]
    ac = None  # lazy-loaded, at most one API call per cycle

    # Detect physical remote use (only when we have expected state to compare)
    if any(v is not None for v in [st["expected_switch"], st["expected_level"], st["expected_temp"]]):
        ac = get_ac_status(device_id)
        if ac is None:
            return
        changed = (
            (st["expected_switch"] is not None and ac.get("switch")   != st["expected_switch"]) or
            (st["expected_level"]  is not None and ac.get("level")    != st["expected_level"])  or
            (st["expected_temp"]   is not None and ac.get("temp_set") != st["expected_temp"])
        )
        if changed:
            print(f"[{ac_key}] Physische Fernbedienung erkannt → Manuell")
            settings[ac_key]["mode"] = "manual"
            save_settings(settings)
            st["expected_switch"] = st["expected_level"] = st["expected_temp"] = None
            cache[cache_key] = ac
            return

    # Outside time window: make sure AC is off
    if not is_in_time_window(settings["time_from"], settings["time_to"]):
        if st["expected_switch"] is not False:
            if ac is None:
                ac = get_ac_status(device_id)
            if ac is not None and ac.get("switch", False):
                print(f"[{ac_key}] Außerhalb Zeitfenster und AN → Ausschalten")
                ac_turn_off(ac_key, device_id)
                st["expected_level"] = None
                st["expected_temp"]  = None
        if ac is not None:
            cache[cache_key] = ac
        return

    if sensor_temp == 0:
        return

    target      = ac_settings.get("target_temp", 22)
    hyst        = settings.get("hysteresis", 0.2)
    kp          = ac_settings.get("kp", 1.0)
    ki          = ac_settings.get("ki", 0.0)
    fan_up      = settings.get("fan_up",      PI_DEFAULTS["fan_up"])
    fan_down    = settings.get("fan_down",    PI_DEFAULTS["fan_down"])
    anti_windup = settings.get("anti_windup", PI_DEFAULTS["anti_windup"])
    if ac is None:
        ac = get_ac_status(device_id)
    if ac is None:
        return
    ac_on = ac.get("switch", False)
    diff  = sensor_temp - target

    if not ac_on and diff > hyst:
        # Turn on: reset integral, calculate initial fan without hysteresis
        st["integral"]    = 0.0
        st["current_fan"] = 0
        control = kp * diff
        new_fan = pi_fan_level(control, 0, fan_up, fan_down)
        ac_turn_on(ac_key, device_id)
        ac_set_fan_speed(ac_key, device_id, new_fan)
        ac_set_temperature(ac_key, device_id, AC_TARGET_TEMP)
        st["current_fan"] = new_fan

    elif ac_on and diff < -hyst:
        # Turn off: reset PI state
        st["integral"]    = 0.0
        st["current_fan"] = 0
        ac_turn_off(ac_key, device_id)

    elif ac_on:
        # PI: accumulate integral, calculate control signal
        st["integral"] = max(-anti_windup, min(anti_windup, st["integral"] + diff))
        control = kp * diff + ki * st["integral"]
        new_fan = pi_fan_level(control, st["current_fan"], fan_up, fan_down)

        if new_fan != st["current_fan"]:
            print(f"[{ac_key}] Lüfter {st['current_fan']}→{new_fan} "
                  f"(diff={diff:+.2f}, I={st['integral']:.1f}, ctrl={control:.2f})")
            ac_set_fan_speed(ac_key, device_id, new_fan)
            st["current_fan"] = new_fan

        # Ensure AC target stays at 20°C
        if ac.get("temp_set") != AC_TARGET_TEMP:
            ac_set_temperature(ac_key, device_id, AC_TARGET_TEMP)

    cache[cache_key] = ac


def _count_fds():
    try:
        return len(os.listdir(f"/proc/{os.getpid()}/fd"))
    except OSError:
        return -1


# --- Automation Loop ---
def automation_loop():
    cycle = 0
    while True:
        try:
            settings = load_settings()

            # Read all sensors once per cycle and cache them
            sensors = switchbot.get_all_sensors()
            cache["sensors"] = sensors
            schlaf_temp = sensors["schlafzimmer"]["temperature"]
            gaeste_temp = sensors["gaestezimmer"]["temperature"]
            wohn_temp   = sensors["balkon"]["temperature"]

            run_ac_automation("master", DEVICE_MASTER, schlaf_temp, settings)
            run_ac_automation("gaeste", DEVICE_GAESTE, gaeste_temp, settings)

            # Log from cache — no extra Tuya API calls
            ac_m = cache.get("ac_master") or {}
            ac_g = cache.get("ac_gaeste") or {}
            log_status(schlaf_temp, gaeste_temp, wohn_temp, ac_m, ac_g, settings)

        except Exception as e:
            print(f"[Automation Error] {e}")

        cycle += 1
        if cycle % 60 == 0:
            gc.collect()
            fds = _count_fds()
            print(f"[Health] cycle={cycle} fds={fds}")

        time.sleep(60)


# --- Flask App ---
app = Flask(__name__, static_folder='static')
app.secret_key = os.getenv("FLASK_SECRET_KEY", "fallback-dev-key")
app.permanent_session_lifetime = 60 * 60 * 24 * 30  # 30 Tage


def load_users():
    with open(USERS_FILE) as f:
        return json.load(f)


def check_code(code):
    for user in load_users():
        if user.get("active") and user.get("code") == code:
            return user["name"]
    return None


@app.before_request
def require_login():
    public = {"/login"}
    if request.path not in public and not request.path.startswith("/static"):
        if not session.get("user"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "unauthorized"}), 401
            return redirect(url_for("login", next=request.path))


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        name = check_code(request.form.get("code", "").strip())
        if name:
            session.permanent = True
            session["user"] = name
            return redirect(request.args.get("next") or "/")
        error = "Falscher Code."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/history")
def history():
    return render_template("history.html")


@app.route("/tuning")
def tuning():
    return render_template("tuning.html")


@app.route("/api/tuning", methods=["GET"])
def api_get_tuning():
    s = load_settings()
    return jsonify({
        "fan_up":      s.get("fan_up",      PI_DEFAULTS["fan_up"]),
        "fan_down":    s.get("fan_down",    PI_DEFAULTS["fan_down"]),
        "anti_windup": s.get("anti_windup", PI_DEFAULTS["anti_windup"]),
        "master_kp":   s["master"].get("kp", 1.0),
        "master_ki":   s["master"].get("ki", 0.01),
        "gaeste_kp":   s["gaeste"].get("kp", 1.0),
        "gaeste_ki":   s["gaeste"].get("ki", 0.02),
    })


@app.route("/api/tuning", methods=["POST"])
def api_post_tuning():
    body = request.json
    s = load_settings()
    s["fan_up"]      = body["fan_up"]
    s["fan_down"]    = body["fan_down"]
    s["anti_windup"] = body["anti_windup"]
    s["master"]["kp"] = body["master_kp"]
    s["master"]["ki"] = body["master_ki"]
    s["gaeste"]["kp"] = body["gaeste_kp"]
    s["gaeste"]["ki"] = body["gaeste_ki"]
    save_settings(s)
    # Reset PI state on all ACs so new values take effect immediately
    for key in state:
        state[key]["integral"]    = 0.0
        state[key]["current_fan"] = 0
    return jsonify({"success": True})


@app.route("/api/history")
def api_history():
    range_param = request.args.get("range", "24h")
    step = {"6h": 1, "24h": 5, "7d": 10, "30d": 60, "90d": 180, "1y": 720}.get(range_param, 5)
    since = int(time.time()) - {"6h": 21600, "24h": 86400, "7d": 604800, "30d": 2592000, "90d": 7776000, "1y": 31536000}.get(range_param, 86400)

    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM history WHERE ts >= ? AND (ROWID % ?) = 0 ORDER BY ts ASC",
            (since, step)
        ).fetchall()

    return jsonify([dict(r) for r in rows])


@app.route("/api/status")
def api_status():
    settings = load_settings()

    def fmt_ac(ac):
        return {
            "switch":       ac.get("switch", False),
            "temp_current": ac.get("temp_current", 0),
            "temp_set":     ac.get("temp_set", 0),
            "level":        ac.get("level", "low"),
            "mode":         ac.get("mode", "cold"),
        }

    return jsonify({
        "sensors":       cache.get("sensors", {}),
        "ac_master":     fmt_ac(cache.get("ac_master", {})),
        "ac_gaeste":     fmt_ac(cache.get("ac_gaeste", {})),
        "mode_master":   settings["master"].get("mode", "auto"),
        "mode_gaeste":   settings["gaeste"].get("mode", "auto"),
    })


@app.route("/api/settings", methods=["GET"])
def api_get_settings():
    return jsonify(load_settings())


@app.route("/api/settings", methods=["POST"])
def api_post_settings():
    save_settings(request.json)
    return jsonify({"success": True})


@app.route("/api/mode/<ac_key>", methods=["POST"])
def api_set_mode(ac_key):
    if ac_key not in ("master", "gaeste"):
        return jsonify({"error": "Invalid AC key"}), 400
    mode = request.json.get("mode")
    if mode not in ("auto", "manual"):
        return jsonify({"error": "Invalid mode"}), 400

    settings = load_settings()
    settings[ac_key]["mode"] = mode
    save_settings(settings)

    if mode == "auto":
        st = state[ac_key]
        device_id = DEVICE_MASTER if ac_key == "master" else DEVICE_GAESTE
        # Sync expected state with current hardware so remote detection works immediately
        ac = get_ac_status(device_id)
        st["expected_switch"] = ac.get("switch", False)
        st["expected_level"]  = ac.get("level", "low")
        st["expected_temp"]   = AC_TARGET_TEMP
        # Reset PI state on mode switch
        st["integral"]    = 0.0
        st["current_fan"] = 0
        ac_set_temperature(ac_key, device_id, AC_TARGET_TEMP)

    return jsonify({"success": True, "mode": mode})


def refresh_cache_after_manual(ac_key, device_id):
    time.sleep(1)
    ac = get_ac_status(device_id)
    if ac is not None:
        cache[f"ac_{ac_key}"] = ac
    cache["last_manual_fetch"][ac_key] = time.time()


@app.route("/api/ac/<ac_key>/on", methods=["POST"])
def api_ac_on(ac_key):
    if ac_key not in ("master", "gaeste"):
        return jsonify({"error": "Invalid AC key"}), 400
    device_id = DEVICE_MASTER if ac_key == "master" else DEVICE_GAESTE
    ac_turn_on(ac_key, device_id)
    threading.Thread(target=refresh_cache_after_manual, args=(ac_key, device_id), daemon=True).start()
    return jsonify({"success": True})


@app.route("/api/ac/<ac_key>/off", methods=["POST"])
def api_ac_off(ac_key):
    if ac_key not in ("master", "gaeste"):
        return jsonify({"error": "Invalid AC key"}), 400
    device_id = DEVICE_MASTER if ac_key == "master" else DEVICE_GAESTE
    ac_turn_off(ac_key, device_id)
    threading.Thread(target=refresh_cache_after_manual, args=(ac_key, device_id), daemon=True).start()
    return jsonify({"success": True})


@app.route("/api/ac/<ac_key>/fan", methods=["POST"])
def api_ac_fan(ac_key):
    if ac_key not in ("master", "gaeste"):
        return jsonify({"error": "Invalid AC key"}), 400
    level = request.json.get("level")
    if level not in (1, 2, 3):
        return jsonify({"error": "Invalid level (1/2/3)"}), 400
    device_id = DEVICE_MASTER if ac_key == "master" else DEVICE_GAESTE
    ac_set_fan_speed(ac_key, device_id, level)
    threading.Thread(target=refresh_cache_after_manual, args=(ac_key, device_id), daemon=True).start()
    return jsonify({"success": True})


def startup_safe_state():
    """Beim Start: beide ACs auf Manuell und Aus setzen — mit Verifikation und Retry."""
    print("Startup: Setze beide Klimaanlagen auf Manuell + Aus...")
    try:
        settings = load_settings()
        settings["master"]["mode"] = "manual"
        settings["gaeste"]["mode"] = "manual"
        save_settings(settings)
    except Exception as e:
        print(f"Startup-Fehler (Settings): {e}")

    for attempt in range(1, 4):
        try:
            ac_turn_off("master", DEVICE_MASTER)
            ac_turn_off("gaeste", DEVICE_GAESTE)
            time.sleep(3)

            master_status = get_ac_status(DEVICE_MASTER)
            gaeste_status = get_ac_status(DEVICE_GAESTE)

            if master_status is None or gaeste_status is None:
                print(f"Startup: Versuch {attempt} — Tuya API nicht erreichbar, überspringe Verifikation")
                continue

            master_on = master_status.get("switch", True)
            gaeste_on = gaeste_status.get("switch", True)

            if not master_on and not gaeste_on:
                print(f"Startup: Beide Klimaanlagen sind AUS und auf Manuell. (Versuch {attempt})")
                return

            print(f"Startup: Versuch {attempt} — noch an (Master={master_on}, Gäste={gaeste_on}), retry...")
        except Exception as e:
            print(f"Startup-Fehler (Versuch {attempt}): {e}")
        time.sleep(5)

    print("Startup: WARNUNG — Klimaanlagen konnten nicht zuverlässig ausgeschaltet werden!")


if __name__ == "__main__":
    init_db()
    startup_safe_state()
    t = threading.Thread(target=automation_loop, daemon=True)
    t.start()
    print("Automation gestartet (Master + Gästezimmer).")
    app.run(host="0.0.0.0", port=8080, debug=False)
