"""Configuration: config.json next to the project, with SATURN_* env overrides."""
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DEFAULTS = {
    "printer_ip": "192.168.0.189",
    "printer_port": 3030,
    "video_port": 3031,
    "udp_port": 3000,
    "mainboard_id": "",
    "web_host": "127.0.0.1",
    "web_port": 8080,
    "open_browser": True,
    "light_enabled": False,
    "light": {
        "cmd": 403,
        "on": {"LightStatus": {"SecondLight": True, "RgbLight": [0, 0, 0]}},
        "off": {"LightStatus": {"SecondLight": False, "RgbLight": [0, 0, 0]}},
    },
}

ENV_MAP = {
    "SATURN_IP": ("printer_ip", str),
    "SATURN_PORT": ("printer_port", int),
    "SATURN_VIDEO_PORT": ("video_port", int),
    "SATURN_UDP_PORT": ("udp_port", int),
    "SATURN_WEB_HOST": ("web_host", str),
    "SATURN_WEB_PORT": ("web_port", int),
}


def load_config(path=None):
    cfg = json.loads(json.dumps(DEFAULTS))
    p = Path(path or os.environ.get("SATURN_CONFIG") or ROOT / "config.json")
    if p.exists():
        cfg.update(json.loads(p.read_text(encoding="utf-8")))
    for env, (key, cast) in ENV_MAP.items():
        if os.environ.get(env):
            cfg[key] = cast(os.environ[env])
    return cfg
