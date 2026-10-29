# anime_core.py - logika scraping ani-cli web (dipindah dari anime.py), dipakai anime_bp.py
# anime.py - versi web dari ani-cli, untuk pemakaian pribadi.
# Hanya memakai stdlib Python + curl (Termux / Linux / macOS / Windows).
#
# Dijalankan lewat dashboard (app.py) di http://localhost:5000/anime
#            python3 anime.py --host 0.0.0.0   -> bisa dibuka dari HP/PC lain di jaringan yang sama
#
# Riwayat memakai file yang sama dengan ani-cli (ani-hsts), jadi progres keduanya tersinkron.

import argparse
import base64
import hashlib
import html
import json
import os
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote_plus, urlencode, urljoin, urlparse

BRAND = "DIESKYBYZ"
BASE = "https://hianime.at"
SEARCH_API = BASE + "/search?keyword={}"
EPISODES_API = BASE + "/api/theme/episode/list/{}"
SERVERS_API = BASE + "/api/theme/episode/servers?episodeId={}"
AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
         "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
EMBED_KEY = b"otaku-embed-v1"

HIST_DIR = os.environ.get("ANI_CLI_HIST_DIR") or os.path.join(
    os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "ani-cli")
HIST_FILE = os.path.join(HIST_DIR, "ani-hsts")
import storage_layout
META_FILE = os.path.join(storage_layout.ANIME_DIR, "ani-web-meta.json")      # waktu tonton + daftar episode yang sudah ditonton
POSTER_FILE = os.path.join(storage_layout.ANIME_DIR, "ani-web-posters.json")  # id anime -> url poster
CACHE_DIR = os.environ.get("ANI_WEB_CACHE") or storage_layout.ANIME_CACHE_DIR

CONTENT_TYPES = {
    ".ts": "video/mp2t",
    ".m4s": "video/mp4",
    ".mp4": "video/mp4",
    ".aac": "audio/aac",
    ".vtt": "text/vtt; charset=utf-8",
    ".webvtt": "text/vtt; charset=utf-8",
}


class ScrapeError(Exception):
    pass


# ---------- cache disk (LRU, batas ukuran) + cache memori ----------

class DiskCache:
    def __init__(self, d, limit):
        self.d, self.limit, self.lock = d, limit, threading.Lock()
        self.size = 0
        if limit:
            os.makedirs(d, exist_ok=True)
            self.size = sum(e.stat().st_size for e in os.scandir(d) if e.is_file())

    def _path(self, key):
        return os.path.join(self.d, hashlib.sha1(key.encode()).hexdigest())

    def get(self, key):
        if not self.limit:
            return None
        p = self._path(key)
        try:
            with open(p, "rb") as f:
                data = f.read()
            os.utime(p)  # tandai baru dipakai
            return data
        except OSError:
            return None

    def put(self, key, data):
        if not self.limit or len(data) > self.limit // 10:
            return
        p = self._path(key)
        tmp = "%s.tmp%d" % (p, threading.get_ident())
        try:
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, p)
        except OSError:
            return
        with self.lock:
            self.size += len(data)
            if self.size > self.limit:
                self._evict()

    def _evict(self):
        files = []
        for e in os.scandir(self.d):
            if e.is_file() and ".tmp" not in e.name:
                st = e.stat()
                files.append((st.st_mtime, st.st_size, e.path))
        files.sort()
        self.size = sum(f[1] for f in files)
        for _, sz, path in files:
            if self.size <= self.limit * 0.9:
                break
            try:
                os.remove(path)
                self.size -= sz
            except OSError:
                pass


CACHE = DiskCache(CACHE_DIR, 300 * 1024 * 1024)
_memo = {}


def memo(key, ttl, fn):
    e = _memo.get(key)
    if e and time.time() - e[0] < ttl:
        return e[1]
    v = fn()
    _memo[key] = (time.time(), v)
    return v


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".new"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


# ---------- HTTP lewat curl ----------

def fetch(url, referer=None, binary=False, data=None):
    cmd = ["curl", "-sL", "-A", AGENT, "--max-time", "15"]
    if referer:
        cmd += ["-e", referer]
    if data is not None:
        cmd += ["-H", "Content-Type: application/json", "--data", data]
    cmd += ["-w", "\n__HTTP__%{http_code}", url]
    try:
        proc = subprocess.run(cmd, capture_output=True, check=False)
    except FileNotFoundError:
        raise ScrapeError("curl tidak ditemukan. Pasang dengan: pkg install curl")
    if proc.returncode != 0:
        raise ScrapeError(f"Gagal mengambil {url} (curl exit {proc.returncode})")
    idx = proc.stdout.rfind(b"\n__HTTP__")
    if idx < 0:
        raise ScrapeError("Respons tidak valid dari curl")
    code = int(proc.stdout[idx + 9:].strip() or b"0")
    body = proc.stdout[:idx]
    if not 200 <= code < 300:
        if b"Just a moment" in body:
            raise ScrapeError("Diblokir Cloudflare. Coba pasang curl-impersonate.")
        raise ScrapeError(f"HTTP {code} dari {url}")
    return body if binary else body.decode("utf-8", "replace")


def b64decode_bytes(s):
    s = s.strip()
    s += "=" * (-len(s) % 4)
    return base64.b64decode(s)


# ---------- scraping (port dari ani-cli) ----------

POSTERS = load_json(POSTER_FILE, {})
_miss = {}


def pick_img(seg, title, loose=True):
    cands = []
    for tag in re.findall(r"<img\b[^>]*>", seg):
        def g(n):
            m = re.search(r'(?<![\w-])' + n + r'="([^"]*)"', tag)
            return html.unescape(m.group(1)) if m else ""
        src = g("data-src") or g("src")
        if src.startswith(("http", "/")):
            cands.append((urljoin(BASE, src), g("alt")))
    for src, alt in reversed(cands):
        if alt == title:
            return src
    return cands[-1][0] if loose and cands else None


def sniff_image(b):
    if b[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return "image/webp"
    if b[:3] == b"GIF":
        return "image/gif"
    return None


def anilist_cover(title):
    q = "query($s:String){Media(search:$s,type:ANIME){coverImage{large}}}"
    body = fetch("https://graphql.anilist.co", data=json.dumps({"query": q, "variables": {"s": title}}))
    return json.loads(body)["data"]["Media"]["coverImage"]["large"]


def get_poster(aid, title):
    key = "poster:" + aid
    data = CACHE.get(key)
    if data:
        return data
    if time.time() - _miss.get(key, 0) < 600:
        return None
    for source in ("site", "anilist"):
        try:
            if source == "site":
                if not POSTERS.get(aid):
                    continue
                data = fetch(POSTERS[aid], referer=BASE + "/", binary=True)
            else:
                if not title:
                    continue
                data = fetch(anilist_cover(title), binary=True)
        except Exception:
            data = None
        if data and sniff_image(data):
            CACHE.put(key, data)
            return data
    _miss[key] = time.time()
    return None


def search(query):
    page = fetch(SEARCH_API.format(quote_plus(query)))
    if "<title>Just a moment" in page:
        raise ScrapeError("Diblokir Cloudflare.")
    # sidebar "top 10" mengulang markup hasil, jadi dipotong dulu
    page = page.split('id="main-sidebar"')[0]
    parts = page.replace("\n", " ").split('<div class="film-detail">')
    pat = re.compile(r'<h3 class="film-name">\s*<a href="[^"]*/([^"/]*)"\s*title="([^"]*)"')
    results, seen, changed = [], set(), False
    for i, chunk in enumerate(parts[1:], 1):
        m = pat.search(chunk)
        if m and m.group(1) not in seen:
            seen.add(m.group(1))
            title = html.unescape(m.group(2))
            # poster biasanya tepat sebelum blok film-detail; cadangan: di dalam blok
            img = pick_img(parts[i - 1], title) or pick_img(chunk, title, loose=False)
            if img and POSTERS.get(m.group(1)) != img:
                POSTERS[m.group(1)] = img
                changed = True
            results.append({"id": m.group(1), "title": title})
    if changed:
        try:
            save_json(POSTER_FILE, POSTERS)
        except OSError:
            pass
    return results


def episodes(anime_id):
    page = fetch(EPISODES_API.format(anime_id.rsplit("-", 1)[-1]))
    page = page.replace("\\", "").replace("ep-item", "\nep-item")
    pat = re.compile(r'data-number="([^"]*)".*data-id="([0-9]+)".*/watch/'
                     + re.escape(anime_id) + r"\?ep=")
    eps = []
    for line in page.split("\n"):
        m = pat.search(line)
        if m:
            eps.append({"no": m.group(1), "ep_id": m.group(2)})
    return eps


def deobfuscate(blob):
    # blob = base64(json XOR "otaku-embed-v1")
    raw = b64decode_bytes(blob)
    return bytes(b ^ EMBED_KEY[i % len(EMBED_KEY)] for i, b in enumerate(raw)).decode("utf-8", "replace")


def pick_subtitle(text):
    for obj in re.findall(r"\{[^{}]*\}", text):
        if re.search(r'"default"\s*:\s*true', obj):
            m = re.search(r'"src"\s*:\s*"([^"]*)"', obj)
            if m:
                return m.group(1)
    return None


def resolve(anime_id, ep_no, mode):
    eps = memo(("e", anime_id), 300, lambda: episodes(anime_id))
    ep = next((e for e in eps if e["no"] == ep_no), None)
    if ep is None:
        raise ScrapeError(f"Episode {ep_no} tidak ditemukan")

    servers = (fetch(SERVERS_API.format(ep["ep_id"]))
               .replace('\\"', '"').replace("server-item", "\nserver-item"))
    server_pat = re.compile(r'data-type="' + re.escape(mode)
                            + r'".*data-server-name="ZokoAnime".*data-hash="([^"]*)"')
    hash_ = None
    for line in servers.split("\n"):
        m = server_pat.search(line)
        if m:
            hash_ = m.group(1)
            break
    if not hash_:
        raise ScrapeError(f"Tidak ada server {mode} (ZokoAnime) untuk episode ini")

    try:
        embed = b64decode_bytes(hash_).decode("utf-8", "replace").strip()
    except Exception:
        raise ScrapeError("Hash server tidak valid")

    referer = re.sub(r"^(https?://[^/]*).*", r"\1/", embed)
    mal = re.search(r"/mal/(\d+)/", embed)

    embed_page = fetch(embed)
    m = re.search(r'window\.__P="([^"]*)"', embed_page)
    if not m:
        raise ScrapeError("Blob embed tidak ditemukan")
    text = deobfuscate(m.group(1)).replace("\\/", "/")

    m = re.search(r'"src"\s*:\s*"([^"]*\.m3u8[^"]*)"', text)
    if not m:
        raise ScrapeError("Link stream (m3u8) tidak ditemukan")

    sub = pick_subtitle(text)
    return {
        "stream": m.group(1),
        "referer": referer,
        "subtitle": sub,
        "mal_id": mal.group(1) if mal else None,
    }


# ---------- proxy: HLS dengan header Referer ----------

def proxy_url(url, referer):
    q = {"u": url}
    if referer:
        q["r"] = referer
    return "/anime/proxy?" + urlencode(q)


def rewrite_playlist(text, base, referer):
    uri_pat = re.compile(r'URI="([^"]*)"')
    out = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            out.append(line)
        elif s.startswith("#"):
            out.append(uri_pat.sub(
                lambda m: 'URI="' + proxy_url(urljoin(base, m.group(1)), referer) + '"', line))
        else:
            out.append(proxy_url(urljoin(base, s), referer))
    return "\n".join(out) + "\n"


def content_type_for(url):
    ext = os.path.splitext(urlparse(url).path)[1].lower()
    return CONTENT_TYPES.get(ext, "application/octet-stream")


# ---------- riwayat (format sama dengan ani-cli: ep \t id \t judul) ----------

def read_history():
    rows = []
    if os.path.exists(HIST_FILE):
        with open(HIST_FILE, encoding="utf-8", errors="replace") as f:
            for line in f:
                p = line.rstrip("\n").split("\t")
                if len(p) >= 3 and p[1]:
                    rows.append({"ep": p[0], "id": p[1], "title": p[2]})
    meta = load_json(META_FILE, {})
    for i, r in enumerate(rows):
        m = meta.get(r["id"], {})
        r["ts"] = m.get("ts", 0)
        r["watched"] = m.get("watched", [])
        r["_i"] = i
    rows.sort(key=lambda r: (r["ts"], r["_i"]))  # terlama -> terbaru
    for r in rows:
        del r["_i"]
    return rows


def delete_history(anime_id=None):
    rows = [] if anime_id is None else [r for r in read_history() if r["id"] != anime_id]
    meta = {} if anime_id is None else load_json(META_FILE, {})
    meta.pop(anime_id, None)
    os.makedirs(HIST_DIR, exist_ok=True)
    with open(HIST_FILE, "w", encoding="utf-8") as f:
        for r in rows:
            f.write("\t".join([r["ep"], r["id"], r["title"]]) + "\n")
    save_json(META_FILE, meta)


def update_history(anime_id, title, ep):
    os.makedirs(HIST_DIR, exist_ok=True)
    title = title.replace("\t", " ").replace("\n", " ")
    rows = [[r["ep"], r["id"], r["title"]] for r in read_history()]
    new_rows, found = [], False
    for r in rows:
        if r[1] == anime_id:
            if not found:
                new_rows.append([ep, anime_id, title])
                found = True
        else:
            new_rows.append(r)
    if not found:
        new_rows.append([ep, anime_id, title])
    tmp = HIST_FILE + ".new"
    with open(tmp, "w", encoding="utf-8") as f:
        for r in new_rows:
            f.write("\t".join(r) + "\n")
    os.replace(tmp, HIST_FILE)
    meta = load_json(META_FILE, {})
    m = meta.setdefault(anime_id, {})
    m["ts"] = int(time.time())
    if ep not in m.setdefault("watched", []):
        m["watched"].append(ep)
    save_json(META_FILE, meta)


