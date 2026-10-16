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
                 "--video-port", str(PORTS["video"]), "--udp-port", str(PORTS["udp"]), "--idle-kill", "4",
                 "--host", "0.0.0.0"]      # 0.0.0.0 so the multi-printer test can reach it as a second address (127.0.0.2)
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

            # render upload (local folder of .goo files)
            rdir = os.path.join(tempfile.mkdtemp(), "Rendered Prints")
            os.makedirs(os.path.join(rdir, "Arthas", "parts"))
            gpay = os.urandom(300_000)
            open(os.path.join(rdir, "Arthas", "parts", "Arthas Base.goo"), "wb").write(gpay)
            open(os.path.join(rdir, "Arthas", "notes.txt"), "w").write("not a goo")
            open(os.path.join(rdir, "top.goo"), "wb").write(os.urandom(1000))
            st_, rl = await get("/api/render/list")
            check("render folder blank by default -> asks to set one", st_ == 400 and rl.get("no_folder"), rl)
            _, c = await get("/api/config")
            check("config reports blank render folder by default", c["render_folder"] == "", c)
            async with s.get(BASE + "/static/app.js") as rr:
                check("static files are served with Cache-Control: no-cache", rr.headers.get("Cache-Control") == "no-cache", dict(rr.headers))
            r = await post("/api/settings", {"render_folder": rdir})
            check("settings saves render folder", r["ok"] and r["render_folder"] == rdir and json.load(open(cfgfile))["render_folder"] == rdir, r)
            _, c = await get("/api/config")
            check("config reports render folder", c["render_folder"] == rdir)
            _, rl = await get("/api/render/list")
            check("render root lists folder + .goo only", sorted(e["name"] for e in rl["entries"]) == ["Arthas", "top.goo"], rl)
            _, rl = await get("/api/render/list?path=Arthas")
            check("render subfolder hides non-.goo files", [e["name"] for e in rl["entries"]] == ["parts"], rl)
            st_, rl = await get("/api/render/list?path=../")
            check("render list refuses path escape", st_ == 403, rl)
            st_, rl = await get("/api/render/list?path=Arthas/../../")
            check("render list refuses nested escape", st_ == 403, rl)
            r = await post("/api/render/upload", {"path": "../x.goo"})
            check("render upload refuses path escape", not r["ok"], r)
            r = await post("/api/render/upload", {"path": "Arthas/notes.txt"})
            check("render upload refuses non-.goo", not r["ok"], r)
            r = await post("/api/render/upload", {"path": "Arthas/parts/Arthas Base.goo"})
            check("render upload accepted", r["ok"] and r["size"] == len(gpay), r)
            await asyncio.sleep(0.5)
            x = await wait_for(up_done, 20)
            check("render upload completes", x and x["upload"]["phase"] == "done" and x["upload"]["name"] == "Arthas Base.goo", x and x["upload"])
            _, ctl = await post_raw("/_mock/state", {})
            check("render upload reaches printer under plain file name", ctl["last_upload_name"] == "Arthas Base.goo", ctl["last_upload_name"])
            _, pf = await get("/api/render/printer-files")
            have = {(f["name"], f["size"]) for f in pf["files"]}
            check("printer-files lists the uploaded render with its size", ("Arthas Base.goo", len(gpay)) in have, sorted(have)[:8])
            check("printer-files has no entry for a render that was never uploaded", ("top.goo", 1000) not in have)

            # queue: files wait their turn, can be removed, speeds and the free-space check
            _, sp = await get("/api/files?path=/local/")
            r = await post("/api/upload/check", {"sizes": [1000]})
            check("space check answers", r["ok"] and "known" in r, r)
            if r.get("known"):
                r2 = await post("/api/upload/check", {"sizes": [r["free"] + 1]})
                check("space check flags a too-big upload", r2["over"] and r2["after"] == -1, r2)
                check("space check passes a small upload", not r["over"], r)
            bigs = []
            for i in range(3):
                data = os.urandom(25_000_000 if i == 0 else 300_000)
                fd = aiohttp.FormData(); fd.add_field("print_after", "0"); fd.add_field("file", data, filename=f"queue_{i}.ctb")
                async with s.post(BASE + "/api/upload", data=fd) as rr:
                    bigs.append(await rr.json())
            check("three uploads accepted", all(b.get("ok") for b in bigs), bigs)
            _, x = await get("/api/state")
            qn = [q["name"] for q in x["queue"]]
            check("second and third wait in the queue", qn == ["queue_1.ctb", "queue_2.ctb"], qn)
            check("first one is sending", x["upload"]["name"] == "queue_0.ctb", x["upload"])
            r = await post("/api/upload/queue/remove", {"id": x["queue"][1]["id"]})
            check("queued file can be removed", r["ok"] and r["removed"] == 1, r)
            seen = {}

            async def queue_done():
                _, y = await get("/api/state")
                u = y["upload"]
                if u and u["name"] == "queue_0.ctb" and u.get("speed_avg") and u["phase"] == "sending":
                    seen["avg"], seen["now"] = u["speed_avg"], u.get("speed_now")
                return y if u and u["name"] == "queue_1.ctb" and u["phase"] in ("done", "error") else None
            y = await wait_for(queue_done, 30)
            check("queue runs the next file after the first", y and y["upload"]["phase"] == "done" and y["queue"] == [], y and (y["upload"], y["queue"]))
            _, j = await get("/api/files?path=/local/")
            names = [f["name"] for f in j["files"]]
            check("queued files reached the printer; removed one did not", "queue_0.ctb" in names and "queue_1.ctb" in names and "queue_2.ctb" not in names, names)
            check("upload reports speeds", y["upload"].get("speed_avg") and y["upload"]["speed_avg"] > 0, y["upload"])

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
            r = await post("/api/print-params", {"params": {"NormalExposureTime": 1700}})
            check("print tweak during a print: accepted", r["ok"] and r["results"][0]["ack"] == 0, r)
            _, x = await get("/api/state")
            check("current tweak value shown from what was set", x["print_params"].get("NormalExposureTime") == 1700, x["print_params"])
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

            # multiple printers
            async def scanned():
                _, cc = await get("/api/config")
                return cc if cc["printers"] else None
            c = await wait_for(scanned, 10)
            first = c["printers"][0]
            check("startup scan added the printer that answered (named by the printer)", first["name"] == "Saturn 4 Ultra (mock)" and first["ip"] == "127.0.0.1", c["printers"])
            _, st_ = await get("/api/state")
            check("state carries the active printer's saved name", st_["printer_name"] == "Saturn 4 Ultra (mock)", st_.get("printer_name"))
            r = await post("/api/discover")
            check("scan lists every printer that answered", r["ok"] and [p["MainboardIP"] for p in r["printers"]] == ["127.0.0.1"], r)
            check("config lists the active printer with all four ports", [x["ip"] for x in c["printers"]] == ["127.0.0.1"] and c["active_printer"] == first["id"]
                  and (first["video_port"], first["udp_port"], first["web_port"]) == (PORTS["video"], PORTS["udp"], PORTS["web"]) and first["printer_port"] == PORTS["printer"], c["printers"])
            r = await post("/api/printers", {"printers": [{**first, "name": "Resin 1"}, {"name": "Resin 2", "ip": "127.0.0.2", "printer_port": PORTS["printer"], "video_port": PORTS["video"], "udp_port": PORTS["udp"]}]})
            two = next((x for x in r.get("printers", []) if x["name"] == "Resin 2"), {})
            check("save printer list (ids and default ports filled in)", r["ok"] and not r["switched"] and two.get("id") and two.get("web_port") == 8080, r)
            r = await post("/api/printers", {"printers": [{**first}, {**two, "name": ""}]})
            check("a printer added without a name gets the name it reports", r["ok"] and next(x for x in r["printers"] if x["ip"] == "127.0.0.2")["name"] == "Saturn 4 Ultra (mock)", r)
            r = await post("/api/printers", {"printers": [{**first, "name": "Resin 1"}, {**two}]})
            saved = json.load(open(cfgfile))
            check("printer list persisted in config.json with all ports", [x["ip"] for x in saved["printers"]] == ["127.0.0.1", "127.0.0.2"] and set(saved["printers"][1]) >= {"id", "name", "ip", "printer_port", "video_port", "udp_port", "web_port"}, saved.get("printers"))
            r = await post("/api/printers", {"printers": [{"name": "x", "ip": "not-an-ip"}]})
            check("printer list rejects bad IP", not r["ok"], r)
            r = await post("/api/printers", {"printers": [{**first}, {"name": "x", "ip": "127.0.0.3", "printer_port": 99999}]})
            check("printer list rejects bad port", not r["ok"], r)
            r = await post("/api/printers", {"printers": [{**first, "name": "Resin 1"}, {**two}, {"name": "Duplicate", "ip": "127.0.0.2", "printer_port": PORTS["printer"]}]})
            check("duplicate IP addresses are allowed", r["ok"] and [x["ip"] for x in r["printers"]].count("127.0.0.2") == 2 and len({x["id"] for x in r["printers"]}) == 3, r)
            r = await post("/api/printers/select", {"id": "nope"})
            check("select refuses an unknown printer", not r["ok"], r)
            r = await post("/api/printers/select", {"id": two["id"]})
            check("select another printer", r["ok"] and r["active_printer"] == two["id"], r)

            async def on_two():
                _, x = await get("/api/state")
                return x if x["printer_ip"] == "127.0.0.2" and x["connected"] and x["attributes"] else None
            check("app connects to the newly selected printer", await wait_for(on_two, 15))
            check("selection persisted in config.json", json.load(open(cfgfile))["active_printer"] == two["id"] and json.load(open(cfgfile))["printer_ip"] == "127.0.0.2")
            _, c = await get("/api/config")
            r = await post("/api/printers", {"printers": [x for x in c["printers"] if x["id"] == first["id"]]})
            check("removing the active printer switches to the first one left", r["ok"] and r["switched"] and r["active_printer"] == first["id"], r)

            async def on_one():
                _, x = await get("/api/state")
                return x if x["printer_ip"] == "127.0.0.1" and x["connected"] and x["attributes"] else None
            check("app reconnects to the remaining printer", await wait_for(on_one, 15))
            # delete every printer, then add one again
            r = await post("/api/printers", {"printers": []})
            check("every printer can be deleted", r["ok"] and r["printers"] == [] and r["active_printer"] == "" and r["switched"], r)
            saved = json.load(open(cfgfile))
            check("empty list is remembered in config.json", saved["printers"] == [] and saved["printers_configured"] is True and saved["printer_ip"] == "")

            async def idle():
                _, x = await get("/api/state")
                return x if not x["connected"] and x["printer_ip"] == "" else None
            check("with no printers the app disconnects and idles", await wait_for(idle, 10))
            r = await post("/api/settings", {"render_folder": rdir})
            check("settings (render folder) still save with no printer", r["ok"], r)
            from saturn_control.config import load_config
            check("config loads as 'no printer' after everything was deleted", load_config(cfgfile)["printer_ip"] == "" and load_config(cfgfile)["printers"] == [])
            r = await post("/api/printers", {"printers": [{**first}]})
            check("adding a printer again reconnects", r["ok"] and r["switched"] and r["active_printer"] == first["id"], r)
            check("app connects to the re-added printer", await wait_for(on_one, 15))
            r = await get("/")
            check("page links carry a version so updates are never hidden by the cache", b"/static/app.js?v=" in r[1] and b"/static/style.css?v=" in r[1])

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
