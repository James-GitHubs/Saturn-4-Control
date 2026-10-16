# Changelog

## Upload queue, free-space check, speeds (2026-10-12)
- **Queue**: while a file is uploading, the Upload buttons (Files and Render upload tabs, and double-click) become **＋ Queue**. Files wait under the current upload ("Up next"), one is sent at a time, each can be removed with ✕ and **Clear queue** empties the list. Cancel only stops the current file; the queue moves on. If an upload fails, the files still waiting are removed (the printer is probably offline or full). The app won't auto-quit or switch printers while files are queued.
- **Free space**: the upload dialog shows the printer's free space, what is already uploading/queued, the size of these files, and what is left afterwards. If it won't fit, the dialog warns and the button becomes **Force upload** (or **Force add to queue**); Cancel backs out. The figure comes from the printer's internal storage, so forcing is available if it is wrong.
- **Speed**: the upload box shows the average speed since the upload started, the current speed (last ~3 seconds) and the time left.
- **Render upload tab refreshes itself**: once a file is verified the "Uploaded" column is re-checked (immediately, then again a few seconds later, since the printer lists new files a moment late), so Yes appears without a manual refresh.
- Free space is read from the printer's file list, or from its own "free space" reading (the one in machine settings) when the file list doesn't report it.
- New endpoints `/api/upload/check` and `/api/upload/queue/remove`; `/api/upload` and `/api/render/upload` now queue instead of answering "already in progress". Render files are hashed when their turn comes.

## Printer names and UDP scan (2026-10-12)
- Each printer's name is stored in its own entry in `config.json` (`printers` → `name`) and only changes that printer. A printer added without a name gets the name it reports; you can overwrite it per printer. An entry that has no name yet takes the name the printer reports when it connects. The header shows the printer's own model name, then your name if it differs.
- Startup scan: when no printers are saved, the app scans the network (UDP) on start and adds every printer that answers; the first one becomes active. Printers already saved are never replaced.
- The Printer tab's **Scan (UDP)** now lists every printer found, one per row (`Name @ IP (firmware)`), selectable so the IP can be copied. The scan broadcasts on the network and also asks each saved printer's subnet and address directly. The scan reply does not include ports (the app says so); any port field a printer does send would be listed.

## Delete all printers (2026-10-12)
- Every printer in the list can now be deleted. The app then disconnects and idles, the header says "No printer – add one", and the empty list is remembered (`printers_configured` in `config.json`) so a restart doesn't bring the old printer back. Add a printer in the gear settings to reconnect.
- Saving only the render folder in settings no longer needs a printer IP.

## Printers: ports, duplicates, names (2026-10-12)
- Each saved printer now has its own `printer_port` (editable in settings, default 3030), `video_port` (3031), `udp_port` (3000) and `web_port` (8080); the last three are edited in `config.json` only. Switching printer applies that printer's ports. `web_port` is read at startup, so a different web port needs an app restart.
- Settings: printers list is `Name - IP - Port` with editable fields, a gap before Delete, and an Add row with a port box (defaults to 3030).
- The same IP address can be added more than once (each entry has its own id; `active_printer` in `config.json` is the active one).
- Header: shows `<printer's own name> - <your name for it>`, then the status light. The dropdown keeps the status text colour, highlights the row under the cursor and marks the active printer.
- The page's script and style links now carry a version number, so an update can never be hidden by a cached copy.

## Multiple printers (2026-10-12)
- Click the status text at the top left (**Connected · 192.168.0.189 ▾**) to pick which printer to use. The app switches to it, frees the camera on the one you leave, and reloads the page.
- Gear icon settings now has a **Printers** list: add a name + IP, delete printers (at least one stays). New config key `printers` (a list of `{name, ip}`); `printer_ip` is still the active printer, so existing config files keep working.
- Switching is blocked while an upload is running, and nothing from the previous printer (status, camera, upload) is kept on screen.
- Tests: e2e checks for saving, validating, selecting and removing printers (the mock printer gained a `--host` option for this).

## Camera cleanup on window close (2026-10-12)
- Windows: closing the cmd window (X), logging off or shutting down now runs the normal shutdown first (stops ffmpeg, tells the printer to free the camera slot) instead of killing the app on the spot. Ending Python in Task Manager still can't be caught.
- Test: `tools/test_close_cleanup.py` (checks the slot is freed by that shutdown path on the mock printer; the Windows hook itself only runs on Windows).

## Render upload: Uploaded column (2026-10-12)
- New **Uploaded** column on the Render upload tab: **Yes** when a file with the same name and size is already on the printer, **No** otherwise, and a dash if the printer can't be reached. It checks when the tab opens, on Refresh, and after each upload finishes.
- New endpoint `/api/render/printer-files` (the printer's print-files folder plus its /local/ and /usb/ lists, name and size).
- Tests: e2e checks for the printer file list.

## Render upload tab (2026-10-12)
- New **Render upload** tab: browse a folder on the PC running the app (tree of subfolders and `.goo` files), click **Upload** next to a file or double-click it, confirm the name in the popup, and the file is sent straight to the printer (same progress and cancel as the Files tab). Files in the tree can be double-clicked too.
- New config key `render_folder` (blank by default, so set it before first use). Change it in `config.json`, the gear icon settings, or the tab's **Change folder** button. The tab can only see inside that folder.
- New endpoints `/api/render/list` and `/api/render/upload`; `/api/settings` now also saves `render_folder`.
- Static files (page script and styles) are now sent with `Cache-Control: no-cache`, so updates show on a normal refresh instead of needing Ctrl+F5.
- Tests: new e2e checks for the blank default, path escapes, listing and uploading.
