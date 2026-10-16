"""Configuration: config.json next to the project, with SATURN_* env overrides."""
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DEFAULTS = {
    "printer_ip": "",                 # blank = find the printer by scanning the network on first run
    "printer_port": 3030,
    "video_port": 3031,
    "udp_port": 3000,
    "mainboard_id": "",
    "web_host": "127.0.0.1",
    "web_port": 8080,
    "open_browser": True,
    "auto_exit": True,
    "auto_exit_seconds": 10,
    "light_enabled": False,          # show the light button in the header (see the Printer tab's light test)
    "keepalive_seconds": 12,         # how often the app polls the printer to prove the link is alive
    "keepalive_timeout": 8,
    "printers": [],                   # saved printers (see normalize_printers); printer_ip / printer_port / ... mirror the active one
    "active_printer": "",             # id of the active entry in "printers"
    "printers_configured": False,     # becomes true once the list is saved from the app; then an empty list stays empty (no printer)
    "render_folder": "C:\\",             # Render upload tab: folder on the PC running this app that holds rendered .goo files (change it in Settings)
    "camera_quality": 2,             # ffmpeg JPEG quality for the RTSP camera, 2 (best) .. 31 (worst)
}

ENV_MAP = {
    "SATURN_IP": ("printer_ip", str),
    "SATURN_PORT": ("printer_port", int),
    "SATURN_VIDEO_PORT": ("video_port", int),
    "SATURN_UDP_PORT": ("udp_port", int),
    "SATURN_WEB_HOST": ("web_host", str),
    "SATURN_WEB_PORT": ("web_port", int),
}


PRINTER_PORTS = {"printer_port": 3030, "video_port": 3031, "udp_port": 3000, "web_port": 8080}


def normalize_printers(items, fallback=None):
    """Clean a list of saved printers: every entry gets an id, a name and all four ports (defaults filled in).
    Several entries may share an IP address."""
    import uuid
    fallback = fallback or PRINTER_PORTS
    out, ids = [], set()
    for x in items or []:
        if not isinstance(x, dict):
            continue
        ip = str(x.get("ip", "")).strip()
        if not ip:
            continue
        pid = str(x.get("id") or "")
        if not pid or pid in ids:
            pid = uuid.uuid4().hex[:8]
        ids.add(pid)
        e = {"id": pid, "name": str(x.get("name", "")).strip() or ip, "ip": ip}
        for k, dflt in fallback.items():
            try:
                e[k] = int(x.get(k, dflt))
            except (TypeError, ValueError):
                e[k] = dflt
        out.append(e)
    return out


def load_config(path=None):
    cfg = json.loads(json.dumps(DEFAULTS))
    p = Path(path or os.environ.get("SATURN_CONFIG") or ROOT / "config.json")
    if p.exists():
        cfg.update(json.loads(p.read_text(encoding="utf-8")))
    cfg["printers"] = normalize_printers(cfg.get("printers"), {k: cfg.get(k, d) for k, d in PRINTER_PORTS.items()})
    active = next((x for x in cfg["printers"] if x["id"] == cfg.get("active_printer")), None) \
        or next((x for x in cfg["printers"] if x["ip"] == cfg.get("printer_ip")), None) or (cfg["printers"] or [None])[0]
    if cfg.get("printers_configured") and not cfg["printers"]:      # every printer was deleted in the app
        cfg["active_printer"] = ""
        cfg["printer_ip"] = ""
    if active:                                   # the active saved printer decides the IP and all ports at startup
        cfg["active_printer"] = active["id"]
        cfg["printer_ip"] = active["ip"]
        for k in PRINTER_PORTS:
            cfg[k] = active[k]
    for env, (key, cast) in ENV_MAP.items():
        if os.environ.get(env):
            cfg[key] = cast(os.environ[env])
    return cfg
