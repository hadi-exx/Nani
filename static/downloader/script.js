// Universal Downloader - UI logic
// Tab Download / Queue / History, progress polling, streaming, history CRUD.
document.addEventListener('DOMContentLoaded', () => {
    const API = '/downloader';

    const $ = (id) => document.getElementById(id);

    // ---------- Elemen ----------
    const mediaUrl = $('mediaUrl');
    const btnFetch = $('btnFetch');
    const formatSeg = $('formatSeg');
    const qualitySelect = $('qualitySelect');
    const qualityGroup = $('qualityGroup');
    const loading = $('loading');
    const statusMsg = $('statusMsg');
    const resultCard = $('resultCard');
    const resThumb = $('resThumb');
    const resPlayer = $('resPlayer');
    const imageSlider = $('imageSlider');
    const currentImage = $('currentImage');
    const prevImg = $('prevImg');
    const nextImg = $('nextImg');
    const imgCounter = $('imgCounter');
    const resPlatform = $('resPlatform');
    const resTitle = $('resTitle');
    const resUploader = $('resUploader');
    const downloadButtons = $('downloadButtons');
    const queueList = $('queueList');
    const queueEmpty = $('queueEmpty');
    const queueCount = $('queueCount');
    const btnClearQueue = $('btnClearQueue');
    const historyList = $('historyList');
    const historyEmpty = $('historyEmpty');
    const btnClearHistory = $('btnClearHistory');

    // ---------- State ----------
    let currentMeta = null;
    let images = [];
    let imgIdx = 0;
    let activeFormat = 'video';
    let pollTimer = null;
    let lastJobs = [];
    let lastDoneCount = -1;
    let toastTimer = null;

    const STATUS_TEXT = {
        queued: 'Menunggu',
        downloading: 'Mengunduh',
        converting: 'Konversi',
        done: 'Selesai',
        error: 'Gagal',
    };

    // Kalau stream proxy menolak / format tidak didukung, jangan biarkan
    // player kosong tanpa penjelasan.
    resPlayer.addEventListener('error', () => {
        if (resPlayer.classList.contains('hidden') || !resPlayer.src) return;
        resPlayer.classList.add('hidden');
        resPlayer.removeAttribute('src');
        showMsg('Preview gagal diputar. Coba download langsung, atau tambahkan cookies.txt untuk konten private.');
    });

    // ---------- Helper ----------
    function humanSize(bytes) {
        const n = Number(bytes) || 0;
        if (!n) return '';
        const units = ['B', 'KB', 'MB', 'GB', 'TB'];
        let value = n;
        let i = 0;
        while (value >= 1024 && i < units.length - 1) {
            value /= 1024;
            i += 1;
        }
        const digits = i > 0 && value < 10 ? 1 : 0;
        return `${value.toFixed(digits)}${units[i]}`;
    }

    function formatDate(ts) {
        if (!ts) return '';
        try {
            return new Date(ts * 1000).toLocaleString('id-ID', {
                day: '2-digit',
                month: 'short',
                hour: '2-digit',
                minute: '2-digit',
            });
        } catch (_) {
            return '';
        }
    }

    function toast(message) {
        let el = document.querySelector('.dl-toast');
        if (!el) {
            el = document.createElement('div');
            el.className = 'dl-toast';
            el.setAttribute('role', 'status');
            document.body.appendChild(el);
        }
        el.textContent = message;
        el.classList.add('show');
        clearTimeout(toastTimer);
        toastTimer = setTimeout(() => el.classList.remove('show'), 2200);
    }

    function showMsg(message, type = 'error') {
        statusMsg.textContent = message;
        statusMsg.className = `status-msg ${type}`;
    }

    function clearMsg() {
        statusMsg.textContent = '';
        statusMsg.className = 'status-msg hidden';
    }

    function confirmDialog(message) {
        return new Promise((resolve) => {
            const modal = document.getElementById('confirmModal');
            const titleEl = document.getElementById('confirmTitle');
            const msgEl = document.getElementById('confirmMessage');
            const okBtn = document.getElementById('confirmOk');
            const cancelBtn = document.getElementById('confirmCancel');

            let settled = false;
            const cleanup = () => {
                modal.classList.add('hidden');
                okBtn.onclick = null;
                cancelBtn.onclick = null;
            };
            const done = (v) => {
                if (settled) return;
                settled = true;
                cleanup();
                resolve(v);
            };

            titleEl.textContent = 'Konfirmasi';
            msgEl.textContent = message;
            okBtn.onclick = () => done(true);
            cancelBtn.onclick = () => done(false);
            modal.classList.remove('hidden');
            modal.onclick = (e) => { if (e.target === modal) done(false); };
        });
    }

    async function apiFetch(url, options) {
        const res = await fetch(url, options);
        const data = await res.json().catch(() => ({}));
        if (!res.ok || data.error) {
            throw new Error(data.error || `Permintaan gagal (${res.status})`);
        }
        return data;
    }

    // ---------- Tabs ----------
    function switchTab(name) {
        document.querySelectorAll('.tab').forEach((tab) => {
            tab.classList.toggle('active', tab.dataset.tab === name);
        });
        document.querySelectorAll('.tab-panel').forEach((panel) => {
            panel.classList.toggle('hidden', panel.id !== `tab-${name}`);
        });
        if (name === 'history') loadHistory();
        if (name === 'queue') pollJobs();
    }

    document.querySelectorAll('.tab').forEach((tab) => {
        tab.addEventListener('click', () => switchTab(tab.dataset.tab));
    });

    // ---------- Format ----------
    formatSeg.querySelectorAll('.seg').forEach((seg) => {
        seg.addEventListener('click', () => {
            activeFormat = seg.dataset.format;
            formatSeg.querySelectorAll('.seg').forEach((s) => {
                s.classList.toggle('active', s === seg);
            });
            qualityGroup.classList.toggle('hidden', activeFormat === 'audio');
            if (currentMeta && !resultCard.classList.contains('hidden')) {
                renderResult(currentMeta);
            }
        });
    });

    function buildFormat() {
        const quality = qualitySelect.value;
        if (activeFormat === 'audio' || quality === 'best') return '';
        return `bestvideo[height<=${quality}][ext=mp4]+bestaudio[ext=m4a]/best[height<=${quality}][ext=mp4]/best`;
    }

    // ---------- Preview ----------
    function resetPreview() {
        try {
            resPlayer.pause();
        } catch (_) { /* noop */ }
        resPlayer.removeAttribute('src');
        try {
            resPlayer.load();
        } catch (_) { /* noop */ }
        resPlayer.classList.add('hidden');
        resThumb.classList.add('hidden');
        resThumb.removeAttribute('src');
        imageSlider.classList.add('hidden');
    }

    function updateSlider() {
        if (!images.length) return;
        currentImage.src = images[imgIdx];
        imgCounter.textContent = `${imgIdx + 1} / ${images.length}`;
    }

    prevImg.addEventListener('click', () => {
        if (images.length < 2) return;
        imgIdx = (imgIdx - 1 + images.length) % images.length;
        updateSlider();
    });

    nextImg.addEventListener('click', () => {
        if (images.length < 2) return;
        imgIdx = (imgIdx + 1) % images.length;
        updateSlider();
    });

    function renderResult(data) {
        resetPreview();
        resPlatform.textContent = data.platform || 'Media';
        resTitle.textContent = data.title || 'Tanpa judul';
        resUploader.textContent = data.uploader || '';
        downloadButtons.innerHTML = '';

        images = Array.isArray(data.images) ? data.images.filter(Boolean) : [];
        const isImage = Boolean(data.is_image) && images.length > 0;

        if (isImage) {
            imgIdx = 0;
            updateSlider();
            imageSlider.classList.remove('hidden');
        } else if (data.thumbnail) {
            resThumb.src = data.thumbnail;
            resThumb.classList.remove('hidden');
        }

        if (isImage) {
            images.forEach((img, i) => {
                const link = document.createElement('a');
                link.className = 'btn download-btn';
                link.textContent = images.length > 1 ? `⬇ Foto ${i + 1}` : '⬇ Unduh Foto';
                const name = `${data.title || 'foto'}-${i + 1}`;
                link.href = `${API}/download_file?url=${encodeURIComponent(img)}` +
                    `&is_image=true&filename=${encodeURIComponent(name)}`;
                downloadButtons.appendChild(link);
            });
            return;
        }

        const dl = document.createElement('button');
        dl.type = 'button';
        dl.className = 'btn download-btn';
        dl.textContent = activeFormat === 'audio' ? '⬇ Download MP3' : '⬇ Download Video';
        dl.addEventListener('click', () => enqueueCurrent());
        downloadButtons.appendChild(dl);

        const previewSrc = data.preview_url || (data.playable === false ? null : data.download_url);
        if (previewSrc) {
            const pv = document.createElement('button');
            pv.type = 'button';
            pv.className = 'btn ghost';
            pv.textContent = '▶ Preview';
            pv.addEventListener('click', () => {
                resThumb.classList.add('hidden');
                imageSlider.classList.add('hidden');
                const ref = encodeURIComponent(data.raw_url || '');
                resPlayer.src = `${API}/stream_remote?url=${encodeURIComponent(previewSrc)}&ref=${ref}`;
                resPlayer.classList.remove('hidden');
                resPlayer.play().catch(() => { /* butuh interaksi user */ });
            });
            downloadButtons.appendChild(pv);
        }
    }

    // ---------- Fetch metadata ----------
    async function fetchMeta() {
        const url = mediaUrl.value.trim();
        if (!url) {
            showMsg('Masukkan URL terlebih dahulu.');
            mediaUrl.focus();
            return;
        }

        clearMsg();
        loading.classList.remove('hidden');
        resultCard.classList.add('hidden');
        btnFetch.disabled = true;

        try {
            const data = await apiFetch(`${API}/api/download`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ url }),
            });
            currentMeta = data;
            renderResult(data);
            resultCard.classList.remove('hidden');
        } catch (err) {
            showMsg(err.message || 'Gagal memproses URL.');
        } finally {
            loading.classList.add('hidden');
            btnFetch.disabled = false;
        }
    }

    btnFetch.addEventListener('click', fetchMeta);
    mediaUrl.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
            e.preventDefault();
            fetchMeta();
        }
    });

    // ---------- Queue ----------
    async function enqueueCurrent() {
        const url = (currentMeta && currentMeta.raw_url) || mediaUrl.value.trim();
        if (!url) {
            showMsg('URL tidak tersedia.');
            return;
        }
        try {
            await apiFetch(`${API}/api/queue`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    urls: [url],
                    audio_only: activeFormat === 'audio',
                    format: buildFormat(),
                    title: (currentMeta && currentMeta.title) || '',
                    platform: (currentMeta && currentMeta.platform) || '',
                }),
            });
            toast('Masuk ke queue download');
            switchTab('queue');
            pollJobs();
        } catch (err) {
            showMsg(err.message || 'Gagal menambah ke queue.');
        }
    }

    function buildQueueItem(job) {
        const item = document.createElement('div');
        item.className = `queue-item status-${job.status}`;
        item.dataset.id = job.id;

        const head = document.createElement('div');
        head.className = 'qi-head';
        const platform = document.createElement('span');
        platform.className = 'qi-platform';
        platform.textContent = job.platform || 'Media';
        const status = document.createElement('span');
        status.className = `qi-status ${job.status}`;
        status.textContent = STATUS_TEXT[job.status] || job.status;
        head.append(platform, status);

        const title = document.createElement('div');
        title.className = 'qi-title';
        title.textContent = job.title || job.url || 'Media';
        title.title = job.url || '';

        const track = document.createElement('div');
        track.className = 'progress-track';
        const fill = document.createElement('div');
        fill.className = 'progress-fill';
        const pct = job.status === 'done'
            ? 100
            : Math.max(0, Math.min(100, Number(job.progress) || 0));
        fill.style.width = `${pct}%`;
        track.appendChild(fill);

        const meta = document.createElement('div');
        meta.className = 'qi-meta';
        if (job.status === 'error') {
            meta.textContent = job.error || 'Gagal mengunduh';
        } else {
            const bits = [`${pct.toFixed(0)}%`];
            if (job.size) bits.push(job.size);
            if (job.status === 'downloading' && job.speed) bits.push(job.speed);
            if (job.status === 'downloading' && job.eta) bits.push(`ETA ${job.eta}s`);
            meta.textContent = bits.join(' · ');
        }

        const actions = document.createElement('div');
        actions.className = 'qi-actions';

        const mkButton = (label, cls, action) => {
            const btn = document.createElement('button');
            btn.type = 'button';
            btn.className = `btn small ${cls}`;
            btn.textContent = label;
            btn.dataset.action = action;
            btn.dataset.id = job.id;
            return btn;
        };

        if (['queued', 'downloading', 'converting'].includes(job.status)) {
            actions.appendChild(mkButton('Batalkan', 'ghost', 'cancel'));
        }
        if (job.status === 'error') {
            actions.appendChild(mkButton('Ulangi', 'primary', 'retry'));
        }
        if (job.status === 'done') {
            const save = document.createElement('a');
            save.className = 'btn small download-btn';
            save.textContent = '⬇ Simpan';
            save.href = `${API}/file/${job.id}`;
            actions.appendChild(save);
            if (!job.audio_only) {
                actions.appendChild(mkButton('▶ Stream', 'ghost', 'stream'));
            }
        }

        item.append(head, title, track, meta, actions);
        return item;
    }

    function renderQueue(jobs) {
        lastJobs = jobs;

        const active = jobs.filter((j) => ['queued', 'downloading', 'converting'].includes(j.status)).length;
        queueCount.textContent = String(active);

        const doneCount = jobs.filter((j) => j.status === 'done').length;
        const historyVisible = !document.getElementById('tab-history').classList.contains('hidden');
        if (lastDoneCount !== -1 && doneCount > lastDoneCount && historyVisible) {
            loadHistory();
        }
        lastDoneCount = doneCount;

        queueList.innerHTML = '';
        queueEmpty.classList.toggle('hidden', jobs.length > 0);

        jobs.slice().reverse().forEach((job) => {
            queueList.appendChild(buildQueueItem(job));
        });
    }

    async function pollJobs() {
        try {
            const res = await fetch(`${API}/api/jobs`, { cache: 'no-store' });
            if (!res.ok) return;
            const data = await res.json();
            renderQueue(Array.isArray(data.jobs) ? data.jobs : []);
        } catch (_) {
            /* diamkan, coba lagi di poll berikutnya */
        }
    }

    function startPolling() {
        if (pollTimer) return;
        pollTimer = setInterval(() => {
            if (!document.hidden) pollJobs();
        }, 1500);
    }

    queueList.addEventListener('click', async (e) => {
        const btn = e.target.closest('button[data-action]');
        if (!btn) return;
        const { id, action } = btn.dataset;

        try {
            if (action === 'cancel') {
                await apiFetch(`${API}/api/jobs/${id}/cancel`, { method: 'POST' });
                toast('Download dibatalkan');
            } else if (action === 'retry') {
                await apiFetch(`${API}/api/jobs/${id}/retry`, { method: 'POST' });
                toast('Download diulang');
            } else if (action === 'stream') {
                const job = lastJobs.find((j) => j.id === id);
                if (job) showLocalStream(job);
                return;
            }
            pollJobs();
        } catch (err) {
            toast(err.message || 'Aksi gagal');
        }
    });

    btnClearQueue.addEventListener('click', async () => {
        try {
            await apiFetch(`${API}/api/jobs/clear`, { method: 'DELETE' });
            pollJobs();
        } catch (err) {
            toast(err.message || 'Gagal membersihkan queue');
        }
    });

    function showLocalStream(job) {
        switchTab('download');
        resetPreview();
        resPlatform.textContent = job.platform || 'Media';
        resTitle.textContent = job.title || job.filename || 'Media';
        resUploader.textContent = [job.filename, job.size].filter(Boolean).join(' · ');
        downloadButtons.innerHTML = '';

        const save = document.createElement('a');
        save.className = 'btn download-btn';
        save.textContent = '⬇ Simpan File';
        save.href = `${API}/file/${job.id}`;
        save.setAttribute('download', '');
        downloadButtons.appendChild(save);

        resultCard.classList.remove('hidden');
        resPlayer.src = `${API}/stream/${job.id}`;
        resPlayer.classList.remove('hidden');
        resPlayer.play().catch(() => { /* user bisa tekan play manual */ });
        resultCard.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }

    // ---------- History ----------
    function buildHistoryItem(item) {
        const wrap = document.createElement('div');
        wrap.className = 'hist-item';

        const info = document.createElement('div');
        info.className = 'hi-info';

        const platform = document.createElement('span');
        platform.className = 'qi-platform';
        platform.textContent = item.platform || 'Media';

        const title = document.createElement('strong');
        title.className = 'hi-title';
        title.textContent = item.title || item.filename || 'Media';
        title.title = item.filename || '';

        const meta = document.createElement('span');
        meta.className = 'hi-meta';
        meta.textContent = [
            item.audio_only ? 'MP3' : 'MP4',
            humanSize(item.size),
            formatDate(item.created_at),
        ].filter(Boolean).join(' · ');

        info.append(platform, title, meta);

        const actions = document.createElement('div');
        actions.className = 'hi-actions';

        const download = document.createElement('a');
        download.className = 'icon-btn';
        download.href = `${API}/history/file/${item.id}`;
        download.title = 'Unduh ulang';
        download.setAttribute('aria-label', 'Unduh ulang');
        download.textContent = '⬇';

        const remove = document.createElement('button');
        remove.type = 'button';
        remove.className = 'icon-btn danger';
        remove.dataset.id = item.id;
        remove.title = 'Hapus dari riwayat';
        remove.setAttribute('aria-label', 'Hapus dari riwayat');
        remove.textContent = '🗑';

        actions.append(download, remove);
        wrap.append(info, actions);
        return wrap;
    }

    function renderHistory(items) {
        historyList.innerHTML = '';
        historyEmpty.classList.toggle('hidden', items.length > 0);
        items.forEach((item) => historyList.appendChild(buildHistoryItem(item)));
    }

    async function loadHistory() {
        try {
            const data = await apiFetch(`${API}/api/history`, { cache: 'no-store' });
            renderHistory(Array.isArray(data.items) ? data.items : []);
        } catch (err) {
            toast(err.message || 'Gagal memuat riwayat');
        }
    }

    historyList.addEventListener('click', async (e) => {
        const btn = e.target.closest('button.icon-btn.danger[data-id]');
        if (!btn) return;
        const ok = await confirmDialog('Hapus item ini dari riwayat?');
        if (!ok) return;

        try {
            await apiFetch(`${API}/api/history/${btn.dataset.id}`, { method: 'DELETE' });
            loadHistory();
        } catch (err) {
            toast(err.message || 'Gagal menghapus item');
        }
    });

    btnClearHistory.addEventListener('click', async () => {
        const ok = await confirmDialog('Hapus SEMUA riwayat download?');
        if (!ok) return;
        try {
            await apiFetch(`${API}/api/history`, { method: 'DELETE' });
            loadHistory();
            toast('Riwayat dibersihkan');
        } catch (err) {
            toast(err.message || 'Gagal membersihkan riwayat');
        }
    });

    // ---------- Init ----------
    startPolling();
    pollJobs();
});
