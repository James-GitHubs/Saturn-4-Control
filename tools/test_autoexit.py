"""The app should quit by itself a few seconds after the last browser tab closes.

    python tools/test_autoexit.py
"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parent.parent
P = dict(printer=13130, video=13131, udp=13100, web=18180)


async def main():
    cfgfile = os.path.join(tempfile.mkdtemp(), "config.json")
    json.dump({"open_browser": False, "auto_exit": True, "auto_exit_seconds": 2}, open(cfgfile, "w"))
    env = dict(os.environ, SATURN_IP="127.0.0.1", SATURN_PORT=str(P["printer"]), SATURN_VIDEO_PORT=str(P["video"]),
               SATURN_UDP_PORT=str(P["udp"]), SATURN_WEB_PORT=str(P["web"]), SATURN_CONFIG=cfgfile)
    mock = subprocess.Popen([sys.executable, str(ROOT / "tools/mock_printer.py"), "--port", str(P["printer"]),
                             "--video-port", str(P["video"]), "--udp-port", str(P["udp"])])
    await asyncio.sleep(1.5)
    app = subprocess.Popen([sys.executable, "-m", "saturn_control"], cwd=ROOT, env=env)
    ok = True
    try:
        await asyncio.sleep(3)
        alive = app.poll() is None
        print(("PASS" if alive else "FAIL") + " does not exit before any browser has connected")
        ok &= alive
        async with aiohttp.ClientSession() as s:
            ws = await s.ws_connect(f"http://127.0.0.1:{P['web']}/ws")
            await asyncio.sleep(4)
            alive = app.poll() is None
            print(("PASS" if alive else "FAIL") + " stays up while a tab is open")
            ok &= alive
            await ws.close()                       # tab closed...
            ws = await s.ws_connect(f"http://127.0.0.1:{P['web']}/ws")   # ...and reloaded within grace period
            await asyncio.sleep(3.5)
            alive = app.poll() is None
            print(("PASS" if alive else "FAIL") + " page reload does not quit the app")
            ok &= alive
            await ws.close()
        t0 = time.time()
        while app.poll() is None and time.time() - t0 < 10:
            await asyncio.sleep(0.2)
        exited = app.poll() is not None
        print(("PASS" if exited and app.returncode == 0 else "FAIL") + f" exits cleanly after last tab closes (rc={app.returncode}, {time.time() - t0:.1f}s)")
        ok &= exited and app.returncode == 0
    finally:
        app.terminate()
        mock.terminate()
    return 0 if ok else 1


sys.exit(asyncio.run(main()))
