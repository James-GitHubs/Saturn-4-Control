"""Helpers for the printer's plain HTTP file server (Mongoose, same port as the websocket).

The printer serves its whole filesystem read-only, e.g. /media/mmcblk0p3/ (the .goo files),
/media/mmcblk0p1/file_info/ (thumbnails + file_info.txt) and /media/mmcblk0p2/ (logs).
"""
import html
import json
import posixpath
import re
from urllib.parse import unquote

FILE_INFO_PATH = "/media/mmcblk0p1/file_info/file_info.txt"

# Reading these would stream forever or hang the printer's single web thread.
_VIRTUAL = ("/proc", "/sys", "/dev")

_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
_A = re.compile(r"""<a\s+[^>]*?href\s*=\s*["']?([^"'>\s]+)["']?[^>]*>(.*?)</a>""", re.S | re.I)
_TD = re.compile(r"<td([^>]*)>(.*?)</td>", re.S | re.I)
_NAMEATTR = re.compile(r"""name\s*=\s*["']?(-?\d+)""", re.I)
_SIZE_TXT = re.compile(r"^([\d.]+)\s*([kKmMgGtT]?)[bB]?$")


def _text(s):
    return html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def normalize_dir(path):
    """'/media' -> '/media/'; collapses '..' and doubled slashes."""
    path = "/" + (path or "").strip().lstrip("/")
    path = posixpath.normpath(path)
    return path if path.endswith("/") else path + "/"


def normalize_path(path):
    path = "/" + (path or "").strip().lstrip("/")
    return posixpath.normpath(path)


def is_virtual(path):
    p = normalize_path(path)
    return any(p == v or p.startswith(v + "/") for v in _VIRTUAL)


def _size(text, attrs):
    t = text.strip()
    if t.upper() == "[DIR]":
        return None, True
    if t.isdigit():
        return int(t), False
    m = _SIZE_TXT.match(t)
    if m:
        mult = {"": 1, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3, "t": 1024 ** 4}[m.group(2).lower()]
        return int(float(m.group(1)) * mult), False
    m = _NAMEATTR.search(attrs)
    if m:
        v = int(m.group(1))
        return (None, True) if v < 0 else (v, False)
    return None, False


def _mtime(text, attrs):
    t = text.strip()
    if t.isdigit():
        v = int(t)
    else:
        m = _NAMEATTR.search(attrs)
        if not m:
            return None
        v = int(m.group(1))
    return v if v > 946684800 else None      # 0 / 5 / 13 are "unknown" on this firmware


def parse_listing(page, limit=5000):
    """Parse a Mongoose 'Index of /x/' page into [{name, href, is_dir, size, mtime}], dirs first."""
    out = []
    rows = _ROW.findall(page)
    for row in rows:
        m = _A.search(row)
        if not m:
            continue
        href, label = m.group(1), _text(m.group(2))
        if href == "#" or label in ("Name", "Modified", "Size") or label == ".." or href in ("..", "../", "./", "/"):
            continue
        if href.startswith("?"):
            continue
        tds = _TD.findall(row)
        extra = [(a, _text(b)) for a, b in tds[1:]]
        mtime = _mtime(extra[0][1], extra[0][0]) if len(extra) > 0 else None
        size, is_dir = _size(extra[1][1], extra[1][0]) if len(extra) > 1 else (None, False)
        name = label.rstrip("/") or unquote(href).rstrip("/").rsplit("/", 1)[-1]
        if href.endswith("/") or label.endswith("/"):
            is_dir = True
        out.append({"name": name, "href": href, "is_dir": is_dir, "size": size, "mtime": mtime})
    if not rows:          # unknown layout: at least list the links
        for href, label in _A.findall(page):
            label = _text(label)
            if href.startswith(("#", "?", "..", "/")) or not label:
                continue
            out.append({"name": label.rstrip("/"), "href": href, "is_dir": href.endswith("/"),
                        "size": None, "mtime": None})
    out.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
    return out[:limit]


# ------------------------------------------------------------------ file_info.txt
_FRAGMENT = re.compile(r"^[0-9a-fA-F-]{32}_")
_PERCENT = re.compile(r"%[0-9A-Fa-f]{2}")


def looks_like_fragment(name):
    """Interrupted uploads are stored as '<uuid first 32 chars>_<name>'."""
    return bool(_FRAGMENT.match(name))


def has_percent_codes(name):
    return bool(_PERCENT.search(name))


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_file_info(text):
    """file_info.txt -> {file_name: entry}. The printer re-appends entries on every sync, so the same
    file can appear many times; keep the best (complete) and latest one."""
    text = text.replace("\x00", "")
    entries = []
    try:
        entries = json.loads(text).get("files") or []
    except (ValueError, AttributeError):
        for m in re.finditer(r"\{[^{}]*\"file_name\"[^{}]*\}", text, re.S):
            try:
                entries.append(json.loads(m.group(0)))
            except ValueError:
                pass
    best = {}
    for e in entries:
        if not isinstance(e, dict) or not e.get("file_name"):
            continue
        prev = best.get(e["file_name"])
        if prev is None or _complete(e) or not _complete(prev):
            best[e["file_name"]] = e
    return {n: _clean(e) for n, e in best.items()}


def _complete(e):
    return (_f(e.get("est_time")) or 0) > 0 and e.get("preview_image_path") not in (None, "", "unknow")


def _clean(e):
    lh = _f(e.get("layer_height"))
    prev = e.get("preview_image_path")
    layers = _f(e.get("total_layers"))
    est = _f(e.get("est_time"))
    return {
        "layer_height": round(lh, 3) if lh else None,
        "layers": int(layers) if layers else None,
        "est_seconds": est if est else None,
        "size": _f(e.get("file_size")) or None,
        "create_time": int(e["create_time"]) if _f(e.get("create_time")) else None,
        "thumbnail": prev if isinstance(prev, str) and prev.startswith("/") else None,
        "resin_name": e.get("resin_name") if e.get("resin_name") not in ("", "unknow") else None,
    }
