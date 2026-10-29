"""Cookie untuk yt-dlp. Dicari berurutan:
1. env YT_COOKIES (kalau diset)
2. ~/cookies.txt  (home Termux)
3. cookies.txt di folder proyek
Kalau tidak ada, yt-dlp jalan tanpa cookie seperti biasa."""
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_CANDIDATES = [
    os.getenv("YT_COOKIES", ""),
    os.path.join(os.path.expanduser("~"), "cookies.txt"),
    os.path.join(_HERE, "cookies.txt"),
]


def cookie_file():
    for path in _CANDIDATES:
        if path and os.path.isfile(path):
            return path
    return None


def cookie_args():
    """Untuk subprocess yt-dlp (music.py)."""
    path = cookie_file()
    return ["--cookies", path] if path else []


def cookie_opts():
    """Untuk yt_dlp.YoutubeDL(opts) (downloader_bp.py)."""
    path = cookie_file()
    return {"cookiefile": path} if path else {}
