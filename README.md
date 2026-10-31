# Dieskybxyz Dashbord

Dashboard lokal yang berjalan di Termux dan bisa dipasang sebagai aplikasi (PWA).
**Semua fitur berjalan di satu port: `http://localhost:5000`.**

## Instalasi dari nol (Termux baru)

1. Pasang **Termux** dari F-Droid (bukan Play Store).
2. Perbarui dan pasang paket dasar:
   ```bash
   pkg update && pkg upgrade -y
   pkg install python curl ffmpeg unzip -y
   ```
3. Izinkan akses penyimpanan: `termux-setup-storage`, lalu tekan **Izinkan**.
4. Ekstrak proyek ke folder kerja:
   ```bash
   cp ~/storage/downloads/termux-ai.zip ~/
   cd ~
   unzip termux-ai.zip -d ~/dashbord
   ```
5. Pasang paket Python:
   ```bash
   pip install flask requests python-dotenv beautifulsoup4 yt-dlp
   pip install -r ~/dashbord/termux-ai/requirements-media.txt
   ```
6. (Opsional) Kunci halaman Proxy: `export PROXY_PASSWORD="passwordmu"`. Tanpa ini, halaman Proxy tidak dikunci.
7. Jalankan:
   ```bash
   cd ~/dashbord/termux-ai
   python app.py
   ```
8. Buka `http://localhost:5000` di Chrome, lalu pasang lewat ⋮ → **Install app**.

Folder Cloud memakai **rclone** (wajib): `pkg install rclone -y`, lalu `rclone config` dan buat remote bernama `cloud:`.

## Yang perlu disiapkan

1. **Paket Termux:** `pkg install python -y`
2. **Paket Python:** `pip install flask requests python-dotenv yt-dlp beautifulsoup4`
3. **Izin penyimpanan HP** (untuk Downloader): `termux-setup-storage`, lalu izinkan di Android.
4. **Rclone** (wajib untuk Folder Cloud): `pkg install rclone -y`, lalu `rclone config` dan buat remote bernama persis `cloud:`.
5. **Internet** untuk pertama kali membuka halaman: ikon Lucide, Tailwind CDN, dan font dimuat dari luar.
6. **Browser Chrome/Edge/Safari** untuk memasang PWA.

## Menjalankan

1. Pasang dependensi (sekali saja):
   ```
   pkg install python rclone -y        # rclone wajib untuk Folder Cloud
   pip install flask requests python-dotenv yt-dlp beautifulsoup4
   pip install -r requirements-media.txt
   ```
2. Jalankan:
   ```
   cd termux-ai
   python app.py
   ```
3. Buka `http://localhost:5000` di browser.

## Menu dan alamat

| Menu | Alamat | Keterangan |
|---|---|---|
| Musik | `/music` | Streaming lagu |
| Nonton Anime | `/anime` | Streaming anime |
| Proxy | `/proxy` | Koneksi aman |
| Folder Cloud | `/cloud/` | Penyimpanan file (dulu port 8080) |
| Universal Downloader | `/downloader/` | Video dan foto dari sosial media |

Tekan judul **Dieskybxyz Dashbord** untuk membuka atau menutup menu.

**Tombol kembali HP:** saat Musik, Anime, Proxy, Cloud, atau Downloader terbuka, tombol kembali akan menutup layer itu dan tetap di dasbor. Di dasbor, tekan kembali sekali untuk melihat pemberitahuan, lalu tekan sekali lagi dalam 2 detik untuk keluar.

## Cookies (opsional, untuk konten private)

Downloader dan Musik memakai cookie kalau ada file `cookies.txt` (format Netscape). Dicari berurutan: `$YT_COOKIES`, `~/cookies.txt`, lalu `cookies.txt` di folder proyek.

- Tanpa cookie: video publik tetap bisa diunduh dan di-preview.
- Dengan cookie: konten private / login / age-restricted ikut jalan, termasuk preview-nya (cookie diteruskan oleh proxy `/downloader/stream_remote`).

Preview memakai format muxed (video+audio). Untuk YouTube, kalau format muxed tidak tersedia, server meminta ulang format progresif 360p khusus preview — jadi preview bisa tampil walau kualitas download tetap yang terbaik.

## Pasang sebagai aplikasi (PWA)

- **Android (Chrome):** buka `http://localhost:5000`, lalu pilih menu ⋮ → **Install app** atau **Tambahkan ke layar utama**. Di Chrome juga muncul tombol **⬇ Pasang aplikasi**.
- **Desktop (Chrome/Edge):** ikon pasang di address bar, atau tombol **⬇ Pasang aplikasi**.
- **iPhone (Safari):** tombol Bagikan → **Tambahkan ke Layar Utama**.

Setelah dipasang, shell dashboard (tampilan, ikon, manifest) tersimpan di cache. Musik, anime, proxy, dan Cloud tetap memerlukan server yang sedang berjalan, karena datanya tidak pernah disimpan di cache.

## Folder Cloud

- Halaman: `http://localhost:5000/cloud/`.
- Penyimpanan **hanya memakai rclone**, dengan remote bernama `cloud:`. Tidak ada lagi mode penyimpanan HP/Android.
- Rclone dijalankan otomatis sebagai proses pembantu di `127.0.0.1:5572`, bukan sebagai server untuk dibuka pengguna.
- Fitur: daftar file, pencarian, favorit, pratinjau, unduh, buat folder, upload (maks 10 GB per file), pindah, ganti nama, dan tempat sampah (disimpan 30 hari).
- Cache daftar folder disimpan di `data/cloud/cache.json`. Daftar tempat sampah ada di `data/cloud/trash.json`, dan isi item ada di folder `.cloud_trash`.
- Tautan publik mengikuti dukungan remote rclone yang dipakai (mis. Google Drive).

## Struktur

```
termux-ai/
├── app.py                  # Flask: satu server untuk semua fitur (port 5000)
├── cloud_core.py           # Logika Cloud: rclone RC, cache, tempat sampah
├── cloud_bp.py             # Blueprint Cloud di /cloud
├── music.py, anime_bp.py,  # Blueprint Musik, Anime, Proxy, Downloader
│   proxy_bp.py, anime_core.py, downloader_bp.py
├── storage_layout.py       # Tata letak data per fitur
├── templates/index.html    # Halaman dashboard
├── static/
│   ├── manifest.webmanifest  # Manifest PWA
│   ├── sw.js                 # Service worker (cache shell dashboard)
│   ├── icons/                # Ikon PWA
│   ├── css/style.css, js/script.js, js/pwa.js, connection.js
│   ├── music/, anime/, proxy/, downloader/, cloud/
└── data/                   # Data runtime (cloud/, proxy/, anime/, music/, ...)
```

## Catatan

- Ikon memakai Lucide dari unpkg, dan Cloud memakai Tailwind CDN, jadi butuh internet untuk pertama kali dimuat.
- Dashboard hanya bisa diakses dari perangkat ini (`localhost` / `127.0.0.1`). Cloud juga menolak permintaan dengan Host selain itu.
- Setelah mengubah `sw.js`, naikkan `VERSION` di dalamnya agar cache lama diganti.
- Tampilan Musik dan Anime memakai palet yang sama dengan dashboard (warna, font, dan kartu).
