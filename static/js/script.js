document.addEventListener('DOMContentLoaded', () => {
    const title = document.getElementById('dashTitle');
    const menu = document.getElementById('menuContent');
    const panel = document.getElementById('dashPanel');

    const isOpen = () => !menu.classList.contains('is-hidden');
    const setMenu = (open) => {
        menu.classList.toggle('is-hidden', !open);
        title.setAttribute('aria-expanded', String(open));
    };

    setMenu(false);

    // Seluruh kartu (panel) bisa ditekan untuk buka / tutup menu.
    // Tekan kartu menu (nav-card) tetap menjalankan aksinya sendiri.
    panel.addEventListener('click', (e) => {
        if (e.target.closest('.nav-card')) return;
        setMenu(!isOpen());
    });

    // Judul tetap bisa dibuka lewat keyboard
    title.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            setMenu(!isOpen());
        }
    });

    // Klik di luar kartu: tutup menu
    document.addEventListener('click', (e) => {
        if (isOpen() && !panel.contains(e.target)) {
            setMenu(false);
        }
    });

    // Layer Musik, Anime, Proxy, Cloud, Downloader. Hanya satu layer yang tampil;
    // layer lain tetap dimuat supaya musik / anime yang sedang berjalan tidak berhenti.
    const LAYERS = {
        music: { card: 'musicCard', frame: 'musicFrame', iframe: 'musicIframe' },
        anime: { card: 'animeCard', frame: 'animeFrame', iframe: 'animeIframe' },
        proxy: { card: 'proxyCard', frame: 'proxyFrame', iframe: 'proxyIframe' },
        cloud: { card: 'cloudCard', frame: 'cloudFrame', iframe: 'cloudIframe' },
        downloader: { card: 'downloaderCard', frame: 'downloaderFrame', iframe: 'downloaderIframe' },
    };

    let openName = null; // layer yang sedang tampil, null jika di dasbor

    const hideLayer = (name) => {
        const frame = document.getElementById(LAYERS[name].frame);
        if (frame) frame.classList.add('hidden');
    };
    const hideAllLayers = () => {
        Object.keys(LAYERS).forEach(hideLayer);
        openName = null;
    };

    // ---- Tombol kembali HP ----
    // Setiap layer yang dibuka dicatat di riwayat browser, jadi tombol kembali
    // menutup layer dulu dan tidak keluar dari dasbor.
    // Di dasbor, tekan kembali 2x dalam 2 detik untuk keluar (ada pemberitahuan di tekan pertama).
    const EXIT_WINDOW = 2000;
    let lastBackAt = 0;
    let toastTimer = null;

    const toast = document.createElement('div');
    toast.textContent = 'Tekan sekali lagi untuk keluar';
    toast.setAttribute('role', 'status');
    toast.style.cssText = 'position:fixed;left:50%;bottom:28px;transform:translateX(-50%) translateY(20px);' +
        'background:rgba(15,23,42,.92);color:#fff;padding:10px 18px;border-radius:999px;font-size:14px;' +
        'font-family:inherit;z-index:10000;opacity:0;pointer-events:none;transition:opacity .2s,transform .2s;';
    document.body.appendChild(toast);

    const showToast = () => {
        toast.style.opacity = '1';
        toast.style.transform = 'translateX(-50%) translateY(0)';
        clearTimeout(toastTimer);
        toastTimer = setTimeout(() => {
            toast.style.opacity = '0';
            toast.style.transform = 'translateX(-50%) translateY(20px)';
        }, EXIT_WINDOW);
    };

    // Dasbor selalu punya satu "penjaga" di atasnya agar tekan kembali pertama tertangkap di sini.
    if (!(history.state && history.state.guard)) {
        history.replaceState({ root: true }, '');
        history.pushState({ guard: true }, '');
    }

    const openLayer = (name) => {
        const l = LAYERS[name];
        const frame = document.getElementById(l.frame);
        const iframe = document.getElementById(l.iframe);
        if (!frame || !iframe) return;
        setMenu(false);
        Object.keys(LAYERS).forEach((other) => { if (other !== name) hideLayer(other); });
        if (!iframe.getAttribute('src')) iframe.src = iframe.dataset.src;
        frame.classList.remove('hidden');
        if (openName !== name) {
            if (history.state && history.state.layer) history.replaceState({ layer: name }, '');
            else history.pushState({ layer: name }, '');
        }
        openName = name;
    };

    // Dipanggil dari tombol di dalam layer maupun dari halaman layer (postMessage).
    const closeLayer = (name) => {
        if (openName !== name) { hideLayer(name); return; }
        if (history.state && history.state.layer) history.back(); // popstate yang menutup layer
        else hideAllLayers();
    };

    Object.keys(LAYERS).forEach((name) => {
        const card = document.getElementById(LAYERS[name].card);
        if (!card) return;
        card.addEventListener('click', (e) => {
            e.preventDefault();
            openLayer(name);
        });
    });

    // Tombol kembali di dalam layer mengirim pesan penutupan
    window.addEventListener('message', (e) => {
        if (e.origin !== location.origin || !e.data) return;
        const m = /^(\w+):close$/.exec(e.data.type || '');
        if (m && LAYERS[m[1]]) closeLayer(m[1]);
    });

    window.addEventListener('popstate', (e) => {
        // Tombol kembali HP ditekan
        if (openName) {
            hideAllLayers();
        } else if (isOpen()) {
            setMenu(false);
        } else {
            const now = Date.now();
            if (now - lastBackAt < EXIT_WINDOW) {
                history.back(); // tekan kedua: keluar dari dasbor
                return;
            }
            lastBackAt = now;
            showToast();
        }
        // Pastikan tekan kembali berikutnya tetap tertangkap dasbor
        if (!(e.state && e.state.guard)) history.pushState({ guard: true }, '');
    });
});
