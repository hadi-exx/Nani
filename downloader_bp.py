# downloader_bp.py - Universal Downloader (Blueprint, dashboard port 5000)
#
# Fitur:
#   - Download queue (banyak URL, diproses berurutan)
#   - Progress bar real-time (via progress hook yt-dlp)
#   - Format conversion (video MP4 / audio MP3)
#   - Streaming video (preview inline, proxy Range)
#   - File organizer (folder per platform)
#   - Download history (persisten + hapus 1 item / clear semua)
#
# Halaman : /downloader/            (layer ?embed=1 dari dashboard)
# API     : /downloader/api/*
import os
import re
import json
import time
import uuid
import queue
import threading
import tempfile

import requests
import yt_dlp
from urllib.parse import urlparse
from ytdlp_cookies import cookie_opts, cookie_file
from flask import (
    Blueprint, jsonify, request, send_file,
    send_from_directory, Response, stream_with_context,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UI_DIR = os.path.join(BASE_DIR, "static", "downloader")
DOWNLOAD_FOLDER = os.path.join(tempfile.gettempdir(), "web_downloader")
DATA_DIR = os.path.join(BASE_DIR, "data", "downloader")
HISTORY_FILE = os.path.join(DATA_DIR, "history.json")
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)
os.makedirs(DATA_DIR, exist_ok=True)

bp = Blueprint("downloader", __name__, url_prefix="/downloader")

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Accept': 'application/json, text/plain, */*'
}

# ============================================================
# Helpers
# ============================================================
def clean_filename(name):
    name = re.sub(r'[\\/*?:"<>|#]', '', name or '')
    return name.strip()[:60] or "media_download"

def _platform_folder(platform):
    p = (platform or 'other').lower()
    p = re.sub(r'[^a-z0-9]+', '_', p).strip('_') or 'other'
    folder = os.path.join(DOWNLOAD_FOLDER, p)
    os.makedirs(folder, exist_ok=True)
    return folder

def _newest_file(folder, exclude=None):
    files = [
        os.path.join(folder, f) for f in os.listdir(folder)
        if os.path.isfile(os.path.join(folder, f)) and not f.endswith('.part')
    ]
    if exclude:
        files = [f for f in files if f != exclude]
    if not files:
        return None
    return max(files, key=os.path.getmtime)

def _human_size(n):
    if not n:
        return ''
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


# --- Header tambahan untuk CDN (Referer / Origin / Cookie) -------------------
# Banyak CDN (TikTok, IG, FB, X) menolak permintaan tanpa Referer yang cocok,
# dan konten private butuh cookie. Dipakai oleh /stream_remote.
_REFERER_MAP = {
    'tiktok.com': 'https://www.tiktok.com/',
    'tiktokcdn.com': 'https://www.tiktok.com/',
    'tiktokcdn-us.com': 'https://www.tiktok.com/',
    'instagram.com': 'https://www.instagram.com/',
    'cdninstagram.com': 'https://www.instagram.com/',
    'facebook.com': 'https://www.facebook.com/',
    'fbcdn.net': 'https://www.facebook.com/',
    'twitter.com': 'https://x.com/',
    'x.com': 'https://x.com/',
    'twimg.com': 'https://x.com/',
    'youtube.com': 'https://www.youtube.com/',
    'googlevideo.com': 'https://www.youtube.com/',
    'pinterest.com': 'https://www.pinterest.com/',
    'pinimg.com': 'https://www.pinterest.com/',
    'reddit.com': 'https://www.reddit.com/',
    'redd.it': 'https://www.reddit.com/',
    'vimeo.com': 'https://vimeo.com/',
    'dailymotion.com': 'https://www.dailymotion.com/',
    'twitch.tv': 'https://www.twitch.tv/',
    'bilibili.com': 'https://www.bilibili.com/',
}

def _host(url):
    try:
        return (urlparse(url).hostname or '').lower()
    except Exception:
        return ''

def _referer_for(url, ref=None):
    """Referer yang cocok untuk CDN. Prioritas: halaman asal (ref), lalu peta host."""
    if ref:
        try:
            p = urlparse(ref)
            if p.scheme in ('http', 'https') and p.hostname:
                return f"{p.scheme}://{p.hostname}/"
        except Exception:
            pass
    host = _host(url)
    for domain, referer in _REFERER_MAP.items():
        if host == domain or host.endswith('.' + domain):
            return referer
    return ''

def _cookie_header(url):
    """Ambil cookie yang domainnya cocok dari cookies.txt (format Netscape)."""
    path = cookie_file()
    if not path:
        return ''
    host = _host(url)
    if not host:
        return ''
    found = []
    try:
        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                parts = line.split('\t')
                if len(parts) < 7:
                    continue
                domain, _flag, _path, _secure, _exp, name, value = parts[:7]
                d = domain.lstrip('.').lower()
                if d and (host == d or host.endswith('.' + d)):
                    found.append(f"{name}={value}")
    except Exception:
        return ''
    return '; '.join(found)

def _stream_headers(url, ref=None, with_range=None):
    headers = {
        'User-Agent': HEADERS['User-Agent'],
        'Accept': '*/*',
        # Jangan minta kompresi: kalau di-decode, Content-Length jadi tidak cocok.
        'Accept-Encoding': 'identity',
    }
    referer = _referer_for(url, ref)
    if referer:
        headers['Referer'] = referer
        headers['Origin'] = referer.rstrip('/')
    cookies = _cookie_header(url)
    if cookies:
        headers['Cookie'] = cookies
    if with_range:
        headers['Range'] = with_range
    return headers


# --- Pemilihan format yang bisa diputar di browser --------------------------
def _muxed_formats(info):
    return [
        f for f in (info.get('formats') or [])
        if f.get('url')
        and (f.get('vcodec') or 'none') != 'none'
        and (f.get('acodec') or 'none') != 'none'
    ]

def _pick_muxed(info):
    """URL muxed (video+audio) yang paling ramah browser, atau None.

    formats[-1] sering video-only (mis. YouTube itag 401 = AV1 4K tanpa audio),
    jadi player hanya menampilkan kotak hitam. Urutan prioritas:
    mp4 h264 <=720p > mp4 <=720p > muxed lain <=720p > mp4 > muxed apa pun.
    """
    muxed = _muxed_formats(info)
    if not muxed:
        return None

    def h(f):
        return f.get('height') or 0

    def is_mp4(f):
        return f.get('ext') == 'mp4' or 'mp4' in (f.get('mime_type') or '')

    def is_h264(f):
        v = (f.get('vcodec') or '').lower()
        return 'avc1' in v or 'h264' in v

    tiers = [
        [f for f in muxed if is_mp4(f) and is_h264(f) and 0 < h(f) <= 720],
        [f for f in muxed if is_mp4(f) and 0 < h(f) <= 720],
        [f for f in muxed if 0 < h(f) <= 720],
        [f for f in muxed if is_mp4(f)],
        muxed,
    ]
    for tier in tiers:
        if tier:
            return max(tier, key=h)['url']
    return muxed[-1]['url']

def _best_any(info):
    """URL terbaik apa pun (boleh video-only) sebagai cadangan."""
    formats = [f for f in (info.get('formats') or []) if f.get('url')]
    if formats:
        with_height = [f for f in formats if f.get('height')]
        return (max(with_height, key=lambda f: f['height']) if with_height else formats[-1])['url']
    return info.get('url')

def _progressive_preview(url, base_opts):
    """Coba sekali lagi khusus untuk preview: minta format progresif (muxed).

    YouTube (client web) tidak lagi menyediakan format muxed; client
    android masih memberi itag 18 (360p, h264 + aac). Ekstraksi ini hanya
    dipakai untuk URL preview, jadi kualitas download tidak terpengaruh.
    """
    opts = dict(base_opts)
    opts.update({
        'format': '18/best[ext=mp4][vcodec!=none][acodec!=none]/best[acodec!=none]',
        # Client 'android' masih memberi itag 18 (360p h264+aac) dan URL-nya
        # bisa diunduh; 'android_vr' memberi URL yang ditolak 403.
        'extractor_args': {'youtube': {'player_client': ['android', 'web']}},
    })
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False) or {}
    except Exception:
        return None
    chosen = info.get('url')
    if chosen:
        return chosen
    return _pick_muxed(info)

# ============================================================
# History (persisten di data/downloader/history.json)
# ============================================================
_hist_lock = threading.Lock()

def _load_history():
    try:
        with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return []

def _save_history(items):
    try:
        with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
            json.dump(items[:100], f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def add_history(item):
    with _hist_lock:
        items = _load_history()
        items.insert(0, item)
        _save_history(items)

# ============================================================
# Job queue
# ============================================================
JOBS = {}
JOB_ORDER = []
_jobs_lock = threading.Lock()
_job_queue = queue.Queue()
_worker_started = False

def _start_worker():
    global _worker_started
    if _worker_started:
        return
    _worker_started = True
    threading.Thread(target=_worker_loop, daemon=True).start()

def _public_job(job):
    return {
        'id': job['id'],
        'url': job.get('url', ''),
        'title': job.get('title') or 'Menunggu...',
        'platform': job.get('platform') or 'Media',
        'status': job.get('status', 'queued'),
        'progress': job.get('progress', 0),
        'speed': _human_size(job.get('speed')) + '/s' if job.get('speed') else '',
        'eta': job.get('eta'),
        'size': _human_size(job.get('size')),
        'filename': job.get('filename', ''),
        'error': job.get('error', ''),
        'audio_only': job.get('audio_only', False),
        'created_at': job.get('created_at', 0),
    }

def _worker_loop():
    while True:
        job_id = _job_queue.get()
        try:
            job = JOBS.get(job_id)
            if not job or job.get('cancelled'):
                continue
            _run_job(job)
        except Exception as e:
            job = JOBS.get(job_id)
            if job:
                job['status'] = 'error'
                job['error'] = str(e)
        finally:
            _job_queue.task_done()

def _run_job(job):
    job['status'] = 'downloading'
    job['progress'] = 0
    # Folder per platform, lalu subfolder per job supaya file tidak saling timpa
    # (penting untuk konversi MP3 yang menghapus file sumber).
    folder = os.path.join(_platform_folder(job.get('platform')), job['id'])
    os.makedirs(folder, exist_ok=True)

    def hook(d):
        if job.get('cancelled'):
            raise Exception('Dibatalkan oleh pengguna')
        if d.get('status') == 'downloading':
            total = d.get('total_bytes') or d.get('total_bytes_estimate')
            done = d.get('downloaded_bytes', 0)
            job['progress'] = round(done / total * 100, 1) if total else job.get('progress', 0)
            job['speed'] = d.get('speed')
            job['eta'] = d.get('eta')
            job['size'] = total
        elif d.get('status') == 'finished':
            job['progress'] = 100

    ydl_opts = {
        **cookie_opts(),
        'outtmpl': os.path.join(folder, '%(title).60s.%(ext)s'),
        'quiet': True,
        'no_warnings': True,
        'noprogress': True,
        'user_agent': HEADERS['User-Agent'],
        'progress_hooks': [hook],
        'noplaylist': True,
        'retries': 3,
    }

    if job.get('audio_only'):
        ydl_opts.update({
            'format': 'bestaudio/best',
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
        })
    else:
        ydl_opts.update({
            'format': job.get('format') or
                      'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
            'merge_output_format': 'mp4',
        })

    job['status'] = 'converting' if job.get('audio_only') else 'downloading'
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(job['url'], download=True)

    if job.get('cancelled'):
        job['status'] = 'error'
        job['error'] = 'Dibatalkan'
        return

    path = _newest_file(folder)
    if not path:
        job['status'] = 'error'
        job['error'] = 'File hasil download tidak ditemukan'
        return

    job['title'] = info.get('title') or job.get('title')
    job['platform'] = info.get('extractor_key') or job.get('platform')
    job['filename'] = os.path.basename(path)
    job['filepath'] = path
    job['size'] = os.path.getsize(path)
    job['progress'] = 100
    job['status'] = 'done'
    job['finished_at'] = time.time()

    add_history({
        'id': uuid.uuid4().hex[:12],
        'job_id': job['id'],
        'title': job['title'],
        'platform': job['platform'],
        'filename': job['filename'],
        'size': job['size'],
        'audio_only': job.get('audio_only', False),
        'url': job.get('url', ''),
        'path': job.get('filepath', ''),
        'created_at': time.time(),
    })

# ============================================================
# Halaman
# ============================================================
@bp.get('/')
@bp.get('')
def home():
    return send_from_directory(UI_DIR, 'index.html')

# ============================================================
# Info / preview metadata (dipakai tombol "Process")
# ============================================================
@bp.post('/api/download')
def process_media():
    data = request.get_json(silent=True) or {}
    url = (data.get('url') or '').strip()
    if not url:
        return jsonify({'error': 'URL tidak boleh kosong!'}), 400

    try:
        r = requests.head(url, allow_redirects=True, headers=HEADERS, timeout=6)
        url = r.url
    except Exception:
        pass

    ydl_opts = {
        **cookie_opts(),
        'quiet': True,
        'no_warnings': True,
        'dump_single_json': True,
        'user_agent': HEADERS['User-Agent'],
        'age_limit': 99,
        'noplaylist': True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if info:
            images = []
            if info.get('entries'):
                for entry in info['entries']:
                    if entry and entry.get('url'):
                        images.append(entry['url'])
            has_formats = bool(info.get('formats'))
            is_image = bool(images) or (
                not has_formats and not info.get('url')
            ) or (info.get('ext') in ('jpg', 'jpeg', 'png', 'webp', 'gif'))
            # Gambar tunggal (tanpa entries) tetap bisa diunduh lewat /download_file.
            if is_image and not images and info.get('url'):
                images = [info['url']]
            # Extractors yang langsung memberi satu file siap pakai tidak punya
            # daftar formats (atau codec-nya tidak dilaporkan), jadi URL
            # tunggalnya sudah pasti muxed.
            preview_url = _pick_muxed(info)
            if not preview_url and info.get('url'):
                codec_unknown = not info.get('vcodec') and not info.get('acodec')
                if not has_formats or codec_unknown:
                    preview_url = info['url']
            if not preview_url and not is_image:
                # Format muxed tidak ada (umum di YouTube) -> coba sekali lagi.
                preview_url = _progressive_preview(url, ydl_opts)
            return jsonify({
                'success': True,
                'title': info.get('title', 'Media'),
                'thumbnail': info.get('thumbnail', ''),
                'uploader': info.get('uploader', info.get('extractor', 'Unknown')),
                'platform': info.get('extractor_key', 'Media'),
                'is_image': is_image,
                'images': images,
                # Hanya URL muxed yang bisa diputar; kalau tidak ada, preview
                # dimatikan daripada menampilkan kotak hitam tanpa suara.
                'download_url': preview_url or _best_any(info),
                'preview_url': preview_url,
                'playable': bool(preview_url) and not is_image,
                'raw_url': url,
                'duration': info.get('duration'),
            })
    except Exception:
        pass

    return jsonify({'error': 'Gagal mengambil data dari URL ini. Pastikan link valid / akun tidak private.'}), 400

# ============================================================
# Queue API
# ============================================================
@bp.post('/api/queue')
def api_queue():
    data = request.get_json(silent=True) or {}
    urls = data.get('urls')
    if not urls:
        single = (data.get('url') or '').strip()
        urls = [single] if single else []
    urls = [u.strip() for u in urls if u and u.strip()]
    if not urls:
        return jsonify({'error': 'Tidak ada URL'}), 400

    audio_only = bool(data.get('audio_only'))
    fmt = data.get('format') or ''
    title_hint = data.get('title') or ''
    platform_hint = data.get('platform') or ''

    _start_worker()
    created = []
    for u in urls:
        job = {
            'id': uuid.uuid4().hex[:10],
            'url': u,
            'title': title_hint,
            'platform': platform_hint or 'Media',
            'status': 'queued',
            'progress': 0,
            'audio_only': audio_only,
            'format': fmt,
            'created_at': time.time(),
            'cancelled': False,
        }
        with _jobs_lock:
            JOBS[job['id']] = job
            JOB_ORDER.append(job['id'])
        _job_queue.put(job['id'])
        created.append(job['id'])

    return jsonify({'ok': True, 'jobs': created})

@bp.get('/api/jobs')
def api_jobs():
    with _jobs_lock:
        jobs = [_public_job(JOBS[jid]) for jid in JOB_ORDER if jid in JOBS]
    return jsonify({'jobs': jobs})

@bp.post('/api/jobs/<job_id>/cancel')
def api_job_cancel(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({'error': 'Job tidak ditemukan'}), 404
    job['cancelled'] = True
    if job.get('status') == 'queued':
        job['status'] = 'error'
        job['error'] = 'Dibatalkan'
    return jsonify({'ok': True})

@bp.post('/api/jobs/<job_id>/retry')
def api_job_retry(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({'error': 'Job tidak ditemukan'}), 404
    job.update({'status': 'queued', 'progress': 0, 'error': '', 'cancelled': False})
    _start_worker()
    _job_queue.put(job_id)
    return jsonify({'ok': True})

@bp.delete('/api/jobs/clear')
def api_jobs_clear():
    with _jobs_lock:
        done_ids = [jid for jid in JOB_ORDER if JOBS.get(jid, {}).get('status') in ('done', 'error')]
        for jid in done_ids:
            JOBS.pop(jid, None)
        JOB_ORDER[:] = [jid for jid in JOB_ORDER if jid in JOBS]
    return jsonify({'ok': True})

# ============================================================
# File & streaming
# ============================================================
@bp.get('/file/<job_id>')
def download_result(job_id):
    job = JOBS.get(job_id)
    if not job or job.get('status') != 'done' or not job.get('filepath'):
        return "File belum siap", 404
    return send_file(job['filepath'], as_attachment=True, download_name=job['filename'])

@bp.get('/stream/<job_id>')
def stream_result(job_id):
    job = JOBS.get(job_id)
    if not job or job.get('status') != 'done' or not job.get('filepath'):
        return "File belum siap", 404
    return send_file(job['filepath'], conditional=True)

@bp.get('/stream_remote')
def stream_remote():
    """Proxy streaming untuk preview video remote dengan dukungan Range.

    Query: url = URL media, ref = halaman asal (opsional, untuk Referer).
    Cookie dari cookies.txt ikut dikirim kalau domainnya cocok.
    """
    src = request.args.get('url')
    if not src:
        return "URL kosong", 400

    headers = _stream_headers(src, ref=request.args.get('ref'),
                             with_range=request.headers.get('Range'))
    try:
        r = requests.get(src, headers=headers, stream=True, timeout=20)
    except Exception as e:
        return f"Gagal stream: {e}", 502

    if r.status_code >= 400:
        r.close()
        return (f"Sumber menolak streaming (HTTP {r.status_code}). "
                "Mungkin butuh cookies.txt atau link sudah kedaluwarsa."), 502

    def generate():
        try:
            for chunk in r.iter_content(chunk_size=64 * 1024):
                if chunk:
                    yield chunk
        finally:
            r.close()

    resp_headers = {}
    for h in ('Content-Type', 'Content-Length', 'Content-Range', 'Accept-Ranges'):
        if h in r.headers:
            resp_headers[h] = r.headers[h]
    return Response(
        stream_with_context(generate()),
        status=r.status_code,
        headers=resp_headers,
        direct_passthrough=True,
    )

# ============================================================
# History API
# ============================================================
@bp.get('/api/history')
def api_history():
    return jsonify({'items': _load_history()})

@bp.delete('/api/history/<item_id>')
def api_history_delete(item_id):
    with _hist_lock:
        items = _load_history()
        items = [i for i in items if i.get('id') != item_id]
        _save_history(items)
    return jsonify({'ok': True})

@bp.delete('/api/history')
def api_history_clear():
    with _hist_lock:
        _save_history([])
    return jsonify({'ok': True})

@bp.get('/history/file/<item_id>')
def history_download(item_id):
    items = _load_history()
    item = next((i for i in items if i.get('id') == item_id), None)
    if not item:
        return "Riwayat tidak ditemukan", 404
    job = JOBS.get(item.get('job_id'))
    path = (job or {}).get('filepath') or item.get('path')
    if path and os.path.exists(path):
        return send_file(
            path,
            as_attachment=True,
            download_name=item.get('filename') or os.path.basename(path),
        )
    return "File sudah tidak tersedia", 404

# ============================================================
# Fallback: download langsung (kompatibilitas lama)
# ============================================================
@bp.get('/download_file')
def download_file():
    raw_url = request.args.get('raw_url')
    direct_url = request.args.get('url')
    filename = clean_filename(request.args.get('filename', 'media.mp4'))
    is_image = request.args.get('is_image', 'false') == 'true'

    if not direct_url and not raw_url:
        return "URL file tidak valid", 400

    if is_image and direct_url:
        try:
            r = requests.get(direct_url, headers=HEADERS, timeout=15)
            img_filename = filename if filename.lower().endswith(('.jpg', '.jpeg', '.png', '.webp')) else f"{filename}.jpg"
            img_path = os.path.join(DOWNLOAD_FOLDER, img_filename)
            with open(img_path, 'wb') as f:
                f.write(r.content)
            return send_file(img_path, as_attachment=True, download_name=img_filename)
        except Exception as e:
            return f"Gagal mengunduh gambar: {e}", 500

    vid_filename = filename if filename.lower().endswith('.mp4') else f"{filename}.mp4"
    out_path = os.path.join(DOWNLOAD_FOLDER, vid_filename)
    if os.path.exists(out_path):
        try:
            os.remove(out_path)
        except Exception:
            pass

    if raw_url:
        ydl_opts = {
            **cookie_opts(),
            'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
            'outtmpl': out_path,
            'quiet': True,
            'user_agent': HEADERS['User-Agent'],
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([raw_url])
            if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                return send_file(out_path, as_attachment=True, download_name=vid_filename)
        except Exception:
            pass

    if direct_url:
        try:
            r = requests.get(direct_url, headers=HEADERS, stream=True, timeout=20)
            with open(out_path, 'wb') as f:
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)
            if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                return send_file(out_path, as_attachment=True, download_name=vid_filename)
        except Exception as e:
            return f"Gagal mengunduh video: {e}", 500

    return "Gagal memproses file video.", 500
