# Changelog

## Render upload tab (2026-10-12)
- New **Render upload** tab: browse a folder on the PC running the app (tree of subfolders and `.goo` files), click **Upload** next to a file or double-click it, confirm the name in the popup, and the file is sent straight to the printer (same progress and cancel as the Files tab). Files in the tree can be double-clicked too.
- New config key `render_folder` (blank by default, so set it before first use). Change it in `config.json`, the gear icon settings, or the tab's **Change folder** button. The tab can only see inside that folder.
- New endpoints `/api/render/list` and `/api/render/upload`; `/api/settings` now also saves `render_folder`.
- Static files (page script and styles) are now sent with `Cache-Control: no-cache`, so updates show on a normal refresh instead of needing Ctrl+F5.
- Tests: new e2e checks for the blank default, path escapes, listing and uploading.
