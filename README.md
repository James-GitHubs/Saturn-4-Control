# Saturn Control

A local web UI (Fluidd-style) for an Elegoo Saturn 4 Ultra, using Elegoo's SDCP v3.0.0 network protocol.

## Run it (Windows)
1. Install Python 3.9+ from python.org (tick "Add python.exe to PATH") if you don't have it.
2. Double-click `start-saturn-control.bat` (right-click > Send to > Desktop for a shortcut).
   First run creates a `.venv` and installs `aiohttp`; later runs just launch. The browser opens at http://localhost:8080.

## Config (`config.json`)
- `printer_ip` - **the one place the printer IP lives** (192.168.0.189).
- `auto_exit` - `true` (default): the app quits by itself a few seconds after you close the browser tab (never mid-upload), and the launcher window closes. Set `false` to keep it running.
- `web_host` - `127.0.0.1` = this PC only. Use `0.0.0.0` to open it from your phone/other PCs on the LAN (no password, so LAN only).
- `web_port`, `printer_port` (3030), `video_port` (3031), `udp_port` (3000), `mainboard_id` (auto-detected).
- `light` - the command sent by the light button (see caveat below).

## Features
Live camera (+snapshot, fullscreen), print progress/layer/time/ETA, pause/resume/stop, skip preheat, stop feeding,
file browser (internal + USB, folders, thumbnails, layer height, layers, est. time, storage bar, filter),
print from file (with start layer), multi-select delete, refresh, upload (button or drag-and-drop, optional
print-after-upload, progress, cancel), print history with thumbnails/status/time-lapse links, rename printer,
time-lapse switch, light, hardware self-check, printer info, printer error/notice messages, UDP discovery,
and a Fluidd-style Console under the page: a live, timestamped log of every command sent to the printer and every reply/message back (filters, raw JSON, copy/save), with a box to send raw SDCP commands.

## Changing the printer IP
Click the gear icon (top right), enter the new IP, Save. It is written to `config.json` and takes effect immediately.

## Things that depend on your printer's firmware
- **File layer height / layers / time / thumbnail**: the public SDCP spec only returns name, size and type in the
  file list. The app displays those extra fields if your firmware sends them (it matches many likely field names).
  If they show "-", scroll the Console (tick "raw JSON"), find the `258` response, and send it to Claude so the field names can be matched.
- **Light**: not in the public spec. The button sends `Cmd 403 {"LightStatus":{"SecondLight":1|0}}` (what other
  Elegoo models use). If it does nothing, try other payloads from the Console box and put the working one in `config.json`.
- **Camera**: uses the URL the printer returns (your Saturn gives RTSP; the app converts it with a bundled ffmpeg, installed automatically). Only a few clients may watch at once; the app shares one stream between
  all browser tabs, but close Lychee/the phone app camera if you see a "maximum streams" error.

## Developing / testing without a printer
`pip install -r requirements-dev.txt`, then `python tools/test_e2e.py` (starts `tools/mock_printer.py` and drives the app; add `--minimal` for a printer that only returns spec fields).
