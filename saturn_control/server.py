"""Web UI + JSON API in front of the printer's SDCP interface."""
import asyncio
import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

import aiohttp
from aiohttp import web

from .config import PRINTER_PORTS, ROOT, load_config, normalize_printers
from .httpfs import has_percent_codes, is_virtual, looks_like_fragment, normalize_dir, parse_listing
from .printer import CMD, PrinterClient, PrinterError, _num, discover, pick

STATIC = Path(__file__).parent / "static"


def ffmpeg_path():
    """System ffmpeg if present, else the one bundled with the imageio-ffmpeg pip package."""
    p = shutil.which("ffmpeg")
    if p:
        return p
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001
        return None


async def stop_ffmpeg(proc, grace=3.0):
    """Stop ffmpeg politely: 'q' on stdin makes it send the RTSP TEARDOWN, which frees the printer's
    camera slot. A hard kill() skips that and leaves the slot occupied until the printer times it out."""
    if proc.returncode is not None:
        return
    try:
        if proc.stdin:
            proc.stdin.write(b"q")
            await proc.stdin.drain()
    except Exception:  # noqa: BLE001
        pass
    for sig, wait in (("q", grace), ("terminate", 2.0), ("kill", 2.0)):
        if sig == "terminate":
            proc.terminate()
        elif sig == "kill":
            proc.kill()
        try:
            await asyncio.wait_for(proc.wait(), wait)
            return
        except asyncio.TimeoutError:
            continue


def jerr(msg, status=400, **extra):
    return web.json_response({"ok": False, "message": msg, **extra}, status=status)


CLEANED = threading.Event()       # set once the app has released the camera and closed the printer link
LOOP = {}                         # the running event loop, for the console-close handler


def _graceful_exit():
    raise web.GracefulExit()


def install_console_close_handler():
    """Windows: closing the cmd window (X), logging off or shutting down kills Python without running its
    cleanup, which leaves the printer's camera slot (and ffmpeg) open. Catch those events, run the normal
    shutdown (stops ffmpeg, frees the camera slot) and give it up to 4 s, as Windows allows about 5 s."""
    if os.name != "nt":
        return
    try:
        import ctypes
        proto = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_uint)

        def handler(ctrl_type):
            if ctrl_type not in (2, 5, 6):          # CTRL_CLOSE_EVENT, CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT
                return 0                            # Ctrl+C / Ctrl+Break keep their normal handling
            loop = LOOP.get("loop")
            if loop is not None and not loop.is_closed():
                loop.call_soon_threadsafe(_graceful_exit)
                CLEANED.wait(4)
            return 1
        _CLOSE_HANDLER.append(proto(handler))       # keep a reference so it is not garbage collected
        ctypes.windll.kernel32.SetConsoleCtrlHandler(_CLOSE_HANDLER[-1], 1)
    except Exception as e:  # noqa: BLE001 - never stop the app from starting over this
        print(f"Could not install the window-close handler: {e}")


_CLOSE_HANDLER = []


def create_app(cfg=None):
    cfg = cfg or load_config()
    app = web.Application(client_max_size=1024 ** 2)
    app["cfg"] = cfg
    app["clients"] = set()
    app["logq"] = []
    app["imgcache"] = OrderedDict()
    app["dirty"] = None  # asyncio.Event, created in on_startup

    routes = web.RouteTableDef()

    def printer() -> PrinterClient:
        return app["printer"]

    async def run_cmd(cmd, data=None, timeout=10):
        try:
            ok, ack, msg, d = await printer().command(cmd, data, timeout)
        except PrinterError as e:
            return jerr(str(e), 502)
        return web.json_response({"ok": ok, "ack": ack, "message": msg, "data": d})

    # ---------------------------------------------------------------- pages
    @routes.get("/")
    async def index(request):
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        for name in ("style.css", "app.js"):          # version the links by file time so an update is never hidden by a cached copy
            try:
                v = int((STATIC / name).stat().st_mtime)
            except OSError:
                continue
            html = html.replace(f"/static/{name}", f"/static/{name}?v={v}")
        return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-cache"})

    def state_payload():
        st = printer().public_state()
        a = next((x for x in cfg.get("printers") or [] if x["id"] == cfg.get("active_printer")), None)
        st["printer_name"] = a["name"] if a else ""
        return st

    @routes.get("/api/state")
    async def state(request):
        return web.json_response(state_payload())

    scan = {"on": False}                              # True while the startup scan is looking for printers

    def printer_list():
        """Saved printers (id, name, ip and the four ports each). Always contains the active one."""
        lst = cfg.get("printers") or []
        if not lst and (scan["on"] or cfg.get("printers_configured")):
            return []             # still scanning for printers, or the user deleted every printer: nothing to show or connect to
        if not any(x["id"] == cfg.get("active_printer") for x in lst):
            name = (printer().attributes or {}).get("Name") if "printer" in app else None
            seed = normalize_printers([{"name": name or "Printer", "ip": cfg["printer_ip"], **{k: cfg[k] for k in PRINTER_PORTS if k in cfg}}])[0]
            cfg["printers"] = lst = [seed] + [x for x in lst]
            cfg["active_printer"] = seed["id"]
        return lst

    def active_printer():
        return next((x for x in printer_list() if x["id"] == cfg.get("active_printer")), None)

    def save_config(updates):
        path = Path(os.environ.get("SATURN_CONFIG") or ROOT / "config.json")
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        data.update(updates)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return path

    def active_updates():
        a = active_printer()
        if a is None:
            return {"printers": [], "printers_configured": True, "active_printer": "", "printer_ip": ""}
        return {"printers": printer_list(), "printers_configured": True, "active_printer": a["id"], "printer_ip": a["ip"],
                "printer_port": a["printer_port"], "video_port": a["video_port"], "udp_port": a["udp_port"]}

    async def drop_camera():
        """Stop our camera stream and tell the printer we are done with it."""
        t = hub["task"]
        if hub["off"]:
            hub["off"].cancel()
            hub["off"] = None
        if t:
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    async def leave_printer():
        """Before moving to another printer (or another address): stop our camera stream and free its slot."""
        p = printer()
        try:
            await asyncio.wait_for(drop_camera(), 5)
            if p.connected:
                await asyncio.wait_for(p.enable_video(False), 3)
        except Exception:  # noqa: BLE001
            pass

    def apply_active():
        """Point the running app at the active saved printer."""
        a = active_printer()
        if a is None:                                # no printers left: disconnect and idle
            cfg["printer_ip"] = ""
            printer().reconfigure("")
            return
        same = printer().ip == a["ip"] and printer().port == a["printer_port"]     # e.g. the scan found the printer we are already on
        cfg.update(printer_ip=a["ip"], printer_port=a["printer_port"], video_port=a["video_port"], udp_port=a["udp_port"])
        if not same:
            printer().port = a["printer_port"]
            printer().reconfigure(a["ip"])

    def upload_running():
        up = printer().upload
        return bool(up and up.get("phase") in ("sending", "verifying")) or bool(printer().queue)

    def printer_label(d):
        return str(d.get("Name") or d.get("MachineName") or "").strip()

    async def scan_printers(timeout=2.5):
        """UDP scan: broadcast, plus the subnet broadcast and a direct ask for every printer we already know."""
        targets = [("255.255.255.255", cfg.get("udp_port", 3000))]
        for x in list(cfg.get("printers") or []) + [{"ip": cfg.get("printer_ip"), "udp_port": cfg.get("udp_port", 3000)}]:
            ip = str(x.get("ip") or "")
            if ip.count(".") == 3:
                port = int(x.get("udp_port") or cfg.get("udp_port", 3000))
                for t in ((ip.rsplit(".", 1)[0] + ".255", port), (ip, port)):
                    if t not in targets:
                        targets.append(t)
        return await discover(timeout=timeout, targets=targets)

    async def fill_names(raw):
        """New printers added without a name get the name the printer itself reports."""
        async def one(x):
            ip = str(x.get("ip", "")).strip()
            try:
                port = int(x.get("udp_port") or cfg.get("udp_port", 3000))
                found = await discover(ip, port, timeout=1.5)
            except Exception:  # noqa: BLE001
                return
            name = next((printer_label(d) for d in found if printer_label(d)), "")
            if name:
                x["name"] = name
        await asyncio.gather(*[one(x) for x in raw if not str(x.get("name", "")).strip()])

    def autoname_active():
        """The active printer has no name yet (it shows its IP): take the name it reports."""
        a = active_printer() if not (cfg.get("printers_configured") and not cfg.get("printers")) else None
        n = str((printer().attributes or {}).get("Name") or "").strip()
        if a and n and printer().connected and a["name"] == a["ip"]:
            a["name"] = n
            try:
                save_config({"printers": printer_list()})
            except OSError:
                pass

    @routes.post("/api/printers")
    async def save_printers(request):
        b = await request.json()
        import ipaddress
        raw = b.get("printers") or []
        for x in raw:
            ip = str(x.get("ip", "")).strip()
            try:
                ipaddress.ip_address(ip)
            except ValueError:
                return jerr(f"'{ip}' is not a valid IP address")
            for k in PRINTER_PORTS:
                if x.get(k) not in (None, ""):
                    try:
                        if not 1 <= int(x[k]) <= 65535:
                            raise ValueError
                    except (TypeError, ValueError):
                        return jerr(f"'{x[k]}' is not a valid port number")
        await fill_names(raw)
        cleaned = normalize_printers(raw)
        old = active_printer()
        new_active = next((x for x in cleaned if old and x["id"] == old["id"]), None)
        if not cleaned:
            changed = old is not None
        else:
            changed = old is None or new_active is None or any(new_active[k] != old[k] for k in ("ip", *PRINTER_PORTS))
        if changed and upload_running():
            return jerr("An upload is in progress; wait for it to finish before changing the active printer", 409)
        cfg["printers"] = cleaned
        cfg["printers_configured"] = True
        cfg["active_printer"] = (new_active or cleaned[0])["id"] if cleaned else ""
        try:
            save_config(active_updates())
        except OSError as e:
            return jerr(f"Could not save the config: {e}", 500)
        if changed:
            await leave_printer()
            apply_active()
        return web.json_response({"ok": True, "printers": printer_list(), "active_printer": cfg["active_printer"], "switched": changed})

    @routes.post("/api/printers/select")
    async def select_printer(request):
        b = await request.json()
        pid = str(b.get("id", ""))
        if pid not in [x["id"] for x in printer_list()]:
            return jerr("That printer is not in the list")
        if pid == cfg["active_printer"]:
            return web.json_response({"ok": True, "active_printer": pid, "printers": printer_list()})
        if upload_running():
            return jerr("An upload is in progress; wait for it to finish before switching printers", 409)
        await leave_printer()
        cfg["active_printer"] = pid
        try:
            save_config(active_updates())
        except OSError as e:
            return jerr(f"Could not save the config: {e}", 500)
        apply_active()
        return web.json_response({"ok": True, "active_printer": pid, "printers": printer_list()})

    @routes.get("/api/config")
    async def get_config(request):
        return web.json_response({"printer_ip": cfg["printer_ip"], "printers": printer_list(), "active_printer": cfg["active_printer"], "printer_port": cfg["printer_port"],
                                  "video_port": cfg["video_port"],
                                  "light_enabled": bool(cfg.get("light_enabled")),
                                  "render_folder": cfg.get("render_folder", "")})

    @routes.post("/api/settings")
    async def save_settings(request):
        b = await request.json()
        ip = (b.get("printer_ip") or "").strip()          # optional: only the render folder may be sent
        if ip:
            import ipaddress
            try:
                ipaddress.ip_address(ip)
            except ValueError:
                return jerr("That is not a valid IP address")
        render_folder = b.get("render_folder")
        if render_folder is not None:
            render_folder = str(render_folder).strip().strip('"')
        path = Path(os.environ.get("SATURN_CONFIG") or ROOT / "config.json")
        try:
            data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            if ip:
                data["printer_ip"] = ip
                for x in cfg.get("printers") or []:           # keep the saved entry of the active printer in step
                    if x["id"] == cfg.get("active_printer"):
                        x["ip"] = ip
                if cfg.get("printers"):
                    data["printers"] = cfg["printers"]
            if render_folder is not None:
                data["render_folder"] = render_folder
            path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError as e:
            return jerr(f"Could not save {path}: {e}", 500)
        if ip:
            if ip != printer().ip:
                printer().reconfigure(ip)
            cfg["printer_ip"] = ip
        if render_folder is not None:
            cfg["render_folder"] = render_folder
        return web.json_response({"ok": True, "printer_ip": cfg["printer_ip"], "render_folder": cfg.get("render_folder", ""),
                                  "saved_to": str(path)})

    # ---------------------------------------------------------------- live updates
    @routes.get("/ws")
    async def ws_handler(request):
        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(request)
        t = app.get("exit_task")
        if t:
            t.cancel()
            app["exit_task"] = None
        try:
            await ws.send_json({"type": "state", "state": state_payload()})
            await ws.send_json({"type": "log_init", "entries": list(printer().log)[-300:]})
            app["clients"].add(ws)
            async for _ in ws:
                pass
        finally:
            app["clients"].discard(ws)
            if not app["clients"] and cfg.get("auto_exit", True):
                app["exit_task"] = asyncio.create_task(exit_when_idle())
        return ws

    async def exit_when_idle():
        """Last browser tab closed: wait a few seconds (page reloads reconnect), then quit the app."""
        await asyncio.sleep(float(cfg.get("auto_exit_seconds", 10)))
        while app["clients"] == set():
            if upload_running():
                await asyncio.sleep(2)       # never quit in the middle of an upload (or with files still queued)
                continue
            printer().note("Browser closed - shutting down")
            asyncio.get_running_loop().call_soon(_raise_exit)
            return

    def _raise_exit():
        raise web.GracefulExit()

    async def broadcaster():
        while True:
            try:
                await asyncio.wait_for(app["dirty"].wait(), timeout=5)
            except asyncio.TimeoutError:
                pass
            app["dirty"].clear()
            autoname_active()
            pending, app["logq"][:] = list(app["logq"]), []
            if app["clients"]:
                payloads = [json.dumps({"type": "state", "state": state_payload()})]
                if pending:
                    payloads.append(json.dumps({"type": "log", "entries": pending}))
                for ws in list(app["clients"]):
                    try:
                        for payload in payloads:
                            await ws.send_str(payload)
                    except Exception:  # noqa: BLE001
                        app["clients"].discard(ws)
            await asyncio.sleep(0.25)

    # ---------------------------------------------------------------- print control
    @routes.post("/api/print")
    async def start_print(request):
        b = await request.json()
        if not b.get("filename"):
            return jerr("filename required")
        printer().note(f"UI: start print {b['filename']}")
        return await run_cmd(CMD["START"], {"Filename": b["filename"],
                                            "StartLayer": int(b.get("start_layer") or 0)}, 20)

    @routes.post("/api/pause")
    async def pause(request):
        return await run_cmd(CMD["PAUSE"])

    @routes.post("/api/resume")
    async def resume(request):
        return await run_cmd(CMD["CONTINUE"])

    @routes.post("/api/stop")
    async def stop(request):
        return await run_cmd(CMD["STOP"])

    @routes.post("/api/skip-preheat")
    async def skip_preheat(request):
        return await run_cmd(CMD["SKIP_PREHEAT"])

    @routes.post("/api/stop-feeding")
    async def stop_feeding(request):
        return await run_cmd(CMD["STOP_FEEDING"])

    @routes.post("/api/rename")
    async def rename(request):
        b = await request.json()
        name = (b.get("name") or "").strip()
        if not name:
            return jerr("name required")
        r = await run_cmd(CMD["RENAME"], {"Name": name})
        await printer().request(CMD["ATTRIBUTES"], timeout=5) if printer().connected else None
        return r

    @routes.post("/api/refresh")
    async def refresh(request):
        try:
            await printer().request(CMD["STATUS"])
            await printer().request(CMD["ATTRIBUTES"])
        except PrinterError as e:
            return jerr(str(e), 502)
        return web.json_response({"ok": True})

    @routes.post("/api/timelapse")
    async def timelapse(request):
        b = await request.json()
        return await run_cmd(CMD["TIMELAPSE"], {"Enable": 1 if b.get("enable") else 0})

    @routes.post("/api/light")
    async def light(request):
        b = await request.json()
        on = bool(b.get("on"))
        p = printer()
        if not p.connected:
            return jerr("Not connected to the printer", 502)
        results = await p.light_test(on)
        answered = [r for r in results if r["answered"]]
        ok = any(r["ack"] == 0 for r in answered)
        if ok:
            msg = "OK"
        elif answered:
            msg = f"The printer answered but refused (Ack {answered[0]['ack']})"
        else:
            msg = ("The printer ignored the light command. Over the local network it probably does not "
                   "accept it (the Matrix app sends it through Elegoo's cloud channel).")
        return web.json_response({"ok": ok, "light": p.light, "results": results, "message": msg})

    @routes.post("/api/print-params")
    async def print_params(request):
        b = await request.json()
        params = {k: v for k, v in (b.get("params") or {}).items()
                  if k in ("NormalExposureTime", "BottomExposureTime", "BottomLayer", "TransitionLayer",
                           "LightOffTime", "BottomRestTimeAfterDrop", "NormalRestTimeAfterDrop",
                           "BottomResetTimeBeforeLift", "NormalResetTimeBeforeLift")}
        if not params:
            return jerr("no parameters given")
        if not printer().connected:
            return jerr("Not connected to the printer", 502)
        results = await printer().set_print_params(params)
        ok = all(r.get("answered") and r.get("ack") == 0 for r in results)
        if ok:
            msg = "OK"
        elif not any(r.get("answered") for r in results):
            msg = "The printer ignored the change (it may only accept it from the Matrix app)."
        else:
            msg = "The printer refused at least one change."
        return web.json_response({"ok": ok, "results": results, "message": msg})

    @routes.post("/api/raw")
    async def raw(request):
        """Send exactly the text given, unchanged. Nothing is parsed or rejected."""
        b = await request.json()
        text = b.get("text")
        if not isinstance(text, str) or not text.strip():
            return jerr("nothing to send")
        try:
            r = await printer().send_raw(text, wait=float(b.get("wait") or 6))
        except PrinterError as e:
            return jerr(str(e), 502)
        return web.json_response({"ok": True, **r})

    @routes.get("/api/log")
    async def log(request):
        return web.json_response(list(printer().log))

    @routes.post("/api/note")
    async def note(request):
        b = await request.json()
        printer().note(str(b.get("text", ""))[:500], b.get("level", "info"))
        return web.json_response({"ok": True})

    @routes.post("/api/discover")
    async def do_discover(request):
        found = await scan_printers(2.5)
        return web.json_response({"ok": True, "printers": found})

    # ---------------------------------------------------------------- files
    async def annotate_from_history(items):
        """Fill gaps in the file list from print history: last printed time, thumbnail, slice info."""
        try:
            hist = await printer().history_cached()
        except PrinterError:
            return
        by = {}
        for h in hist:
            n = (h.get("name") or "").rsplit("/", 1)[-1].lower()
            by.setdefault(n, []).append(h)
            by.setdefault(n.rsplit(".", 1)[0], []).append(h)
        for f in items:
            if f["is_dir"]:
                continue
            n = f["name"].lower()
            runs = by.get(n) or by.get(n.rsplit(".", 1)[0])
            if not runs:
                continue
            runs.sort(key=lambda h: h.get("begin") or 0, reverse=True)
            f["last_printed"] = runs[0].get("begin")
            f["print_count"] = len(runs)
            if not f.get("thumbnail"):
                f["thumbnail"] = next((r["thumbnail"] for r in runs if r.get("thumbnail")), None)
            si = next((r["slice_info"] for r in runs if r.get("slice_info")), None)
            if si:
                f["slice_info"] = si
                if f["layer_height"] is None:
                    v = _num(pick(si, ["layerHeight", "layer_height", "thickness"]))
                    f["layer_height"] = round(v, 3) if v is not None else None
                if f["layers"] is None:
                    v = _num(pick(si, ["totalLayer", "layers", "layerCount"]))
                    f["layers"] = int(v) if v is not None else None
                if f["est_seconds"] is None:
                    f["est_seconds"] = _num(pick(si, ["estimatedTime", "printTime", "totalTime"]))

    @routes.get("/api/files")
    async def files(request):
        path = request.query.get("path") or "/local/"
        try:
            items, storage = await printer().list_files(path)
        except PrinterError as e:
            return jerr(str(e), 502)
        items.sort(key=lambda f: (not f["is_dir"], f["name"].lower()))
        info = await printer().file_info()
        for f in items:
            if f["is_dir"]:
                continue
            f["fragment"] = looks_like_fragment(f["name"])
            f["percent_codes"] = has_percent_codes(f["name"])
            e = info.get(f["name"])
            if e and not f["fragment"]:
                for k in ("layer_height", "layers", "est_seconds", "size", "create_time", "thumbnail"):
                    if e.get(k) is not None:
                        f[k] = e[k]
                f["info_source"] = "file_info"
        await annotate_from_history(items)
        return web.json_response({"ok": True, "path": path, "files": items, "storage": storage})

    @routes.get("/api/http/list")
    async def http_list(request):
        """Directory listing of the printer's HTTP file server (read-only), parsed to JSON."""
        p = printer()
        path = normalize_dir(request.query.get("path") or "/")
        url = f"http://{p.ip}:{p.port}{quote(path, safe='/')}"
        try:
            async with p.session.get(url, timeout=aiohttp.ClientTimeout(total=25)) as r:
                if r.status != 200:
                    return jerr(f"The printer's web server answered HTTP {r.status} for {path}", 404 if r.status == 404 else 502)
                page = (await r.read()).decode("utf-8", "replace")
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            return jerr(f"Could not read {path}: {type(e).__name__}: {e}", 502)
        entries = parse_listing(page)
        for e in entries:
            e["path"] = path + e["name"] + ("/" if e["is_dir"] else "")
            e["readable"] = e["is_dir"] or not is_virtual(e["path"])
        return web.json_response({"ok": True, "path": path, "entries": entries,
                                  "virtual": is_virtual(path),
                                  "note": None if entries or "Index of" in page else "Not a directory listing"})

    @routes.post("/api/files/delete")
    async def delete(request):
        b = await request.json()
        files_, folders = b.get("files") or [], b.get("folders") or []
        if not files_ and not folders:
            return jerr("nothing to delete")
        try:
            r = await printer().delete(files_, folders)
        except PrinterError as e:
            return jerr(str(e), 502)
        return web.json_response(r)

    # ---------------------------------------------------------------- history
    @routes.get("/api/history")
    async def history(request):
        try:
            items, total = await printer().history()
        except PrinterError as e:
            return jerr(str(e), 502)
        return web.json_response({"ok": True, "items": items, "total": total})

    # ---------------------------------------------------------------- upload
    # Uploads go through a queue: one file is sent at a time, the rest wait their turn.
    async def run_item(p, item):
        """Send one queued file (and start the print afterwards if asked)."""
        try:
            md5 = item.get("md5")
            if not md5:             # render files are hashed when their turn comes, so queueing is instant
                def hash_file():
                    h = hashlib.md5()
                    with open(item["path"], "rb") as f:
                        for chunk in iter(lambda: f.read(1 << 20), b""):
                            h.update(chunk)
                    return h.hexdigest()
                md5 = await asyncio.get_running_loop().run_in_executor(None, hash_file)
            if not p.connected:
                p.upload = {"id": item["id"], "name": item["name"], "total": item["size"], "sent": 0, "phase": "error",
                            "error": "Not connected to the printer", "started": time.time(), "print_after": False}
                p.on_change()
                return p.upload
            up = await p.upload_file(item["path"], item["name"], item["size"], md5, item.get("print_after", False))
            if up["phase"] == "done" and item.get("print_after"):
                await asyncio.sleep(1.0)
                fname = item["name"]
                for base in ("/local/", "/usb/"):
                    try:
                        items, _ = await p.list_files(base)
                    except PrinterError:
                        continue
                    match = next((f for f in items if f["name"] == fname and not f["is_dir"]), None)
                    if match:
                        ok, ack, msg, _ = await p.command(CMD["START"], {"Filename": match["path"], "StartLayer": 0}, 20)
                        if not ok:
                            up["error"] = f"Uploaded, but print did not start: {msg}"
                        break
                else:
                    up["error"] = "Uploaded, but could not find the file to start printing"
                p.on_change()
            return up
        finally:
            if item.get("tmpdir"):
                shutil.rmtree(item["tmpdir"], ignore_errors=True)

    async def queue_worker(p):
        try:
            while p.queue:
                item = p.queue.pop(0)
                await p.refresh_space()          # baseline free space for this file (printer is idle between files)
                t = asyncio.create_task(run_item(p, item))
                app["upload_task"] = t
                p.on_change()
                await asyncio.wait({t})          # a cancelled upload just moves on to the next file
                up = None if t.cancelled() or t.exception() else t.result()
                if up and up.get("phase") == "error" and p.queue:
                    dropped = len(p.queue)       # something is wrong (offline, full...): do not hammer the printer
                    for q in p.queue:
                        if q.get("tmpdir"):
                            shutil.rmtree(q["tmpdir"], ignore_errors=True)
                    p.queue.clear()
                    p.note(f"Upload failed: {dropped} queued file(s) removed", "error")
                p.on_change()
        finally:
            app["queue_task"] = None
            p.on_change()

    def enqueue(p, item):
        item["id"] = uuid.uuid4().hex
        p.queue.append(item)
        t = app.get("queue_task")
        if not t or t.done():
            app["queue_task"] = asyncio.create_task(queue_worker(p))
        p.on_change()

    @routes.post("/api/upload/check")
    async def upload_check(request):
        """How much room is there for these sizes (on top of what is already uploading/queued)?"""
        p = printer()
        b = await request.json()
        if not p.connected:
            return jerr("Not connected to the printer", 502)
        if not upload_running():                  # idle: the printer's own number is the truth
            await p.refresh_space()
        sp = p.space_info()
        add = sum(int(x) for x in b.get("sizes") or [])
        out = {"ok": True, "known": sp["free"] is not None, "free": sp["free"], "add": add}
        if sp["free"] is not None:
            out["after_now"] = sp["after"]                      # what is left once everything already in line is done
            out["after"] = sp["after"] - add                    # ...and with these files too
            out["over"] = out["after"] < 0
        return web.json_response(out)

    @routes.post("/api/upload")
    async def upload(request):
        p = printer()
        if not p.connected:
            return jerr("Not connected to the printer", 502)
        tmpdir = tempfile.mkdtemp(prefix="saturn-up-")
        print_after, fname, tmp, md5, size = False, None, None, None, 0
        try:
            reader = await request.multipart()
            async for part in reader:
                if part.name == "print_after":
                    print_after = (await part.text()).strip() in ("1", "true", "on")
                elif part.name == "file":
                    fname = os.path.basename(part.filename or "upload.ctb")
                    tmp = os.path.join(tmpdir, "payload")
                    h = hashlib.md5()
                    with open(tmp, "wb") as out:
                        while True:
                            chunk = await part.read_chunk(1 << 20)
                            if not chunk:
                                break
                            h.update(chunk)
                            size += len(chunk)
                            out.write(chunk)
                    md5 = h.hexdigest()
        except Exception as e:  # noqa: BLE001
            shutil.rmtree(tmpdir, ignore_errors=True)
            return jerr(f"Upload receive failed: {e}", 400)
        if not tmp or size == 0:
            shutil.rmtree(tmpdir, ignore_errors=True)
            return jerr("No file received")
        enqueue(p, {"name": fname, "size": size, "md5": md5, "path": tmp, "tmpdir": tmpdir, "print_after": print_after})
        return web.json_response({"ok": True, "name": fname, "size": size, "md5": md5}, status=202)

    @routes.post("/api/upload/cancel")
    async def upload_cancel(request):
        t = app.get("upload_task")
        if t and not t.done():
            t.cancel()
            return web.json_response({"ok": True})
        return jerr("No upload running", 409)

    @routes.post("/api/upload/queue/remove")
    async def queue_remove(request):
        p = printer()
        b = await request.json()
        ids = {b["id"]} if b.get("id") else {q["id"] for q in p.queue}       # no id = clear the whole queue
        gone = [q for q in p.queue if q["id"] in ids]
        p.queue[:] = [q for q in p.queue if q["id"] not in ids]
        for q in gone:
            if q.get("tmpdir"):
                shutil.rmtree(q["tmpdir"], ignore_errors=True)
        p.on_change()
        return web.json_response({"ok": True, "removed": len(gone)})

    # ---------------------------------------------------------------- render upload (local .goo folder)
    def render_root():
        return Path(cfg.get("render_folder") or "").expanduser()

    def render_resolve(rel):
        """Map a client-supplied relative path onto the render folder; None if it escapes it."""
        if not cfg.get("render_folder"):
            return Path(), None
        root = render_root().resolve()
        parts = [x for x in str(rel or "").replace("\\", "/").split("/") if x]
        if any(x == ".." for x in parts):
            return root, None
        target = root.joinpath(*parts).resolve()
        if target != root and root not in target.parents:
            return root, None
        return root, target

    @routes.get("/api/render/list")
    async def render_list(request):
        rel = request.query.get("path", "")
        if not cfg.get("render_folder"):
            return jerr("No render folder set yet. Use Change folder to choose one.", 400, no_folder=True)
        root, target = render_resolve(rel)
        if target is None:
            return jerr("That path is outside the render folder", 403)
        if not root.is_dir():
            return jerr(f"Render folder not found: {root}. Set it in the app settings (gear icon).", 404)
        if not target.is_dir():
            return jerr("Folder not found", 404)
        entries = []
        try:
            for e in os.scandir(target):
                try:
                    is_dir = e.is_dir()
                    if not is_dir and not (e.is_file() and e.name.lower().endswith(".goo")):
                        continue
                    st = e.stat()
                except OSError:
                    continue
                full = Path(e.path).resolve()
                if root not in full.parents:     # symlink/junction pointing outside the folder
                    continue
                entries.append({"name": e.name, "path": full.relative_to(root).as_posix(), "is_dir": is_dir,
                                "size": None if is_dir else st.st_size, "mtime": int(st.st_mtime)})
        except OSError as e:
            return jerr(f"Could not read the folder: {e}", 500)
        here = "" if target == root else target.relative_to(root).as_posix()
        return web.json_response({"ok": True, "root": str(root), "path": here, "entries": entries})

    @routes.get("/api/render/printer-files")
    async def render_printer_files(request):
        """Name + size of every model file on the printer (its HTTP print-files folder and the SDCP /local/ and /usb/ lists)."""
        p = printer()
        if not p.connected:
            return jerr("Not connected to the printer", 502)
        found, errors = {}, []

        def add(name, size):
            found[(unquote(name), size)] = True

        async def walk_http(path, depth):
            url = f"http://{p.ip}:{p.port}{quote(path, safe='/')}"
            async with p.session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status != 200:
                    raise PrinterError(f"HTTP {r.status} for {path}")
                page = (await r.read()).decode("utf-8", "replace")
            for e in parse_listing(page):
                if e["is_dir"]:
                    if depth < 3:
                        await walk_http(path + e["name"] + "/", depth + 1)
                elif e.get("size") is not None:
                    add(e["name"], int(e["size"]))

        async def walk_sdcp(path, depth):
            items, _ = await p.list_files(path)
            for f in items:
                if f["is_dir"]:
                    if depth < 3:
                        await walk_sdcp(f["path"].rstrip("/") + "/", depth + 1)
                elif f.get("size") is not None:
                    add(f["name"], int(f["size"]))

        for walker, base in ((walk_http, "/media/mmcblk0p3/"), (walk_sdcp, "/local/"), (walk_sdcp, "/usb/")):
            try:
                await walker(base, 0)
            except (PrinterError, aiohttp.ClientError, asyncio.TimeoutError) as e:
                errors.append(f"{base}: {e}")
        if not found and errors:
            return jerr("Could not read the printer's files: " + "; ".join(errors), 502)
        return web.json_response({"ok": True, "files": [{"name": n, "size": s} for n, s in found]})

    @routes.post("/api/render/upload")
    async def render_upload(request):
        p = printer()
        b = await request.json()
        if not cfg.get("render_folder"):
            return jerr("No render folder set yet. Use Change folder to choose one.", 400, no_folder=True)
        root, target = render_resolve(b.get("path"))
        if target is None:
            return jerr("That path is outside the render folder", 403)
        if not target.is_file() or target.suffix.lower() != ".goo":
            return jerr("File not found (only .goo files can be uploaded from here)", 404)
        if not p.connected:
            return jerr("Not connected to the printer", 502)
        size = target.stat().st_size
        if size == 0:
            return jerr("That file is empty")
        enqueue(p, {"name": target.name, "size": size, "path": str(target), "print_after": False})
        return web.json_response({"ok": True, "name": target.name, "size": size}, status=202)

    # ---------------------------------------------------------------- proxying printer-hosted resources
    IMG_EXT = (".bmp", ".png", ".jpg", ".jpeg", ".gif")

    def printer_urls(u):
        """Candidate absolute URLs on the printer for u, or [] if u points anywhere else.
        A path starting with '/' is a real (decoded) file path, so it is percent-encoded here; if that
        404s and the path contains '%', the raw text is tried too (names with literal %20 in them)."""
        p = printer()
        if u.startswith("/"):
            base = f"http://{p.ip}:{p.port}"
            out = [base + quote(u, safe="/")]
            if "%" in u:
                out.append(base + u)
            return out
        pu = urlparse(u)
        if pu.scheme in ("http", "https") and pu.hostname == p.ip:
            return [u]
        return []

    def cache_put(key, body, ctype):
        c = app["imgcache"]
        c[key] = (body, ctype)
        c.move_to_end(key)
        while len(c) > 300:
            c.popitem(last=False)

    @routes.get("/api/fetch")
    async def fetch(request):
        cands = printer_urls(request.query.get("u", ""))
        if not cands:
            return jerr("Only printer-hosted URLs may be fetched", 403)
        path = unquote(urlparse(cands[0]).path)
        if is_virtual(path):
            return jerr("Reading virtual system files (/proc, /sys, /dev) is blocked: they can hang the printer", 403)
        download = bool(request.query.get("download"))
        is_img = path.lower().endswith(IMG_EXT) and not download
        if is_img and "Range" not in request.headers:
            hit = app["imgcache"].get(cands[0])
            if hit:
                return web.Response(body=hit[0], content_type=hit[1], headers={"Cache-Control": "max-age=3600"})
        headers = {}
        if "Range" in request.headers:
            headers["Range"] = request.headers["Range"]
        last_err = None
        for url in cands:
            try:
                async with printer().session.get(
                        url, headers=headers,
                        timeout=aiohttp.ClientTimeout(total=None, sock_connect=8, sock_read=60)) as r:
                    if r.status == 404 and url != cands[-1]:
                        continue
                    ctype = r.headers.get("Content-Type", "application/octet-stream").split(";")[0].strip()
                    if path.lower().endswith(".bmp") and r.status == 200:
                        ctype = "image/bmp"          # the printer serves BMP thumbnails as octet-stream
                    if is_img and r.status == 200:
                        body = await r.read()
                        cache_put(cands[0], body, ctype)
                        return web.Response(body=body, content_type=ctype, headers={"Cache-Control": "max-age=3600"})
                    resp = web.StreamResponse(status=r.status)
                    resp.headers["Content-Type"] = ctype
                    for h in ("Content-Length", "Content-Range", "Accept-Ranges"):
                        if h in r.headers:
                            resp.headers[h] = r.headers[h]
                    resp.headers["Cache-Control"] = "max-age=300"
                    if download:
                        base = os.path.basename(path) or "download"
                        ascii_name = base.encode("ascii", "replace").decode().replace('"', "'").replace("?", "_")
                        resp.headers["Content-Disposition"] = f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(base)}"
                    await resp.prepare(request)
                    async for chunk in r.content.iter_chunked(64 * 1024):
                        await resp.write(chunk)
                    return resp
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                last_err = e
        return jerr(f"Printer fetch failed: {last_err}", 502)

    # ---------------------------------------------------------------- camera
    # The printer allows very few simultaneous camera clients, so one upstream reader
    # fans frames out to every browser tab / snapshot request.
    hub = {"task": None, "frame": None, "seq": 0, "cond": None, "viewers": 0, "error": None, "off": None}

    async def upstream_frames(url):
        if url.startswith("rtsp://"):
            ff = ffmpeg_path()
            if not ff:
                raise PrinterError("Printer offers an RTSP stream (%s) but no ffmpeg was found; "
                                   "run: .venv\\Scripts\\pip install imageio-ffmpeg" % url)
            # No "nobuffer/low_delay" and no frame-rate resampling: those smear motion into blocks.
            attempts = [("tcp", ["-fflags", "+genpts", "-max_delay", "1000000", "-probesize", "2000000", "-analyzeduration", "2000000"]),
                        ("tcp", []), ("udp", [])]
            errors = []
            for transport, extra in attempts:
                proc = await asyncio.create_subprocess_exec(
                    ff, "-hide_banner", "-loglevel", "warning", *extra,
                    "-rtsp_transport", transport, "-i", url,
                    "-an", "-q:v", str(printer().cfg.get("camera_quality", 2)), "-f", "mpjpeg", "-",
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                got = False
                try:
                    while True:
                        chunk = await asyncio.wait_for(proc.stdout.read(65536), 20)
                        if not chunk:
                            break
                        got = True
                        yield chunk
                except asyncio.TimeoutError:
                    errors.append(f"{transport}: no data for 20 s")
                finally:
                    await asyncio.shield(stop_ffmpeg(proc))      # graceful: sends RTSP TEARDOWN
                err = (await proc.stderr.read()).decode("utf-8", "replace").strip()
                msg = f"ffmpeg ({transport}) ended: " + (err[-600:] or "no error output")
                printer().note(msg, "error")
                if got:                       # it worked for a while; let the caller reconnect
                    raise PrinterError("Camera stream ended")
                errors.append(msg)
            raise PrinterError(" | ".join(errors)[-900:])
        else:
            async with printer().session.get(
                    url, timeout=aiohttp.ClientTimeout(total=None, sock_connect=8, sock_read=15)) as r:
                if r.status != 200:
                    raise PrinterError(f"Camera stream returned HTTP {r.status}")
                async for chunk in r.content.iter_any():
                    yield chunk

    async def hub_run():
        buf = b""
        try:
            url = await video_source()
            async for chunk in upstream_frames(url):
                buf += chunk
                while True:
                    st = buf.find(b"\xff\xd8")
                    if st < 0:
                        buf = buf[-1:]
                        break
                    en = buf.find(b"\xff\xd9", st + 2)
                    if en < 0:
                        buf = buf[st:]
                        break
                    hub["frame"] = buf[st:en + 2]
                    buf = buf[en + 2:]
                    hub["seq"] += 1
                    hub["error"] = None
                    async with hub["cond"]:
                        hub["cond"].notify_all()
        except asyncio.CancelledError:
            raise
        except (PrinterError, aiohttp.ClientError, asyncio.TimeoutError) as e:
            hub["error"] = str(e) or type(e).__name__
        finally:
            printer().video_enabled = False
            try:      # free the printer's single camera slot so the next try is not refused with Ack 1
                await asyncio.shield(printer().enable_video(False))
            except Exception:  # noqa: BLE001
                pass
            hub["task"] = None
            hub["frame"] = None
            async with hub["cond"]:
                hub["cond"].notify_all()

    async def video_source():
        p = printer()
        ok, msg, url = await p.enable_video(True)
        if not ok and "maximum number of clients" in msg:
            # Release anything of ours that is still registered, give the printer a moment, retry once.
            p.note("Camera slot busy: releasing and retrying once", "error")
            try:
                await p.enable_video(False)
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(2.0)
            ok, msg, url = await p.enable_video(True)
        if not ok:
            try:
                await p.request(CMD["ATTRIBUTES"], timeout=4, quiet=True)
            except PrinterError:
                pass
            cs = p.camera_slots()
            if cs and cs["used"] >= cs["max"]:
                msg = (f"The printer says {cs['used']} of {cs['max']} camera slots are in use. "
                       "Close the live camera in the Matrix app / Lychee (or press Release camera) and try again.")
            raise PrinterError(msg)
        return url or p.default_video_url()

    async def hub_acquire():
        if hub["cond"] is None:
            hub["cond"] = asyncio.Condition()
        hub["viewers"] += 1
        if hub["off"]:
            hub["off"].cancel()
            hub["off"] = None
        if hub["task"] is None:
            hub["error"] = None
            hub["task"] = asyncio.create_task(hub_run())

    async def hub_release():
        hub["viewers"] -= 1
        if hub["viewers"] <= 0:
            async def later():
                await asyncio.sleep(15)
                if hub["viewers"] <= 0 and hub["task"]:
                    hub["task"].cancel()
                    try:
                        await printer().enable_video(False)
                    except Exception:  # noqa: BLE001
                        pass
            hub["off"] = asyncio.create_task(later())

    async def next_frame(after_seq, timeout=15):
        async with hub["cond"]:
            try:
                await asyncio.wait_for(hub["cond"].wait_for(
                    lambda: (hub["frame"] is not None and hub["seq"] != after_seq) or hub["error"] or hub["task"] is None), timeout)
            except asyncio.TimeoutError:
                hub["error"] = hub["error"] or "Timed out waiting for a camera frame"
        return hub["frame"] if hub["seq"] != after_seq and hub["frame"] else None

    @routes.get("/video")
    async def video(request):
        await hub_acquire()
        try:
            frame = await next_frame(-1)
            if frame is None:
                return web.Response(status=503, text=hub["error"] or "No camera frames")
            resp = web.StreamResponse()
            resp.headers["Content-Type"] = "multipart/x-mixed-replace; boundary=frame"
            resp.headers["Cache-Control"] = "no-store"
            await resp.prepare(request)
            seq = -1
            while frame is not None:
                seq = hub["seq"]
                await resp.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(frame) + frame + b"\r\n")
                frame = await next_frame(seq)
            return resp
        except ConnectionError:       # viewer closed the tab
            return web.Response()
        finally:
            await hub_release()

    @routes.post("/api/camera/release")
    async def camera_release(request):
        """Drop our stream (stops ffmpeg politely) and tell the printer we are done with the camera."""
        await drop_camera()
        try:
            await printer().enable_video(False)
            await printer().request(CMD["ATTRIBUTES"], timeout=4, quiet=True)
        except PrinterError as e:
            return jerr(str(e), 502)
        return web.json_response({"ok": True, "camera": printer().camera_slots()})

    @routes.get("/snapshot.jpg")
    async def snapshot(request):
        await hub_acquire()
        try:
            frame = hub["frame"] or await next_frame(-1)
            if frame is None:
                return web.Response(status=503, text=hub["error"] or "No camera frames")
            return web.Response(body=frame, content_type="image/jpeg", headers={"Cache-Control": "no-store"})
        finally:
            await hub_release()

    # ---------------------------------------------------------------- lifecycle
    def on_log(entry):
        app["logq"].append(entry)
        if app["dirty"] is not None:
            app["dirty"].set()

    def on_change():
        d = app["dirty"]
        if d is not None:
            d.set()

    async def on_startup(app_):
        LOOP["loop"] = asyncio.get_running_loop()
        app["dirty"] = asyncio.Event()
        app["printer"] = PrinterClient(cfg, on_change, on_log)
        await app["printer"].start()
        app["bcast"] = asyncio.create_task(broadcaster())
        if not cfg.get("printers"):
            scan["on"] = True
            app["scan_task"] = asyncio.create_task(startup_scan())

    async def startup_scan():
        """No printers saved yet: scan the network and add every printer that answers."""
        try:
            await asyncio.sleep(0.5)
            found = await scan_printers(2.5)
            have = {x["ip"] for x in cfg.get("printers") or []}
            fresh = []
            for d in found:
                ip = str(d.get("MainboardIP") or "").strip()
                if ip and ip not in have:
                    have.add(ip)
                    fresh.append({"name": printer_label(d), "ip": ip})
            if not fresh:
                return
            ports = {k: cfg[k] for k in PRINTER_PORTS if k in cfg}
            entries = normalize_printers(fresh, ports)
            had_active = bool(cfg.get("active_printer")) and any(x["id"] == cfg["active_printer"] for x in cfg.get("printers") or [])
            cfg["printers"] = list(cfg.get("printers") or []) + entries
            cfg["printers_configured"] = True
            if not had_active:
                cfg["active_printer"] = entries[0]["id"]
            printer().note("Scan found " + ", ".join(f"{e['name']} @ {e['ip']}" for e in entries))
            try:
                save_config(active_updates())
            except OSError:
                pass
            if not had_active:
                apply_active()
            on_change()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            printer().note(f"Startup scan failed: {e}", "error")
        finally:
            scan["on"] = False

    async def on_cleanup(app_):
        app["bcast"].cancel()
        if app.get("scan_task"):
            app["scan_task"].cancel()
        t = hub["task"]
        if t:
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        try:      # leave no camera slot behind on the printer
            await asyncio.wait_for(app["printer"].enable_video(False), 3)
        except Exception:  # noqa: BLE001
            pass
        await app["printer"].stop()
        CLEANED.set()

    app.router.add_routes(routes)
    app.router.add_static("/static", STATIC, show_index=False)

    async def no_cache_static(request, response):     # always re-check the page scripts, so updates show on a normal refresh
        if request.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
    app.on_response_prepare.append(no_cache_static)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


def main():
    cfg = load_config()
    host, port = cfg["web_host"], cfg["web_port"]
    shown = "localhost" if host in ("127.0.0.1", "0.0.0.0") else host
    print(f"Saturn control for printer {cfg['printer_ip']}:{cfg['printer_port']}")
    print(f"Open  http://{shown}:{port}   (Ctrl+C to quit)")
    if cfg.get("open_browser", True):
        import threading
        import webbrowser
        threading.Timer(1.5, webbrowser.open, args=[f"http://localhost:{port}"]).start()
    install_console_close_handler()
    web.run_app(create_app(cfg), host=host, port=port, print=None)


if __name__ == "__main__":
    main()
