"""
Dieskybxyz Dashbord - satu server untuk semua fitur di http://localhost:5000

  /              dashboard (PWA bisa dipasang dari sini)
  /music         musik
  /anime         nonton anime
  /proxy         proxy
  /cloud/        folder cloud
"""
import importlib
from pathlib import Path

from flask import Flask, render_template, jsonify, send_from_directory

BASE = Path(__file__).resolve().parent
STATIC_DIR = BASE / "static"
PORT = 5000

app = Flask(__name__)

# ---- Musik, Anime, Proxy: tiap modul dimuat sendiri -------------------------
# Kalau satu gagal (misal paket belum terpasang), dashboard tetap jalan.
for _mod in ("music", "anime_bp", "proxy_bp", "downloader_bp"):
    try:
        _m = importlib.import_module(_mod)
        app.register_blueprint(_m.bp)
        if _mod == "proxy_bp":
            app.register_error_handler(404, _m.fallback_404)
        print(f"[media] {_mod} aktif")
    except Exception as e:
        print(f"[media] {_mod} dilewati: {e}")

# ---- Cloud: blueprint di /cloud ---------------------------------------------
import cloud_bp  # noqa: E402

app.register_blueprint(cloud_bp.bp)
print("[cloud] aktif di /cloud/")


# ---- Halaman utama & PWA ----------------------------------------------------
@app.route("/")
def home():
    return render_template("index.html")


@app.get("/api/ping")
def media_ping():
    return jsonify(ok=True)


@app.get("/connection.js")
def media_connection_js():
    return send_from_directory(STATIC_DIR, "connection.js", mimetype="application/javascript")


@app.get("/music")
@app.get("/music/")
def music_page():
    return send_from_directory(STATIC_DIR / "music", "index.html")


@app.get("/manifest.webmanifest")
def manifest():
    resp = send_from_directory(STATIC_DIR, "manifest.webmanifest",
                               mimetype="application/manifest+json")
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/sw.js")
def service_worker():
    # Service worker harus di root agar cakupannya mencakup seluruh dashboard.
    resp = send_from_directory(STATIC_DIR, "sw.js", mimetype="application/javascript")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["Service-Worker-Allowed"] = "/"
    return resp


if __name__ == "__main__":
    cloud_bp.init_cloud()
    print(f"\n🚀 Dashboard: http://localhost:{PORT}  (semua fitur di port ini)\n")
    app.run(debug=True, port=PORT)
