"""End-to-end test: starts the mock printer + the app on spare ports and drives the HTTP API.

    python tools/test_e2e.py
"""
import asyncio
import hashlib
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parent.parent
PORTS = dict(printer=13030, video=13031, udp=13000, web=18080)
BASE = f"http://127.0.0.1:{PORTS['web']}"
results = []


def check(name, cond, extra=""):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""), flush=True)


async def wait_for(fn, timeout=15, step=0.2):
    end = time.time() + timeout
    while time.time() < end:
        r = await fn()
        if r:
            return r
        await asyncio.sleep(step)
    return None


async def main(minimal=False):
    cfgfile = os.path.join(tempfile.mkdtemp(), "config.json")
    open(cfgfile, "w").write('{"open_browser": false}')
    env = dict(os.environ, SATURN_IP="127.0.0.1", SATURN_PORT=str(PORTS["printer"]),
               SATURN_VIDEO_PORT=str(PORTS["video"]), SATURN_UDP_PORT=str(PORTS["udp"]),
               SATURN_WEB_PORT=str(PORTS["web"]), SATURN_CONFIG=cfgfile)
    mock_args = [sys.executable, str(ROOT / "tools/mock_printer.py"), "--port", str(PORTS["printer"]),
                 "--video-port", str(PORTS["video"]), "--udp-port", str(PORTS["udp"])]
    if minimal:
        mock_args.append("--minimal")
    mock = subprocess.Popen(mock_args)
    await asyncio.sleep(1.5)
    app = subprocess.Popen([sys.executable, "-m", "saturn_control"], cwd=ROOT, env=env)
    try:
        async with aiohttp.ClientSession() as s:
            async def get(p, **kw):
                async with s.get(BASE + p, **kw) as r:
                    return r.status, (await r.json() if "json" in r.headers.get("Content-Type", "") else await r.read())

            async def post(p, body=None):
                async with s.post(BASE + p, json=body or {}) as r:
                    return await r.json()

            async def connected():
                try:
                    st, j = await get("/api/state")
                    return j if st == 200 and j["connected"] and j["attributes"] and j["status"] else None
                except aiohttp.ClientError:
                    return None
            st = await wait_for(connected)
            check("app connects, learns mainboard id, gets attributes+status", st and st["mainboard_id"] == "000000000001d354")
            if not st:
                return
            check("attributes parsed", st["attributes"]["MachineName"].startswith("Saturn 4 Ultra"))

            # files
            _, j = await get("/api/files?path=/local/")
            names = [f["name"] for f in j["files"]]
            check("file list (local)", "benchy_0.05.ctb" in names and "minis" in names, names)
            f = next(f for f in j["files"] if f["name"] == "benchy_0.05.ctb")
            if minimal:
                check("minimal firmware: no metadata but no crash", f["layers"] is None and f["layer_height"] is None and f["est_seconds"] is None)
            else:
                check("file metadata extracted (layer height/layers/time/thumb)",
                      f["layer_height"] == 0.05 and f["layers"] == 1840 and f["est_seconds"] == 13200 and f["thumbnail"], f)
                st_, body = await get("/api/fetch", params={"u": f["thumbnail"]})
                check("thumbnail proxy returns PNG", st_ == 200 and body[:4] == b"\x89PNG")
            check("storage usage reported", j["storage"] and j["storage"]["total"] > 0)
            _, jj = await get("/api/files?path=/local/minis/")
            check("subfolder listing", [x["name"] for x in jj["files"]] == ["orc_bust.ctb"], jj)
            _, ju = await get("/api/files?path=/usb/")
            check("usb listing", [x["name"] for x in ju["files"]] == ["usb_test.ctb"])
            st_, _ = await get("/api/fetch", params={"u": "http://example.com/x"})
            check("fetch proxy refuses non-printer hosts", st_ == 403)

            # upload
            payload = os.urandom(2_500_000)  # 3 chunks
            fd = aiohttp.FormData()
            fd.add_field("print_after", "0")
            fd.add_field("file", payload, filename="uploaded_test.ctb")
            async with s.post(BASE + "/api/upload", data=fd) as r:
                up = await r.json()
            check("upload accepted", up.get("ok") and up["md5"] == hashlib.md5(payload).hexdigest(), up)

            async def up_done():
                _, x = await get("/api/state")
                return x if x["upload"] and x["upload"]["phase"] in ("done", "error") else None
            x = await wait_for(up_done, 20)
            check("upload completes (3x1MB chunks, md5 verified by printer)", x and x["upload"]["phase"] == "done" and x["upload"]["sent"] == len(payload), x and x["upload"])
            _, j = await get("/api/files?path=/local/")
            check("uploaded file shows in list", "uploaded_test.ctb" in [f["name"] for f in j["files"]])

            # print control
            r = await post("/api/print", {"filename": "/local/missing.ctb"})
            check("print of missing file -> clear error", not r["ok"] and "not found" in r["message"].lower(), r)
            r = await post("/api/print", {"filename": "/local/calibration_cube.ctb"})
            check("start print", r["ok"], r)

            async def printing():
                _, x = await get("/api/state")
                return x if x["machine_status_codes"] == [1] and x["status"]["PrintInfo"]["CurrentLayer"] > 1 else None
            x = await wait_for(printing)
            check("state shows printing + layer progress", x and x["progress"] > 0 and x["remaining_ms"] is not None, x and x["status"])
            r = await post("/api/print", {"filename": "/local/benchy_0.05.ctb"})
            check("second print rejected as busy", not r["ok"] and "busy" in r["message"].lower(), r)
            r = await post("/api/pause")
            check("pause", r["ok"])
            x = await wait_for(lambda: _state_is(get, 6))
            check("state shows paused", x)
            r = await post("/api/resume")
            check("resume", r["ok"])
            r = await post("/api/stop")
            check("stop", r["ok"])
            x = await wait_for(lambda: _state_idle(get))
            check("returns to idle after stop", x)
            for p in ("skip-preheat", "stop-feeding"):
                check(p, (await post("/api/" + p))["ok"])

            # history
            _, h = await get("/api/history")
            check("history lists tasks (newest first)", h["ok"] and len(h["items"]) >= 4 and h["items"][0]["begin"] >= h["items"][-1]["begin"], h)
            check("history item fields", h["items"][-1]["status_text"] in ("Completed", "Stopped", "Error"))

            # misc settings
            check("rename", (await post("/api/rename", {"name": "Test Saturn"}))["ok"])
            x = await wait_for(lambda: _attr_name(get, "Test Saturn"))
            check("rename reflected in attributes", x)
            check("timelapse toggle", (await post("/api/timelapse", {"enable": True}))["ok"])
            r = await post("/api/light", {"on": True})
            check("light on (assumed Cmd 403)", r["ok"] and r["light"] is True, r)
            r = await post("/api/raw", {"cmd": 0, "data": {}})
            check("raw command", r["ok"])
            r = await post("/api/discover")
            check("discover endpoint responds", r["ok"])

            # camera
            st_, snap = await get("/snapshot.jpg")
            check("snapshot via MJPEG proxy", st_ == 200 and snap[:2] == b"\xff\xd8", snap[:80])
            async with s.get(BASE + "/video") as r:
                ok_ct = "multipart" in r.headers.get("Content-Type", "")
                chunk = await r.content.read(4096)
            check("live MJPEG proxy alongside a snapshot (single upstream stream shared)", ok_ct and b"--frame" in chunk, chunk[:60])
            async with s.get(BASE + "/video") as r2:
                c2 = await r2.content.read(2000)
            check("second concurrent viewer works (printer allows only 1 stream)", b"--frame" in c2, c2[:60])

            # history-derived file info (works even when firmware gives no metadata)
            _, j = await get("/api/files?path=/local/")
            bf = next(f for f in j["files"] if f["name"] == "benchy_0.05.ctb")
            check("file shows last printed time from history", bf.get("last_printed") and bf.get("print_count", 0) >= 1, bf.get("last_printed"))
            check("file thumbnail falls back to history thumbnail", bf["thumbnail"])

            # other clients' traffic shows in console
            import json as _json
            async with s.ws_connect(BASE + "/ws") as bws:
                async with s.ws_connect(f"http://127.0.0.1:{PORTS['printer']}/websocket") as other:
                    await other.send_str(_json.dumps({"Id": "x", "Data": {"Cmd": 387, "Data": {"Enable": 0}, "RequestID": "abc123", "MainboardID": "000000000001d354", "TimeStamp": 1, "From": 3}, "Topic": "sdcp/request/000000000001d354"}))
                    seen = None
                    for _ in range(40):
                        m = await asyncio.wait_for(bws.receive_json(), 5)
                        if m["type"] == "log" and any(e["kind"] == "other" and "Cmd 387" in e["text"] for e in m["entries"]):
                            seen = m
                            break
                    check("console shows commands answered to OTHER clients", seen)
            r = await post("/api/settings", {"printer_ip": "not an ip"})
            check("settings rejects invalid IP", not r["ok"])
            r = await post("/api/settings", {"printer_ip": "127.0.0.1"})
            check("settings saves IP to config.json", r["ok"] and '"printer_ip": "127.0.0.1"' in open(cfgfile).read() and '"open_browser": false' in open(cfgfile).read())

            # explorer + probe
            _, ex = await get("/api/explore?path=/local/")
            check("explorer returns raw printer reply", ex["ok"] and ex["reply"]["FileList"] and "Ack" in ex["reply"], ex)
            _, ex = await get("/api/explore?path=/nonexistent/")
            check("explorer reports refused path", not ex["ok"] and ex["reply"]["Ack"] == 3, ex)
            _, pr = await get("/api/probe?port=%d&path=/thumb/x.png" % PORTS["printer"])
            check("http probe shows status + hex of first bytes", pr["status"] in (200, 206) and pr["hex"].startswith("89 50 4e 47"), pr)
            _, pr2 = await get("/api/probe?port=%d&path=/websocket" % PORTS["video"])
            check("http probe handles non-200 gracefully", pr2["ok"] and pr2["status"] == 404, pr2)
            _, pr2 = await get("/api/probe?port=%d&path=/websocket" % PORTS["video"])
            check("http probe handles non-200 gracefully", pr2["ok"] and pr2["status"] == 404, pr2)

            # delete
            r = await post("/api/files/delete", {"files": ["/local/uploaded_test.ctb", "/local/nope.ctb"], "folders": []})
            check("delete reports failures", r["ok"] and r["failed"] == ["/local/nope.ctb"], r)
            _, j = await get("/api/files?path=/local/")
            check("deleted file gone", "uploaded_test.ctb" not in [f["name"] for f in j["files"]])

            # live websocket
            async with s.ws_connect(BASE + "/ws") as ws:
                m = await asyncio.wait_for(ws.receive_json(), 5)
                check("browser websocket pushes state", m["type"] == "state" and m["state"]["connected"])
                m2 = await asyncio.wait_for(ws.receive_json(), 5)
                texts = [e["text"] for e in m2.get("entries", [])]
                check("console log history sent on connect (commands + replies)", m2["type"] == "log_init" and any(t.startswith("Cmd 258") and "->" not in t for t in texts) and any("-> OK" in t for t in texts), texts[:5])
                await post("/api/pause")
                m3 = await asyncio.wait_for(ws.receive_json(), 5)
                while m3["type"] != "log":
                    m3 = await asyncio.wait_for(ws.receive_json(), 5)
                check("live log entries stream to browser", any("Cmd 129" in e["text"] for e in m3["entries"]), m3)

            # reconnect after printer dies
            mock.terminate(); mock.wait()
            x = await wait_for(lambda: _disc(get), 15)
            check("detects printer going offline", x)
            r = await post("/api/pause")
            check("commands fail cleanly while offline", not r["ok"], r)
    finally:
        app.terminate(); mock.terminate()
    bad = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(bad)}/{len(results)} passed")
    return 1 if bad else 0


async def _state_is(get, code):
    _, x = await get("/api/state")
    return x if x["print_status_code"] == code else None


async def _state_idle(get):
    _, x = await get("/api/state")
    return x if x["machine_status_codes"] == [0] else None


async def _attr_name(get, name):
    _, x = await get("/api/state")
    return x if x["attributes"].get("Name") == name else None


async def _disc(get):
    _, x = await get("/api/state")
    return x if not x["connected"] else None


if __name__ == "__main__":
    sys.exit(asyncio.run(main(minimal="--minimal" in sys.argv)))
