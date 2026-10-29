# downloader_bp.py - Universal Downloader sebagai Blueprint (dashboard, port 5000).
# Halaman : /downloader/   (dibuka sebagai layer ?embed=1 dari dashboard)
# API     : /downloader/api/download, /downloader/download_file
import os
import re
import tempfile

import requests
import yt_dlp
from ytdlp_cookies import cookie_opts
from flask import Blueprint, jsonify, request, send_file, send_from_directory

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UI_DIR = os.path.join(BASE_DIR, "static", "downloader")
DOWNLOAD_FOLDER = os.path.join(tempfile.gettempdir(), "web_downloader")
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

bp = Blueprint("downloader", __name__, url_prefix="/downloader")

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Accept': 'application/json, text/plain, */*'
}

def clean_filename(name):
    name = re.sub(r'[\\/*?:"<>|#]', '', name)
    return name.strip()[:60] or "media_download"

def resolve_tiktok_api(url):
    try:
        api_url = f"https://api.tiklydown.eu.org/api/download?url={url}"
        res = requests.get(api_url, headers=HEADERS, timeout=10)
        if res.status_code == 200:
            data = res.json()
            video_url = data.get('video', {}).get('noWatermark') or data.get('video', {}).get('watermark')
            images_data = data.get('images', [])
            images = [img.get('url') for img in images_data if img.get('url')]
            thumbnail = data.get('cover') or (images[0] if images else '')
            
            if video_url or images:
                return {
                    'success': True,
                    'title': data.get('title', 'TikTok Media'),
                    'thumbnail': thumbnail,
                    'uploader': data.get('author', {}).get('name', 'TikTok User'),
                    'platform': 'TikTok',
                    'is_image': len(images) > 0,
                    'images': images,
                    'download_url': video_url,
                    'raw_url': url
                }
    except Exception:
        pass
    return None

def resolve_instagram_api(url):
    try:
        api_url = f"https://api.vkrdown.com/v1/instagram?url={url}"
        res = requests.get(api_url, headers=HEADERS, timeout=10)
        if res.status_code == 200:
            data = res.json()
            if data.get('data'):
                media_list = data['data']
                images = [item['url'] for item in media_list if item.get('type') == 'image']
                videos = [item['url'] for item in media_list if item.get('type') == 'video']
                
                is_img = len(images) > 0 and len(videos) == 0
                dl_url = videos[0] if videos else (images[0] if images else '')

                return {
                    'success': True,
                    'title': 'Instagram Media',
                    'thumbnail': images[0] if images else '',
                    'uploader': 'Instagram User',
                    'platform': 'Instagram',
                    'is_image': is_img,
                    'images': images if is_img else [],
                    'download_url': dl_url,
                    'raw_url': url
                }
    except Exception:
        pass
    return None


@bp.get('/')
@bp.get('')
def home():
    return send_from_directory(UI_DIR, 'index.html')

@bp.post('/api/download')
def process_media():
    data = request.json
    url = data.get('url', '').strip()

    if not url:
        return jsonify({'error': 'URL tidak boleh kosong!'}), 400

    try:
        r = requests.head(url, allow_redirects=True, headers=HEADERS, timeout=5)
        url = r.url
    except Exception:
        pass

    ydl_opts = {
        **cookie_opts(),
        'quiet': True,
        'no_warnings': True,
        'dump_single_json': True,
        'user_agent': HEADERS['User-Agent'],
        'age_limit': 99
    }

    # 1. Coba pake Engine Utama (yt-dlp)
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if info:
                title = info.get('title', 'Media_Download')
                uploader = info.get('uploader', info.get('extractor', 'Unknown'))
                platform = info.get('extractor_key', 'Media')
                thumbnail = info.get('thumbnail', '')

                images = []
                if 'entries' in info:
                    for entry in info['entries']:
                        if entry and entry.get('url'):
                            images.append(entry['url'])

                is_image = False
                if info.get('ext') in ['jpg', 'png', 'webp', 'jpeg'] or (not info.get('formats') and not images):
                    is_image = True
                    if not images and info.get('url'):
                        images.append(info.get('url'))

                download_url = info.get('url')
                if not download_url and 'formats' in info:
                    formats = [f for f in info['formats'] if f.get('url')]
                    if formats:
                        download_url = formats[-1]['url']

                return jsonify({
                    'success': True,
                    'title': title,
                    'thumbnail': thumbnail,
                    'uploader': uploader,
                    'platform': platform,
                    'is_image': is_image or len(images) > 0,
                    'images': images,
                    'download_url': download_url,
                    'raw_url': url
                })
    except Exception:
        pass

    # 2. Fallback jika TikTok / IG diblokir yt-dlp
    if 'tiktok.com' in url or 'vt.tiktok.com' in url:
        res = resolve_tiktok_api(url)
        if res: return jsonify(res)
    
    if 'instagram.com' in url:
        res = resolve_instagram_api(url)
        if res: return jsonify(res)

    return jsonify({'error': 'Gagal mengambil data dari URL ini. Pastikan akun tidak diprivate!'}), 400

@bp.get('/download_file')
def download_file():
    raw_url = request.args.get('raw_url')
    direct_url = request.args.get('url')
    filename = request.args.get('filename', 'media.mp4')
    is_image = request.args.get('is_image', 'false') == 'true'

    filename = clean_filename(filename)

    if not direct_url and not raw_url:
        return "URL file tidak valid", 400

    # Unduh Gambar
    if is_image and direct_url:
        try:
            r = requests.get(direct_url, headers=HEADERS, timeout=15)
            img_filename = filename if filename.endswith('.jpg') else f"{filename}.jpg"
            img_path = os.path.join(DOWNLOAD_FOLDER, img_filename)
            with open(img_path, 'wb') as f:
                f.write(r.content)
            return send_file(img_path, as_attachment=True, download_name=img_filename)
        except Exception as e:
            return f"Gagal mengunduh gambar: {str(e)}", 500

    # Unduh Video
    vid_filename = filename if filename.endswith('.mp4') else f"{filename}.mp4"
    out_path = os.path.join(DOWNLOAD_FOLDER, vid_filename)
    if os.path.exists(out_path):
        try: os.remove(out_path)
        except: pass

    if raw_url:
        ydl_opts = {
            **cookie_opts(),
            'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
            'outtmpl': out_path,
            'quiet': True,
            'user_agent': HEADERS['User-Agent']
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
                for chunk in r.iter_content(chunk_size=1024*1024):
                    if chunk:
                        f.write(chunk)
            if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                return send_file(out_path, as_attachment=True, download_name=vid_filename)
        except Exception as e:
            return f"Gagal mengunduh video: {str(e)}", 500

    return "Gagal memproses file video.", 500
