# SPDX-FileCopyrightText: Copyright (C) 2025 ARDUINO SA <http://www.arduino.cc>
#
# SPDX-License-Identifier: MPL-2.0
import csv
import datetime
import io
import json
import math
import os
import re
import threading
import time
import uuid
import zipfile
from arduino.app_bricks.dbstorage_tsstore import TimeSeriesStore
from arduino.app_bricks.web_ui import WebUI
from arduino.app_utils import App, Bridge

db = TimeSeriesStore()
ui = WebUI()

# =============================================================================
# WebUI's expose_api JSON-encodes whatever your function returns - including
# plain strings. That's why /download_csv and /files were showing up as a
# quoted, backslash-escaped JSON string instead of a real CSV/HTML page: a
# python str return value gets wrapped as "the whole file, escaped" rather
# than sent as-is with the right Content-Type. The routing syntax this Brick
# uses ("/get_samples/{resource}/{start}/{aggr_window}") is FastAPI/Starlette
# path syntax, so returning a real fastapi Response object (instead of a
# plain string) should bypass that JSON-encoding and be sent through as-is.
# If your installed web_ui Brick version doesn't sit on top of FastAPI, this
# import will fail and we silently fall back to the old (broken) behavior -
# in that case check the Brick's own docs/source for how to return a
# non-JSON response.
# =============================================================================
try:
    from fastapi.responses import PlainTextResponse, HTMLResponse, Response
except Exception:
    PlainTextResponse = None
    HTMLResponse = None
    Response = None

# =============================================================================
# Local storage: every recorded sample is written to the time-series DB (used
# by the charts) and to a CSV file on the board's own disk. If the Arduino
# Cloud Brick is configured it's also pushed there. See "recording state"
# below for when writes actually happen.
# =============================================================================
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(DATA_DIR, exist_ok=True)
SESSIONS_META = os.path.join(DATA_DIR, "sessions.json")  # {filename: {"saved": bool, "created": ts_ms}}
RETENTION_DAYS = 7

_state_lock = threading.Lock()
_recording = False    # True between Start and Stop
_current_file = None  # full path of the active session CSV, or None

# Marks "where the live view starts". A page refresh re-queries everything
# from this point forward (see on_live_history below), so the live chart
# survives refreshes even while not recording. Only Clear moves this
# forward - Start/Stop don't touch it, so pausing/resuming never wipes the
# view. Starts at 0 (the actual beginning of the DB) so a fresh boot shows
# the full history, matching the original "show me everything" behavior.
_view_start_ts = 0
# "-3650d" was never actually confirmed to work against this DB - the only
# range strings proven to work anywhere else in this codebase are "-1h" and
# "-1d" (used by the 1h/1D tabs). It's likely "-3650d" was silently
# returning zero rows the whole time, which is exactly what "refresh acts
# like Clear" looks like: the live view would just sit empty until new
# live socket data trickled back in. "-1d" trades away same-day-only
# rehydration on refresh (a session older than 24h won't reload its early
# history) for actually working.
LIVE_HISTORY_START = '-1d'


def _load_meta():
    if os.path.exists(SESSIONS_META):
        with open(SESSIONS_META, "r") as f:
            return json.load(f)
    return {}


def _save_meta(meta):
    tmp = SESSIONS_META + ".tmp"
    with open(tmp, "w") as f:
        json.dump(meta, f, indent=2)
    os.replace(tmp, SESSIONS_META)


def _unique_filename(base_name):
    """Turn a user-supplied base name into a safe, unused '<name>.csv'
    filename. If '<name>.csv' is already taken (existing meta entry or a
    leftover file on disk), append _1, _2, ... until a free one is found."""
    safe = re.sub(r'[^A-Za-z0-9_\- ]+', '', base_name).strip()
    safe = re.sub(r'\s+', '_', safe) or "session"

    meta = _load_meta()
    taken = set(meta.keys())

    def is_free(candidate):
        return candidate not in taken and not os.path.exists(os.path.join(DATA_DIR, candidate))

    candidate = f"{safe}.csv"
    if is_free(candidate):
        return candidate

    n = 1
    while True:
        candidate = f"{safe}_{n}.csv"
        if is_free(candidate):
            return candidate
        n += 1


def _new_session_file(base_name=None):
    if base_name:
        name = _unique_filename(base_name)
    else:
        name = f"magnetic_field_log_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}.csv"
    path = os.path.join(DATA_DIR, name)
    with open(path, "w", newline="") as f:
        # This header row just labels the four columns (so Excel/pandas/etc.
        # pick up the right column names automatically instead of "Column1,
        # Column2..."). It's not measurement data - keep it, don't strip it.
        csv.writer(f).writerow(["timestamp_ms", "timestamp_iso", "voltage_V", "B_field_uT", "gain_code"])
    meta = _load_meta()
    meta[name] = {"saved": False, "created": int(time.time() * 1000)}
    _save_meta(meta)
    return path


def on_start(base_name: str = None):
    global _recording, _current_file
    with _state_lock:
        if not _recording:
            if _current_file is None:
                _current_file = _new_session_file(base_name)
            # else: resuming an existing (paused) session - base_name is
            # ignored here on purpose, there's nothing to rename. The
            # front-end now only prompts for a name when _current_file is
            # actually None (see setupRecordingControls in app.js), so this
            # branch simply won't receive a base_name during a resume.
            _recording = True
    return _status()


def on_stop():
    global _recording
    with _state_lock:
        _recording = False
    return _status()


def on_clear():
    global _current_file, _recording, _view_start_ts
    with _state_lock:
        # Reset all the way back to the "just booted" state: no active file,
        # not recording. The live chart keeps showing values in real time
        # regardless (that's handled independently in record_sensor_samples
        # below) - Clear just means "forget the current session, and don't
        # start a new one until I actually press Start". The abandoned file
        # (if any) isn't deleted - it stays on disk under its old name,
        # subject to the normal 7-day retention unless it was saved.
        _current_file = None
        _recording = False
        # Also move the live-view boundary forward to now, so a page refresh
        # (or a fresh /live_history query) won't show anything from before
        # this Clear - this is the ONLY thing that resets the live view.
        _view_start_ts = int(time.time() * 1000)
    return _status()


def on_save():
    with _state_lock:
        if not _current_file:
            return {"status": "no_active_session"}
        name = os.path.basename(_current_file)
        meta = _load_meta()
        if name in meta:
            meta[name]["saved"] = True
            meta[name]["saved_at"] = int(time.time() * 1000)
            _save_meta(meta)
    return _status()


def _status():
    name = os.path.basename(_current_file) if _current_file else None
    saved = False
    saved_at = None
    if name:
        info = _load_meta().get(name, {})
        saved = info.get("saved", False)
        saved_at = info.get("saved_at")
    return {"recording": _recording, "file": name, "saved": saved, "saved_at": saved_at}


def on_status():
    with _state_lock:
        return _status()


def on_live_history():
    """What the live tab actually loads on page open/refresh: everything
    recorded since the last Clear (not since the last Start/Stop), so the
    view survives a refresh even when nothing is currently being recorded
    to CSV/cloud."""
    with _state_lock:
        boundary = _view_start_ts
    # The window-seconds feature invites viewing long sessions (e.g. a full
    # day at a fast sample rate can be well past a million raw points), so
    # this cap is generous - the browser itself downsamples to at most 1000
    # rendered points regardless, so a bigger fetch here just means a more
    # complete picture to downsample from, at the cost of a larger one-time
    # payload on page load/refresh.
    samples = db.read_samples(measure="B_field", start_from=LIVE_HISTORY_START,
                               aggr_window="1m", aggr_func="mean", limit=500000)
    return [{"ts": s[1], "value": s[2]} for s in samples if s[1] >= boundary]


def on_list_sessions():
    meta = _load_meta()
    current_name = os.path.basename(_current_file) if _current_file else None
    return sorted(
        [{"file": name, "created": info.get("created"), "saved": info.get("saved", False),
          "active": name == current_name} for name, info in meta.items()],
        key=lambda s: s["created"] or 0, reverse=True
    )


def on_download_csv(file: str = None):
    # Browser usage: http://<board-ip>:7000/download_csv?file=magnetic_field_log_...csv
    # (leave `file` out to download the currently active session)
    name = file or (os.path.basename(_current_file) if _current_file else None)
    if not name:
        return {"error": "No session file yet - press Start first."}
    meta = _load_meta()
    if name not in meta:
        return {"error": f"Unknown session file: {name}"}
    path = os.path.join(DATA_DIR, name)
    if not os.path.exists(path):
        return {"error": f"File missing on disk: {name}"}
    with open(path, "r", newline="") as f:
        content = f.read()
    if PlainTextResponse is not None:
        return PlainTextResponse(
            content,
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )
    return content  # fallback: will show up JSON-escaped, see note above


def on_delete_file(file: str):
    """Permanently removes a session file (and its manifest entry) from the
    board's disk. Used by the Delete button on the /files page. Refuses to
    delete whatever the currently active session is, since that file may
    still be open for writing."""
    with _state_lock:
        if _current_file and os.path.basename(_current_file) == file:
            return {"error": "Can't delete the active session - press Stop or Clear first."}
        meta = _load_meta()
        if file not in meta:
            return {"error": f"Unknown file: {file}"}
        path = os.path.join(DATA_DIR, file)
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError as e:
            return {"error": f"Could not delete {file}: {e}"}
        del meta[file]
        _save_meta(meta)
    return {"status": "deleted", "file": file}


def on_delete_files(files: str):
    """Bulk version of on_delete_file - `files` is a comma-separated list of
    filenames, used by the "Delete selected" button on the /files page."""
    names = [n for n in files.split(",") if n]
    deleted, skipped = [], []
    with _state_lock:
        meta = _load_meta()
        active_name = os.path.basename(_current_file) if _current_file else None
        for name in names:
            if name == active_name or name not in meta:
                skipped.append(name)
                continue
            path = os.path.join(DATA_DIR, name)
            try:
                if os.path.exists(path):
                    os.remove(path)
                del meta[name]
                deleted.append(name)
            except OSError:
                skipped.append(name)
        _save_meta(meta)
    return {"deleted": deleted, "skipped": skipped}


def on_download_zip(files: str):
    """Bundles several session files into a single .zip - used by the
    "Download selected" button on the /files page when more than one file
    is selected (a single file just downloads directly as CSV)."""
    names = [n for n in files.split(",") if n]
    if not names:
        return {"error": "No files specified."}
    meta = _load_meta()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in names:
            if name not in meta:
                continue
            path = os.path.join(DATA_DIR, name)
            if os.path.exists(path):
                zf.write(path, arcname=name)
    buf.seek(0)
    if Response is not None:
        return Response(
            content=buf.getvalue(),
            media_type="application/zip",
            headers={"Content-Disposition": 'attachment; filename="recordings.zip"'},
        )
    return {"error": "Zip download requires the fastapi-based web_ui Brick."}


def _render_files_page(names, meta, *, title, show_expiry, show_save_button, other_page_link):
    def fmt_ts(ms):
        try:
            return datetime.datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return ""

    def expiry_html(name):
        if not show_expiry:
            return ""
        created = meta[name].get("created", 0)
        days_left = RETENTION_DAYS - (time.time() * 1000 - created) / 86400000
        if days_left <= 0:
            return '<span class="expiry expiry-soon">removing soon</span>'
        if days_left < 1:
            return '<span class="expiry expiry-soon">expires in &lt;1 day</span>'
        return f'<span class="expiry">expires in {int(days_left)}d</span>'

    def save_btn_html(name):
        if not show_save_button:
            return ""
        return f'<button class="save-btn" onclick="saveFile(\'{name}\')">Save</button>'

    if names:
        rows = "\n".join(
            f'<li>'
            f'<input type="checkbox" class="file-check" value="{name}">'
            f'<a href="/download_csv?file={name}" download="{name}">{name}</a>'
            f'<span class="ts">{fmt_ts(meta[name].get("created", 0))}</span>'
            f'{expiry_html(name)}'
            f'{save_btn_html(name)}'
            f'<button class="del-btn" onclick="deleteFile(\'{name}\')">Delete</button>'
            f'</li>'
            for name in names
        )
    else:
        rows = '<li class="empty">Nothing here.</li>'

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>{title}</title>
<style>
  body {{ font-family: -apple-system, Segoe UI, Roboto, sans-serif; background: #DAE3E3; padding: 32px; }}
  h1 {{ font-size: 1.3rem; color: #222; margin-bottom: 4px; }}
  .other-link {{ display: inline-block; margin-bottom: 16px; font-size: 0.85rem; color: #1864ab; text-decoration: none; }}
  .other-link:hover {{ text-decoration: underline; }}
  .bulk-bar {{ display: flex; align-items: center; gap: 12px; max-width: 700px;
               margin-bottom: 10px; font-size: 0.9rem; color: #444; }}
  .bulk-bar label {{ display: flex; align-items: center; gap: 6px; cursor: pointer; }}
  .bulk-bar button {{ padding: 6px 14px; border-radius: 6px; border: 1px solid #ccc;
                       background: #fff; cursor: pointer; font-size: 0.85rem; }}
  .bulk-bar button:disabled {{ opacity: 0.5; cursor: not-allowed; }}
  .bulk-bar button:not(:disabled):hover {{ background: #f0f0f0; }}
  #bulk-delete {{ border-color: #c92a2a; color: #c92a2a; }}
  #bulk-delete:not(:disabled):hover {{ background: #fff5f5; }}
  ul {{ list-style: none; padding: 0; max-width: 700px; }}
  li {{ display: flex; align-items: center; gap: 12px;
        background: #fff; border: 1px solid #e5e5e5; border-radius: 8px;
        padding: 10px 14px; margin-bottom: 8px; }}
  li.empty {{ color: #888; justify-content: center; }}
  a {{ color: #1864ab; text-decoration: none; font-weight: 600; flex: 1; }}
  a:hover {{ text-decoration: underline; }}
  .ts {{ color: #888; font-size: 0.85rem; white-space: nowrap; }}
  .expiry {{ color: #999; font-size: 0.8rem; white-space: nowrap; }}
  .expiry-soon {{ color: #c92a2a; font-weight: 600; }}
  .del-btn {{ border: 1px solid #c92a2a; color: #c92a2a; background: #fff;
              border-radius: 6px; padding: 5px 10px; cursor: pointer; font-size: 0.85rem; }}
  .del-btn:hover {{ background: #fff5f5; }}
  .save-btn {{ border: 1px solid #2b8a3e; color: #2b8a3e; background: #fff;
               border-radius: 6px; padding: 5px 10px; cursor: pointer; font-size: 0.85rem; }}
  .save-btn:hover {{ background: #f4fbf5; }}
</style>
</head>
<body>
  <h1>{title} ({len(names)})</h1>
  <a class="other-link" href="{other_page_link[1]}">{other_page_link[0]}</a>
  <div class="bulk-bar">
    <label><input type="checkbox" id="select-all"> Select all</label>
    <button id="bulk-download" disabled>Download selected</button>
    <button id="bulk-delete" disabled>Delete selected</button>
  </div>
  <ul>
    {rows}
  </ul>
  <script>
    function checks() {{ return Array.from(document.querySelectorAll('.file-check')); }}
    const selectAll = document.getElementById('select-all');
    const bulkDownloadBtn = document.getElementById('bulk-download');
    const bulkDeleteBtn = document.getElementById('bulk-delete');

    function updateBulkButtons() {{
        const n = checks().filter(c => c.checked).length;
        bulkDownloadBtn.disabled = n === 0;
        bulkDeleteBtn.disabled = n === 0;
    }}

    checks().forEach(c => c.addEventListener('change', () => {{
        if (!c.checked) selectAll.checked = false;
        updateBulkButtons();
    }}));

    selectAll.addEventListener('change', () => {{
        checks().forEach(c => c.checked = selectAll.checked);
        updateBulkButtons();
    }});

    bulkDownloadBtn.addEventListener('click', () => {{
        const selected = checks().filter(c => c.checked).map(c => c.value);
        if (selected.length === 0) return;
        if (selected.length === 1) {{
            window.location.href = '/download_csv?file=' + encodeURIComponent(selected[0]);
        }} else {{
            window.location.href = '/download_zip?files=' + encodeURIComponent(selected.join(','));
        }}
    }});

    bulkDeleteBtn.addEventListener('click', async () => {{
        const selected = checks().filter(c => c.checked).map(c => c.value);
        if (selected.length === 0) return;
        if (!confirm(`Delete ${{selected.length}} file(s)? This can't be undone.`)) return;
        const res = await fetch('/delete_files?files=' + encodeURIComponent(selected.join(',')), {{ method: 'POST' }});
        const data = await res.json();
        if (data.skipped && data.skipped.length) {{
            alert('Some files could not be deleted: ' + data.skipped.join(', '));
        }}
        location.reload();
    }});

    async function deleteFile(name) {{
        if (!confirm(`Delete ${{name}}? This can't be undone.`)) return;
        const res = await fetch('/delete_file?file=' + encodeURIComponent(name), {{ method: 'POST' }});
        const data = await res.json();
        if (data.error) {{ alert(data.error); return; }}
        location.reload();
    }}

    async function saveFile(name) {{
        const res = await fetch('/mark_saved?file=' + encodeURIComponent(name), {{ method: 'POST' }});
        const data = await res.json();
        if (data.error) {{ alert(data.error); return; }}
        location.reload();
    }}
  </script>
</body>
</html>"""
    if HTMLResponse is not None:
        return HTMLResponse(html)
    return html  # fallback: will show up JSON-escaped, see note above


def on_mark_saved(file: str):
    """Promotes an arbitrary (not necessarily active) session file to
    "saved", protecting it from the 7-day retention cleanup. Used by the
    Save button on the /files/unsaved page."""
    meta = _load_meta()
    if file not in meta:
        return {"error": f"Unknown file: {file}"}
    meta[file]["saved"] = True
    meta[file]["saved_at"] = int(time.time() * 1000)
    _save_meta(meta)
    return {"status": "saved", "file": file}


def on_files_page():
    """Every *saved* (protected) recording - these are never auto-deleted."""
    meta = _load_meta()
    saved_names = sorted(
        [name for name, info in meta.items() if info.get("saved")],
        key=lambda name: meta[name].get("created", 0),
        reverse=True,
    )
    return _render_files_page(
        saved_names, meta,
        title="Saved recordings",
        show_expiry=False,
        show_save_button=False,
        other_page_link=("View unsaved files (auto-removed after 7 days) →", "/files/unsaved"),
    )


def on_unsaved_files_page():
    """Every recording that hasn't been Saved - these get auto-deleted 7
    days after they were created unless you Save them (button on each row)
    or they're still the currently active session."""
    meta = _load_meta()
    unsaved_names = sorted(
        [name for name, info in meta.items() if not info.get("saved")],
        key=lambda name: meta[name].get("created", 0),
        reverse=True,
    )
    return _render_files_page(
        unsaved_names, meta,
        title="Unsaved recordings",
        show_expiry=True,
        show_save_button=True,
        other_page_link=("← View saved files", "/files"),
    )


# --- Retention cleanup: run periodically, delete unsaved files > 7 days old ---
def cleanup_old_sessions():
    while True:
        try:
            meta = _load_meta()
            cutoff = time.time() * 1000 - RETENTION_DAYS * 86400 * 1000
            changed = False
            for name, info in list(meta.items()):
                path = os.path.join(DATA_DIR, name)
                if not info.get("saved") and info.get("created", 0) < cutoff and path != _current_file:
                    if os.path.exists(path):
                        os.remove(path)
                    del meta[name]
                    changed = True
            if changed:
                _save_meta(meta)
        except Exception as e:
            print(f"Cleanup error: {e}")
        time.sleep(6 * 3600)  # check every 6h


threading.Thread(target=cleanup_old_sessions, daemon=True).start()

ui.expose_api("GET", "/get_samples/{resource}/{start}/{aggr_window}",
              lambda resource, start, aggr_window, limit=100:
              [{"ts": s[1], "value": s[2]} for s in db.read_samples(measure=resource, start_from=start, aggr_window=aggr_window, aggr_func="mean", limit=limit)])
ui.expose_api("GET", "/live_history", on_live_history)
ui.expose_api("GET", "/download_csv", on_download_csv)
ui.expose_api("GET", "/download_zip", on_download_zip)
ui.expose_api("GET", "/list_sessions", on_list_sessions)
ui.expose_api("GET", "/files", on_files_page)
ui.expose_api("GET", "/files/unsaved", on_unsaved_files_page)
ui.expose_api("POST", "/mark_saved", on_mark_saved)
ui.expose_api("POST", "/delete_file", on_delete_file)
ui.expose_api("POST", "/delete_files", on_delete_files)
ui.expose_api("GET", "/record/status", on_status)
ui.expose_api("POST", "/record/start", on_start)
ui.expose_api("POST", "/record/stop", on_stop)
ui.expose_api("POST", "/record/save", on_save)
ui.expose_api("POST", "/record/clear", on_clear)

# =============================================================================
# Sample rate control. The interval between readings is a `delay()` inside
# the sketch's loop(), which normally means "recompile and reflash to
# change it". But Bridge is bidirectional - Python can call INTO the sketch
# the same way the sketch calls into Python - so the sketch has been changed
# (see the .ino file) to expose set/get functions for that delay, letting you
# change it live from the webpage with no reflash needed.
# =============================================================================
def on_get_sample_rate():
    try:
        ms = Bridge.call("get_sample_interval")
        return {"interval_ms": ms}
    except Exception as e:
        return {"error": f"Could not reach the sketch: {e}"}


def on_set_sample_rate(interval_ms: int):
    try:
        Bridge.call("set_sample_interval", int(interval_ms))
        return {"interval_ms": int(interval_ms)}
    except Exception as e:
        return {"error": f"Could not reach the sketch: {e}"}


ui.expose_api("GET", "/sample_rate", on_get_sample_rate)
ui.expose_api("POST", "/sample_rate/{interval_ms}", on_set_sample_rate)

# =============================================================================
# Gain control (ADS1115 PGA setting). Smaller full-scale range (higher
# "gain" code below) = finer LSB = better resolution, as long as the signal
# doesn't exceed that range. Same Bridge mechanism as sample rate: the
# sketch exposes set_gain/get_gain, called on demand (never every loop -
# see the .ino file's comment on why that mattered before).
#
#   gain    FSR        LSB
#   0       +-6.144 V  187.5 uV
#   1       +-4.096 V  125 uV
#   2       +-2.048 V  62.5 uV
#   4       +-1.024 V  31.25 uV
#   8       +-0.512 V  15.6 uV
#   16      +-0.256 V  7.8 uV
#
# Auto-gain mode: once a minute, looks at the peak |voltage| seen since the
# last check and picks the smallest range (best resolution) that keeps that
# peak comfortably under the range's ceiling (see AUTO_GAIN_HEADROOM),
# leaving margin so a slightly larger swing next minute doesn't clip.
# =============================================================================
GAIN_TABLE = [
    (0, 6.144),
    (1, 4.096),
    (2, 2.048),
    (4, 1.024),
    (8, 0.512),
    (16, 0.256),
]
VALID_GAINS = {g for g, _ in GAIN_TABLE}
AUTO_GAIN_CHECK_INTERVAL_S = 60
AUTO_GAIN_HEADROOM = 0.8  # stay under 80% of the range's ceiling

_auto_gain = {
    "enabled": False,
    "last_check_ts": 0,
    "peak_since_check": 0.0,
    "samples_since_check": 0,
}

# Cached locally so record_sensor_samples can stamp each CSV row with the
# gain that was active for it, without a Bridge round-trip on every sample -
# updated whenever gain actually changes (manually or via auto-gain).
# Starts at 0 to match the sketch's own startup default (Bridge isn't
# connected yet at this point in the script, before App.run(), so it can't
# be queried here - but the sketch always initializes to gain 0 in its own
# setup(), so this matches reality until something changes it).
_gain_cache = {"value": 0}


def on_get_gain():
    try:
        gain = Bridge.call("get_gain")
        with _state_lock:
            auto = _auto_gain["enabled"]
        return {"gain": gain, "auto": auto}
    except Exception as e:
        return {"error": f"Could not reach the sketch: {e}"}


def on_set_gain(gain: int):
    gain = int(gain)
    if gain not in VALID_GAINS:
        return {"error": f"Gain must be one of {sorted(VALID_GAINS)}."}
    with _state_lock:
        _auto_gain["enabled"] = False  # manual gain turns auto off
    try:
        ok = Bridge.call("set_gain", gain)
        if not ok:
            return {"error": f"Sketch rejected gain {gain}."}
        _gain_cache["value"] = gain
        return {"gain": gain, "auto": False}
    except Exception as e:
        return {"error": f"Could not reach the sketch: {e}"}


def on_set_auto_gain(enabled: str):
    turn_on = str(enabled).lower() in ("1", "true", "on", "yes")
    with _state_lock:
        _auto_gain["enabled"] = turn_on
        _auto_gain["last_check_ts"] = int(time.time() * 1000)
        _auto_gain["peak_since_check"] = 0.0
        _auto_gain["samples_since_check"] = 0
    return {"auto": turn_on}


def _note_voltage_for_auto_gain(voltage):
    """Called on every sample, from inside record_sensor_samples. Only ever
    updates in-memory counters - deliberately makes NO Bridge call here.
    Calling Bridge.call() to actually change gain from within this handler
    (which itself runs *because* the sketch called into Python via
    Bridge.notify) is a reentrant call on the same bridge connection while
    it's already busy handling an incoming message - a likely cause of the
    gain silently failing to actually apply. The real decision + Bridge.call
    happens on _auto_gain_loop's own thread instead, see below."""
    with _state_lock:
        if not _auto_gain["enabled"]:
            return
        _auto_gain["peak_since_check"] = max(_auto_gain["peak_since_check"], abs(voltage))
        _auto_gain["samples_since_check"] += 1


def _auto_gain_loop():
    """Runs on its own thread (like cleanup_old_sessions) so the actual
    Bridge.call("set_gain", ...) never happens from inside the
    record_sensor_samples notify handler."""
    while True:
        time.sleep(5)  # check-if-it's-time-yet cadence; the real gate is AUTO_GAIN_CHECK_INTERVAL_S below
        with _state_lock:
            if not _auto_gain["enabled"]:
                continue
            elapsed_s = (int(time.time() * 1000) - _auto_gain["last_check_ts"]) / 1000
            if elapsed_s < AUTO_GAIN_CHECK_INTERVAL_S:
                continue
            if _auto_gain["samples_since_check"] == 0:
                continue  # nothing measured this window - don't act on stale data
            peak = _auto_gain["peak_since_check"]
            _auto_gain["last_check_ts"] = int(time.time() * 1000)
            _auto_gain["peak_since_check"] = 0.0
            _auto_gain["samples_since_check"] = 0

        chosen_gain = GAIN_TABLE[0][0]  # fall back to the safest (widest) range
        for gain, fsr in GAIN_TABLE:
            if peak <= fsr * AUTO_GAIN_HEADROOM:
                chosen_gain = gain
            else:
                break
        try:
            ok = Bridge.call("set_gain", chosen_gain)
            if ok:
                _gain_cache["value"] = chosen_gain
                print(f"[auto-gain] peak={peak:.4f}V over last {AUTO_GAIN_CHECK_INTERVAL_S}s -> gain={chosen_gain}")
            else:
                print(f"[auto-gain] sketch rejected gain {chosen_gain}")
        except Exception as e:
            print(f"[auto-gain] failed to set gain: {e}")


threading.Thread(target=_auto_gain_loop, daemon=True).start()

ui.expose_api("GET", "/gain", on_get_gain)
ui.expose_api("POST", "/gain/{gain}", on_set_gain)
ui.expose_api("POST", "/gain_auto/{enabled}", on_set_auto_gain)

# =============================================================================
# Arduino Cloud sync (optional). Add & configure the "Arduino Cloud" Brick in
# App Lab first (device credentials + a Thing with float variables named
# "B_field" and "voltage"). If it isn't configured yet, this is skipped
# rather than crashing the app.
# =============================================================================
iot_cloud = None
try:
    from arduino.app_bricks.arduino_cloud import ArduinoCloud
    iot_cloud = ArduinoCloud()
    iot_cloud.register("B_field")
    iot_cloud.register("voltage")
    print("Arduino Cloud sync enabled.")
except Exception as e:
    print(f"Arduino Cloud sync not available yet ({e}). Add/configure the Arduino Cloud Brick in App Lab to enable it.")


def record_sensor_samples(voltage: float):
    """Callback invoked by the board sketch via Bridge.notify to send sensor samples."""
    if voltage is None:
        print("Received invalid sensor samples: voltage=%s" % (voltage))
        return

    ts = int(datetime.datetime.now().timestamp() * 1000)
    V = float(voltage)
    # convert voltage to B strength TODO: adjust to voltage distributor:  currently set at 1/3
    B = 3 * V / 0.1  # microT

    # The live webpage always shows the current value in real time, whether
    # or not it's being recorded.
    ui.send_message('voltage', {"value": V, "ts": ts})
    ui.send_message('B_field', {"value": B, "ts": ts})

    # Auto-gain runs independent of recording state too - it's about the
    # instrument's own ranging, not about what gets saved. This only ever
    # records the voltage in memory - the actual gain change (if any) is
    # decided and applied on a separate thread, never from here.
    _note_voltage_for_auto_gain(V)

    # DB writes are unconditional (not gated by "recording") so the live
    # chart can rebuild its full view from on_live_history() after a page
    # refresh, even when nothing is currently being recorded. Only the CSV
    # file and Arduino Cloud sync below - the actual "saved output" this
    # app produces - are gated by the recording flag.
    db.write_sample("voltage", V, ts)
    db.write_sample("B_field", B, ts)

    with _state_lock:
        recording = _recording
        file_path = _current_file

    if not recording:
        return  # Stop was pressed (or Start never was, or Clear just reset us) - nothing gets written to CSV/cloud

    # --- Persist to CSV + Arduino Cloud, only while recording ---
    if file_path:
        with open(file_path, "a", newline="") as f:
            csv.writer(f).writerow([ts, datetime.datetime.fromtimestamp(ts / 1000).isoformat(), f"{V:.6f}", f"{B:.6f}", _gain_cache["value"]])

    if iot_cloud is not None:
        try:
            iot_cloud.B_field = B
            iot_cloud.voltage = V
        except Exception as e:
            print(f"Arduino Cloud sync failed: {e}")


print("Registering 'record_sensor_samples' callback.")
Bridge.provide("record_sensor_samples", record_sensor_samples)
print(f"Session files are stored on this board at: {DATA_DIR}")
print("Starting App...")
App.run()
