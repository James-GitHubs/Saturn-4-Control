"""Mock Elegoo printer (SDCP v3.0.0) for testing the app without hardware.

    python tools/mock_printer.py [--port 3030] [--video-port 3031] [--udp-port 3000]
                                 [--minimal] [--light-unsupported]

Implements everything in the public SDCP spec plus an *assumed* light command (Cmd 403).
--minimal makes file lists contain only the spec-guaranteed fields (no layers/time/thumbnail),
to exercise the UI fallbacks. Needs Pillow for thumbnails and camera frames.
"""
import argparse
import asyncio
import hashlib
import io
import json
import os
import random
import tempfile
import time
import uuid
from pathlib import Path

from aiohttp import web
from PIL import Image, ImageDraw

MAINBOARD = "000000000001d354"
BRAND = uuid.uuid4().hex


class Mock:
    def __init__(self, args):
        self.args = args
        self.root = Path(args.dir or tempfile.mkdtemp(prefix="mock-printer-"))
        for d in ("local", "usb"):
            (self.root / d).mkdir(parents=True, exist_ok=True)
        self.meta = {}
        self.clients = set()
        self.name = "Saturn 4 Ultra (mock)"
        self.status = 0
        self.prev = 0
        self.pi = {"Status": 0, "CurrentLayer": 0, "TotalLayer": 0, "CurrentTicks": 0,
                   "TotalTicks": 0, "Filename": "", "ErrorNumber": 0, "TaskId": ""}
        self.light = 0
        self.timelapse = 0
        self.streams = 0
        self.uploads = {}
        self.history = []
        self.print_task = None
        self.paused = False
        self.seed()

    # --------------------------------------------------------------- data
    def seed(self):
        for i, (n, lh, layers, secs) in enumerate([
                ("benchy_0.05.ctb", 0.05, 1840, 13200), ("dragon_miniature.ctb", 0.03, 3120, 21600),
                ("calibration_cube.ctb", 0.05, 400, 3300)]):
            self.add_file("local", n, lh, layers, secs)
        (self.root / "local" / "minis").mkdir(exist_ok=True)
        self.add_file("local/minis", "orc_bust.ctb", 0.04, 2500, 17400)
        self.add_file("usb", "usb_test.ctb", 0.05, 600, 5000)
        now = int(time.time())
        for k in range(3):
            self.history.append({
                "Thumbnail": self.thumb_url(f"hist{k}.png"), "TaskName": ["benchy_0.05.ctb", "calibration_cube.ctb", "dragon_miniature.ctb"][k],
                "BeginTime": now - 86400 * (k + 1), "EndTime": now - 86400 * (k + 1) + 3600 * (k + 1),
                "TaskStatus": [1, 1, 3][k], "SliceInformation": {}, "AlreadyPrintLayer": [1840, 400, 1200][k],
                "TaskId": uuid.uuid4().hex, "MD5": "0" * 32, "CurrentLayerTalVolume": 12.5 + k,
                "TimeLapseVideoStatus": 1 if k == 0 else 0,
                "TimeLapseVideoUrl": f"http://127.0.0.1:{self.args.port}/timelapse.mp4" if k == 0 else "",
                "ErrorStatusReason": 0 if k < 2 else 2})

    def add_file(self, folder, name, lh, layers, secs):
        p = self.root / folder / name
        p.write_bytes(os.urandom(2048))
        self.meta[f"/{folder}/{name}"] = {"layer_height": lh, "layers": layers, "print_time": secs}

    def thumb_url(self, name):
        return f"http://127.0.0.1:{self.args.port}/thumb/{name}"

    def resolve(self, url):
        url = url or "/local/"
        if not url.startswith("/"):
            url = "/local/" + url
        return url, self.root / url.strip("/")

    def disk(self, storage):
        return {"usedSize": sum(f.stat().st_size for f in (self.root / storage).rglob("*") if f.is_file()),
                "totalSize": 8 * 1024 ** 3, "storageType": 0 if storage == "local" else 1}

    # --------------------------------------------------------------- push
    def envelope(self, topic, payload):
        return {"Id": BRAND, **payload, "MainboardID": MAINBOARD, "TimeStamp": int(time.time()),
                "Topic": f"sdcp/{topic}/{MAINBOARD}"}

    def status_msg(self):
        cur = [self.status]
        return self.envelope("status", {"Status": {
            "CurrentStatus": cur, "PreviousStatus": self.prev, "PrintScreen": 12345, "ReleaseFilm": 321,
            "TempOfUVLED": 31.5, "TimeLapseStatus": self.timelapse, "TempOfBox": 28, "TempTargetBox": 0,
            "PrintInfo": dict(self.pi)}})

    def attr_msg(self):
        return self.envelope("attributes", {"Attributes": {
            "Name": self.name, "MachineName": "Saturn 4 Ultra 16K (mock)", "BrandName": "ELEGOO",
            "ProtocolVersion": "V3.0.0", "FirmwareVersion": "V1.4.2", "Resolution": "15120x6230",
            "XYZsize": "218.88x122.88x220", "MainboardIP": "127.0.0.1", "MainboardID": MAINBOARD,
            "NumberOfVideoStreamConnected": self.streams, "MaximumVideoStreamAllowed": 1,
            "NetworkStatus": "wlan", "UsbDiskStatus": 1, "Capabilities": ["FILE_TRANSFER", "PRINT_CONTROL", "VIDEO_STREAM"],
            "SupportFileType": ["CTB", "GOO"],
            "DevicesStatus": {"TempSensorStatusOfUVLED": 1, "LCDStatus": 1, "SgStatus": 1, "ZMotorStatus": 1,
                              "RotateMotorStatus": 1, "RelaseFilmState": 1, "XMotorStatus": 1},
            "ReleaseFilmMax": 2000, "TempOfUVLEDMax": 60, "CameraStatus": 1,
            "RemainingMemory": 8 * 1024 ** 3 * 8, "TLPNoCapPos": 50.0, "TLPStartCapPos": 30.0, "TLPInterLayers": 20}})

    async def broadcast(self, msg):
        for ws in list(self.clients):
            try:
                await ws.send_str(json.dumps(msg))
            except Exception:  # noqa: BLE001
                self.clients.discard(ws)

    # --------------------------------------------------------------- commands
    async def handle(self, ws, req):
        d = req["Data"]
        cmd, data = d["Cmd"], d.get("Data") or {}
        ack, out = 0, {}
        if req.get("Topic") != f"sdcp/request/{MAINBOARD}" or d.get("MainboardID") != MAINBOARD:
            return  # real boards ignore requests for another mainboard
        if cmd == 0:
            await ws.send_str(json.dumps(self.status_msg()))
        elif cmd == 1:
            await ws.send_str(json.dumps(self.attr_msg()))
        elif cmd == 128:
            ack = await self.start_print(data)
        elif cmd == 129:
            if self.status == 1 and not self.paused:
                self.paused = True
                self.pi["Status"] = 6
            else:
                ack = 1
        elif cmd == 131:
            if self.paused:
                self.paused = False
                self.pi["Status"] = 3
            else:
                ack = 1
        elif cmd == 130:
            if self.status == 1:
                await self.finish(stopped=True)
            else:
                ack = 1
        elif cmd in (132, 133):
            pass
        elif cmd == 192:
            self.name = data.get("Name", self.name)
            await self.broadcast(self.attr_msg())
        elif cmd == 255:
            ack = 0 if data.get("Uuid") in self.uploads else 1
        elif cmd == 258:
            url, path = self.resolve(data.get("Url"))
            if not path.exists():
                ack = 3
            else:
                storage = "usb" if url.startswith("/usb") else "local"
                fl = []
                for p in sorted(path.iterdir()):
                    rel = "/" + str(p.relative_to(self.root)).replace(os.sep, "/")
                    e = {"name": rel, **self.disk(storage), "type": 0 if p.is_dir() else 1}
                    if p.is_file() and not self.args.minimal:
                        m = self.meta.get(rel, {"layer_height": 0.05, "layers": 100, "print_time": 600})
                        e.update(layer_height=m["layer_height"], layers=m["layers"], print_time=m["print_time"],
                                 thumbnail=self.thumb_url(p.name + ".png"))
                    fl.append(e)
                out = {"FileList": fl}
        elif cmd == 259:
            err = []
            for f in data.get("FileList", []):
                _, p = self.resolve(f)
                if p.is_file():
                    p.unlink()
                    self.meta.pop(f, None)
                else:
                    err.append(f)
            for f in data.get("FolderList", []):
                _, p = self.resolve(f)
                try:
                    p.rmdir()
                except OSError:
                    err.append(f)
            if err:
                out = {"ErrData": err}
        elif cmd == 320:
            out = {"HistoryData": [h["TaskId"] for h in self.history]}
        elif cmd == 321:
            ids = set(data.get("Id") or [])
            out = {"HistoryDetailList": [h for h in self.history if h["TaskId"] in ids]}
        elif cmd == 386:
            if data.get("Enable"):
                if self.streams >= 1:
                    ack = 1
                else:
                    out = {"VideoUrl": "rtsp://127.0.0.1:1/video" if self.args.rtsp_dead else f"http://127.0.0.1:{self.args.video_port}/video"}
            else:
                pass
        elif cmd == 387:
            self.timelapse = 1 if data.get("Enable") else 0
        elif cmd == 403 and not self.args.light_unsupported and "LightStatus" in data:
            self.light = int(bool(data["LightStatus"].get("SecondLight", 0)))
        else:
            return  # unknown command: silence (like the real board does for some)
        resp = {"Id": BRAND, "Data": {"Cmd": cmd, "Data": {"Ack": ack, **out},
                                       "RequestID": d["RequestID"], "MainboardID": MAINBOARD,
                                       "TimeStamp": int(time.time())},
                "Topic": f"sdcp/response/{MAINBOARD}"}
        await self.broadcast(resp)   # assumed: board publishes responses to every connected client
        if cmd in (128, 129, 130, 131, 387):
            await self.broadcast(self.status_msg())

    async def start_print(self, data):
        if self.status == 1:
            return 1
        name = data.get("Filename", "")
        _, p = self.resolve(name)
        if not p.is_file():
            return 2
        m = self.meta.get("/" + str(p.relative_to(self.root)).replace(os.sep, "/"), {"layers": 60, "print_time": 120})
        self.prev, self.status, self.paused = 0, 1, False
        layers = min(m["layers"], 60)  # shortened so tests finish quickly
        self.pi = {"Status": 1, "CurrentLayer": data.get("StartLayer", 0), "TotalLayer": layers,
                   "CurrentTicks": 0, "TotalTicks": layers * 1000, "Filename": p.name, "ErrorNumber": 0,
                   "TaskId": uuid.uuid4().hex}
        self.print_task = asyncio.create_task(self.run_print())
        return 0

    async def run_print(self):
        begin = int(time.time())
        await asyncio.sleep(0.4)
        self.pi["Status"] = 3
        while self.pi["CurrentLayer"] < self.pi["TotalLayer"]:
            await asyncio.sleep(0.25)
            if self.status != 1:
                return
            if self.paused:
                continue
            self.pi["CurrentLayer"] += 1
            self.pi["CurrentTicks"] += 1000
            self.pi["Status"] = random.choice([2, 3, 4])
            await self.broadcast(self.status_msg())
        await self.finish(stopped=False, begin=begin)

    async def finish(self, stopped, begin=None):
        self.prev, self.status = 1, 0
        self.pi["Status"] = 8 if stopped else 9
        self.history.append({
            "Thumbnail": self.thumb_url(self.pi["Filename"] + ".png"), "TaskName": self.pi["Filename"],
            "BeginTime": begin or int(time.time()) - 5, "EndTime": int(time.time()),
            "TaskStatus": 3 if stopped else 1, "SliceInformation": {}, "AlreadyPrintLayer": self.pi["CurrentLayer"],
            "TaskId": self.pi["TaskId"], "MD5": "0" * 32, "CurrentLayerTalVolume": 3.2,
            "TimeLapseVideoStatus": 0, "TimeLapseVideoUrl": "", "ErrorStatusReason": 0})
        await self.broadcast(self.status_msg())


def png(text, size=(256, 256)):
    img = Image.new("RGB", size, (30, 40, 60))
    d = ImageDraw.Draw(img)
    rnd = random.Random(text)
    for _ in range(8):
        x, y = rnd.randint(0, 200), rnd.randint(0, 200)
        d.ellipse([x, y, x + rnd.randint(20, 80), y + rnd.randint(20, 80)], fill=tuple(rnd.randint(80, 255) for _ in range(3)))
    d.text((8, 8), text[:28], fill=(255, 255, 255))
    b = io.BytesIO()
    img.save(b, "PNG")
    return b.getvalue()


def make_apps(m: Mock):
    app = web.Application(client_max_size=2 * 1024 ** 3)

    async def ws_handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        m.clients.add(ws)
        # Real boards push status/attributes on connect, with the mainboard id inside.
        await ws.send_str(json.dumps(m.attr_msg()))
        await ws.send_str(json.dumps(m.status_msg()))
        async for msg in ws:
            if msg.data == "ping":
                await ws.send_str("pong")
                continue
            try:
                await m.handle(ws, json.loads(msg.data))
            except Exception as e:  # noqa: BLE001
                print("mock error", e)
        m.clients.discard(ws)
        return ws

    async def upload(request):
        r = await request.multipart()
        fields, chunk, fname = {}, b"", None
        async for part in r:
            if part.name == "File":
                fname = part.filename
                chunk = await part.read(decode=False)
            else:
                fields[part.name] = (await part.text())
        md5 = request.headers.get("S-File-MD5") or fields.get("S-File-MD5")
        off, total, uid = int(fields["Offset"]), int(fields["TotalSize"]), fields["Uuid"]
        up = m.uploads.setdefault(uid, {"name": fname, "got": 0, "tmp": m.root / f".up-{uid}"})
        if off < 0:
            return web.json_response({"code": "111111", "success": False, "data": None,
                                      "messages": [{"field": "common_field", "message": -1}]})
        if off != up["got"]:
            return web.json_response({"code": "111111", "success": False, "data": None,
                                      "messages": [{"field": "common_field", "message": -2}]})
        with open(up["tmp"], "ab") as f:
            f.write(chunk)
        up["got"] += len(chunk)
        if up["got"] >= total:
            h = hashlib.md5(up["tmp"].read_bytes()).hexdigest()
            if fields.get("Check") == "1" and h != md5:
                up["tmp"].unlink()
                m.uploads.pop(uid, None)
                await m.broadcast(m.envelope("error", {"Data": {"Data": {"ErrorCode": 1}}}))
                return web.json_response({"code": "111111", "success": False, "data": None,
                                          "messages": [{"field": "common_field", "message": -4}]})
            m.prev, m.status = m.status, 2
            await m.broadcast(m.status_msg())
            await asyncio.sleep(0.8)  # "verifying"
            os.replace(up["tmp"], m.root / "local" / up["name"])
            m.meta[f"/local/{up['name']}"] = {"layer_height": 0.05, "layers": 321, "print_time": 4000}
            m.uploads.pop(uid, None)
            m.status = 0
            await m.broadcast(m.status_msg())
        return web.json_response({"code": "000000", "messages": None, "data": {}, "success": True})

    async def thumb(request):
        return web.Response(body=png(request.match_info["name"]), content_type="image/png")

    async def timelapse(request):
        return web.Response(body=b"\x00" * 1024, content_type="video/mp4")

    app.router.add_get("/websocket", ws_handler)
    app.router.add_post("/uploadFile/upload", upload)
    app.router.add_get("/thumb/{name}", thumb)
    app.router.add_get("/timelapse.mp4", timelapse)

    vapp = web.Application()

    async def video(request):
        if m.streams >= 1:
            return web.Response(status=503, text="max streams")
        m.streams += 1
        resp = web.StreamResponse()
        resp.headers["Content-Type"] = "multipart/x-mixed-replace; boundary=frame"
        await resp.prepare(request)
        n = 0
        try:
            while True:
                img = Image.new("RGB", (640, 480), (15, 15, 25))
                d = ImageDraw.Draw(img)
                d.rectangle([20 + (n * 7) % 560, 200, 80 + (n * 7) % 560, 260], fill=(255, 120, 0))
                d.text((10, 10), f"MOCK CAMERA frame {n}  {time.strftime('%H:%M:%S')}", fill=(255, 255, 255))
                b = io.BytesIO()
                img.save(b, "JPEG", quality=70)
                data = b.getvalue()
                await resp.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(data) + data + b"\r\n")
                n += 1
                await asyncio.sleep(0.2)
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            m.streams -= 1
        return resp

    vapp.router.add_get("/video", video)
    return app, vapp


class Udp(asyncio.DatagramProtocol):
    def connection_made(self, t):
        self.t = t

    def datagram_received(self, data, addr):
        if data.strip() == b"M99999":
            self.t.sendto(json.dumps({"Id": BRAND, "Data": {
                "Name": "Saturn 4 Ultra (mock)", "MachineName": "Saturn 4 Ultra 16K", "BrandName": "ELEGOO",
                "MainboardIP": "127.0.0.1", "MainboardID": MAINBOARD, "ProtocolVersion": "V3.0.0",
                "FirmwareVersion": "V1.4.2"}}).encode(), addr)


async def amain(args):
    m = Mock(args)
    app, vapp = make_apps(m)
    for a, port in ((app, args.port), (vapp, args.video_port)):
        r = web.AppRunner(a)
        await r.setup()
        await web.TCPSite(r, "127.0.0.1", port).start()
    loop = asyncio.get_running_loop()
    await loop.create_datagram_endpoint(Udp, local_addr=("127.0.0.1", args.udp_port))
    print(f"Mock printer: ws/http :{args.port}, video :{args.video_port}, udp :{args.udp_port}, data {m.root}", flush=True)
    await asyncio.Event().wait()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=3030)
    ap.add_argument("--video-port", type=int, default=3031)
    ap.add_argument("--udp-port", type=int, default=3000)
    ap.add_argument("--dir")
    ap.add_argument("--minimal", action="store_true")
    ap.add_argument("--rtsp-dead", action="store_true", help="advertise an unreachable RTSP camera URL")
    ap.add_argument("--light-unsupported", action="store_true")
    asyncio.run(amain(ap.parse_args()))
