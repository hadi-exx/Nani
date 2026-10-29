"""Tata letak folder data dan cache per fitur.

data/ai/      : riwayat chat, memori, key terakhir, log terminal
data/music/   : favorit, riwayat, dan status player musik
data/anime/   : meta tontonan dan poster (web)
data/proxy/   : simpanan situs dan secret login proxy
cache/anime/  : cache stream dan poster anime

Data lama disalin (bukan dipindah) sekali saat pertama dijalankan.
File yang sudah ada di tujuan tidak pernah ditimpa.
"""
import os
import shutil

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
CACHE_DIR = os.path.join(BASE_DIR, "cache")

AI_DIR = os.path.join(DATA_DIR, "ai")
MUSIC_DIR = os.path.join(DATA_DIR, "music")
ANIME_DIR = os.path.join(DATA_DIR, "anime")
PROXY_DIR = os.path.join(DATA_DIR, "proxy")
ANIME_CACHE_DIR = os.path.join(CACHE_DIR, "anime")

# Lokasi lama data AI (sebelum dipisah per fitur)
_LEGACY_AI = os.path.join(DATA_DIR)
# Lokasi lama data web anime (default ani-cli)
_LEGACY_ANIME = os.environ.get("ANI_CLI_HIST_DIR") or os.path.join(
    os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "ani-cli")


def _copy_file(src, dst):
    if os.path.exists(src) and not os.path.exists(dst):
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        print(f"[storage] disalin: {src} -> {dst}", flush=True)


def _copy_dir(src, dst):
    if not os.path.isdir(src):
        return
    for root, _dirs, files in os.walk(src):
        rel = os.path.relpath(root, src)
        for name in files:
            _copy_file(os.path.join(root, name), os.path.join(dst, rel, name))


def ensure_layout():
    for d in (AI_DIR, MUSIC_DIR, ANIME_DIR, PROXY_DIR, ANIME_CACHE_DIR):
        os.makedirs(d, exist_ok=True)

    _copy_file(os.path.join(_LEGACY_AI, "history.json"), os.path.join(AI_DIR, "history.json"))
    _copy_file(os.path.join(_LEGACY_AI, "key_terakhir.json"), os.path.join(AI_DIR, "key_terakhir.json"))
    _copy_file(os.path.join(_LEGACY_AI, "memory.json"), os.path.join(AI_DIR, "memory.json"))
    _copy_dir(os.path.join(_LEGACY_AI, "runs"), os.path.join(AI_DIR, "runs"))

    # Hanya file khusus web. ani-hsts TIDAK disalin/dipindah karena dipakai bersama ani-cli.
    _copy_file(os.path.join(_LEGACY_ANIME, "ani-web-meta.json"), os.path.join(ANIME_DIR, "ani-web-meta.json"))
    _copy_file(os.path.join(_LEGACY_ANIME, "ani-web-posters.json"), os.path.join(ANIME_DIR, "ani-web-posters.json"))


ensure_layout()
