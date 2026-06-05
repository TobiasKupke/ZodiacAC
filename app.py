import json
import os
import threading
import time
from datetime import datetime

import tinytuya
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request

import switchbot

load_dotenv()

# --- Tuya Setup ---
ACCESS_ID = os.getenv("TUYA_ACCESS_ID")
ACCESS_SECRET = os.getenv("TUYA_ACCESS_SECRET")
DEVICE_ID = os.getenv("TUYA_DEVICE_ID")
LEVEL_MAP = {1: "low", 2: "middle", 3: "high"}

cloud = tinytuya.Cloud(
    apiRegion="eu",
    apiKey=ACCESS_ID,
    apiSecret=ACCESS_SECRET,
)

# --- Settings ---
SETTINGS_FILE = os.path.join(os.path.dirname(__file__), "settings.json")

# Tracks what the automation last set — used to detect physical remote use
expected_ac_switch   = None
expected_ac_level    = None
expected_ac_temp_set = None


def load_settings():
    with open(SETTINGS_FILE) as f:
        data = json.load(f)
    # Migrate old "enabled" field to "mode"
    if "enabled" in data and "mode" not in data:
        data["mode"] = "auto" if data.pop("enabled") else "manual"
        save_settings(data)
    data.setdefault("mode", "auto")
    return data


def save_settings(settings):
    with open(SETTINGS_FILE, "w") as f:
        json.dump(settings, f, indent=2)


# --- AC Control ---
def send_ac(commands):
    return cloud.cloudrequest(
        f"v1.0/devices/{DEVICE_ID}/commands",
        action="POST",
        post={"commands": commands},
    )


def get_ac_status():
    result = cloud.cloudrequest(f"v1.0/devices/{DEVICE_ID}/status")
    return {item["code"]: item["value"] for item in result.get("result", [])}


def ac_turn_on():
    global expected_ac_switch
    send_ac([{"code": "switch", "value": True}])
    expected_ac_switch = True


def ac_turn_off():
    global expected_ac_switch
    send_ac([{"code": "switch", "value": False}])
    expected_ac_switch = False


def ac_set_temperature(degrees):
    global expected_ac_temp_set
    send_ac([{"code": "temp_set", "value": int(degrees)}])
    expected_ac_temp_set = int(degrees)


def ac_set_fan_speed(speed):
    global expected_ac_level
    send_ac([{"code": "level", "value": LEVEL_MAP[speed]}])
    expected_ac_level = LEVEL_MAP[speed]


def fan_speed_for_diff(diff):
    if diff < 1.0:
        return 1
    elif diff < 2.0:
        return 2
    return 3


# --- Time Window ---
def is_in_time_window(time_from_str, time_to_str):
    now = datetime.now().time()
    fmt = "%H:%M"
    t_from = datetime.strptime(time_from_str, fmt).time()
    t_to = datetime.strptime(time_to_str, fmt).time()
    if t_from > t_to:  # overnight (e.g. 21:00 - 07:00)
        return now >= t_from or now < t_to
    return t_from <= now < t_to


# --- Automation Loop ---
def automation_loop():
    global expected_ac_switch, expected_ac_level, expected_ac_temp_set
    while True:
        try:
            settings = load_settings()

            if settings.get("mode") != "auto":
                time.sleep(30)
                continue

            # Detect physical remote use — always check, regardless of time window
            if any(v is not None for v in [expected_ac_switch, expected_ac_level, expected_ac_temp_set]):
                ac = get_ac_status()
                physical_change = (
                    (expected_ac_switch   is not None and ac.get("switch")   != expected_ac_switch)   or
                    (expected_ac_level    is not None and ac.get("level")    != expected_ac_level)    or
                    (expected_ac_temp_set is not None and ac.get("temp_set") != expected_ac_temp_set)
                )
                if physical_change:
                    print("[Automation] Physische Fernbedienung erkannt → wechsle zu Manuell")
                    settings["mode"] = "manual"
                    save_settings(settings)
                    expected_ac_switch   = None
                    expected_ac_level    = None
                    expected_ac_temp_set = None
                    time.sleep(60)
                    continue

            # Only control AC within time window
            if not is_in_time_window(settings["time_from"], settings["time_to"]):
                time.sleep(60)
                continue

            sensor = switchbot.get_sensor_status(switchbot.METER_PRO_CO2_ID)
            current_temp = sensor.get("temperature", 0)

            if current_temp == 0:
                time.sleep(60)
                continue

            target = settings["target_temp"]
            hysteresis = settings.get("hysteresis", 0.2)
            ac = get_ac_status()
            ac_on = ac.get("switch", False)
            diff = current_temp - target

            if not ac_on and diff > hysteresis:
                ac_turn_on()
                ac_set_fan_speed(fan_speed_for_diff(diff))
                ac_set_temperature(int(target))
            elif ac_on and diff < -hysteresis:
                ac_turn_off()
            elif ac_on:
                ac_set_fan_speed(fan_speed_for_diff(abs(diff)))

        except Exception as e:
            print(f"[Automation Error] {e}")

        time.sleep(60)


# --- Flask App ---
app = Flask(__name__)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    sensors = switchbot.get_all_sensors()
    ac = get_ac_status()
    settings = load_settings()
    return jsonify({
        "sensors": sensors,
        "ac": {
            "switch": ac.get("switch", False),
            "temp_current": ac.get("temp_current", 0),
            "temp_set": ac.get("temp_set", 0),
            "level": ac.get("level", "low"),
            "mode": ac.get("mode", "cold"),
        },
        "control_mode": settings.get("mode", "auto"),
    })


@app.route("/api/settings", methods=["GET"])
def api_get_settings():
    return jsonify(load_settings())


@app.route("/api/settings", methods=["POST"])
def api_post_settings():
    save_settings(request.json)
    return jsonify({"success": True})


@app.route("/api/mode", methods=["POST"])
def api_set_mode():
    global expected_ac_switch, expected_ac_level, expected_ac_temp_set
    mode = request.json.get("mode")
    if mode not in ("auto", "manual"):
        return jsonify({"error": "Invalid mode"}), 400
    settings = load_settings()
    settings["mode"] = mode
    save_settings(settings)
    if mode == "auto":
        # Reset tracking — automation takes over fresh
        expected_ac_switch   = None
        expected_ac_level    = None
        expected_ac_temp_set = None
    return jsonify({"success": True, "mode": mode})


@app.route("/api/ac/on", methods=["POST"])
def api_ac_on():
    global expected_ac_switch
    ac_turn_on()
    expected_ac_switch = True
    return jsonify({"success": True})


@app.route("/api/ac/off", methods=["POST"])
def api_ac_off():
    global expected_ac_switch
    ac_turn_off()
    expected_ac_switch = False
    return jsonify({"success": True})


if __name__ == "__main__":
    t = threading.Thread(target=automation_loop, daemon=True)
    t.start()
    print("Automation gestartet.")
    app.run(host="0.0.0.0", port=8080, debug=False)
