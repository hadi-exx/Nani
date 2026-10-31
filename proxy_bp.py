"""Proxy web pribadi, digabung ke server utama (port yang sama).

- /proxy            : beranda proxy (perlu login)
- /proxy?url=...    : ambil halaman lewat server, lalu tulis ulang link agar tetap lewat proxy
- /proxy/login      : halaman login (sekali masuk, cookie bertahan 1 tahun)
- /api/proxy/bookmarks : simpanan situs
Fallback: permintaan path lain dari halaman proxy (mis. navigasi JavaScript /search/xxx)
diteruskan ke situs asal lewat header Referer.
"""
import hashlib
import hmac
import html as htmllib
import ipaddress
import json
import os
import re
import secrets
import socket
import threading
import time
import uuid
from urllib.parse import parse_qs, quote, urljoin, urlparse

import requests
import urllib3.util.connection as _u3_connection
from bs4 import BeautifulSoup
from bs4.element import Stylesheet
from flask import Blueprint, Response, g, jsonify, make_response, redirect, request, send_from_directory

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_PROXY = os.path.join(BASE_DIR, "static", "proxy")
DATA_DIR = os.path.join(BASE_DIR, "data", "proxy")
BOOKMARK_FILE = os.path.join(DATA_DIR, "bookmarks.json")
SECRET_FILE = os.path.join(DATA_DIR, "secret.key")

PASSWORD = os.getenv("PROXY_PASSWORD", "")
ALLOW_PRIVATE = os.getenv("ALLOW_PRIVATE_TARGETS") == "1"  # hanya untuk testing lokal
MAX_BYTES = 25 * 1024 * 1024
TIMEOUT = 20
MAX_REDIRECTS = 5
MAX_BOOKMARKS = 500
MAX_JARS = 500
COOKIE_AGE = 365 * 24 * 3600
AUTH_COOKIE = "px_auth"
SID_COOKIE = "px_sid"
DEFAULT_UA = ("Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36")
DEFAULT_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
DEFAULT_LANG = "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7"

os.makedirs(DATA_DIR, exist_ok=True)


def _load_secret():
    if os.path.exists(SECRET_FILE):
        with open(SECRET_FILE, encoding="utf-8") as f:
            s = f.read().strip()
        if s:
            return s
    s = secrets.token_hex(32)
    with open(SECRET_FILE, "w", encoding="utf-8") as f:
        f.write(s)
    try:
        os.chmod(SECRET_FILE, 0o600)
    except OSError:
        pass
    return s


SECRET = _load_secret()

if not PASSWORD:
    print("[proxy] PROXY_PASSWORD belum diset: halaman proxy tidak dikunci.")


class ProxyError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


# ---------- Login ----------
def _auth_token():
    return hmac.new(SECRET.encode(), PASSWORD.encode(), hashlib.sha256).hexdigest()


def _is_authed():
    if not PASSWORD:
        return True
    tok = request.cookies.get(AUTH_COOKIE, "")
    return bool(tok) and hmac.compare_digest(tok, _auth_token())


# ---------- Sesi & cookie situs (disimpan di server, tidak diteruskan ke browser) ----------
_jars = {}
_jars_lock = threading.Lock()


def _sid():
    if "sid" not in g:
        sid = request.cookies.get(SID_COOKIE, "")
        if not re.fullmatch(r"[a-f0-9]{32}", sid):
            sid = secrets.token_hex(16)
            g.new_sid = True
        g.sid = sid
    return g.sid


def _attach_sid(resp):
    if g.get("new_sid"):
        resp.set_cookie(SID_COOKIE, g.sid, max_age=COOKIE_AGE, httponly=True, samesite="Lax", path="/")
    return resp


def _cookie_header(sid, host):
    with _jars_lock:
        host_map = _jars.get(sid, {}).get(host, {})
        return "; ".join(f"{k}={v}" for k, v in host_map.items())


def _store_cookies(sid, host, resp):
    raw = []
    try:
        raw = resp.raw.headers.getlist("Set-Cookie")
    except AttributeError:
        v = resp.headers.get("Set-Cookie")
        raw = [v] if v else []
    if not raw:
        return
    with _jars_lock:
        if sid not in _jars:
            if len(_jars) >= MAX_JARS:
                _jars.pop(next(iter(_jars)))
            _jars[sid] = {}
        host_map = _jars[sid].setdefault(host, {})
        for item in raw:
            parts = [p.strip() for p in item.split(";")]
            if "=" not in parts[0]:
                continue
            name, value = parts[0].split("=", 1)
            name = name.strip()
            if not name:
                continue
            expired = any(re.fullmatch(r"max-age\s*=\s*0", p, re.I) for p in parts[1:])
            if expired:
                host_map.pop(name, None)
            else:
                host_map[name] = value.strip()


# ---------- Keamanan: blokir alamat internal (anti SSRF) ----------
def _is_private_ip(ip):
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return (addr.is_private or addr.is_loopback or addr.is_link_local
            or addr.is_multicast or addr.is_reserved or addr.is_unspecified)


def _assert_public(url):
    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        raise ProxyError(400, "Hanya http dan https yang didukung.")
    if p.username or p.password:
        raise ProxyError(400, "URL dengan kredensial tidak diizinkan.")
    if ALLOW_PRIVATE:
        return
    host = (p.hostname or "").rstrip(".").lower()
    if not host or host == "localhost" or host.endswith(".localhost"):
        raise ProxyError(403, "Alamat lokal tidak diizinkan.")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        # DNS operator bisa membalas NXDOMAIN untuk situs yang diblokir.
        # Kalau DoH masih menemukan IP publik, lanjutkan lewat jalur DoH.
        if not any(not _is_private_ip(ip) for ip in _doh_resolve(host)):
            raise ProxyError(502, "Domain tidak ditemukan. Periksa kembali alamat yang kamu ketik.")
        return
    if any(_is_private_ip(i[4][0]) for i in infos):
        raise ProxyError(403, "Alamat jaringan internal tidak diizinkan.")


# ---------- Resolusi DNS lewat DoH (bypass DNS operator yang memblokir) -----
# Sebagian ISP memalsukan jawaban DNS untuk situs tertentu ke halaman blokir,
# sehingga koneksi HTTPS gagal karena sertifikat tidak cocok. Di sini hostname
# di-resolve lewat DoH agar dapat IP asli; koneksi tetap memakai SNI/hostname
# asli sehingga validasi TLS tidak dilemahkan. Kalau DoH gagal, proxy kembali
# memakai DNS sistem seperti biasa (PROXY_DOH=0 untuk mematikan fitur ini).
DOH_ENABLED = os.getenv("PROXY_DOH", "1") != "0"
DOH_URL = os.getenv("PROXY_DOH_URL", "https://cloudflare-dns.com/dns-query")
DOH_TTL = 600
DOH_FAIL_TTL = 60
DOH_TIMEOUT = 4
DOH_MAX_IPS = 4

_doh_cache = {}
_doh_lock = threading.Lock()
_dns_override = threading.local()

_orig_create_connection = _u3_connection.create_connection


def _doh_resolve(host):
    """Daftar IPv4 publik hasil DoH untuk host (list kosong kalau tidak ada)."""
    host = (host or "").rstrip(".").lower()
    if not host or not DOH_ENABLED:
        return []
    try:
        ipaddress.ip_address(host)  # sudah berupa IP literal, tak perlu resolve
        return []
    except ValueError:
        pass

    now = time.time()
    with _doh_lock:
        hit = _doh_cache.get(host)
        if hit and hit[1] > now:
            return hit[0]

    ips = []
    try:
        resp = requests.get(
            DOH_URL,
            params={"name": host, "type": "A"},
            headers={"accept": "application/dns-json"},
            timeout=DOH_TIMEOUT,
        )
        answers = (resp.json().get("Answer") or []) if resp.ok else []
        for ans in answers:
            if ans.get("type") != 1:
                continue
            ip = str(ans.get("data") or "")
            if ip and not _is_private_ip(ip) and ip not in ips:
                ips.append(ip)
    except (requests.RequestException, ValueError, AttributeError):
        ips = []

    with _doh_lock:
        if len(_doh_cache) > 500:
            _doh_cache.clear()
        _doh_cache[host] = (ips, now + (DOH_TTL if ips else DOH_FAIL_TTL))
    return ips


def _create_connection(address, *args, **kwargs):
    """Sambung ke IP hasil DoH, tapi SNI/nama sertifikat tetap hostname asli."""
    host, port = address[0], address[1]
    ip = getattr(_dns_override, "ip", None)
    if ip and getattr(_dns_override, "host", None) == host:
        address = (ip, port)
    return _orig_create_connection(address, *args, **kwargs)


_u3_connection.create_connection = _create_connection


def _requests_with_doh(method, url, **kwargs):
    """requests.request() dengan resolusi DoH, plus cadangan IP lain."""
    host = urlparse(url).hostname
    ips = _doh_resolve(host)
    if not ips:
        return requests.request(method, url, **kwargs)

    # POST tidak diulang agar tidak mengirim data dua kali.
    candidates = ips[:DOH_MAX_IPS] if method in ("GET", "HEAD") else ips[:1]
    prev = (getattr(_dns_override, "host", None), getattr(_dns_override, "ip", None))
    last_error = None
    try:
        for ip in candidates:
            _dns_override.host, _dns_override.ip = host, ip
            try:
                return requests.request(method, url, **kwargs)
            except requests.exceptions.ConnectionError as e:
                last_error = e
        raise last_error
    finally:
        _dns_override.host, _dns_override.ip = prev


def _normalize(raw):
    s = (raw or "").strip()
    if not s:
        raise ProxyError(400, "URL kosong.")
    if not re.match(r"^[a-z][a-z0-9+.-]*://", s, re.I):
        s = "https://" + s
    try:
        p = urlparse(s)
        p.port  # memunculkan ValueError jika port tidak valid
    except ValueError:
        raise ProxyError(400, "URL tidak valid.")
    if p.scheme not in ("http", "https") or not p.hostname:
        raise ProxyError(400, "URL tidak valid.")
    if not p.path:
        p = p._replace(path="/")
    return p.geturl()


def _read_limited(resp):
    buf = bytearray()
    for chunk in resp.iter_content(65536):
        buf.extend(chunk)
        if len(buf) > MAX_BYTES:
            raise ProxyError(413, "Ukuran halaman terlalu besar.")
    return bytes(buf)


def _fetch(method, url, sid, body, ctype, ua, accept, lang):
    """Ambil URL dan ikuti redirect, dengan validasi alamat di setiap hop."""
    current = url
    for hop in range(MAX_REDIRECTS + 1):
        _assert_public(current)
        p = urlparse(current)
        host = p.hostname
        headers = {
            "User-Agent": ua,
            "Accept": accept,
            "Accept-Language": lang,
            "Referer": f"{p.scheme}://{p.netloc}/",
        }
        ck = _cookie_header(sid, host)
        if ck:
            headers["Cookie"] = ck
        if method == "POST" and body is not None and ctype:
            headers["Content-Type"] = ctype
        try:
            resp = _requests_with_doh(
                method, current, headers=headers,
                data=body if method == "POST" else None,
                allow_redirects=False, timeout=TIMEOUT, stream=True,
            )
        except requests.exceptions.Timeout:
            raise ProxyError(504, "Situs tidak merespons tepat waktu (timeout).")
        except requests.exceptions.ConnectionError:
            raise ProxyError(502, "Gagal terhubung ke situs.")
        except requests.RequestException as e:
            raise ProxyError(502, f"Gagal mengambil halaman: {e.__class__.__name__}")

        _store_cookies(sid, host, resp)
        loc = resp.headers.get("Location")
        if 300 <= resp.status_code < 400 and loc:
            resp.close()
            if hop >= MAX_REDIRECTS:
                raise ProxyError(508, "Terlalu banyak pengalihan (redirect).")
            current = urljoin(current, loc)
            if resp.status_code in (301, 302, 303):
                method, body = "GET", None
            continue
        content = _read_limited(resp)
        return resp, content, current
    raise ProxyError(508, "Terlalu banyak pengalihan (redirect).")


# ---------- Tulis ulang URL ----------
SKIP_PREFIX = re.compile(r"^(#|data:|blob:|javascript:|mailto:|tel:|about:|sms:)", re.I)
URL_RE = re.compile(r"url\(\s*(['\"]?)([^'\")]*)\1\s*\)", re.I)
IMPORT_RE = re.compile(r"@import\s+(['\"])([^'\"]+)\1", re.I)
DROP_EQUIV = {"content-security-policy", "content-security-policy-report-only", "x-frame-options"}
URL_ATTRS = [
    ("a", "href"), ("area", "href"), ("link", "href"), ("img", "src"), ("img", "data-src"),
    ("script", "src"), ("iframe", "src"), ("frame", "src"), ("embed", "src"), ("source", "src"),
    ("video", "src"), ("video", "poster"), ("audio", "src"), ("track", "src"), ("input", "src"),
    ("object", "data"),
]


def _proxy_path(href):
    return "/proxy?url=" + quote(href, safe="")


def _proxify(raw, base):
    if raw is None:
        return raw
    v = str(raw).strip()
    if not v or SKIP_PREFIX.match(v):
        return raw
    try:
        absolute = urljoin(base, v)
    except ValueError:
        return raw
    if urlparse(absolute).scheme not in ("http", "https"):
        return raw
    return _proxy_path(absolute)


def _proxify_srcset(value, base):
    parts = []
    for part in value.split(","):
        bits = part.strip().split()
        if not bits:
            continue
        parts.append(" ".join([_proxify(bits[0], base)] + bits[1:]))
    return ", ".join(parts)


def _rewrite_css(css, base):
    css = URL_RE.sub(lambda m: f"url({m.group(1)}{_proxify(m.group(2), base)}{m.group(1)})", css)
    css = IMPORT_RE.sub(lambda m: f"@import {m.group(1)}{_proxify(m.group(2), base)}{m.group(1)}", css)
    return css


def _decode(content, ctype, sniff):
    m = re.search(r"charset=([^;\s]+)", ctype or "", re.I)
    charset = m.group(1).strip("'\"") if m else None
    if not charset and sniff:
        m2 = re.search(rb'<meta[^>]+charset\s*=\s*["\']?\s*([\w-]+)', content[:2048], re.I)
        if m2:
            charset = m2.group(1).decode("latin1")
    if charset:
        try:
            return content.decode(charset, "replace")
        except LookupError:
            pass
    return content.decode("utf-8", "replace")


def _bar_html(page_url):
    safe = htmllib.escape(page_url, quote=True)
    js_url = json.dumps(page_url).replace("<", "\\u003c")
    return f"""
<style id="px-style">
#px-bar{{position:fixed;top:0;left:0;right:0;z-index:2147483647;display:flex;align-items:center;gap:6px;padding:8px 10px;background:rgba(24,24,37,.94);border-bottom:1px solid #313244;box-shadow:0 6px 20px rgba(0,0,0,.35);font:13px/1.2 system-ui,-apple-system,'Segoe UI',sans-serif;color:#cdd6f4;box-sizing:border-box}}
#px-bar *{{box-sizing:border-box;font:inherit}}
#px-bar .px-btn{{flex:none;width:32px;height:32px;display:grid;place-items:center;border:1px solid #313244;border-radius:9px;background:#1e1e2e;color:#cdd6f4;text-decoration:none;cursor:pointer;padding:0;font-size:15px}}
#px-bar .px-btn:hover{{background:#313244}}
#px-bar .px-btn:disabled{{opacity:.5}}
#px-bar #px-save.saved{{color:#f9e2af}}
#px-bar form{{flex:1;display:flex;gap:6px;margin:0;min-width:0}}
#px-bar input[type=text]{{flex:1;min-width:0;height:32px;padding:0 10px;border-radius:9px;border:1px solid #45475a;background:#1e1e2e;color:#cdd6f4;outline:none}}
#px-bar .px-go{{flex:none;height:32px;padding:0 12px;border:0;border-radius:9px;background:#89b4fa;color:#11111b;font-weight:700;cursor:pointer}}
#px-spacer{{height:52px}}
</style>
<div id="px-bar">
  <button type="button" class="px-btn" id="px-home" title="Kembali ke AI">&#8962;</button>
  <button type="button" class="px-btn" id="px-back" title="Mundur">&#8592;</button>
  <button type="button" class="px-btn" id="px-fwd" title="Maju">&#8594;</button>
  <button type="button" class="px-btn" id="px-save" title="Simpan situs">&#9734;</button>
  <button type="button" class="px-btn" id="px-full" title="Buka penuh" style="display:none">&#8599;</button>
  <form action="/proxy" method="GET" autocomplete="off">
    <input type="text" name="url" value="{safe}" spellcheck="false" required>
    <button type="submit" class="px-go">Buka</button>
  </form>
</div>
<div id="px-spacer"></div>
<script>
(function(){{
  var TARGET = {js_url};
  var inFrame = window.parent && window.parent !== window;
  var save = document.getElementById('px-save');
  var full = document.getElementById('px-full');
  if (inFrame) full.style.display = '';
  document.getElementById('px-home').onclick = function(){{
    if (inFrame) window.parent.postMessage({{type:'proxy:close'}}, location.origin);
    else location.href = '/';
  }};
  document.getElementById('px-back').onclick = function(){{ history.back(); }};
  document.getElementById('px-fwd').onclick = function(){{ history.forward(); }};
  full.onclick = function(){{ window.top.location.href = location.href; }};
  save.onclick = function(){{
    save.disabled = true;
    fetch('/api/proxy/bookmarks', {{
      method: 'POST', headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{url: TARGET, title: document.title}})
    }}).then(function(r){{
      return r.json().then(function(j){{ if (!r.ok) throw new Error(j.error || 'Gagal menyimpan'); return j; }});
    }}).then(function(){{
      save.textContent = '\\u2605'; save.classList.add('saved');
    }}).catch(function(e){{ alert(e.message); }}).finally(function(){{ save.disabled = false; }});
  }};
}})();
</script>"""


def _rewrite_html(text, page_url):
    soup = BeautifulSoup(text, "html.parser")

    base = page_url
    b = soup.find("base", href=True)
    if b:
        try:
            base = urljoin(page_url, b["href"])
        except ValueError:
            pass
    for tag in soup.find_all("base"):
        tag.decompose()

    for el in soup.find_all(attrs={"integrity": True}):
        del el["integrity"]
    for el in soup.find_all(attrs={"crossorigin": True}):
        del el["crossorigin"]

    for m in soup.find_all("meta", attrs={"http-equiv": True}):
        eq = (m.get("http-equiv") or "").lower()
        if eq in DROP_EQUIV:
            m.decompose()
        elif eq == "refresh":
            c = m.get("content", "")
            m["content"] = re.sub(
                r"(url\s*=\s*)(.+)$",
                lambda mm: mm.group(1) + _proxify(mm.group(2).strip().strip("'\""), base),
                c, flags=re.I,
            )

    for tag, attr in URL_ATTRS:
        for el in soup.find_all(tag, attrs={attr: True}):
            el[attr] = _proxify(el[attr], base)

    for el in soup.find_all(["img", "source"], attrs={"srcset": True}):
        el["srcset"] = _proxify_srcset(el["srcset"], base)

    for el in soup.find_all(attrs={"style": True}):
        el["style"] = _rewrite_css(el["style"], base)

    for el in soup.find_all("style"):
        if el.string is not None:
            el.string.replace_with(Stylesheet(_rewrite_css(str(el.string), base)))

    for form in soup.find_all("form"):
        method = (form.get("method") or "get").lower()
        action = form.get("action")
        try:
            u = urljoin(base, action.strip()) if action and action.strip() else page_url
        except ValueError:
            continue
        if urlparse(u).scheme not in ("http", "https"):
            continue
        if method == "post":
            form["action"] = _proxy_path(u)
            continue
        p = urlparse(u)
        params = parse_qs(p.query, keep_blank_values=True)
        clean = p._replace(query="").geturl()
        form["action"] = "/proxy"
        form["method"] = "GET"
        hidden = [("url", clean)]
        for k, vals in params.items():
            for v in vals:
                hidden.append((k, v))
        for name, value in reversed(hidden):
            tag = soup.new_tag("input", type="hidden", attrs={"name": name, "value": value})
            form.insert(0, tag)

    out = str(soup)
    bar = _bar_html(page_url)
    m = re.search(r"<body[^>]*>", out, re.I)
    if m:
        out = out[:m.end()] + bar + out[m.end():]
    else:
        out = bar + out
    return out


# ---------- Respons ke browser ----------
def _error_page(message, title="Gagal memuat halaman"):
    return f"""<!DOCTYPE html><html lang="id"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0"><title>{htmllib.escape(title)}</title>
<style>body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#11111b;color:#cdd6f4;font-family:system-ui,sans-serif;padding:20px;box-sizing:border-box}}
.card{{max-width:420px;width:100%;text-align:center;background:#313244;border-radius:18px;padding:30px 24px}}
h1{{margin:0 0 8px;font-size:20px}}p{{margin:0 0 20px;color:#a6adc8;line-height:1.5;word-break:break-word}}
a{{display:inline-block;padding:9px 16px;border-radius:10px;background:#89b4fa;color:#11111b;text-decoration:none;font-weight:600}}</style>
</head><body><div class="card"><h1>{htmllib.escape(title)}</h1><p>{htmllib.escape(message)}</p>
<a href="/">Kembali ke AI</a></div></body></html>"""


def _proxy_to(method, target, body=None, ctype=None):
    sid = _sid()
    ua = request.headers.get("User-Agent") or DEFAULT_UA
    accept = request.headers.get("Accept") or DEFAULT_ACCEPT
    lang = request.headers.get("Accept-Language") or DEFAULT_LANG
    try:
        resp, content, final = _fetch(method, target, sid, body, ctype, ua, accept, lang)
    except ProxyError as e:
        r = make_response(_error_page(e.message), e.status)
        r.headers["Content-Type"] = "text/html; charset=utf-8"
        return _attach_sid(r)

    ct = (resp.headers.get("Content-Type") or "").lower()
    headers = {"X-Content-Type-Options": "nosniff"}
    if resp.headers.get("Content-Disposition"):
        headers["Content-Disposition"] = resp.headers["Content-Disposition"]

    if "text/html" in ct or "application/xhtml+xml" in ct:
        body_text = _rewrite_html(_decode(content, ct, True), final)
        r = Response(body_text, status=resp.status_code, headers=headers,
                     content_type="text/html; charset=utf-8")
    elif "text/css" in ct:
        css = _rewrite_css(_decode(content, ct, False), final)
        r = Response(css, status=resp.status_code, headers=headers,
                     content_type="text/css; charset=utf-8")
    else:
        r = Response(content, status=resp.status_code, headers=headers,
                     content_type=ct or "application/octet-stream")
    return _attach_sid(r)


# ---------- Bookmark ----------
_bm_lock = threading.Lock()


def _read_bookmarks():
    try:
        with open(BOOKMARK_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return []
    return data if isinstance(data, list) else []


def _write_bookmarks(data):
    tmp = BOOKMARK_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, BOOKMARK_FILE)


# ---------- Blueprint ----------
bp = Blueprint("proxy", __name__)
PUBLIC_ENDPOINTS = {"proxy.login_page", "proxy.login_submit", "proxy.logout"}


@bp.before_request
def _require_login():
    if request.endpoint in PUBLIC_ENDPOINTS or _is_authed():
        return None
    if request.path.startswith("/api/"):
        return jsonify({"error": "Perlu login"}), 401
    return redirect("/proxy/login")


@bp.get("/proxy/login")
def login_page():
    if _is_authed():
        return redirect("/proxy")
    return send_from_directory(STATIC_PROXY, "login.html")


@bp.post("/proxy/login")
def login_submit():
    if not PASSWORD:
        return redirect("/proxy")
    given = hashlib.sha256((request.form.get("password") or "").encode()).digest()
    real = hashlib.sha256(PASSWORD.encode()).digest()
    if not hmac.compare_digest(given, real):
        return redirect("/proxy/login?err=1")
    resp = make_response(redirect("/proxy"))
    resp.set_cookie(AUTH_COOKIE, _auth_token(), max_age=COOKIE_AGE, httponly=True,
                    samesite="Strict", path="/")
    return resp


@bp.get("/proxy/logout")
def logout():
    resp = make_response(redirect("/proxy/login"))
    resp.set_cookie(AUTH_COOKIE, "", max_age=0, httponly=True, samesite="Strict", path="/")
    return resp


@bp.route("/proxy", methods=["GET", "POST"])
def proxy_entry():
    raw = request.args.get("url")
    if not raw:
        return send_from_directory(STATIC_PROXY, "index.html")
    try:
        target = _normalize(raw)
    except ProxyError as e:
        r = make_response(_error_page(e.message), e.status)
        r.headers["Content-Type"] = "text/html; charset=utf-8"
        return _attach_sid(r)
    # Parameter lain (mis. dari form GET) diteruskan ke URL target
    extra = [(k, v) for k, v in request.args.items(multi=True) if k != "url"]
    if extra:
        from urllib.parse import urlencode, urlsplit, urlunsplit
        sp = urlsplit(target)
        q = sp.query + ("&" if sp.query else "") + urlencode(extra)
        target = urlunsplit((sp.scheme, sp.netloc, sp.path, q, sp.fragment))
    body = request.get_data() if request.method == "POST" else None
    return _proxy_to(request.method, target, body, request.headers.get("Content-Type"))


@bp.get("/api/proxy/bookmarks")
def bookmarks_list():
    try:
        return jsonify(_read_bookmarks())
    except (OSError, ValueError):
        return jsonify({"error": "Gagal membaca data simpanan"}), 500


@bp.post("/api/proxy/bookmarks")
def bookmarks_add():
    data = request.get_json(silent=True) or {}
    try:
        target = _normalize(str(data.get("url") or ""))
    except ProxyError:
        return jsonify({"error": "URL tidak valid"}), 400
    title = re.sub(r"\s+", " ", str(data.get("title") or "")).strip()[:120] or urlparse(target).hostname
    with _bm_lock:
        try:
            items = _read_bookmarks()
        except (OSError, ValueError):
            return jsonify({"error": "Gagal membaca data simpanan"}), 500
        existing = next((b for b in items if b.get("url") == target), None)
        if existing:
            return jsonify(existing), 200
        if len(items) >= MAX_BOOKMARKS:
            return jsonify({"error": "Daftar simpanan sudah penuh."}), 409
        item = {"id": uuid.uuid4().hex, "url": target, "title": title}
        items.insert(0, item)
        _write_bookmarks(items)
    return jsonify(item), 201


@bp.delete("/api/proxy/bookmarks/<item_id>")
def bookmarks_delete(item_id):
    with _bm_lock:
        try:
            items = _read_bookmarks()
        except (OSError, ValueError):
            return jsonify({"error": "Gagal membaca data simpanan"}), 500
        kept = [b for b in items if b.get("id") != item_id]
        if len(kept) == len(items):
            return jsonify({"error": "Simpanan tidak ditemukan"}), 404
        _write_bookmarks(kept)
    return ("", 204)


# ---------- Fallback 404: navigasi halaman target yang berpindah lewat JavaScript ----------
def _origin_from_referer(ref):
    try:
        r = urlparse(ref or "")
        if r.path != "/proxy":
            return None
        t = parse_qs(r.query).get("url", [""])[0]
        return _normalize(t) if t else None
    except ProxyError:
        return None


def fallback_404(e):
    origin = _origin_from_referer(request.headers.get("Referer"))
    if not origin or request.path in ("/", "") or request.path.startswith("/api/"):
        return e.get_response()
    if not _is_authed():
        return redirect("/proxy/login")
    op = urlparse(origin)
    rel = request.path + (("?" + request.query_string.decode()) if request.query_string else "")
    target = urljoin(f"{op.scheme}://{op.netloc}/", rel)
    if urlparse(target).netloc != op.netloc:
        return make_response(_error_page("Permintaan ke domain lain tidak diizinkan."), 400)
    body = request.get_data() if request.method == "POST" else None
    return _proxy_to(request.method, target, body, request.headers.get("Content-Type"))
