"""
Cloud Drive - logika inti (penyimpanan HP / Rclone RC, cache, tempat sampah).
Dipakai oleh cloud_bp.py dan dijalankan lewat app.py di port 5000.
"""
import base64
import http.client
import json
import logging
import mimetypes
import os
import posixpath
import shutil
import stat as statmod
import datetime
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
import uuid

PORT = 5000                 # satu port untuk seluruh dashboard
RCLONE_HOST = "127.0.0.1"
RCLONE_PORT = 5572
# Mode penyimpanan lokal Android. Tidak membutuhkan Rclone atau Google Drive.
STORAGE_MODE = "android"  # "android" atau "drive"; default aman ke penyimpanan HP
mode_lock = threading.RLock()

def is_local_mode():
    return STORAGE_MODE == "android"

def current_remote():
    return "local:" if is_local_mode() else "cloud:"
LOCAL_ROOT = os.path.expanduser("~/storage/shared")
try:
    os.makedirs(LOCAL_ROOT, exist_ok=True)
    # Pastikan direktori benar-benar dapat ditulis.
    _probe = os.path.join(LOCAL_ROOT, ".cloud_write_test")
    with open(_probe, "wb") as _f:
        _f.write(b"1")
    os.remove(_probe)
except OSError:
    # Jika izin penyimpanan Android belum diberikan, gunakan fallback privat agar
    # aplikasi tetap bisa dijalankan; jalankan termux-setup-storage untuk shared storage.
    LOCAL_ROOT = os.path.join(os.path.expanduser("~"), "CloudV5Storage")
    os.makedirs(LOCAL_ROOT, exist_ok=True)
REMOTE = "local:"
RC_USER = "cloud"
RC_PASS = secrets.token_urlsafe(32)  # acak setiap kali server dijalankan

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Aset UI cloud disajikan oleh cloud_bp.py dari static/cloud/
DATA_DIR = os.path.join(BASE_DIR, "data", "cloud")
os.makedirs(DATA_DIR, exist_ok=True)

# Hanya operasi ini yang boleh diteruskan ke Rclone
ALLOWED_OPS = {
    "/operations/about",
    "/operations/list",
    "/operations/stat",
    "/operations/mkdir",
    "/operations/moveto",
    "/operations/purge",
    "/operations/deletefile",
    "/operations/publiclink",
}

ALLOWED_HOSTS = {f"localhost:{PORT}", f"127.0.0.1:{PORT}"}
ALLOWED_ORIGINS = {f"http://localhost:{PORT}", f"http://127.0.0.1:{PORT}"}

MAX_JSON_BODY = 1 * 1024 * 1024
MAX_UPLOAD_BYTES = 10 * 1024 ** 3     # 10 GB per file, ubah jika perlu
MAX_NAME_BYTES = 255
CHUNK = 256 * 1024
UNSAFE_INLINE_TYPES = ("text/html", "application/xhtml+xml", "text/xml", "application/xml")

log = logging.getLogger("cloud")

rclone_proc = None

# ---------------------------------------------------------------------------
# Cache disk: daftar folder & info kuota, tetap ada saat server dimatikan
# ---------------------------------------------------------------------------
CACHE_FILE = os.path.join(DATA_DIR, "cache.json")
cache_lock = threading.Lock()
cache_data = {"lists": {}, "about": None}


def load_cache():
    global cache_data
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("lists"), dict):
            cache_data = {"lists": data["lists"], "about": data.get("about")}
            log.info("Cache dimuat: %d folder tersimpan", len(cache_data["lists"]))
    except FileNotFoundError:
        pass
    except (OSError, ValueError):
        log.warning("cache.json rusak, cache diabaikan")


def _save_cache_locked():
    try:
        tmp = CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cache_data, fh, ensure_ascii=False)
        os.replace(tmp, CACHE_FILE)
    except OSError as exc:
        log.warning("Gagal menyimpan cache: %s", exc)


def cache_put_list(remote, items):
    with cache_lock:
        cache_data["lists"][remote] = {"ts": time.time(), "list": items}
        _save_cache_locked()


def cache_put_about(data):
    with cache_lock:
        cache_data["about"] = {"ts": time.time(), **data}
        _save_cache_locked()


def cache_invalidate(exact=(), prefixes=()):
    """Hapus cache folder yang berubah. Prefix kosong berarti seluruh drive."""
    with cache_lock:
        lists = cache_data["lists"]
        for key in list(lists):
            hit = key in exact or any(
                p == "" or key == p or key.startswith(p + "/") for p in prefixes)
            if hit:
                del lists[key]
        _save_cache_locked()


def after_mutation(rc_path, payload):
    if rc_path in ("/operations/mkdir", "/operations/deletefile"):
        cache_invalidate(exact=(posixpath.dirname(payload["remote"]),))
    elif rc_path == "/operations/purge":
        remote = payload["remote"]
        cache_invalidate(exact=(posixpath.dirname(remote),), prefixes=(remote,))
    elif rc_path == "/operations/moveto":
        src, dst = payload["srcRemote"], payload["dstRemote"]
        cache_invalidate(exact=(posixpath.dirname(src), posixpath.dirname(dst)),
                         prefixes=(src,))


class CloudError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# Tempat sampah: item dipindah ke folder TRASH_DIR di Drive. Daftar asalnya
# disimpan di trash.json, dan item dihapus permanen setelah TRASH_DAYS hari.
# ---------------------------------------------------------------------------
TRASH_DIR = ".cloud_trash"           # folder sistem di root Drive (disembunyikan)
TRASH_DAYS = 30
TRASH_FILE = os.path.join(DATA_DIR, "trash.json")
trash_lock = threading.RLock()
trash_items = []


def load_trash():
    global trash_items
    try:
        with open(TRASH_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return
    except (OSError, ValueError):
        log.warning("trash.json rusak, daftar tempat sampah diabaikan")
        return
    if isinstance(data, list):
        trash_items = [
            r for r in data
            if isinstance(r, dict)
            and isinstance(r.get("id"), str) and isinstance(r.get("name"), str)
            and isinstance(r.get("origPath"), str)
            and isinstance(r.get("deletedAt"), (int, float))
        ]
        log.info("Tempat sampah: %d item tercatat", len(trash_items))


def _save_trash_locked():
    try:
        tmp = TRASH_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(trash_items, fh, ensure_ascii=False)
        os.replace(tmp, TRASH_FILE)
    except OSError as exc:
        log.warning("Gagal menyimpan trash.json: %s", exc)


def _public_rec(rec):
    return {
        "id": rec["id"],
        "name": rec["name"],
        "origPath": rec["origPath"],
        "isDir": bool(rec.get("isDir")),
        "size": int(rec.get("size") or 0),
        "deletedAt": rec["deletedAt"],
        "expiresAt": rec["deletedAt"] + TRASH_DAYS * 86400,
    }


def find_trash_locked(tid):
    for rec in trash_items:
        if rec["id"] == tid:
            return rec
    raise CloudError("Item tidak ditemukan di tempat sampah", 404)


def unique_name(parent, name):
    """Memberi nama baru jika nama asli sudah dipakai di lokasi tujuan."""
    def taken(candidate):
        return stat_item(f"{parent}/{candidate}" if parent else candidate) is not None

    if not taken(name):
        return name
    root, ext = os.path.splitext(name)
    for i in range(1, 100):
        suffix = " (dipulihkan)" if i == 1 else f" (dipulihkan {i})"
        candidate = root + suffix + ext
        if not taken(candidate):
            return candidate
    raise CloudError("Terlalu banyak item dengan nama yang sama di lokasi asal")


def purge_expired():
    cutoff = time.time() - TRASH_DAYS * 86400
    with trash_lock:
        keep = []
        for rec in trash_items:
            if rec["deletedAt"] >= cutoff:
                keep.append(rec)
                continue
            try:
                _purge_trash_dir(rec["id"])
                log.info("Tempat sampah: '%s' kedaluwarsa, dihapus permanen", rec["name"])
            except (CloudError, OSError) as exc:
                log.warning("Gagal menghapus kedaluwarsa '%s': %s", rec["name"], exc)
                keep.append(rec)
        if len(keep) != len(trash_items):
            trash_items[:] = keep
            _save_trash_locked()


def trash_list():
    purge_expired()
    with trash_lock:
        items = [_public_rec(r) for r in trash_items]
    return sorted(items, key=lambda r: r["deletedAt"], reverse=True)


def trash_move(remote):
    if remote == TRASH_DIR or remote.startswith(TRASH_DIR + "/"):
        raise CloudError("Item ini sudah ada di tempat sampah")
    item = stat_item(remote)
    if item is None:
        raise CloudError("Item tidak ditemukan", 404)
    name = item.get("Name") or posixpath.basename(remote)
    tid = uuid.uuid4().hex
    rel_dir = f"{TRASH_DIR}/{tid}"
    with trash_lock:
        rc_call("/operations/mkdir", {"fs": REMOTE, "remote": rel_dir})
        try:
            rc_call("/operations/moveto", {
                "srcFs": REMOTE, "srcRemote": remote,
                "dstFs": REMOTE, "dstRemote": f"{rel_dir}/{name}",
            })
        except CloudError:
            _purge_trash_dir(tid)   # bersihkan folder kosong yang tertinggal
            raise
        rec = {
            "id": tid, "name": name, "origPath": remote,
            "isDir": bool(item.get("IsDir")),
            "size": max(0, int(item.get("Size") or 0)),
            "deletedAt": time.time(),
        }
        trash_items.append(rec)
        _save_trash_locked()
    cache_invalidate(exact=(posixpath.dirname(remote),), prefixes=(remote,))
    return _public_rec(rec)


def trash_restore(tid):
    with trash_lock:
        rec = find_trash_locked(tid)
        src = f"{TRASH_DIR}/{tid}/{rec['name']}"
        if stat_item(src) is None:
            trash_items.remove(rec)
            _save_trash_locked()
            raise CloudError("Item sudah tidak ada di Drive", 404)
        parent = posixpath.dirname(rec["origPath"])
        to_root = bool(parent) and stat_item(parent) is None
        if to_root:
            parent = ""                       # folder asal sudah tidak ada
        name = unique_name(parent, rec["name"])
        dst = f"{parent}/{name}" if parent else name
        rc_call("/operations/moveto", {
            "srcFs": REMOTE, "srcRemote": src, "dstFs": REMOTE, "dstRemote": dst,
        })
        _purge_trash_dir(tid)
        trash_items.remove(rec)
        _save_trash_locked()
    cache_invalidate(exact=(parent,), prefixes=(dst,))
    return {"path": dst, "renamed": name != rec["name"], "toRoot": to_root}


def trash_delete(tid):
    with trash_lock:
        rec = find_trash_locked(tid)
        _purge_trash_dir(tid)
        trash_items.remove(rec)
        _save_trash_locked()
    return {"ok": True}


def trash_empty():
    done = failed = 0
    with trash_lock:
        for rec in list(trash_items):
            try:
                _purge_trash_dir(rec["id"])
                trash_items.remove(rec)
                done += 1
            except (CloudError, OSError):
                failed += 1
        _save_trash_locked()
    return {"deleted": done, "failed": failed}


# ---------------------------------------------------------------------------
# Validasi input
# ---------------------------------------------------------------------------
def check_remote_path(value, allow_empty=False):
    if not isinstance(value, str) or len(value) > 4096:
        raise CloudError("Path tidak valid")
    if "\x00" in value or "\\" in value or value.startswith("/"):
        raise CloudError("Path tidak valid")
    parts = [p for p in value.split("/") if p]
    if any(p in (".", "..") for p in parts):
        raise CloudError("Path tidak valid")
    if not parts and not allow_empty:
        raise CloudError("Path tidak boleh kosong")
    return "/".join(parts)


def check_name(value):
    if not isinstance(value, str) or not value:
        raise CloudError("Nama tidak valid")
    if value != value.strip() or value in (".", ".."):
        raise CloudError("Nama tidak valid")
    if len(value.encode("utf-8")) > MAX_NAME_BYTES:
        raise CloudError("Nama terlalu panjang")
    if any(c in value for c in '/\\"') or any(ord(c) < 32 for c in value):
        raise CloudError("Nama mengandung karakter yang tidak diizinkan")
    return value


def check_user_path(value, allow_empty=False):
    """Path dari klien: aturan biasa, plus folder tempat sampah tidak boleh disentuh langsung."""
    path = check_remote_path(value, allow_empty=allow_empty)
    if path == TRASH_DIR or path.startswith(TRASH_DIR + "/"):
        raise CloudError("Path tidak valid", 403)
    return path


def validate_op(rc_path, body):
    """Membangun ulang payload dari nol: field tambahan dari klien dibuang."""
    if not isinstance(body, dict):
        raise CloudError("Body harus berupa JSON object")
    # fs tidak dipercaya dari klien; backend menentukan remote berdasarkan pilihan aktif.
    active_remote = current_remote()

    if rc_path == "/operations/about":
        return {"fs": active_remote}

    if rc_path == "/operations/list":
        return {"fs": active_remote, "remote": check_user_path(body.get("remote", ""), allow_empty=True)}

    if rc_path in ("/operations/stat", "/operations/mkdir",
                   "/operations/purge", "/operations/deletefile"):
        # remote kosong ditolak: mencegah purge/hapus seluruh drive
        return {"fs": active_remote, "remote": check_user_path(body.get("remote"))}

    if rc_path == "/operations/moveto":
        return {
            "srcFs": active_remote,
            "srcRemote": check_user_path(body.get("srcRemote")),
            "dstFs": active_remote,
            "dstRemote": check_user_path(body.get("dstRemote")),
        }

    if rc_path == "/operations/publiclink":
        # tautan publik hanya untuk satu item, tidak pernah untuk seluruh drive
        return {"fs": active_remote, "remote": check_user_path(body.get("remote")),
                "unlink": body.get("unlink") is True}

    raise CloudError("Operasi tidak diizinkan", 403)


def parse_range(header, size):
    """Mengembalikan (start, end, partial). Melempar ValueError jika tidak valid."""
    if not header:
        return 0, size - 1, False
    if not header.startswith("bytes=") or "," in header:
        raise ValueError("range")
    start_s, _, end_s = header[6:].strip().partition("-")
    if start_s == "":
        suffix = int(end_s)
        if suffix <= 0 or size == 0:
            raise ValueError("range")
        start, end = max(0, size - suffix), size - 1
    else:
        start = int(start_s)
        end = min(int(end_s), size - 1) if end_s else size - 1
    if start < 0 or start >= size or end < start:
        raise ValueError("range")
    return start, end, True


# ---------------------------------------------------------------------------
# Komunikasi dengan Rclone RC
# ---------------------------------------------------------------------------
def rc_headers(extra=None):
    token = base64.b64encode(f"{RC_USER}:{RC_PASS}".encode("utf-8")).decode("ascii")
    headers = {"Authorization": f"Basic {token}"}
    if extra:
        headers.update(extra)
    return headers


def _local_path(remote, allow_empty=False):
    remote = check_remote_path(remote, allow_empty=allow_empty)
    root = os.path.realpath(LOCAL_ROOT)
    target = os.path.realpath(os.path.join(root, *remote.split("/"))) if remote else root
    if target != root and not target.startswith(root + os.sep):
        raise CloudError("Path di luar penyimpanan tidak diizinkan", 403)
    return target

def _local_item(path):
    # LOCAL_ROOT may be a Termux symlink to /storage/emulated/0.
    # Normalize both sides before relpath so returned paths never contain ../.
    root = os.path.realpath(LOCAL_ROOT)
    path = os.path.realpath(path)
    st = os.stat(path)
    name = os.path.basename(path) or "Root"
    is_dir = statmod.S_ISDIR(st.st_mode)
    if path != root and not path.startswith(root + os.sep):
        raise CloudError("Path di luar penyimpanan tidak diizinkan", 403)
    return {"Name": name, "Path": os.path.relpath(path, root).replace(os.sep, "/") if path != root else "",
            "Size": 0 if is_dir else st.st_size, "IsDir": is_dir,
            "ModTime": datetime.datetime.fromtimestamp(st.st_mtime, datetime.timezone.utc).isoformat(),
            "MimeType": "inode/directory" if is_dir else (mimetypes.guess_type(name)[0] or "application/octet-stream")}

def _local_rc_json(rc_path, payload):
    try:
        if rc_path == "/operations/about":
            usage = shutil.disk_usage(LOCAL_ROOT)
            used = sum(os.path.getsize(os.path.join(dp, fn)) for dp, _, files in os.walk(LOCAL_ROOT) for fn in files if os.path.isfile(os.path.join(dp, fn)))
            return 200, {"used": used, "total": usage.total, "free": max(0, usage.total-used)}
        if rc_path == "/operations/list":
            path = _local_path(payload.get("remote", ""), allow_empty=True)
            if not os.path.isdir(path): return 400, {"error": "Path bukan folder"}
            items=[]
            for name in os.listdir(path):
                if not payload.get("remote") and name == TRASH_DIR: continue
                try: items.append(_local_item(os.path.join(path, name)))
                except OSError: continue
            items.sort(key=lambda x: (not x["IsDir"], x["Name"].casefold()))
            return 200, {"list": items}
        if rc_path == "/operations/stat":
            path = _local_path(payload.get("remote", ""))
            if not os.path.exists(path): return 404, {"error": "object not found"}
            return 200, {"item": _local_item(path)}
        if rc_path == "/operations/mkdir":
            os.makedirs(_local_path(payload["remote"]), exist_ok=True); return 200, {"ok": True}
        if rc_path in ("/operations/purge", "/operations/deletefile"):
            path = _local_path(payload["remote"])
            if not os.path.exists(path): return 404, {"error": "object not found"}
            if os.path.isdir(path):
                if rc_path == "/operations/deletefile": return 400, {"error": "Is a directory"}
                shutil.rmtree(path)
            else: os.remove(path)
            return 200, {"ok": True}
        if rc_path == "/operations/moveto":
            src = _local_path(payload["srcRemote"]); dst = _local_path(payload["dstRemote"])
            if not os.path.exists(src): return 404, {"error": "source not found"}
            if os.path.exists(dst): return 400, {"error": "destination already exists"}
            os.makedirs(os.path.dirname(dst), exist_ok=True); shutil.move(src, dst); return 200, {"ok": True}
        if rc_path == "/operations/publiclink":
            return 501, {"error": "Tautan publik tidak tersedia untuk penyimpanan lokal"}
        return 404, {"error": "Operasi tidak didukung"}
    except CloudError as e: return e.status, {"error": str(e)}
    except FileNotFoundError: return 404, {"error": "object not found"}
    except OSError as e: return 500, {"error": str(e)}

def rc_json(rc_path, payload, timeout=600):
    # Pemeriksaan kesiapan Rclone harus selalu menuju Rclone RC, bahkan saat
    # mode yang aktif masih Android. Kalau tidak, pemilihan Google Drive akan
    # menunggu 15 detik karena /core/version dijawab backend lokal.
    if is_local_mode() and rc_path != "/core/version":
        return _local_rc_json(rc_path, payload)
    # API internal aplikasi tetap memakai /operations/moveto, tetapi Rclone RC
    # menyediakan /operations/movefile untuk memindahkan satu file/folder.
    # Terjemahkan endpoint sebelum meneruskan permintaan ke Rclone.
    rc_endpoint = "/operations/movefile" if rc_path == "/operations/moveto" else rc_path
    conn = http.client.HTTPConnection(RCLONE_HOST, RCLONE_PORT, timeout=timeout)
    try:
        data = json.dumps(payload).encode("utf-8")
        conn.request("POST", rc_endpoint, body=data,
                     headers=rc_headers({"Content-Type": "application/json",
                                         "Content-Length": str(len(data))}))
        resp = conn.getresponse()
        raw = resp.read()
        status = resp.status
    finally:
        conn.close()
    try:
        parsed = json.loads(raw.decode("utf-8") or "{}")
    except ValueError:
        parsed = {"error": raw[:300].decode("utf-8", "replace")}
    return status, parsed


def rc_status():
    try:
        status, _ = rc_json("/core/version", {}, timeout=3)
        return status
    except OSError:
        return None


def rc_call(rc_path, payload, timeout=600):
    """Panggil Rclone; melempar CloudError jika gagal (untuk pemakaian internal)."""
    status, data = rc_json(rc_path, payload, timeout=timeout)
    if status >= 400:
        message = data.get("error") if isinstance(data, dict) else None
        raise CloudError(str(message or "Permintaan ke Drive gagal")[:300], 500)
    return data if isinstance(data, dict) else {}


def stat_item(remote):
    """Info item di Drive, atau None jika tidak ada."""
    status, data = rc_json("/operations/stat", {"fs": current_remote(), "remote": remote})
    if status == 200 and isinstance(data, dict):
        return data.get("item")
    message = str(data.get("error", "")) if isinstance(data, dict) else ""
    if "not found" in message.lower():
        return None
    raise CloudError("Gagal memeriksa item di Drive", 500)


def _purge_trash_dir(tid):
    rel = f"{TRASH_DIR}/{tid}"
    if stat_item(rel) is not None:
        rc_call("/operations/purge", {"fs": current_remote(), "remote": rel})


def port_open(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        return s.connect_ex((RCLONE_HOST, port)) == 0


def start_rclone():
    global rclone_proc
    if port_open(RCLONE_PORT):
        log.error("Port %d sudah dipakai proses lain (kemungkinan rclone rcd lama tanpa password). "
                  "Matikan proses itu dulu, lalu jalankan ulang.", RCLONE_PORT)
        raise CloudError("Rclone tidak siap. Periksa log server dan konfigurasi remote cloud:.", 503)

    env = dict(os.environ, RCLONE_RC_USER=RC_USER, RCLONE_RC_PASS=RC_PASS)
    try:
        rclone_proc = subprocess.Popen(
            ["rclone", "rcd", f"--rc-addr={RCLONE_HOST}:{RCLONE_PORT}"],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        log.error("rclone tidak ditemukan. Pastikan sudah terinstal dan ada di PATH.")
        raise CloudError("Rclone tidak siap. Periksa log server dan konfigurasi remote cloud:.", 503)

    deadline = time.time() + 15
    while time.time() < deadline:
        if rclone_proc.poll() is not None:
            log.error("rclone berhenti sebelum siap. Cek konfigurasi remote 'cloud:'.")
            raise CloudError("Rclone tidak siap. Periksa log server dan konfigurasi remote cloud:.", 503)
        if rc_status() == 200:
            log.info("Rclone API siap.")
            return
        time.sleep(0.3)
    log.error("Rclone API tidak siap dalam 15 detik.")
    stop_rclone()
    raise CloudError("Rclone tidak siap. Periksa log server dan konfigurasi remote cloud:.", 503)


def stop_rclone():
    proc = rclone_proc
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


