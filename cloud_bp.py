"""
Cloud Drive sebagai Blueprint Flask, dimuat di dashboard (port 5000).

Halaman   : http://localhost:5000/cloud/
API       : /cloud/api/...   (kontrak sama dengan versi lama, hanya prefix berubah)

Logika penyimpanan ada di cloud_core.py. File ini hanya berisi routing HTTP.
"""
import base64
import http.client
import json
import logging
import mimetypes
import os
import posixpath
import re
import subprocess
import threading
import urllib.parse
import uuid

from flask import Blueprint, Response, request, send_from_directory
from werkzeug.exceptions import HTTPException

import cloud_core as core
from cloud_core import CloudError

log = logging.getLogger("cloud")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UI_DIR = os.path.join(BASE_DIR, "static", "cloud")

bp = Blueprint("cloud", __name__, url_prefix="/cloud")


# ---------------------------------------------------------------------------
# Utilitas respons
# ---------------------------------------------------------------------------
def _json(obj, status=200):
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    resp = Response(body, status=status, mimetype="application/json")
    resp.charset = "utf-8"
    resp.headers["Cache-Control"] = "no-store"
    return resp


@bp.before_request
def _guard():
    # Hanya menerima permintaan dari dashboard lokal ini (sama seperti versi lama).
    if request.headers.get("Host") not in core.ALLOWED_HOSTS:
        raise CloudError("Host tidak diizinkan", 403)
    site = request.headers.get("Sec-Fetch-Site")
    if site not in (None, "same-origin", "none"):
        raise CloudError("Permintaan lintas situs ditolak", 403)
    if request.method == "POST":
        origin = request.headers.get("Origin")
        if origin is not None and origin not in core.ALLOWED_ORIGINS:
            raise CloudError("Origin tidak diizinkan", 403)


@bp.after_request
def _headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    return resp


@bp.errorhandler(CloudError)
def _cloud_error(exc):
    return _json({"error": str(exc)}, exc.status)


@bp.errorhandler(Exception)
def _unexpected(exc):
    if isinstance(exc, HTTPException):
        return exc
    log.exception("Kesalahan tidak terduga")
    return _json({"error": "Terjadi kesalahan di server"}, 500)


# ---------------------------------------------------------------------------
# Membaca body
# ---------------------------------------------------------------------------
def _read_length(limit):
    raw = request.headers.get("Content-Length")
    if raw is None:
        raise CloudError("Content-Length wajib diisi", 411)
    try:
        length = int(raw)
    except ValueError:
        raise CloudError("Content-Length tidak valid")
    if length < 0:
        raise CloudError("Content-Length tidak valid")
    if length > limit:
        raise CloudError("Data terlalu besar", 413)
    return length


def _json_body(limit=core.MAX_JSON_BODY):
    length = _read_length(limit)
    raw = _read_exact(length) if length else b""
    try:
        body = json.loads(raw.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        raise CloudError("Format JSON tidak valid")
    if not isinstance(body, dict):
        raise CloudError("Body harus berupa JSON object")
    return body


def _read_exact(length):
    chunks, remaining = [], length
    while remaining > 0:
        chunk = request.stream.read(min(core.CHUNK, remaining))
        if not chunk:
            raise CloudError("Upload terputus", 400)
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


# ---------------------------------------------------------------------------
# Halaman & aset UI
# ---------------------------------------------------------------------------
_UI_FILES = {"index.html": "text/html; charset=utf-8",
             "app.js": "application/javascript; charset=utf-8",
             "style.css": "text/css; charset=utf-8"}


@bp.get("/")
def ui_index():
    return _ui_file("index.html")


@bp.get("/<name>")
def ui_asset(name):
    if name not in _UI_FILES:
        raise CloudError("Tidak ditemukan", 404)
    return _ui_file(name)


def _ui_file(name):
    resp = send_from_directory(UI_DIR, name, mimetype=_UI_FILES[name].split(";")[0])
    resp.headers["Cache-Control"] = "no-cache"
    return resp


# ---------------------------------------------------------------------------
# Storage, cache, dan tempat sampah
# ---------------------------------------------------------------------------
@bp.get("/api/storage")
def get_storage():
    return _json({"mode": "rclone", "label": "Rclone (cloud:)"})


@bp.get("/api/cache/<kind>")
def get_cache(kind):
    if kind not in ("list", "about"):
        raise CloudError("Tidak ditemukan", 404)
    if kind == "list":
        remote = core.check_remote_path(request.args.get("remote", ""), allow_empty=True)
        with core.cache_lock:
            entry = core.cache_data["lists"].get(remote)
            payload = None if entry is None else {"list": entry["list"], "ts": entry["ts"]}
    else:
        with core.cache_lock:
            payload = core.cache_data["about"]
    if payload is None:
        raise CloudError("Belum ada cache", 404)
    return _json(payload)


@bp.get("/api/trash/list")
def trash_list_route():
    return _json({"items": core.trash_list(), "days": core.TRASH_DAYS})


@bp.post("/api/trash/<action>")
def trash_route(action):
    if action not in ("move", "restore", "delete", "empty"):
        raise CloudError("Tidak ditemukan", 404)
    body = _json_body()
    if action == "move":
        return _json({"item": core.trash_move(core.check_remote_path(body.get("remote")))})
    if action == "empty":
        return _json(core.trash_empty())
    tid = body.get("id")
    if not isinstance(tid, str) or not re.fullmatch(r"[0-9a-f]{32}", tid):
        raise CloudError("ID tidak valid")
    if action == "restore":
        return _json(core.trash_restore(tid))
    return _json(core.trash_delete(tid))


# ---------------------------------------------------------------------------
# Operasi file (JSON) dan upload
# ---------------------------------------------------------------------------
@bp.post("/api/operations/<op>")
def operation(op):
    rc_path = "/operations/" + op
    if rc_path not in core.ALLOWED_OPS:
        raise CloudError("Operasi tidak diizinkan", 403)
    body = _json_body()
    payload = core.validate_op(rc_path, body)
    status, data = core.rc_json(rc_path, payload)
    if status >= 400:
        message = str(data.get("error") or "Permintaan gagal") if isinstance(data, dict) else "Permintaan gagal"
        # stat pada item yang tidak ada dianggap "tidak ditemukan", bukan error
        if rc_path == "/operations/stat" and "not found" in message.lower():
            return _json({"item": None})
        log.warning("rc %s gagal (%s): %s", rc_path, status, message)
        raise CloudError(message[:300], 500)
    if not isinstance(data, dict):
        data = {}
    if rc_path == "/operations/list":
        items = data.get("list") or []
        if payload["remote"] == "":
            items = [i for i in items if i.get("Name") != core.TRASH_DIR]
        data["list"] = items
        core.cache_put_list(payload["remote"], items)
    elif rc_path == "/operations/about":
        core.cache_put_about(data)
    elif rc_path in ("/operations/mkdir", "/operations/purge",
                     "/operations/deletefile", "/operations/moveto"):
        core.after_mutation(rc_path, payload)
    return _json(data)


@bp.post("/api/upload")
def upload():
    remote_dir = core.check_user_path(request.args.get("remote", ""), allow_empty=True)
    name = core.check_name(request.args.get("name", ""))
    length = _read_length(core.MAX_UPLOAD_BYTES)

    # Diteruskan langsung ke Rclone RC (multipart dibentuk di sini).
    boundary = uuid.uuid4().hex
    pre = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{name}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode("utf-8")
    post = f"\r\n--{boundary}--\r\n".encode("ascii")
    conn = http.client.HTTPConnection(core.RCLONE_HOST, core.RCLONE_PORT, timeout=3600)
    try:
        headers = core.rc_headers({
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(pre) + length + len(post)),
        })
        rc_query = urllib.parse.urlencode({"fs": core.current_remote(), "remote": remote_dir})
        conn.putrequest("POST", f"/operations/uploadfile?{rc_query}")
        for key, value in headers.items():
            conn.putheader(key, value)
        conn.endheaders()
        conn.send(pre)
        _copy_body(length, conn.send)
        conn.send(post)
        resp = conn.getresponse()
        status = resp.status
        raw = resp.read()
    finally:
        conn.close()

    if status >= 400:
        try:
            message = json.loads(raw.decode("utf-8")).get("error") or "Upload gagal"
        except (ValueError, AttributeError):
            message = "Upload gagal"
        log.warning("upload %s gagal (%s): %s", name, status, message)
        raise CloudError(str(message)[:300], 500)
    core.cache_invalidate(exact=(remote_dir,))
    return _json({"ok": True})


def _copy_body(length, write):
    remaining = length
    while remaining > 0:
        chunk = request.stream.read(min(core.CHUNK, remaining))
        if not chunk:
            raise CloudError("Upload terputus", 400)
        write(chunk)
        remaining -= len(chunk)


# ---------------------------------------------------------------------------
# Stream file (pratinjau & unduh) dengan dukungan Range
# ---------------------------------------------------------------------------
@bp.get("/api/stream")
def stream():
    remote = core.check_user_path(request.args.get("path", ""))
    download = request.args.get("download", "0") == "1"

    status, data = core.rc_json("/operations/stat", {"fs": core.current_remote(), "remote": remote})
    item = data.get("item") if status == 200 and isinstance(data, dict) else None
    if not item or item.get("IsDir"):
        raise CloudError("File tidak ditemukan", 404)

    size = int(item.get("Size") or 0)
    name = item.get("Name") or posixpath.basename(remote)
    ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
    if not download and ctype in core.UNSAFE_INLINE_TYPES:
        ctype = "text/plain; charset=utf-8"  # jangan jalankan HTML/XML dari server ini

    try:
        start, end, partial = core.parse_range(request.headers.get("Range"), size)
    except ValueError:
        resp = Response(b"", status=416)
        resp.headers["Content-Range"] = f"bytes */{size}"
        return resp

    length = max(0, end - start + 1)
    headers = {
        "Content-Type": ctype,
        "Content-Length": str(length),
        "Accept-Ranges": "bytes",
        "Content-Disposition": f"{'attachment' if download else 'inline'}; filename*=UTF-8''{urllib.parse.quote(name)}",
        "Cache-Control": "no-store",
    }
    if partial:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    if ctype.startswith("image/svg"):
        headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; sandbox"

    code = 206 if partial else 200
    if length == 0:
        return Response(b"", status=code, headers=headers)

    return Response(_iter_rclone(remote, start, length), status=code,
                    headers=headers, direct_passthrough=True)


def _iter_rclone(remote, start, length):
    proc = subprocess.Popen(
        ["rclone", "cat", "--offset", str(start), "--count", str(length),
         core.current_remote() + remote],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    sent = 0
    try:
        while sent < length:
            chunk = proc.stdout.read(min(core.CHUNK, length - sent))
            if not chunk:
                break
            yield chunk
            sent += len(chunk)
    except (BrokenPipeError, ConnectionResetError):
        pass  # klien menutup koneksi (misalnya video dihentikan)
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        proc.stdout.close()
        if sent < length and proc.stderr:
            err = proc.stderr.read(300).decode("utf-8", "replace")
            if err:
                log.warning("rclone cat %s: %s", remote, err.strip())
        if proc.stderr:
            proc.stderr.close()


# ---------------------------------------------------------------------------
# Inisialisasi (dipanggil dari app.py)
# ---------------------------------------------------------------------------
def init_cloud():
    import atexit
    atexit.register(core.stop_rclone)
    core.load_cache()
    core.load_trash()
    # Satu-satunya backend adalah rclone, jadi proses bantuannya dinyalakan
    # sejak awal. Kalau gagal, server tetap jalan dan errornya jelas di UI.
    try:
        core.start_rclone()
        log.info("Cloud memakai Rclone (%s).", core.current_remote())
    except (CloudError, OSError) as exc:
        log.warning("Rclone belum siap: %s", exc)
    try:
        core.purge_expired()
    except (CloudError, OSError) as exc:
        log.warning("Pembersihan tempat sampah gagal: %s", exc)
