"""Web UI + JSON API in front of the printer's SDCP interface."""
import asyncio
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from collections import OrderedDict
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

import aiohttp
from aiohttp import web

from .config import ROOT, load_config
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
        return web.FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    @routes.get("/api/state")
    async def state(request):
        return web.json_response(printer().public_state())

    @routes.get("/api/config")
    async def get_config(request):
        return web.json_response({"printer_ip": cfg["printer_ip"], "printer_port": cfg["printer_port"],
                                  "video_port": cfg["video_port"],
                                  "light_enabled": bool(cfg.get("light_enabled")),
                                  "render_folder": cfg.get("render_folder", "")})

    @routes.post("/api/settings")
    async def save_settings(request):
        b = await request.json()
        ip = (b.get("printer_ip") or "").strip() or cfg["printer_ip"]
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
            data["printer_ip"] = ip
            if render_folder is not None:
                data["render_folder"] = render_folder
            path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError as e:
            return jerr(f"Could not save {path}: {e}", 500)
        if ip != printer().ip:
            printer().reconfigure(ip)
        cfg["printer_ip"] = ip
        if render_folder is not None:
            cfg["render_folder"] = render_folder
        return web.json_response({"ok": True, "printer_ip": ip, "render_folder": cfg.get("render_folder", ""),
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
            await ws.send_json({"type": "state", "state": printer().public_state()})
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
            up = printer().upload
            if up and up.get("phase") in ("sending", "verifying"):
                await asyncio.sleep(2)       # never quit in the middle of an upload
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
            pending, app["logq"][:] = list(app["logq"]), []
            if app["clients"]:
                payloads = [json.dumps({"type": "state", "state": printer().public_state()})]
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
        found = await discover(timeout=2.0)
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
    @routes.post("/api/upload")
    async def upload(request):
        p = printer()
        if p.upload and p.upload.get("phase") in ("sending", "verifying"):
            return jerr("An upload is already in progress", 409)
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

        async def job():
            try:
                up = await p.upload_file(tmp, fname, size, md5, print_after)
                if up["phase"] == "done" and print_after:
                    await asyncio.sleep(1.0)
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
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)

        app["upload_task"] = asyncio.create_task(job())
        return web.json_response({"ok": True, "name": fname, "size": size, "md5": md5}, status=202)

    @routes.post("/api/upload/cancel")
    async def upload_cancel(request):
        t = app.get("upload_task")
        if t and not t.done():
            t.cancel()
            return web.json_response({"ok": True})
        return jerr("No upload running", 409)

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
        if p.upload and p.upload.get("phase") in ("sending", "verifying"):
            return jerr("An upload is already in progress", 409)
        if not p.connected:
            return jerr("Not connected to the printer", 502)
        size = target.stat().st_size
        if size == 0:
            return jerr("That file is empty")

        def hash_file():
            h = hashlib.md5()
            with open(target, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            return h.hexdigest()
        md5 = await asyncio.get_running_loop().run_in_executor(None, hash_file)

        async def job():
            await p.upload_file(str(target), target.name, size, md5, False)
        app["upload_task"] = asyncio.create_task(job())
        return web.json_response({"ok": True, "name": target.name, "size": size, "md5": md5}, status=202)

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
        app["dirty"] = asyncio.Event()
        app["printer"] = PrinterClient(cfg, on_change, on_log)
        await app["printer"].start()
        app["bcast"] = asyncio.create_task(broadcaster())

    async def on_cleanup(app_):
        app["bcast"].cancel()
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
    web.run_app(create_app(cfg), host=host, port=port, print=None)


if __name__ == "__main__":
    main()
