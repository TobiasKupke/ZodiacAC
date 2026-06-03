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

METER_PRO_CO2_ID = os.getenv("SWITCHBOT_METER_PRO_CO2_ID")
METER_PLUS_ID = os.getenv("SWITCHBOT_METER_PLUS_ID")


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
    resp = requests.get(f"{BASE_URL}/v1.1/devices/{device_id}/status", headers=get_headers())
    return resp.json().get("body", {})


def get_all_sensors():
    co2 = get_sensor_status(METER_PRO_CO2_ID)
    plus = get_sensor_status(METER_PLUS_ID)
    return {
        "meter_pro_co2": {
            "name": "Meter Pro CO2",
            "temperature": co2.get("temperature", 0),
            "humidity": co2.get("humidity", 0),
            "co2": co2.get("CO2", 0),
            "battery": co2.get("battery", 0),
        },
        "meter_plus": {
            "name": "Meter Plus",
            "temperature": plus.get("temperature", 0),
            "humidity": plus.get("humidity", 0),
            "battery": plus.get("battery", 0),
        },
    }
