// Pengecek koneksi ke server Termux + layar "server tidur" yang estetik.
// Dipakai di halaman AI, halaman musik, dan offline.html. Juga mendaftarkan service worker.
(function () {
  const PING_URL = "/api/ping";
  const OK_EVERY_MS = 6000;   // cek rutin saat server hidup
  const RETRY_EVERY_MS = 4000; // saat layar offline tampil
  const isOfflinePage = document.body && document.body.dataset.offline === "1";

  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/sw.js").catch(() => {});
  }

  const css = `
    .sx-off { position: fixed; inset: 0; z-index: 9999; display: flex; align-items: center; justify-content: center;
      padding: 18px; background: #ffffff;
      background-image: radial-gradient(#d9d9d9 1.2px, transparent 1.2px); background-size: 14px 14px;
      font-family: "Comic Neue", "Trebuchet MS", sans-serif; color: #000; animation: sx-in .35s ease both; }
    .sx-card { position: relative; width: 100%; max-width: 380px; background: #fff; border: 4px solid #000;
      border-radius: 14px; box-shadow: 8px 8px 0 #000; padding: 22px 20px 20px; }
    .sx-tag { position: absolute; top: -14px; left: 16px; background: #ffd60a; border: 3px solid #000;
      padding: 3px 10px; font-family: "Bangers", Impact, sans-serif; font-size: 16px; letter-spacing: 2px;
      box-shadow: 3px 3px 0 #000; }
    .sx-dot { display: inline-block; width: 9px; height: 9px; margin-right: 6px; border-radius: 50%; background: #e00;
      vertical-align: middle; animation: sx-blink 1s steps(2, start) infinite; }
    .sx-title { margin: 14px 0 6px; font-family: "Bangers", Impact, sans-serif; font-weight: 400; font-size: 46px;
      line-height: 0.95; letter-spacing: 1px; }
    .sx-title span { background: #ffd60a; padding: 0 6px; border: 3px solid #000; box-decoration-break: clone;
      -webkit-box-decoration-break: clone; }
    .sx-text { margin: 10px 0; font-size: 15px; line-height: 1.4; }
    .sx-small { font-size: 13px; color: #444; }
    .sx-code { margin: 14px 0; background: #000; color: #9dff9d; font-family: "Courier New", monospace;
      font-size: 15px; padding: 10px 12px; border-radius: 8px; box-shadow: 4px 4px 0 #ffd60a; word-break: break-all; }
    .sx-prompt { color: #ffd60a; margin-right: 6px; }
    .sx-foot { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-top: 16px; flex-wrap: wrap; }
    .sx-count { font-size: 13px; color: #333; }
    .sx-count b { font-family: "Bangers", Impact, sans-serif; font-weight: 400; font-size: 18px; letter-spacing: 1px; }
    .sx-btn { font-family: "Bangers", Impact, sans-serif; font-size: 20px; letter-spacing: 1.5px; cursor: pointer;
      background: #000; color: #fff; border: 3px solid #000; border-radius: 8px; padding: 6px 16px;
      box-shadow: 4px 4px 0 #ffd60a; }
    .sx-btn:active { transform: translate(2px, 2px); box-shadow: 2px 2px 0 #ffd60a; }
    .sx-btn:disabled { opacity: .5; cursor: default; }
    .sx-spin { display: inline-block; animation: sx-spin 1s steps(4) infinite; }
    @keyframes sx-in { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: none; } }
    @keyframes sx-blink { to { visibility: hidden; } }
    @keyframes sx-spin { to { transform: rotate(360deg); } }
  `;

  let overlay = null;
  let nextAt = 0;
  let checking = false;
  let fails = 0;

  function ensureStyle() {
    if (document.getElementById("sx-style")) return;
    const style = document.createElement("style");
    style.id = "sx-style";
    style.textContent = css;
    document.head.appendChild(style);
  }

  function buildOverlay() {
    ensureStyle();
    const el = document.createElement("div");
    el.className = "sx-off";
    el.setAttribute("role", "alertdialog");
    el.setAttribute("aria-live", "assertive");
    el.innerHTML = `
      <div class="sx-card">
        <div class="sx-tag"><span class="sx-dot"></span>OFFLINE</div>
        <h2 class="sx-title">SERVER <span>TIDUR</span></h2>
        <p class="sx-text">Aplikasi tidak bisa terhubung ke Termux. Biasanya karena Termux ditutup atau servernya berhenti.</p>
        <div class="sx-code"><span class="sx-prompt">$</span>python3 app.py</div>
        <p class="sx-text sx-small">Buka Termux, jalankan perintah di atas. Aplikasi akan tersambung sendiri.</p>
        <div class="sx-foot">
          <span class="sx-count"><span class="sx-spin">◐</span> coba lagi dalam <b data-count>4</b> dtk</span>
          <button type="button" class="sx-btn" data-retry>COBA LAGI</button>
        </div>
      </div>`;
    el.querySelector("[data-retry]").addEventListener("click", () => { nextAt = 0; tick(); });
    return el;
  }

  function showOverlay() {
    if (overlay) return;
    overlay = buildOverlay();
    document.body.appendChild(overlay);
  }

  function hideOverlay() {
    if (!overlay) return;
    overlay.remove();
    overlay = null;
  }

  async function ping() {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 3000);
    try {
      const res = await fetch(PING_URL + "?t=" + Date.now(), { cache: "no-store", signal: ctrl.signal });
      return res.ok;
    } catch (e) {
      return false;
    } finally {
      clearTimeout(timer);
    }
  }

  async function check() {
    if (checking) return;
    checking = true;
    const ok = await ping();
    checking = false;

    if (ok) {
      fails = 0;
      if (overlay) {
        if (isOfflinePage) location.replace("/");   // dari offline.html: kembali ke aplikasi
        else hideOverlay();
      }
      nextAt = Date.now() + OK_EVERY_MS;
      return;
    }

    fails += 1;
    // Offline page langsung tampil; di aplikasi biasa tunggu 2 kali gagal (hindari kedip karena jaringan sesaat)
    if (isOfflinePage || fails >= 2) showOverlay();
    nextAt = Date.now() + RETRY_EVERY_MS;
  }

  function tick() {
    if (overlay) {
      const left = Math.max(0, Math.ceil((nextAt - Date.now()) / 1000));
      const el = overlay.querySelector("[data-count]");
      if (el) el.textContent = String(left);
    }
    if (Date.now() >= nextAt) check();
  }

  nextAt = 0;
  setInterval(tick, 1000);
  tick();
})();
