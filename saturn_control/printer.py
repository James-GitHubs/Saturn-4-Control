"""SDCP v3.0.0 client for Elegoo resin printers (Saturn 4 Ultra).

Protocol: JSON over WebSocket ws://<ip>:3030/websocket, file upload via
HTTP POST http://<ip>:3030/uploadFile/upload (1 MB chunks), camera via the URL
returned from Cmd 386 (MJPEG over HTTP, normally http://<ip>:3031/video).
"""
import asyncio
import hashlib
import json
import socket
import time
import uuid
from collections import deque

import aiohttp

from .httpfs import FILE_INFO_PATH, parse_file_info

CMD = {
    "STATUS": 0, "ATTRIBUTES": 1, "START": 128, "PAUSE": 129, "STOP": 130,
    "CONTINUE": 131, "STOP_FEEDING": 132, "SKIP_PREHEAT": 133, "RENAME": 192,
    "ABORT_UPLOAD": 255, "FILES": 258, "DELETE": 259, "HISTORY": 320,
    "HISTORY_DETAIL": 321, "VIDEO": 386, "TIMELAPSE": 387,
    # Seen in the Matrix app's cloud (RTM) traffic; whether the local websocket accepts them is untested:
    "CHANGE_PARAMS": 135, "LIGHT": 151,
}

ACK_TEXT = {
    128: {1: "Printer is busy", 2: "File not found", 3: "MD5 check failed",
          4: "File read failed", 5: "Resolution mismatch",
          6: "Unrecognised file format", 7: "Machine model mismatch"},
    255: {1: "Printer is not transferring a file", 2: "Already in verification",
          3: "File not found"},
    386: {1: "Camera already streaming to the maximum number of clients "
             "(close Lychee / the phone app camera and retry)",
          2: "Printer reports no camera", 3: "Unknown camera error"},
    387: {1: "Unknown error"},
}

MACHINE_STATUS = {0: "Idle", 1: "Printing", 2: "File transfer", 3: "Exposure test",
                  4: "Device self-check"}
PRINT_STATUS = {0: "Idle", 1: "Homing", 2: "Lowering", 3: "Exposing", 4: "Lifting",
                5: "Pausing", 6: "Paused", 7: "Stopping", 8: "Stopped",
                9: "Complete", 10: "Checking file"}
PRINT_ERRORS = {
    0: "None", 1: "File MD5 check failed", 2: "File read failed",
    3: "Resolution mismatch", 4: "Format mismatch", 5: "Machine model mismatch",
}
STOP_REASONS = {
    0: "Normal", 1: "Over-temperature", 2: "Strain gauge calibration failed",
    3: "Resin level low", 4: "Model needs more resin than the vat holds",
    5: "No resin detected", 6: "Foreign object detected", 7: "Auto-leveling failed",
    8: "Model detached", 9: "Strain gauge not connected", 10: "LCD connection abnormal",
    11: "Release film usage limit reached", 12: "USB drive removed",
    13: "X-axis motor fault", 14: "Z-axis motor fault", 15: "Resin level too high",
    16: "Resin level too low", 17: "Homing failed", 18: "Model on platform",
    19: "Print exception", 20: "Motor movement abnormal", 21: "No model detected",
    22: "Model warping detected", 24: "Error file", 25: "Camera error",
    26: "Network error", 27: "Server connection failed", 28: "Printer not bound to app",
    29: "Check auto resin feeder", 30: "Container resin low", 31: "Feeder disconnected",
    32: "Feed timeout", 33: "Vat temp sensor offline", 34: "Vat over-temperature",
}
TASK_STATUS = {0: "Other", 1: "Completed", 2: "Error", 3: "Stopped"}

CHUNK = 1024 * 1024


def _norm(k):
    return "".join(c for c in str(k).lower() if c.isalnum())


def pick(obj, names, depth=2):
    """Case/underscore-insensitive lookup of the first matching key, searching nested dicts."""
    if not isinstance(obj, dict):
        return None
    wanted = [_norm(n) for n in names]
    flat = {_norm(k): v for k, v in obj.items()}
    for w in wanted:
        if w in flat and flat[w] not in (None, ""):
            return flat[w]
    if depth > 0:
        for v in obj.values():
            if isinstance(v, dict):
                r = pick(v, names, depth - 1)
                if r is not None:
                    return r
    return None


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def normalize_file(entry):
    """Turn a raw FileList entry into UI fields. The official spec only guarantees
    name/usedSize/totalSize/storageType/type; everything else is picked up
    opportunistically if the firmware sends it."""
    name = entry.get("name") or entry.get("Name") or ""
    t = entry.get("type")
    is_dir = t == 0 or t == "0"
    out = {
        "path": name,
        "name": name.rstrip("/").split("/")[-1] or name,
        "is_dir": is_dir,
        "storage": entry.get("storageType"),
        "raw": entry,
    }
    if is_dir:
        return out
    lh = _num(pick(entry, ["layerHeight", "layer_height", "LayerThickness", "thickness", "layerH"]))
    lh = round(lh, 3) if lh is not None else None
    layers = _num(pick(entry, ["totalLayer", "TotalLayers", "layers", "layerCount", "LayerNum", "layer_num"]))
    secs = None
    ms = _num(pick(entry, ["totalTicks", "totalTimeMs"]))
    if ms is not None:
        secs = ms / 1000.0
    else:
        s = _num(pick(entry, ["estimatedTime", "estimateTime", "printTime", "print_time",
                              "totalTime", "estimated_print_time", "time", "ETA"]))
        if s is not None:
            secs = s
    size = _num(pick(entry, ["fileSize", "file_size", "size"]))
    thumb = pick(entry, ["thumbnail", "thumbnailUrl", "thumb", "preview", "previewUrl", "image", "cover"])
    out.update({
        "layer_height": lh,
        "layers": int(layers) if layers is not None else None,
        "est_seconds": secs,
        "size": size,
        "thumbnail": thumb if isinstance(thumb, str) else None,
        "create_time": pick(entry, ["createTime", "create_time", "modified", "time_created"]),
    })
    return out


def normalize_history(d):
    thumb = d.get("Thumbnail")
    begin, end = d.get("BeginTime"), d.get("EndTime")
    return {
        "task_id": d.get("TaskId"),
        "name": d.get("TaskName"),
        "thumbnail": thumb if isinstance(thumb, str) and thumb else None,
        "begin": begin, "end": end,
        "duration": (end - begin) if isinstance(begin, (int, float)) and isinstance(end, (int, float)) and end >= begin else None,
        "status": d.get("TaskStatus"),
        "status_text": TASK_STATUS.get(d.get("TaskStatus"), "Unknown"),
        "layers_printed": d.get("AlreadyPrintLayer"),
        "volume_ml": d.get("CurrentLayerTalVolume"),
        "md5": d.get("MD5"),
        "timelapse_status": d.get("TimeLapseVideoStatus"),
        "timelapse_url": d.get("TimeLapseVideoUrl") or None,
        "reason": d.get("ErrorStatusReason"),
        "reason_text": STOP_REASONS.get(d.get("ErrorStatusReason"), None),
        "slice_info": d.get("SliceInformation") or None,
    }


class PrinterError(Exception):
    pass


class PrinterClient:
    def __init__(self, cfg, on_change, on_log=None):
        self.cfg = cfg
        self.on_change = on_change
        self.on_log = on_log
        self._seq = 0
        self._seen_initial = set()
        self._hist_cache = (0, [])
        self.ip = cfg["printer_ip"]
        self.port = cfg["printer_port"]
        self.mainboard_id = cfg.get("mainboard_id") or ""
        self.brand_id = uuid.uuid4().hex
        self.ws = None
        self.session = None
        self.connected = False
        self.attributes = {}
        self.status = {}
        self.notices = deque(maxlen=50)
        self.errors = deque(maxlen=50)
        self.log = deque(maxlen=1000)
        self.pending = {}
        self._quiet = set()         # RequestIDs of keep-alive polls: logged as 'debug', not as console noise
        self.reconnects = 0
        self.light_variant = None   # name of the light message variant the printer answered, if any
        self._fi = (0, {})
        self.upload = None          # dict describing the current/last upload
        self.light = None           # optimistic light state (True/False/None)
        self.video_url = None
        self.video_enabled = False
        self.last_message = 0
        self.last_error = ""
        self._tasks = []

    # ------------------------------------------------------------ lifecycle
    async def start(self):
        self.session = aiohttp.ClientSession()
        self._tasks.append(asyncio.create_task(self._run()))

    async def stop(self):
        for t in self._tasks:
            t.cancel()
        if self.ws and not self.ws.closed:
            await self.ws.close()
        if self.session:
            await self.session.close()

    def _log(self, direction, payload):
        """Record a protocol message (dir 'out' = sent, 'in' = received) as a readable console line."""
        self._emit(direction, *_summarise(direction, payload), msg=payload)

    def note(self, text, level="info"):
        """Record an app-level event (connect, upload, errors...)."""
        self._emit("app", level, text)

    def _emit(self, direction, kind, text, msg=None):
        self._seq += 1
        e = {"seq": self._seq, "t": time.time(), "dir": direction, "kind": kind, "text": text, "msg": msg}
        self.log.append(e)
        if self.on_log:
            self.on_log(e)

    # ------------------------------------------------------------ connection
    async def _run(self):
        while True:
            try:
                await self._connect_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.last_error = f"{type(e).__name__}: {e}"
            if self.connected or self.last_error != getattr(self, "_last_logged_err", None):
                self.note("Printer connection lost" if self.connected else f"Cannot connect: {self.last_error}", "error")
                self._last_logged_err = self.last_error
            self.connected = False
            self.ws = None
            for fut in self.pending.values():
                if not fut.done():
                    fut.set_exception(PrinterError("Connection to printer lost"))
                    fut.exception()  # mark retrieved, nobody may be awaiting it
            self.pending.clear()
            self.on_change()
            await asyncio.sleep(3)

    async def _connect_once(self):
        url = f"ws://{self.ip}:{self.port}/websocket"
        # No receive_timeout: the printer pushes nothing while idle, so a read timeout dropped the
        # connection every ~45 s. Liveness is checked by _keepalive() instead (Cmd 0 must be answered).
        async with self.session.ws_connect(url, heartbeat=None, max_msg_size=0, timeout=8) as ws:
            self.ws = ws
            self.connected = True
            self.reconnects += 1
            self.last_error = ""
            self._seen_initial.clear()
            self.status, self.attributes = {}, {}
            self.note(f"Connected to printer at {url}")
            self.on_change()
            hb = asyncio.create_task(self._keepalive(ws))
            init = asyncio.create_task(self._post_connect())
            try:
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        self._handle_text(msg.data)
                    elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        break
            finally:
                hb.cancel()
                init.cancel()

    async def _keepalive(self, ws):
        """Poll the printer every few seconds (status; attributes every 5th time so the camera-client
        counters stay fresh). Two unanswered polls in a row = dead connection -> reconnect."""
        interval = float(self.cfg.get("keepalive_seconds", 12))
        timeout = float(self.cfg.get("keepalive_timeout", 8))
        misses = beats = 0
        while True:
            await asyncio.sleep(interval)
            beats += 1
            try:
                await self.request(CMD["ATTRIBUTES"] if beats % 5 == 0 else CMD["STATUS"],
                                   timeout=timeout, quiet=True)
                misses = 0
            except PrinterError as e:
                if ws.closed:
                    return
                misses += 1
                self.note(f"Keep-alive: no answer ({misses}/2): {e}", "debug")
                if misses >= 2:
                    self.note("Printer stopped answering - reconnecting", "error")
                    await ws.close()
                    return

    async def _post_connect(self):
        if not self.mainboard_id:
            await self._discover_unicast()
        for _ in range(30):                       # wait up to 3 s for unsolicited push
            if self.mainboard_id:
                break
            await asyncio.sleep(0.1)
        try:
            await self.request(CMD["ATTRIBUTES"], timeout=8)
            await self.request(CMD["STATUS"], timeout=8)
        except PrinterError as e:
            self.last_error = str(e)

    async def _discover_unicast(self):
        info = await discover(self.ip, self.cfg.get("udp_port", 3000), timeout=1.5)
        if info:
            d = info[0]
            self.mainboard_id = d.get("MainboardID") or self.mainboard_id
            self.brand_id = d.get("Id") or self.brand_id

    def _handle_text(self, text):
        self.last_message = time.time()
        if text == "pong":
            return
        try:
            obj = json.loads(text)
        except ValueError:
            return
        data = obj.get("Data") if isinstance(obj.get("Data"), dict) else {}
        topic = obj.get("Topic", "")
        kind = topic.split("/")[1] if topic.count("/") >= 2 else ""
        mb = obj.get("MainboardID") or data.get("MainboardID")
        if mb and not self.mainboard_id:
            self.mainboard_id = mb
        if kind == "response" or (not kind and data.get("RequestID")):
            rid = data.get("RequestID")
            fut = self.pending.pop(rid, None)
            quiet = rid in self._quiet
            self._quiet.discard(rid)
            if fut is not None and quiet:
                self._emit("in", "debug", _summarise("in", obj)[1], msg=obj)
                if not fut.done():
                    fut.set_result(data)
            elif fut is None:
                # Not an answer to anything we sent: another client (phone app, Lychee...) is talking to the printer.
                cmd = data.get("Cmd")
                self._emit("in", "other", f"OTHER CLIENT  Cmd {cmd} -> {json.dumps(data.get('Data'), separators=(',', ':'))[:600]}", msg=obj)
            else:
                self._log("in", obj)
                if not fut.done():
                    fut.set_result(data)
            return
        if kind in ("request",) or (data.get("Cmd") is not None and kind != "response"):
            self._emit("in", "other", f"OTHER CLIENT REQUEST  Cmd {data.get('Cmd')} {json.dumps(data.get('Data'), separators=(',', ':'))[:600]}", msg=obj)
            return
        self._log("in", obj)
        if "Status" in obj or "Status" in data:
            new = obj.get("Status") or data.get("Status") or {}
            self._log_changes("status", self.status, new)
            self.status = new
        elif "Attributes" in obj or "Attributes" in data:
            new = obj.get("Attributes") or data.get("Attributes") or {}
            self._log_changes("attributes", self.attributes, new)
            self.attributes = new
        elif kind == "error":
            self.errors.append({"t": time.time(), "data": data.get("Data") or data})
        elif kind == "notice":
            self.notices.append({"t": time.time(), "data": data.get("Data") or data})
        self.on_change()

    NOISY = {"CurrentTicks", "CurrentLayer", "TempOfUVLED", "TimeStamp", "ReleaseFilm",
             "RemainingMemory", "TotalTicks"}

    def _log_changes(self, what, old, new):
        """Log which status/attribute fields changed (so unknown fields, e.g. a light flag, show up)."""
        def flat(d, pre=""):
            out = {}
            for k, v in (d or {}).items():
                if isinstance(v, dict):
                    out.update(flat(v, pre + k + "."))
                else:
                    out[pre + k] = v
            return out
        o, n = flat(old), flat(new)
        if not o:
            if what not in self._seen_initial:
                self._seen_initial.add(what)
                self._emit("in", "change", f"Initial {what} fields: {json.dumps(new, separators=(',', ':'))}", msg=new)
            return
        for k in sorted(n):
            if k.split(".")[-1] in self.NOISY:
                continue
            if k not in o:
                self._emit("in", "change", f"{what} NEW field {k} = {n[k]!r}")
            elif o[k] != n[k]:
                self._emit("in", "change", f"{what} {k}: {o[k]!r} -> {n[k]!r}")

    # ------------------------------------------------------------ requests
    def envelope(self, cmd, data=None, extra=None):
        """The standard SDCP request message. `extra` adds keys next to Cmd/Data (e.g. Belong/ProVer)."""
        return {
            "Id": self.brand_id,
            "Data": {
                "Cmd": cmd, **(extra or {}), "Data": data or {}, "RequestID": uuid.uuid4().hex,
                "MainboardID": self.mainboard_id, "TimeStamp": int(time.time()), "From": 0,
            },
            "Topic": f"sdcp/request/{self.mainboard_id}",
        }

    async def request(self, cmd, data=None, timeout=10, quiet=False):
        """Send an SDCP request and return the response Data dict (contains 'Ack')."""
        if not self.ws or self.ws.closed:
            raise PrinterError("Not connected to the printer")
        msg = self.envelope(cmd, data)
        rid = msg["Data"]["RequestID"]
        fut = asyncio.get_running_loop().create_future()
        self.pending[rid] = fut
        if quiet:
            self._quiet.add(rid)
            self._emit("out", "debug", _summarise("out", msg)[1], msg=msg)
        else:
            self._log("out", msg)
        await self.ws.send_str(json.dumps(msg))
        try:
            resp = await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            self.pending.pop(rid, None)
            self._quiet.discard(rid)
            raise PrinterError(f"Printer did not answer command {cmd} within {timeout}s "
                               "(it may not support it)")
        return resp.get("Data") or {}

    async def send_raw(self, text, wait=6.0):
        """Send `text` over the websocket byte for byte - no parsing, no validation, nothing blocked.
        If it happens to be JSON with a Data.RequestID, also wait for the matching reply."""
        if not self.ws or self.ws.closed:
            raise PrinterError("Not connected to the printer")
        obj, rid = None, None
        try:
            obj = json.loads(text)
        except ValueError:
            pass
        inner = obj.get("Data") if isinstance(obj, dict) else None
        if isinstance(inner, dict):
            rid = inner.get("RequestID")
        fut = None
        if rid:
            fut = asyncio.get_running_loop().create_future()
            self.pending[str(rid)] = fut
        if isinstance(obj, dict):
            self._log("out", obj)
        else:
            self._emit("out", "request", "RAW (not JSON): " + text[:500])
        await self.ws.send_str(text)
        if fut is None:
            return {"sent": True, "json": obj is not None, "reply": None,
                    "note": "Sent. No Data.RequestID in the message, so I am not waiting for a reply - watch the console."}
        try:
            resp = await asyncio.wait_for(fut, wait)
            return {"sent": True, "json": True, "reply": resp}
        except asyncio.TimeoutError:
            self.pending.pop(str(rid), None)
            return {"sent": True, "json": True, "reply": None,
                    "note": f"Sent. The printer did not answer within {wait:g}s (it may ignore this command)."}

    async def light_test(self, on):
        """Try the known ways of asking for the light, stop at the first one the printer answers.
        Cmd 151 {ControlState} is what the Matrix app sends over the cloud channel; the local
        websocket may not support it at all, so this reports exactly what the printer did."""
        state = 1 if on else 0
        machine = (self.attributes or {}).get("MachineName") or "Saturn 4 Ultra 16K"
        variants = [
            ("plain", self.envelope(CMD["LIGHT"], {"ControlState": state})),
            ("matrix-fields", self.envelope(CMD["LIGHT"], {"ControlState": state},
                                            {"Belong": "LCD", "ProVer": "0.0.1", "MachineType": machine})),
        ]
        if self.light_variant:        # one that worked before goes first
            variants.sort(key=lambda v: v[0] != self.light_variant)
        results = []
        for name, msg in variants:
            r = await self.send_raw(json.dumps(msg), wait=3.0)
            ack = ((r.get("reply") or {}).get("Data") or {}).get("Ack")
            results.append({"variant": name, "answered": r.get("reply") is not None, "ack": ack})
            if r.get("reply") is not None:
                if ack == 0:
                    self.light, self.light_variant = on, name
                    self.on_change()
                break
        return results

    async def set_print_params(self, params):
        """Cmd 135 (change print params), one key per message like the Matrix app sends it."""
        out = []
        for k, v in params.items():
            try:
                d = await self.request(CMD["CHANGE_PARAMS"], {k: v}, timeout=4)
                out.append({"key": k, "value": v, "answered": True, "ack": d.get("Ack")})
            except PrinterError as e:
                out.append({"key": k, "value": v, "answered": False, "error": str(e)})
        return out

    async def command(self, cmd, data=None, timeout=10):
        """request() + Ack interpretation. Returns (ok, ack, message, data)."""
        d = await self.request(cmd, data, timeout)
        ack = d.get("Ack", 0)
        ok = ack == 0
        msg = "OK" if ok else ACK_TEXT.get(cmd, {}).get(ack, f"Printer returned Ack {ack}")
        return ok, ack, msg, d

    # ------------------------------------------------------------ files
    async def list_files(self, path):
        ok, ack, msg, d = await self.command(CMD["FILES"], {"Url": path}, timeout=20)
        if not ok:
            raise PrinterError(msg)
        entries = d.get("FileList") or []
        files = [normalize_file(e) for e in entries]
        storage = None
        for e in entries:
            if "totalSize" in e and "usedSize" in e:
                storage = {"used": e["usedSize"], "total": e["totalSize"],
                           "type": e.get("storageType")}
                break
        # Some firmwares return the folder itself as an entry - drop it.
        here = path.rstrip("/")
        files = [f for f in files if f["path"].rstrip("/") != here]
        return files, storage

    async def delete(self, files, folders):
        ok, ack, msg, d = await self.command(
            CMD["DELETE"], {"FileList": files, "FolderList": folders}, timeout=30)
        return {"ok": ok, "ack": ack, "message": msg, "failed": d.get("ErrData") or []}

    async def history(self, limit=60):
        ok, ack, msg, d = await self.command(CMD["HISTORY"], timeout=20)
        if not ok:
            raise PrinterError(msg)
        ids = list(d.get("HistoryData") or [])
        items = []
        for i in range(0, len(ids), 20):
            ok, ack, msg, dd = await self.command(
                CMD["HISTORY_DETAIL"], {"Id": ids[i:i + 20]}, timeout=20)
            if not ok:
                raise PrinterError(msg)
            items += [normalize_history(x) for x in dd.get("HistoryDetailList") or []]
        items.sort(key=lambda x: x.get("begin") or 0, reverse=True)
        return items[:limit], len(ids)

    async def history_cached(self, max_age=60):
        t, items = self._hist_cache
        if time.time() - t > max_age:
            items, _ = await self.history(limit=500)
            self._hist_cache = (time.time(), items)
        return items

    async def file_info(self, max_age=15):
        """{file name: details} from the printer's own database (file_info.txt, served over HTTP)."""
        t, cached = self._fi
        if time.time() - t < max_age:
            return cached
        url = f"http://{self.ip}:{self.port}{FILE_INFO_PATH}"
        try:
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200:
                    return cached
                text = (await r.read()).decode("utf-8", "replace")
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return cached
        info = parse_file_info(text)
        self._fi = (time.time(), info)
        return info

    def camera_slots(self):
        a = self.attributes or {}
        used, mx = a.get("NumberOfVideoStreamConnected"), a.get("MaximumVideoStreamAllowed")
        if used is None or mx is None:
            return None
        return {"used": used, "max": mx, "ours": bool(self.video_enabled),
                "others": max(used - (1 if self.video_enabled else 0), 0)}

    def reconfigure(self, ip):
        """Switch to a new printer IP at runtime: drop the connection and let the loop reconnect."""
        self.ip = ip
        self.cfg["printer_ip"] = ip
        self.mainboard_id = ""
        self._hist_cache = (0, [])
        self._fi = (0, {})
        self.note(f"Printer IP changed to {ip}")
        if self.ws and not self.ws.closed:
            asyncio.ensure_future(self.ws.close())

    # ------------------------------------------------------------ camera
    async def enable_video(self, enable=True):
        ok, ack, msg, d = await self.command(CMD["VIDEO"], {"Enable": 1 if enable else 0})
        if ok and enable:
            self.video_url = d.get("VideoUrl") or None
            self.video_enabled = True
        elif ok:
            self.video_enabled = False
        self.on_change()
        return ok, msg, d.get("VideoUrl")

    def default_video_url(self):
        return f"http://{self.ip}:{self.cfg.get('video_port', 3031)}/video"

    # ------------------------------------------------------------ upload
    async def upload_file(self, path, filename, size, md5, print_after=False):
        """Push a local file to the printer in 1 MB chunks."""
        up = {"id": uuid.uuid4().hex, "name": filename, "total": size, "sent": 0,
              "phase": "sending", "error": None, "started": time.time(),
              "print_after": print_after}
        self.upload = up
        self.note(f"Upload started: {filename} ({size} bytes, md5 {md5})")
        self.on_change()
        file_uuid = uuid.uuid4().hex
        url = f"http://{self.ip}:{self.port}/uploadFile/upload"
        try:
            with open(path, "rb") as f:
                offset = 0
                while True:
                    chunk = f.read(CHUNK)
                    if not chunk and offset >= size:
                        break
                    body, ctype = multipart_body(
                        [("S-File-MD5", md5), ("Check", "1"), ("Offset", str(offset)),
                         ("Uuid", file_uuid), ("TotalSize", str(size))], "File", filename, chunk)
                    async with self.session.post(
                            url, data=body, headers={"S-File-MD5": md5, "Content-Type": ctype},
                            timeout=aiohttp.ClientTimeout(total=120)) as r:
                        body = await r.text()
                        try:
                            js = json.loads(body)
                        except ValueError:
                            raise PrinterError(f"Upload: bad response {r.status}: {body[:200]}")
                    if not js.get("success"):
                        raise PrinterError("Upload rejected: " + _upload_error(js))
                    offset += len(chunk)
                    up["sent"] = offset
                    self.note(f"Upload chunk ok: {offset}/{size} bytes", "debug")
                    self.on_change()
                    if offset >= size:
                        break
            up["phase"] = "verifying"
            self.on_change()
            await self._wait_transfer_done()
            up["phase"] = "done"
            up["finished"] = time.time()
            self.note(f"Upload finished: {filename}")
            self.on_change()
        except asyncio.CancelledError:
            up["phase"] = "cancelled"
            await self._abort_transfer(file_uuid, filename)
            self.on_change()
            raise
        except Exception as e:  # noqa: BLE001
            up["phase"] = "error"
            up["error"] = str(e)
            self.note(f"Upload failed: {e}", "error")
            await self._abort_transfer(file_uuid, filename)
            self.on_change()
        return up

    async def _wait_transfer_done(self):
        for _ in range(120):
            cur = (self.status or {}).get("CurrentStatus") or []
            if isinstance(cur, int):
                cur = [cur]
            if 2 not in cur:
                return
            await asyncio.sleep(0.5)

    async def _abort_transfer(self, file_uuid, filename):
        try:
            await self.request(CMD["ABORT_UPLOAD"], {"Uuid": file_uuid, "FileName": filename}, timeout=5)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------ view
    def public_state(self):
        st = self.status or {}
        pi = st.get("PrintInfo") or {}
        cur = st.get("CurrentStatus")
        cur = [cur] if isinstance(cur, int) else (cur or [])
        total_t, cur_t = pi.get("TotalTicks") or 0, pi.get("CurrentTicks") or 0
        tl, cl = pi.get("TotalLayer") or 0, pi.get("CurrentLayer") or 0
        return {
            "connected": self.connected,
            "last_error": self.last_error,
            "printer_ip": self.ip,
            "mainboard_id": self.mainboard_id,
            "attributes": self.attributes,
            "status": st,
            "machine_status": [MACHINE_STATUS.get(c, f"Status {c}") for c in cur],
            "machine_status_codes": cur,
            "print_status": PRINT_STATUS.get(pi.get("Status"), None),
            "print_status_code": pi.get("Status"),
            "print_error": PRINT_ERRORS.get(pi.get("ErrorNumber"), None) if pi.get("ErrorNumber") else None,
            "progress": (cl / tl * 100.0) if tl else 0,
            "remaining_ms": max(total_t - cur_t, 0) if total_t else None,
            "upload": self.upload,
            "light": self.light,
            "light_variant": self.light_variant,
            "reconnects": self.reconnects,
            "brand_id": self.brand_id,
            "camera": self.camera_slots(),
            "video_enabled": self.video_enabled,
            "notices": list(self.notices)[-10:],
            "errors": list(self.errors)[-10:],
            "last_message": self.last_message,
        }


def _upload_error(js):
    parts = []
    for m in js.get("messages") or []:
        if m.get("field") == "common_field":
            parts.append({-1: "offset error", -2: "offset mismatch", -3: "file open failed",
                          -4: "unknown error"}.get(m.get("message"), str(m.get("message"))))
        else:
            parts.append(f"{m.get('field')}: {m.get('message')}")
    return "; ".join(parts) or json.dumps(js)[:200]


def multipart_body(fields, file_field, filename, payload):
    """Hand-built multipart/form-data. aiohttp's FormData writes the file name percent-encoded
    ('filename="Arthas%2015cm%20Base.goo"' plus a filename* twin), and this printer stores that
    text literally - so the name must go out as plain text, like a browser or Lychee sends it."""
    boundary = "----SaturnControl" + uuid.uuid4().hex
    out = bytearray()
    for k, v in fields:
        out += (f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n').encode()
    safe = filename.replace("\\", "\\\\").replace('"', '\\"').replace("\r", "").replace("\n", "")
    out += (f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; filename="').encode()
    out += safe.encode("utf-8")
    out += b'"\r\nContent-Type: application/octet-stream\r\n\r\n'
    out += payload
    out += f"\r\n--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


async def discover(target="255.255.255.255", port=3000, timeout=2.0):
    """UDP discovery: send 'M99999' and collect replies. Returns list of Data dicts."""
    loop = asyncio.get_running_loop()
    found, ids = [], set()

    class Proto(asyncio.DatagramProtocol):
        def connection_made(self, transport):
            self.t = transport
            transport.sendto(b"M99999", (target, port))

        def datagram_received(self, data, addr):
            try:
                js = json.loads(data.decode("utf-8", "replace"))
            except ValueError:
                return
            d = dict(js.get("Data") or {})
            d["Id"] = js.get("Id")
            key = d.get("MainboardID") or addr[0]
            if key not in ids:
                ids.add(key)
                d.setdefault("MainboardIP", addr[0])
                found.append(d)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.setblocking(False)
    try:
        sock.bind(("", 0))
    except OSError:
        pass
    transport, _ = await loop.create_datagram_endpoint(Proto, sock=sock)
    try:
        await asyncio.sleep(timeout)
    finally:
        transport.close()
    return found


def file_md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def _summarise(direction, m):
    """(kind, one-line text) for a protocol message."""
    names = {v: k.replace("_", " ").title() for k, v in CMD.items()}
    names.update({403: "Set property (light/speed/fan)"})
    d = m.get("Data") if isinstance(m.get("Data"), dict) else {}
    if "Cmd" in m and "Cmd" not in d:        # hand-typed message without the SDCP wrapper
        d = m
    topic = m.get("Topic", "")
    kind = topic.split("/")[1] if topic.count("/") >= 2 else "?"
    if direction == "out":
        c = d.get("Cmd")
        args = json.dumps(d.get("Data") or {}, separators=(",", ":"))
        return "request", f"Cmd {c} {names.get(c, '')} {args if args != '{}' else ''}".replace("  ", " ").strip()
    if kind == "response":
        c = d.get("Cmd")
        inner = dict(d.get("Data") or {})
        ack = inner.pop("Ack", None)
        res = "OK" if ack == 0 else f"Ack {ack}" + (f" ({ACK_TEXT.get(c, {}).get(ack)})" if ACK_TEXT.get(c, {}).get(ack) else "")
        extra = json.dumps(inner, separators=(",", ":"))
        if len(extra) > 300:
            extra = extra[:300] + "..."
        return "response", f"Cmd {c} {names.get(c, '')} -> {res}" + (f" {extra}" if inner else "")
    if kind == "status":
        st = m.get("Status") or d.get("Status") or {}
        pi = st.get("PrintInfo") or {}
        cur = st.get("CurrentStatus")
        cur = [cur] if isinstance(cur, int) else (cur or [])
        return "status", ("Status: " + "/".join(MACHINE_STATUS.get(c, str(c)) for c in cur)
                          + f", print={PRINT_STATUS.get(pi.get('Status'), '-')}, layer {pi.get('CurrentLayer')}/{pi.get('TotalLayer')}"
                          + f", UV {st.get('TempOfUVLED')}C")
    if kind == "attributes":
        return "attributes", "Attributes update received"
    return kind, f"{kind.title()}: " + json.dumps(d.get("Data") or d, separators=(",", ":"))[:300]
