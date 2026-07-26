import hashlib
import hmac
import os
import time
import uuid
import base64
import requests
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("SWITCHBOT_TOKEN")
SECRET = os.getenv("SWITCHBOT_SECRET")
BASE_URL = "https://api.switch-bot.com"

_session = requests.Session()

SCHLAFZIMMER_ID  = os.getenv("SWITCHBOT_METER_PRO_CO2_ID")   # Meter Pro CO2 — Steuerungssensor
WOHNZIMMER_ID    = os.getenv("SWITCHBOT_METER_PLUS_ID")       # Meter Plus 1
GAESTEZIMMER_ID  = "C89202466057"                              # Meter Plus 2


def get_headers():
    t = str(int(time.time() * 1000))
    nonce = str(uuid.uuid4())
    sign_str = TOKEN + t + nonce
    sign = hmac.new(SECRET.encode("utf-8"), sign_str.encode("utf-8"), hashlib.sha256).digest()
    sign_b64 = base64.b64encode(sign).decode("utf-8")
    return {
        "Authorization": TOKEN,
        "sign": sign_b64,
        "t": t,
        "nonce": nonce,
        "Content-Type": "application/json",
    }


def get_sensor_status(device_id):
    resp = _session.get(f"{BASE_URL}/v1.1/devices/{device_id}/status", headers=get_headers())
    return resp.json().get("body", {})


def get_all_sensors():
    schlaf  = get_sensor_status(SCHLAFZIMMER_ID)
    wohn    = get_sensor_status(WOHNZIMMER_ID)
    gaeste  = get_sensor_status(GAESTEZIMMER_ID)
    return {
        "schlafzimmer": {
            "name": "Schlafzimmer",
            "temperature": schlaf.get("temperature", 0),
            "humidity": schlaf.get("humidity", 0),
            "co2": schlaf.get("CO2", 0),
            "battery": schlaf.get("battery", 0),
        },
        "wohnzimmer": {
            "name": "Wohnzimmer",
            "temperature": wohn.get("temperature", 0),
            "humidity": wohn.get("humidity", 0),
            "battery": wohn.get("battery", 0),
        },
        "gaestezimmer": {
            "name": "Gästezimmer",
            "temperature": gaeste.get("temperature", 0),
            "humidity": gaeste.get("humidity", 0),
            "battery": gaeste.get("battery", 0),
        },
    }
