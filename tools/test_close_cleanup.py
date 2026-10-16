"""Check that the shutdown path used by the Windows console-close handler frees the printer's camera slot.

    python tools/test_close_cleanup.py

Starts the mock printer, starts the app in-process, opens the camera, then triggers the same graceful exit the
Windows handler uses (the ctypes part itself only runs on Windows) and checks the slot is released.
"""
import asyncio
import os
import subprocess
import sys
import threading
import time
import urllib.request
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
P = dict(printer=14030, video=14031, udp=14000, web=18090)


def mock_state():
    req = urllib.request.Request(f"http://127.0.0.1:{P['printer']}/_mock/state", data=b"{}", headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=5).read())


def main():
    cfgfile = ROOT / ".test_close_cfg.json"
    cfgfile.write_text('{"open_browser": false, "auto_exit": false}')
    os.environ.update(SATURN_IP="127.0.0.1", SATURN_PORT=str(P["printer"]), SATURN_VIDEO_PORT=str(P["video"]),
                      SATURN_UDP_PORT=str(P["udp"]), SATURN_WEB_PORT=str(P["web"]), SATURN_CONFIG=str(cfgfile))
    mock = subprocess.Popen([sys.executable, str(ROOT / "tools/mock_printer.py"), "--port", str(P["printer"]),
                             "--video-port", str(P["video"]), "--udp-port", str(P["udp"]), "--idle-kill", "30"],
                            stdout=subprocess.DEVNULL)
    time.sleep(1.5)
    from saturn_control import server
    result = {}

    def driver():
        try:
            time.sleep(3)
            urllib.request.urlopen(f"http://127.0.0.1:{P['web']}/snapshot.jpg", timeout=15).read()
            result["during"] = mock_state()["streams"]
            server.LOOP["loop"].call_soon_threadsafe(server._graceful_exit)     # what the Windows handler does
            result["cleaned"] = server.CLEANED.wait(8)
            time.sleep(0.3)
            result["after"] = mock_state()["streams"]
        except Exception as e:  # noqa: BLE001
            result["error"] = repr(e)
            server.LOOP["loop"].call_soon_threadsafe(server._graceful_exit)
    th = threading.Thread(target=driver, daemon=True)
    th.start()
    try:
        server.web.run_app(server.create_app(server.load_config()), host="127.0.0.1", port=P["web"], print=None)
    finally:
        th.join(15)
        mock.terminate()
        cfgfile.unlink(missing_ok=True)
    ok = result.get("during") == 1 and result.get("cleaned") and result.get("after") == 0
    print(("PASS" if ok else "FAIL"), "camera slot freed by the close-window shutdown path", result)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
