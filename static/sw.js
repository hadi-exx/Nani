// Service worker Dieskybxyz Dashbord.
// Hanya menyimpan aset statis dashboard agar cepat dan bisa dibuka saat offline.
// Data API, streaming audio/video, dan halaman lain TIDAK pernah disimpan.
const VERSION = "dashbord-v1";
const SHELL_CACHE = VERSION + "-shell";
const SHELL = [
  "/",
  "/static/css/style.css",
  "/static/js/script.js",
  "/static/js/pwa.js",
  "/static/manifest.webmanifest",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
  "/static/icons/icon-maskable-512.png",
  "/static/icons/apple-touch-icon.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(SHELL_CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k.startsWith("dashbord-") && k !== SHELL_CACHE)
        .map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

function isNeverCached(url) {
  return url.pathname.includes("/api/") || url.pathname.startsWith("/proxy")
    || url.pathname.startsWith("/music") || url.pathname.startsWith("/anime")
    || url.pathname.startsWith("/termuxai") || url.pathname.startsWith("/cloud")
    || url.pathname.startsWith("/downloader");
}

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;       // font & CDN: biarkan browser
  if (req.headers.has("range") || isNeverCached(url)) return; // streaming & API: langsung ke server

  // Halaman utama: coba jaringan dulu, kalau gagal pakai salinan tersimpan.
  if (req.mode === "navigate" && url.pathname === "/") {
    event.respondWith(networkFirst(req));
    return;
  }

  // Aset statis dashboard: jaringan dulu, cadangan dari cache.
  if (SHELL.includes(url.pathname)) {
    event.respondWith(networkFirst(req));
  }
});

async function networkFirst(req) {
  const cache = await caches.open(SHELL_CACHE);
  try {
    const res = await fetch(req);
    if (res.ok && res.status === 200) cache.put(req, res.clone());
    return res;
  } catch (err) {
    const cached = await cache.match(req, { ignoreSearch: true });
    if (cached) return cached;
    if (req.mode === "navigate") {
      const home = await cache.match("/");
      if (home) return home;
    }
    throw err;
  }
}
