"""End-to-end test: starts the mock printer + the app on spare ports and drives the HTTP API.

    python tools/test_e2e.py
"""
import asyncio
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import aiohttp
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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
    open(cfgfile, "w").write('{"open_browser": false, "auto_exit": false, "keepalive_seconds": 1.5, "keepalive_timeout": 2}')
    env = dict(os.environ, SATURN_IP="127.0.0.1", SATURN_PORT=str(PORTS["printer"]),
               SATURN_VIDEO_PORT=str(PORTS["video"]), SATURN_UDP_PORT=str(PORTS["udp"]),
               SATURN_WEB_PORT=str(PORTS["web"]), SATURN_CONFIG=cfgfile)
    mock_args = [sys.executable, str(ROOT / "tools/mock_printer.py"), "--port", str(PORTS["printer"]),
                 "--video-port", str(PORTS["video"]), "--udp-port", str(PORTS["udp"]), "--idle-kill", "4"]
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

            async def post_raw(p, body):
                async with s.post(BASE.replace(str(PORTS["web"]), str(PORTS["printer"])) + p, json=body) as r:
                    return r.status, await r.json()

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
            frag = [f for f in j["files"] if f.get("fragment")]
            check("orphan upload fragment is flagged", len(frag) == 1 and frag[0]["name"].endswith("_broken.goo"), frag)
            check("normal files are not flagged as fragments", not any(f.get("fragment") for f in j["files"] if f["name"] == "benchy_0.05.ctb"))
            f = next(f for f in j["files"] if f["name"] == "benchy_0.05.ctb")
            if minimal:
                check("minimal firmware: file list still works (metadata may come from file_info.txt)", f["name"] == "benchy_0.05.ctb")
            else:
                check("file metadata extracted (layer height/layers/time/thumb)",
                      f["layer_height"] == 0.05 and f["layers"] == 1840 and f["est_seconds"] == 13200 and f["thumbnail"], f)
                st_, body = await get("/api/fetch", params={"u": f["thumbnail"]})
                check("thumbnail proxy returns PNG", st_ == 200 and body[:4] == b"\x89PNG")
            check("storage usage reported", j["storage"] and j["storage"]["total"] > 0)
            check("status uses real key names (vat temp)", st["status"].get("TempOfTank") == 24 and st["status"].get("TempTargetTank") == 30, st["status"])
            check("attributes: camera slots reported 0/2", st["camera"] and st["camera"]["max"] == 2, st.get("camera"))
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

            # upload name with spaces: must reach the printer as plain text, not %20
            payload2 = os.urandom(5000)
            from saturn_control.printer import multipart_body
            body2, ctype2 = multipart_body([("print_after", "0")], "file", "Arthas 15cm Base.goo", payload2)
            async with s.post(BASE + "/api/upload", data=body2, headers={"Content-Type": ctype2}) as r:
                up2 = await r.json()
            await asyncio.sleep(0.5)
            await wait_for(up_done, 20)
            await asyncio.sleep(1.5)
            _, ctl = await post_raw("/_mock/state", {})
            check("upload with spaces: printer receives the plain name (no %20)", ctl["last_upload_name"] == "Arthas 15cm Base.goo", ctl["last_upload_name"])
            _, j = await get("/api/files?path=/local/")
            check("spaced name listed without % codes", any(f["name"] == "Arthas 15cm Base.goo" and not f.get("percent_codes") for f in j["files"]))

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
            _, ctl = await post_raw("/_mock/state", {})
            check("light on via Cmd 151 {ControlState:1}", r["ok"] and ctl["light"] == 1 and r["results"][0]["variant"] == "plain", r)
            r = await post("/api/light", {"on": False})
            _, ctl = await post_raw("/_mock/state", {})
            check("light off via Cmd 151 {ControlState:0}", r["ok"] and ctl["light"] == 0, r)
            r = await post("/api/raw", {"text": json.dumps({"Id": "", "Data": {"Cmd": 0, "Data": {}, "RequestID": "rawtest1", "MainboardID": "000000000001d354", "TimeStamp": 1, "From": 0}, "Topic": "sdcp/request/000000000001d354"})})
            check("raw JSON envelope: sent and answered", r["ok"] and r["reply"] is not None, r)
            weird = '{"Cmd":151,"Belong":"LCD","Data":{"ControlState":1}}'
            r = await post("/api/raw", {"text": weird})
            _, ctl = await post_raw("/_mock/state", {})
            check("raw box sends exactly what was typed (byte for byte, even odd shapes)", weird in ctl["rx"], ctl["rx"][-3:])
            r = await post("/api/raw", {"text": "ping"})
            _, ctl = await post_raw("/_mock/state", {})
            check("raw box sends non-JSON text unchanged and does not refuse", r["ok"] and "ping" in ctl["rx"], r)
            r = await post("/api/print-params", {"params": {"NormalExposureTime": 1700}})
            check("print params while idle: printer refuses cleanly (Ack 1)", not r["ok"] and r["results"][0]["ack"] == 1, r)
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

            # HTTP explorer
            _, hl = await get("/api/http/list?path=/media/mmcblk0p3/")
            hn = {e["name"]: e for e in hl["entries"]}
            check("http listing parsed (names, sizes, folders)", hl["ok"] and hn["minis"]["is_dir"] and hn["benchy_0.05.ctb"]["size"] == 2048 and hn["benchy_0.05.ctb"]["mtime"] > 1_000_000_000, hl)
            _, hr = await get("/api/http/list?path=/")
            check("http root listing", hr["ok"] and {"media", "proc"} <= {e["name"] for e in hr["entries"]}, hr)
            check("virtual /proc folder is flagged", (await get("/api/http/list?path=/proc/"))[1]["virtual"] is True)
            st_, _ = await get("/api/fetch", params={"u": "/proc/kmsg"})
            check("reading virtual files is blocked", st_ == 403)
            st_, body = await get("/api/fetch", params={"u": "/media/mmcblk0p3/benchy_0.05.ctb", "download": "1"})
            check("download through the app", st_ == 200 and len(body) == 2048)
            st_, body = await get("/api/fetch", params={"u": "/media/mmcblk0p1/file_info/file_info.txt"})
            check("file_info.txt readable through the app", st_ == 200 and b'"files"' in body)
            _, bad = await get("/api/http/list?path=/nonexistent/")
            check("missing folder reports an error", not bad["ok"], bad)
            _, j = await get("/api/files?path=/local/")
            bf2 = next(f for f in j["files"] if f["name"] == "dragon_miniature.ctb")
            check("Files tab filled from file_info.txt (layers, thumbnail)", bf2["layers"] == 3120 and bf2["info_source"] == "file_info" and bf2["thumbnail"].endswith("_goo.bmp"), bf2)
            st_, bmp = await get("/api/fetch", params={"u": bf2["thumbnail"]})
            check("BMP thumbnail served as image", st_ == 200)
            # unit checks on the parsers
            sys.path.insert(0, str(ROOT))
            from saturn_control import httpfs
            fi = httpfs.parse_file_info('{"files":[{"file_name":"a.goo","est_time":0,"preview_image_path":"unknow"},{"file_name":"a.goo","est_time":99,"total_layers":5,"layer_height":0.05,"preview_image_path":"/x.bmp"},{"file_name":"a.goo","est_time":0,"preview_image_path":"unknow"}]}')
            check("file_info duplicates collapse to the complete entry", fi["a.goo"]["est_seconds"] == 99 and fi["a.goo"]["thumbnail"] == "/x.bmp", fi)
            mg = httpfs.parse_listing('<table><tr><th>Name</th><th>Modified</th><th>Size</th></tr><tr><td><a href="a%20b.goo">a b.goo</a></td><td>1700000000</td><td>1234</td></tr><tr><td><a href="d/">d/</a></td><td>5</td><td>[DIR]</td></tr></table>')
            check("listing parser: epoch/size/dir/unknown-time", mg[0]["is_dir"] and mg[0]["mtime"] is None and mg[1]["size"] == 1234 and mg[1]["mtime"] == 1700000000, mg)

            # camera slots + release
            r = await post("/api/camera/release")
            check("camera release endpoint", r["ok"], r)

            # keep-alive: with --idle-kill 4 the mock drops silent clients; app must stay connected
            _, s0 = await get("/api/state")
            await asyncio.sleep(9)
            _, s1 = await get("/api/state")
            _, ctl = await post_raw("/_mock/state", {})
            check("keep-alive prevents idle drops (no reconnects in 9 s of silence)", s1["connected"] and s1["reconnects"] == s0["reconnects"] and ctl["idle_kills"] == 0, (s0["reconnects"], s1["reconnects"], ctl["idle_kills"]))

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
