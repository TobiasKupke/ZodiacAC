import json
import os
import sqlite3
import threading
import time
from datetime import datetime

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

# PI fan speed control constants
ANTI_WINDUP  = 15.0  # integral clamp
FAN_UP       = [1.2, 2.2]   # control thresholds to step UP   (1→2, 2→3)
FAN_DOWN_THR = 0.8           # control threshold to step DOWN  (one level at a time)


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
    result = cloud.cloudrequest(f"v1.0/devices/{device_id}/status")
    return {item["code"]: item["value"] for item in result.get("result", [])}


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


def pi_fan_level(control, current_fan):
    """Apply hysteresis: step up at FAN_UP thresholds, step down at FAN_DOWN_THR."""
    if current_fan <= 1:
        if control >= FAN_UP[0]: return 2
        return 1
    elif current_fan == 2:
        if control >= FAN_UP[1]: return 3
        if control < FAN_DOWN_THR: return 1
        return 2
    else:  # 3
        if control < FAN_DOWN_THR: return 2
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
    if ac_settings.get("mode") != "auto":
        return

    st = state[ac_key]
    ac = None  # lazy-loaded, at most one API call per cycle

    # Detect physical remote use (only when we have expected state to compare)
    if any(v is not None for v in [st["expected_switch"], st["expected_level"], st["expected_temp"]]):
        ac = get_ac_status(device_id)
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
            return

    # Outside time window: make sure AC is off
    if not is_in_time_window(settings["time_from"], settings["time_to"]):
        if st["expected_switch"] is not False:  # Skip redundant API call if already confirmed off
            if ac is None:
                ac = get_ac_status(device_id)
            if ac.get("switch", False):
                print(f"[{ac_key}] Außerhalb Zeitfenster und AN → Ausschalten")
                ac_turn_off(ac_key, device_id)
                st["expected_level"] = None
                st["expected_temp"]  = None
        return

    if sensor_temp == 0:
        return

    target = ac_settings.get("target_temp", 22)
    hyst   = settings.get("hysteresis", 0.2)
    kp     = ac_settings.get("kp", 1.0)
    ki     = ac_settings.get("ki", 0.0)
    if ac is None:
        ac = get_ac_status(device_id)
    ac_on = ac.get("switch", False)
    diff  = sensor_temp - target

    if not ac_on and diff > hyst:
        # Turn on: reset integral, calculate initial fan without hysteresis
        st["integral"]    = 0.0
        st["current_fan"] = 0
        control = kp * diff
        new_fan = pi_fan_level(control, 0)
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
        st["integral"] = max(-ANTI_WINDUP, min(ANTI_WINDUP, st["integral"] + diff))
        control = kp * diff + ki * st["integral"]
        new_fan = pi_fan_level(control, st["current_fan"])

        if new_fan != st["current_fan"]:
            print(f"[{ac_key}] Lüfter {st['current_fan']}→{new_fan} "
                  f"(diff={diff:+.2f}, I={st['integral']:.1f}, ctrl={control:.2f})")
            ac_set_fan_speed(ac_key, device_id, new_fan)
            st["current_fan"] = new_fan

        # Ensure AC target stays at 20°C
        if ac.get("temp_set") != AC_TARGET_TEMP:
            ac_set_temperature(ac_key, device_id, AC_TARGET_TEMP)


# --- Automation Loop ---
def automation_loop():
    while True:
        try:
            settings = load_settings()

            # Read all sensors once per cycle
            schlaf_data = switchbot.get_sensor_status(switchbot.SCHLAFZIMMER_ID)
            gaeste_data = switchbot.get_sensor_status(switchbot.GAESTEZIMMER_ID)
            wohn_data   = switchbot.get_sensor_status(switchbot.WOHNZIMMER_ID)
            schlaf_temp = schlaf_data.get("temperature", 0)
            gaeste_temp = gaeste_data.get("temperature", 0)
            wohn_temp   = wohn_data.get("temperature", 0)

            run_ac_automation("master", DEVICE_MASTER, schlaf_temp, settings)
            run_ac_automation("gaeste", DEVICE_GAESTE, gaeste_temp, settings)

            # Log current state to history DB
            ac_master = get_ac_status(DEVICE_MASTER)
            ac_gaeste = get_ac_status(DEVICE_GAESTE)
            log_status(schlaf_temp, gaeste_temp, wohn_temp, ac_master, ac_gaeste, settings)

        except Exception as e:
            print(f"[Automation Error] {e}")

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


@app.route("/api/history")
def api_history():
    range_param = request.args.get("range", "24h")
    step = {"6h": 1, "24h": 5, "7d": 10, "30d": 60}.get(range_param, 5)
    since = int(time.time()) - {"6h": 21600, "24h": 86400, "7d": 604800, "30d": 2592000}.get(range_param, 86400)

    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM history WHERE ts >= ? AND (ROWID % ?) = 0 ORDER BY ts ASC",
            (since, step)
        ).fetchall()

    return jsonify([dict(r) for r in rows])


@app.route("/api/status")
def api_status():
    sensors  = switchbot.get_all_sensors()
    ac_master = get_ac_status(DEVICE_MASTER)
    ac_gaeste = get_ac_status(DEVICE_GAESTE)
    settings  = load_settings()

    def fmt_ac(ac):
        return {
            "switch":       ac.get("switch", False),
            "temp_current": ac.get("temp_current", 0),
            "temp_set":     ac.get("temp_set", 0),
            "level":        ac.get("level", "low"),
            "mode":         ac.get("mode", "cold"),
        }

    return jsonify({
        "sensors":       sensors,
        "ac_master":     fmt_ac(ac_master),
        "ac_gaeste":     fmt_ac(ac_gaeste),
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


@app.route("/api/ac/<ac_key>/on", methods=["POST"])
def api_ac_on(ac_key):
    if ac_key not in ("master", "gaeste"):
        return jsonify({"error": "Invalid AC key"}), 400
    device_id = DEVICE_MASTER if ac_key == "master" else DEVICE_GAESTE
    ac_turn_on(ac_key, device_id)
    return jsonify({"success": True})


@app.route("/api/ac/<ac_key>/off", methods=["POST"])
def api_ac_off(ac_key):
    if ac_key not in ("master", "gaeste"):
        return jsonify({"error": "Invalid AC key"}), 400
    device_id = DEVICE_MASTER if ac_key == "master" else DEVICE_GAESTE
    ac_turn_off(ac_key, device_id)
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

            master_on = get_ac_status(DEVICE_MASTER).get("switch", True)
            gaeste_on = get_ac_status(DEVICE_GAESTE).get("switch", True)

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
