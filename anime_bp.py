# anime_bp.py - ani-cli web di dalam termux-ai (satu server, port yang sama).
# Halaman: /anime (dibuka sebagai layer ?embed=1). API: /api/anime/*, /anime/poster, /anime/proxy.
import os
from urllib.parse import urlparse

from flask import Blueprint, Response, jsonify, request, send_from_directory

import anime_core as ac

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ANIME_DIR = os.path.join(BASE_DIR, "static", "anime")

bp = Blueprint("anime", __name__)


@bp.errorhandler(ac.ScrapeError)
def _scrape_error(e):
    return jsonify({"error": str(e)}), 502


def _err(msg, code=400):
    return jsonify({"error": msg}), code


@bp.get("/anime")
@bp.get("/anime/")
def anime_page():
    return send_from_directory(ANIME_DIR, "index.html")


@bp.get("/api/anime/search")
def a_search():
    kw = request.args.get("q", "").strip()
    if not kw:
        return _err("Kata kunci kosong")
    return jsonify({"results": ac.memo(("s", kw.lower()), 600, lambda: ac.search(kw))})


@bp.get("/api/anime/episodes")
def a_episodes():
    aid = request.args.get("id", "")
    if not aid:
        return _err("id kosong")
    return jsonify({"episodes": ac.memo(("e", aid), 300, lambda: ac.episodes(aid))})


@bp.get("/api/anime/stream")
def a_stream():
    aid, ep = request.args.get("id", ""), request.args.get("ep", "")
    if not aid or not ep:
        return _err("id/ep kosong")
    mode = "dub" if request.args.get("mode") == "dub" else "sub"
    info = ac.resolve(aid, ep, mode)
    return jsonify({
        "url": ac.proxy_url(info["stream"], info["referer"]),
        "subtitle": ac.proxy_url(info["subtitle"], info["referer"]) if info["subtitle"] else None,
        "mal_id": info["mal_id"],
    })


@bp.get("/anime/poster")
def a_poster():
    data = ac.get_poster(request.args.get("id", ""), request.args.get("t", ""))
    if not data:
        return _err("Poster tidak ada", 404)
    return Response(data, content_type=ac.sniff_image(data),
                    headers={"Cache-Control": "public, max-age=604800"})


@bp.get("/api/anime/history")
def a_history_get():
    return jsonify({"history": ac.read_history()})


@bp.post("/api/anime/history")
def a_history_post():
    data = request.get_json(silent=True) or {}
    aid, ep, title = str(data.get("id", "")), str(data.get("ep", "")), str(data.get("title", ""))
    if not aid or not ep:
        return _err("data tidak lengkap")
    ac.update_history(aid, title, ep)
    return jsonify({"ok": True})


@bp.post("/api/anime/history/delete")
def a_history_delete():
    data = request.get_json(silent=True) or {}
    ac.delete_history(None if data.get("all") else str(data.get("id", "")))
    return jsonify({"ok": True})


@bp.get("/anime/proxy")
def a_proxy():
    target = request.args.get("u", "")
    referer = request.args.get("r") or None
    if urlparse(target).scheme not in ("http", "https"):
        return _err("URL tidak valid")
    is_m3u8 = urlparse(target).path.lower().endswith(".m3u8")
    if not is_m3u8:
        hit = ac.CACHE.get("p:" + target)
        if hit:
            return Response(hit, content_type=ac.content_type_for(target),
                            headers={"Cache-Control": "public, max-age=86400"})
    body = ac.fetch(target, referer=referer, binary=True)
    if body[:7] == b"#EXTM3U" or is_m3u8:
        text = body.decode("utf-8", "replace")
        return Response(ac.rewrite_playlist(text, target, referer),
                        content_type="application/vnd.apple.mpegurl")
    ac.CACHE.put("p:" + target, body)
    return Response(body, content_type=ac.content_type_for(target),
                    headers={"Cache-Control": "public, max-age=86400"})
