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
- `light_enabled` - `false` (default) hides the header light button. The Printer tab always has an experimental light test.
- `render_folder` (`E:\Rendered Prints`) - folder on the PC running the app that the **Render Upload** tab browses. Also editable from the gear icon (Settings).
- `camera_quality` (2) - ffmpeg JPEG quality for the RTSP camera, 2 = best, 31 = worst.
- `keepalive_seconds` (12) / `keepalive_timeout` (8) - how often the app polls the printer (Cmd 0) to keep the websocket from being dropped; 2 missed polls reconnect.

## Features
Live camera (+snapshot, fullscreen), print progress/layer/time/ETA, pause/resume/stop, skip preheat, stop feeding,
file browser (internal + USB, folders, thumbnails, layer height, layers, est. time, storage bar, filter),
print from file (with start layer), multi-select delete, refresh, upload (button or drag-and-drop, optional
print-after-upload, progress, cancel), print history with thumbnails/status/time-lapse links, rename printer,
time-lapse switch, light, hardware self-check, printer info, printer error/notice messages, UDP discovery,
and a Fluidd-style Console under the page: a live, timestamped log of every command sent to the printer and every reply/message back (filters, raw JSON, copy/save), with a box that sends exactly what you type (complete JSON message or plain text, nothing parsed or blocked), plus templates and a Fill-envelope helper.

Also: an **Explorer** tab (read-only tree view of the printer's own web server on port 3030: back/forward/up/refresh, sortable, local-time dates, KB/MB sizes, view text/images, download through the app), Files tab details filled from the printer's `file_info.txt`, a badge for files uploaded with `%20` in the name, a toggle to show/delete incomplete upload fragments, vat temperature and film use, and camera slot count with a Release button.

Also: a **Render Upload** tab (same look as Explorer, but it browses a folder on the PC running the app: a tree of subfolders with the `.goo` files inside). Click **Upload** to the right of a file name, or double-click the row, confirm the file name in the popup, and it is sent straight to the printer with the same progress/cancel as the Files tab. It can only see inside the configured render folder.

## Changing the printer IP / render folder
Click the gear icon (top right), enter the new IP and/or the Render Upload folder, Save. Both are written to `config.json` and take effect immediately.

## Things that depend on your printer's firmware
- **File layer height / layers / time / thumbnail**: the public SDCP spec only returns name, size and type in the
  file list. The app displays those extra fields if your firmware sends them (it matches many likely field names).
  If they show "-", scroll the Console (tick "raw JSON"), find the `258` response, and send it to Claude so the field names can be matched.
- **Light and live print tweaks (experimental)**: the Matrix app sends Cmd 151 `{"ControlState":1|0}` (light) and Cmd 135 (one exposure/bottom-layer key per message, exposure in ms) through Elegoo's cloud. Whether the printer accepts them on the local websocket is **unproven**; the Printer tab tries them and shows the printer's answer. If a light variant is acknowledged, set `light_enabled` to `true`.
- **Uploads** go out with a plain file name (the old form encoding stored `%20` literally).
- **Camera**: uses the URL the printer returns (your Saturn gives RTSP; the app converts it with a bundled ffmpeg, installed automatically). Only a few clients may watch at once; the app shares one stream between
  all browser tabs, but close Lychee/the phone app camera if you see a "maximum streams" error.

## Developing / testing without a printer
`pip install -r requirements-dev.txt`, then `python tools/test_e2e.py` (starts `tools/mock_printer.py` and drives the app; add `--minimal` for a printer that only returns spec fields).
