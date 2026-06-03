import tinytuya

ACCESS_ID = "kcrfu9n33xpuswvs9xsy"
ACCESS_SECRET = "b3587120d1414edc9840f47d42789fe3"
DEVICE_ID = "bf44546cfbf61c2262fz27"

LEVEL_MAP = {1: "low", 2: "middle", 3: "high"}

cloud = tinytuya.Cloud(
    apiRegion="eu",
    apiKey=ACCESS_ID,
    apiSecret=ACCESS_SECRET,
)


def send(commands):
    return cloud.cloudrequest(
        f"v1.0/devices/{DEVICE_ID}/commands",
        action="POST",
        post={"commands": commands},
    )


def turn_on():
    return send([{"code": "switch", "value": True}])


def turn_off():
    return send([{"code": "switch", "value": False}])


def set_temperature(degrees):
    if not (5 <= degrees <= 40):
        raise ValueError("Temperatur muss zwischen 5 und 40°C liegen")
    return send([{"code": "temp_set", "value": degrees}])


def set_fan_speed(speed):
    if speed not in LEVEL_MAP:
        raise ValueError("Stärke muss 1 (niedrig), 2 (mittel) oder 3 (hoch) sein")
    return send([{"code": "level", "value": LEVEL_MAP[speed]}])


def status():
    result = cloud.cloudrequest(f"v1.0/devices/{DEVICE_ID}/status")
    return {item["code"]: item["value"] for item in result["result"]}


if __name__ == "__main__":
    print("Setze Ventilator auf Stärke 3 (hoch)...")
    print(set_fan_speed(3))
