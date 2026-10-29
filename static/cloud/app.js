'use strict';

/* ==========================================================================
   Konfigurasi & konstanta
   ========================================================================== */
const API = '/cloud/api';
let REMOTE = 'local:';
let activeStorageMode = 'android';
const FAV_KEY = 'cloud_favs_v2';
const TEXT_EDIT_LIMIT = 2 * 1024 * 1024;
const MAX_NAME_BYTES = 255;
const RENDER_CHUNK = 100;
const TRASH_DAYS = 30;          // harus sama dengan TRASH_DAYS di web.py

const KIND_EXT = {
  image: ['jpg', 'jpeg', 'png', 'webp', 'gif', 'bmp', 'svg', 'avif'],
  video: ['mp4', 'webm', 'm4v', 'mov', 'mkv', 'ogv'],
  audio: ['mp3', 'wav', 'ogg', 'm4a', 'flac', 'aac', 'opus'],
  pdf: ['pdf'],
  text: ['txt', 'md', 'json', 'csv', 'log', 'xml', 'yaml', 'yml', 'ini', 'cfg', 'conf',
    'py', 'js', 'ts', 'jsx', 'tsx', 'css', 'html', 'htm', 'sh', 'bat', 'ps1', 'java',
    'c', 'cpp', 'h', 'cs', 'go', 'rs', 'php', 'rb', 'sql', 'env', 'toml'],
  office: ['doc', 'docx', 'xls', 'xlsx', 'ppt', 'pptx', 'odt', 'ods', 'odp', 'rtf'],
};
const KIND_LABEL = { image: 'Gambar', video: 'Video', audio: 'Audio', pdf: 'PDF', text: 'Teks', office: 'Dokumen', other: 'File' };
const KIND_ICON = { image: '🖼️', video: '🎬', audio: '🎵', pdf: '📕', text: '📝', office: '📘', other: '📄' };

/* ==========================================================================
   State
   ========================================================================== */
const state = {
  currentPath: '',
  rawItems: [],
  items: new Map(),          // path -> item yang sedang ditampilkan
  selected: new Set(),
  isGridView: false,
  category: 'all',           // 'all' | 'favorites'
  favorites: loadFavorites(),
  listRequestId: 0,
  loaded: false,
  history: [],
  visibleItems: [],
  renderedCount: 0,
  observer: null,
  animateNext: false,
  previewList: [],
  previewIndex: -1,
  previewDirty: false,
  previewToken: 0,
};

const dialog = { resolve: null, validate: null, hasInput: false };

class ApiError extends Error {}

/* ==========================================================================
   Utilitas
   ========================================================================== */
const $ = (id) => document.getElementById(id);

function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (key === 'dataset') Object.assign(node.dataset, value);
    else if (key === 'attrs') {
      for (const [attr, v] of Object.entries(value)) node.setAttribute(attr, v);
    } else node[key] = value;
  }
  for (const child of children) if (child) node.append(child);
  return node;
}

function fileKind(name) {
  const dot = name.lastIndexOf('.');
  const ext = dot > 0 ? name.slice(dot + 1).toLowerCase() : '';
  for (const [kind, list] of Object.entries(KIND_EXT)) {
    if (list.includes(ext)) return kind;
  }
  return 'other';
}

function iconFor(item) {
  return item.IsDir ? '📁' : KIND_ICON[fileKind(item.Name)];
}

function formatBytes(n) {
  if (n === null || n === undefined || Number.isNaN(Number(n)) || Number(n) < 0) return '-';
  let value = Number(n);
  if (value === 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (value >= 1024 && i < units.length - 1) { value /= 1024; i++; }
  return `${value.toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
}

function parentOf(path) {
  const i = path.lastIndexOf('/');
  return i < 0 ? '' : path.slice(0, i);
}

function baseName(path) {
  const i = path.lastIndexOf('/');
  return i < 0 ? path : path.slice(i + 1);
}

function joinPath(dir, name) {
  return dir ? `${dir}/${name}` : name;
}

/** Mengembalikan pesan error, atau null jika nama valid. Dipakai untuk nama yang sudah di-trim. */
function validateName(name) {
  if (!name) return 'Nama tidak boleh kosong';
  if (name !== name.trim()) return 'Nama tidak boleh diawali atau diakhiri spasi';
  if (name === '.' || name === '..') return 'Nama tidak valid';
  if (/[\/\\"\u0000-\u001f]/.test(name)) return 'Nama tidak boleh berisi / \\ atau tanda kutip';
  if (new TextEncoder().encode(name).length > MAX_NAME_BYTES) return 'Nama terlalu panjang';
  return null;
}

function cssEscape(value) {
  return (window.CSS && CSS.escape) ? CSS.escape(value) : value.replace(/"/g, '\\"');
}

function streamUrl(path, download = false) {
  return `${API}/stream?path=${encodeURIComponent(path)}${download ? '&download=1' : ''}`;
}

/* ==========================================================================
   Toast
   ========================================================================== */
function showToast(message, type = 'info') {
  const colors = {
    success: 'bg-emerald-950/90 border-emerald-500/50 text-emerald-200',
    error: 'bg-red-950/90 border-red-500/50 text-red-200',
    info: 'bg-slate-900/90 border-blue-500/50 text-blue-200',
  };
  const toast = el('div', {
    class: `pointer-events-auto p-3 rounded-xl border backdrop-blur-md text-xs shadow-xl transition-all duration-300 translate-y-2 opacity-0 ${colors[type] || colors.info}`,
    text: message,
    attrs: { role: 'status' },
  });
  $('toastContainer').append(toast);
  requestAnimationFrame(() => requestAnimationFrame(() => {
    toast.classList.remove('translate-y-2', 'opacity-0');
  }));
  setTimeout(() => {
    toast.classList.add('opacity-0', 'translate-y-2');
    setTimeout(() => toast.remove(), 300);
  }, type === 'error' ? 4500 : 3000);
}

/* ==========================================================================
   Overlay helper (animasi masuk & keluar)
   ========================================================================== */
function openOverlay(id) {
  const overlay = $(id);
  overlay.classList.remove('hidden');
  requestAnimationFrame(() => requestAnimationFrame(() => overlay.classList.add('is-open')));
}

function closeOverlay(id) {
  const overlay = $(id);
  overlay.classList.remove('is-open');
  setTimeout(() => {
    if (!overlay.classList.contains('is-open')) overlay.classList.add('hidden');
  }, 200);
}

/* ==========================================================================
   Dialog kustom (pengganti confirm() dan prompt() bawaan browser)
   ========================================================================== */
function openDialog({ title, message = '', input = null, validate = null, okText = 'OK', danger = false }) {
  return new Promise((resolve) => {
    dialog.resolve = resolve;
    dialog.validate = validate;
    dialog.hasInput = !!input;

    $('dialogTitle').textContent = title;
    $('dialogMessage').textContent = message;
    $('dialogMessage').classList.toggle('hidden', !message);

    const inputEl = $('dialogInput');
    inputEl.classList.toggle('hidden', !input);
    inputEl.value = input ? (input.value || '') : '';
    inputEl.placeholder = input && input.placeholder ? input.placeholder : '';
    $('dialogError').textContent = '';

    const okBtn = $('dialogOkBtn');
    okBtn.textContent = okText;
    okBtn.className = danger ? 'btn-danger' : 'btn-primary';

    openOverlay('dialogOverlay');
    setTimeout(() => {
      if (input) { inputEl.focus(); inputEl.select(); }
      else okBtn.focus();
    }, 60);
  });
}

function closeDialog(result) {
  const resolve = dialog.resolve;
  dialog.resolve = null;
  closeOverlay('dialogOverlay');
  if (resolve) resolve(result);
}

function dialogSubmit() {
  if (dialog.hasInput) {
    const value = $('dialogInput').value.trim();
    const error = dialog.validate ? dialog.validate(value) : null;
    if (error) {
      $('dialogError').textContent = error;
      return;
    }
    closeDialog(value);
  } else {
    closeDialog(true);
  }
}

function onDialogCancel() {
  closeDialog(dialog.hasInput ? null : false);
}

function askConfirm(options) {
  return openDialog(options).then((result) => result === true);
}

function askText(options) {
  return openDialog({ ...options, input: options.input || { value: '' } })
    .then((result) => (typeof result === 'string' ? result : null));
}

/* ==========================================================================
   Tombol sibuk (loading) & aksi yang diberi feedback
   ========================================================================== */
function setBusy(btn, busy) {
  if (!btn) return;
  btn.disabled = busy;
  btn.classList.toggle('opacity-70', busy);
  btn.classList.toggle('cursor-wait', busy);
  const spinner = btn.querySelector('.btn-spinner');
  if (busy && !spinner) {
    btn.prepend(el('span', { class: 'btn-spinner', attrs: { 'aria-hidden': 'true' } }));
  } else if (!busy && spinner) {
    spinner.remove();
  }
}

async function run(btn, action, successMessage) {
  setBusy(btn, true);
  try {
    await action();
    if (successMessage) showToast(successMessage, 'success');
    return true;
  } catch (err) {
    showToast(err.message || 'Terjadi kesalahan', 'error');
    return false;
  } finally {
    setBusy(btn, false);
  }
}

/* ==========================================================================
   API
   ========================================================================== */
async function readJson(res) {
  let data = null;
  try { data = await res.json(); } catch { data = null; }
  if (!res.ok) throw new ApiError((data && data.error) || `Permintaan gagal (${res.status})`);
  return data || {};
}

async function api(endpoint, body) {
  let res;
  try {
    res = await fetch(API + endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
  } catch {
    throw new ApiError('Tidak bisa terhubung ke server lokal');
  }
  return readJson(res);
}

function uploadBlob(remoteDir, name, body, onProgress) {
  return new Promise((resolve, reject) => {
    const url = `${API}/upload?remote=${encodeURIComponent(remoteDir)}&name=${encodeURIComponent(name)}`;
    const xhr = new XMLHttpRequest();
    xhr.open('POST', url, true);
    xhr.setRequestHeader('Content-Type', 'application/octet-stream');
    xhr.upload.onprogress = (e) => {
      if (onProgress && e.lengthComputable && e.total > 0) onProgress(e.loaded / e.total);
    };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) return resolve();
      let message = `Gagal (${xhr.status})`;
      try { message = JSON.parse(xhr.responseText).error || message; } catch { /* abaikan */ }
      reject(new ApiError(message));
    };
    xhr.onerror = () => reject(new ApiError('Koneksi terputus saat mengunggah'));
    xhr.send(body);
  });
}

/* ==========================================================================
   Penyimpanan (kuota)
   ========================================================================== */
function applyQuota(data, fromCache) {
  const usedBytes = Number(data.used) || 0;
  const totalBytes = data.total === null || data.total === undefined ? null : Number(data.total);
  const bar = $('quotaBar');
  const usedEl = $('quotaUsed');
  usedEl.textContent = formatBytes(usedBytes);
  usedEl.classList.remove('text-slate-500', 'font-medium', 'text-sm');
  if (totalBytes) {
    const freeBytes = data.free === null || data.free === undefined
      ? Math.max(0, totalBytes - usedBytes)
      : Number(data.free);
    const pct = Math.min(100, (usedBytes / totalBytes) * 100);
    $('quotaTotal').textContent = formatBytes(totalBytes);
    $('quotaFree').textContent = `Sisa ${formatBytes(freeBytes)}${fromCache ? ' (tersimpan)' : ''}`;
    bar.style.width = `${pct.toFixed(1)}%`;
    bar.dataset.level = pct >= 95 ? 'danger' : pct >= 85 ? 'warn' : 'ok';
  } else {
    $('quotaTotal').textContent = 'tidak terbatas';
    $('quotaFree').textContent = fromCache ? '(tersimpan)' : '';
    bar.style.width = '0%';
  }
}

async function loadStorage() {
  const cached = await fetchCached('/cache/about');
  if (cached) applyQuota(cached, true);
  try {
    const data = await api('/operations/about', { fs: REMOTE });
    applyQuota(data, false);
  } catch {
    if (!cached) {
      $('quotaUsed').textContent = '--';
      $('quotaTotal').textContent = '--';
    }
    $('quotaFree').textContent = cached
      ? 'Gagal memperbarui (menampilkan data tersimpan)'
      : 'Gagal membaca penyimpanan';
  }
}

/* ==========================================================================
   Favorit (disimpan per path, termasuk nama dan jenis)
   ========================================================================== */
function loadFavorites() {
  try {
    const raw = JSON.parse(localStorage.getItem(FAV_KEY) || '[]');
    if (!Array.isArray(raw)) return [];
    return raw.filter((f) => f && typeof f.Path === 'string' && typeof f.Name === 'string');
  } catch {
    return [];
  }
}

function saveFavorites() {
  try {
    localStorage.setItem(FAV_KEY, JSON.stringify(state.favorites));
  } catch {
    showToast('Gagal menyimpan favorit (penyimpanan browser diblokir)', 'error');
  }
}

function isFavorite(path) {
  return state.favorites.some((f) => f.Path === path);
}

function toggleFavorite(item) {
  if (isFavorite(item.Path)) {
    state.favorites = state.favorites.filter((f) => f.Path !== item.Path);
    showToast('Dihapus dari favorit', 'info');
  } else {
    state.favorites.push({ Path: item.Path, Name: item.Name, IsDir: !!item.IsDir, Size: item.Size || 0 });
    showToast('Ditambahkan ke favorit', 'success');
  }
  saveFavorites();
  if (state.category === 'favorites') renderList();
  else replaceCard(item);
}

function favoritesAfterRemove(path) {
  state.favorites = state.favorites.filter((f) => f.Path !== path && !f.Path.startsWith(path + '/'));
  saveFavorites();
}

function favoritesAfterMove(oldPath, newPath) {
  state.favorites = state.favorites.map((f) => {
    if (f.Path === oldPath) return { ...f, Path: newPath, Name: baseName(newPath) };
    if (f.Path.startsWith(oldPath + '/')) return { ...f, Path: newPath + f.Path.slice(oldPath.length) };
    return f;
  });
  saveFavorites();
}

/* ==========================================================================
   Navigasi, breadcrumb, dan kategori
   ========================================================================== */
function styleNav() {
  const active = 'w-full flex items-center gap-3 px-3 py-2.5 rounded-xl text-xs font-semibold bg-blue-600/20 text-blue-400 border border-blue-500/30 transition';
  const idle = 'w-full flex items-center gap-3 px-3 py-2.5 rounded-xl text-xs font-medium text-slate-400 hover:bg-slate-800/60 hover:text-slate-200 transition border border-transparent';
  for (const [id, cat] of [['nav-all', 'all'], ['nav-fav', 'favorites']]) {
    const btn = $(id);
    const isActive = state.category === cat;
    btn.className = isActive ? active : idle;
    btn.setAttribute('aria-current', isActive ? 'page' : 'false');
  }
}

function setCategory(cat, path = state.currentPath) {
  state.category = cat;
  state.selected.clear();
  updateBatchToolbar();
  styleNav();
  $('opsPanel').classList.toggle('hidden', cat === 'favorites');
  if (cat === 'favorites') {
    state.listRequestId++;          // batalkan pemuatan folder yang masih berjalan
    setSyncStatus('hidden');
    renderBreadcrumb();
    renderList();
    updateNavButtons();
  } else {
    loadFiles(path);
  }
}

function navigate(path) {
  if (path !== state.currentPath) {
    state.history.push(state.currentPath);
    if (state.history.length > 50) state.history.shift();
  }
  setCategory('all', path);
}

function openFolder(path) {
  navigate(path);
}

function goBack() {
  if (state.history.length === 0) return;
  setCategory('all', state.history.pop());
}

function goUp() {
  if (state.category === 'favorites' || !state.currentPath) return;
  navigate(parentOf(state.currentPath));
}

function updateNavButtons() {
  $('btnBack').disabled = state.history.length === 0;
  $('btnUp').disabled = state.category === 'favorites' || !state.currentPath;
}

function renderBreadcrumb() {
  const wrap = $('breadcrumb');
  wrap.replaceChildren();

  if (state.category === 'favorites') {
    wrap.append(el('span', { class: 'font-semibold text-blue-400', text: '⭐ Favorit' }));
    return;
  }

  const crumbs = [{ label: 'Root', path: '' }];
  let acc = '';
  for (const part of state.currentPath.split('/').filter(Boolean)) {
    acc = joinPath(acc, part);
    crumbs.push({ label: part, path: acc });
  }
  crumbs.forEach((crumb, i) => {
    if (i > 0) wrap.append(el('span', { class: 'text-slate-600', text: '/' }));
    const isLast = i === crumbs.length - 1;
    wrap.append(el('button', {
      type: 'button',
      class: isLast
        ? 'font-semibold text-blue-400 truncate max-w-[10rem] px-1'
        : 'text-slate-400 hover:text-slate-200 truncate max-w-[10rem] px-1 rounded',
      text: crumb.label,
      title: crumb.label,
      dataset: { nav: crumb.path },
    }));
  });
}

/* ==========================================================================
   Memuat daftar file
   ========================================================================== */
async function fetchCached(endpoint) {
  try {
    const res = await fetch(API + endpoint, { cache: 'no-store' });
    if (!res.ok) return null;
    return await res.json();
  } catch {
    return null;
  }
}

let syncHideTimer = null;
function setSyncStatus(mode) {
  const node = $('syncStatus');
  clearTimeout(syncHideTimer);
  if (mode === 'loading') {
    node.textContent = activeStorageMode === 'drive' ? '⟳ Memperbarui dari Google Drive...' : '⟳ Memperbarui dari penyimpanan HP...';
    node.classList.remove('hidden');
  } else if (mode === 'done') {
    node.textContent = '✓ Sudah terbaru';
    node.classList.remove('hidden');
    syncHideTimer = setTimeout(() => node.classList.add('hidden'), 1500);
  } else {
    node.classList.add('hidden');
  }
}

async function loadFiles(path = state.currentPath) {
  const requestId = ++state.listRequestId;
  const sameFolder = path === state.currentPath && state.loaded;
  state.currentPath = path;
  state.animateNext = !sameFolder;
  if (!sameFolder) {
    state.selected.clear();
    updateBatchToolbar();
  }
  renderBreadcrumb();
  updateNavButtons();

  let fromCache = false;
  if (!sameFolder) {
    const cached = await fetchCached(`/cache/list?remote=${encodeURIComponent(path)}`);
    if (requestId !== state.listRequestId) return;
    if (cached && Array.isArray(cached.list)) {
      state.rawItems = cached.list;
      state.loaded = true;
      fromCache = true;
      renderList();
    } else {
      state.rawItems = [];
      state.loaded = false;
      showListNodes([1, 2, 3].map(() => el('div', { class: 'h-14 skeleton rounded-2xl' })));
    }
  }

  setSyncStatus('loading');
  try {
    const data = await api('/operations/list', { fs: REMOTE, remote: path });
    if (requestId !== state.listRequestId) return;
    const list = Array.isArray(data.list) ? data.list : [];
    if (!state.loaded || JSON.stringify(list) !== JSON.stringify(state.rawItems)) {
      state.rawItems = list;
      state.loaded = true;
      renderList();
    }
    setSyncStatus('done');
  } catch (err) {
    if (requestId !== state.listRequestId) return;
    setSyncStatus('hidden');
    if (state.loaded) {
      showToast(`Gagal memperbarui dari Drive, menampilkan data tersimpan (${err.message})`, 'error');
    } else {
      state.rawItems = [];
      showListNodes([el('div', { class: 'text-center py-10 space-y-3' }, [
        el('p', { class: 'text-red-400 text-xs', text: `Gagal memuat folder: ${err.message}` }),
        el('button', { type: 'button', class: 'btn-ghost', text: 'Coba lagi', onclick: () => loadFiles(path) }),
      ])]);
      showToast('Gagal memuat daftar file', 'error');
    }
  }
}

function refresh() {
  if (state.category === 'favorites') renderList();
  else loadFiles(state.currentPath);
}

function afterChange() {
  refresh();
  loadStorage();
}

/* ==========================================================================
   Filter, urutan, dan render daftar
   ========================================================================== */
function sortItems(items, mode) {
  const collator = new Intl.Collator('id', { numeric: true, sensitivity: 'base' });
  return items.sort((a, b) => {
    if (!!a.IsDir !== !!b.IsDir) return a.IsDir ? -1 : 1;
    if (a.IsDir && b.IsDir) return collator.compare(a.Name, b.Name);
    switch (mode) {
      case 'name-desc': return collator.compare(b.Name, a.Name);
      case 'size-desc': return (b.Size || 0) - (a.Size || 0) || collator.compare(a.Name, b.Name);
      case 'size-asc': return (a.Size || 0) - (b.Size || 0) || collator.compare(a.Name, b.Name);
      default: return collator.compare(a.Name, b.Name);
    }
  });
}

function getVisibleItems() {
  const query = $('searchInput').value.trim().toLowerCase();
  const sortMode = $('sortSelect').value;
  const source = state.category === 'favorites'
    ? state.favorites.map((f) => ({ Name: f.Name, Path: f.Path, IsDir: f.IsDir, Size: f.Size }))
    : state.rawItems;
  const filtered = source.filter((item) => item.Name.toLowerCase().includes(query));
  return sortItems(filtered, sortMode);
}

function showListNodes(nodes) {
  const container = $('fileContainer');
  container.className = 'space-y-2';
  container.replaceChildren(...nodes);
}

function emptyState() {
  const favView = state.category === 'favorites';
  const searching = $('searchInput').value.trim() !== '';
  const message = searching
    ? 'Tidak ada hasil pencarian'
    : favView
      ? 'Belum ada favorit. Klik ☆ pada file atau folder untuk menambahkannya.'
      : 'Folder ini kosong';
  const icon = favView ? '⭐' : searching ? '🔍' : '📂';
  return el('div', { class: 'col-span-full text-center py-12 text-xs text-slate-500' }, [
    el('div', { class: 'text-4xl mb-2', text: icon }),
    el('p', { text: message }),
  ]);
}

function loadViewPref() {
  try { return localStorage.getItem('cloud_view') !== 'list'; } catch { return true; }
}

function saveViewPref() {
  try { localStorage.setItem('cloud_view', state.isGridView ? 'grid' : 'list'); } catch { /* abaikan */ }
}

function updateViewButton() {
  $('viewToggleBtn').textContent = state.isGridView ? '📋 List' : '🎛️ Grid';
}

function renderList() {
  const container = $('fileContainer');
  const items = getVisibleItems();
  state.items = new Map(items.map((item) => [item.Path, item]));
  state.visibleItems = items;
  state.renderedCount = 0;
  if (state.observer) { state.observer.disconnect(); state.observer = null; }

  // selection hanya berisi item yang sedang terlihat
  for (const path of [...state.selected]) {
    if (!state.items.has(path)) state.selected.delete(path);
  }

  container.className = state.isGridView
    ? 'grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 gap-3'
    : 'space-y-2';
  container.replaceChildren();

  if (items.length === 0) {
    container.append(emptyState());
  } else {
    appendChunk();
  }

  if (state.animateNext) {
    state.animateNext = false;
    container.classList.remove('fade-in');
    void container.offsetWidth;   // mulai ulang animasi
    container.classList.add('fade-in');
  }
  updateBatchToolbar();
}

/** Menambahkan RENDER_CHUNK kartu berikutnya; sentinel di akhir memicu muatan berikutnya saat di-scroll. */
function appendChunk() {
  const container = $('fileContainer');
  const old = $('listSentinel');
  if (old) old.remove();

  const end = Math.min(state.visibleItems.length, state.renderedCount + RENDER_CHUNK);
  const fragment = document.createDocumentFragment();
  for (let i = state.renderedCount; i < end; i++) fragment.append(buildCard(state.visibleItems[i]));
  state.renderedCount = end;
  container.append(fragment);

  if (end < state.visibleItems.length) {
    const sentinel = el('div', { id: 'listSentinel', class: 'h-px' });
    container.append(sentinel);
    if (state.observer) state.observer.disconnect();
    if ('IntersectionObserver' in window) {
      state.observer = new IntersectionObserver((entries) => {
        if (entries.some((entry) => entry.isIntersecting)) appendChunk();
      }, { root: null, rootMargin: '600px 0px' });
      state.observer.observe(sentinel);
    } else {
      appendChunk();   // browser lama: muat semua
    }
  }
}

function buildCard(item) {
  const isFolder = !!item.IsDir;
  const selected = state.selected.has(item.Path);
  const fav = isFavorite(item.Path);
  const grid = state.isGridView;

  const meta = isFolder ? 'Folder' : formatBytes(item.Size);
  const location = state.category === 'favorites' ? `Lokasi: /${parentOf(item.Path)}` : '';
  const metaText = location ? `${meta} · ${location}` : meta;

  const check = el('input', {
    type: 'checkbox',
    checked: selected,
    class: 'w-4 h-4 rounded border-slate-700 accent-blue-600 cursor-pointer shrink-0',
    attrs: { 'data-action': 'select', 'aria-label': `Pilih ${item.Name}` },
  });

  const star = el('button', {
    type: 'button',
    class: `icon-btn text-sm ${fav ? 'fav-on' : 'text-slate-400'}`,
    text: fav ? '★' : '☆',
    title: fav ? 'Hapus dari favorit' : 'Tambah ke favorit',
    attrs: { 'data-action': 'fav', 'aria-label': fav ? 'Hapus dari favorit' : 'Tambah ke favorit', 'aria-pressed': String(fav) },
  });

  const open = el('div', {
    class: `open-area flex-1 min-w-0 ${grid ? 'flex-col text-center' : ''}`,
    attrs: { 'data-action': 'open', role: 'button', tabindex: '0', title: item.Name },
  }, [
    el('span', { class: 'card-icon', text: iconFor(item) }),
    el('div', { class: 'min-w-0 w-full' }, [
      el('p', { class: 'card-name', text: item.Name }),
      el('p', { class: 'card-meta', text: metaText }),
    ]),
  ]);

  const actionBtn = (action, icon, label, danger = false) => el('button', {
    type: 'button',
    class: `icon-btn ${danger ? 'text-red-400' : 'text-amber-300'}`,
    text: icon,
    title: label,
    attrs: { 'data-action': action, 'aria-label': `${label} ${item.Name}` },
  });

  const actions = [];
  if (!isFolder) actions.push(actionBtn('download', '⬇️', 'Unduh'));
  actions.push(actionBtn('move', '📁', 'Pindahkan'));
  actions.push(actionBtn('rename', '✏️', 'Ubah nama'));
  actions.push(actionBtn('share', '🔗', 'Bagikan'));
  actions.push(actionBtn('delete', '🗑️', 'Hapus', true));

  const cardClass = grid
    ? `item-card item-grid flex flex-col gap-2 p-3.5 rounded-2xl border ${selected ? 'is-selected' : 'border-slate-800/80'}`
    : `item-card flex items-center gap-3 p-3 rounded-2xl border ${selected ? 'is-selected' : 'border-slate-800/80'}`;

  if (grid) {
    return el('div', { class: cardClass, dataset: { path: item.Path } }, [
      el('div', { class: 'flex justify-between items-center' }, [check, star]),
      open,
      el('div', { class: 'flex justify-center gap-1 pt-2 border-t border-slate-800/60' }, actions),
    ]);
  }

  return el('div', { class: cardClass, dataset: { path: item.Path } }, [
    check,
    star,
    open,
    el('div', { class: 'flex items-center gap-1 shrink-0' }, actions),
  ]);
}

/** Mengganti satu kartu saja, tanpa render ulang seluruh daftar. */
function replaceCard(item) {
  const old = $('fileContainer').querySelector(`[data-path="${cssEscape(item.Path)}"]`);
  if (old) old.replaceWith(buildCard(item));
}

/* ==========================================================================
   Pilihan banyak
   ========================================================================== */
function toggleSelect(item, checked) {
  if (checked) state.selected.add(item.Path);
  else state.selected.delete(item.Path);
  updateBatchToolbar();
  replaceCard(item);
}

function updateBatchToolbar() {
  const count = state.selected.size;
  const total = state.visibleItems.length;
  const visibleSelected = state.visibleItems.filter((i) => state.selected.has(i.Path)).length;

  $('batchToolbar').classList.toggle('hidden', count === 0);
  $('batchCount').textContent = `${count} item dipilih`;

  const cb = $('selectAllCb');
  cb.disabled = total === 0;
  cb.checked = total > 0 && visibleSelected === total;
  cb.indeterminate = visibleSelected > 0 && visibleSelected < total;
  $('selectAllLabel').textContent = cb.checked ? 'Batal pilih semua' : 'Pilih semua';
}

function refreshRenderedCards() {
  $('fileContainer').querySelectorAll('[data-path]').forEach((card) => {
    const item = state.items.get(card.dataset.path);
    if (item) card.replaceWith(buildCard(item));
  });
}

function selectAllVisible() {
  state.visibleItems.forEach((item) => state.selected.add(item.Path));
  updateBatchToolbar();
  refreshRenderedCards();
}

function clearSelection() {
  state.selected.clear();
  updateBatchToolbar();
  refreshRenderedCards();
}


async function deleteSelected() {
  const items = selectedItems();
  if (items.length === 0) return;

  const ok = await askConfirm({
    title: `Hapus ${items.length} item?`,
    message: `Item terpilih akan dipindah ke tempat sampah dan bisa dipulihkan selama ${TRASH_DAYS} hari.`,
    okText: 'Hapus',
    danger: true,
  });
  if (!ok) return;

  const btn = $('deleteSelBtn');
  setBusy(btn, true);
  let done = 0;
  let failed = 0;
  for (let i = 0; i < items.length; i++) {
    const item = items[i];
    $('batchCount').textContent = `Memindahkan ${i + 1}/${items.length}...`;
    try {
      await api('/trash/move', { remote: item.Path });
      favoritesAfterRemove(item.Path);
      state.selected.delete(item.Path);
      done++;
    } catch {
      failed++;
    }
  }
  setBusy(btn, false);
  showToast(
    failed ? `${done} dipindah ke tempat sampah, ${failed} gagal` : `${done} item dipindah ke tempat sampah`,
    failed ? 'error' : 'success'
  );
  afterChange();
}

/* ==========================================================================
   Aksi per item
   ========================================================================== */
function handleCardAction(target) {
  const card = target.closest('[data-path]');
  if (!card) return;
  const item = state.items.get(card.dataset.path);
  if (!item) return;

  switch (target.dataset.action) {
    case 'open':
      return item.IsDir ? openFolder(item.Path) : previewFile(item);
    case 'fav':
      return toggleFavorite(item);
    case 'download':
      return downloadFile(item);
    case 'move':
      return openMoveDialog([item]);
    case 'share':
      return shareItem(item);
    case 'rename':
      return renameItem(item);
    case 'delete':
      return deleteItem(item);
    default:
      return undefined;
  }
}

function downloadFile(item) {
  const link = el('a', { href: streamUrl(item.Path, true), attrs: { download: item.Name } });
  document.body.append(link);
  link.click();
  link.remove();
}

async function createFolder() {
  const input = $('newFolder');
  const name = input.value.trim();
  const error = validateName(name);
  if (error) return showToast(error, 'error');
  if (state.rawItems.some((i) => i.Name === name)) {
    return showToast('Sudah ada item dengan nama itu di folder ini', 'error');
  }

  const ok = await run($('createFolderBtn'),
    () => api('/operations/mkdir', { fs: REMOTE, remote: joinPath(state.currentPath, name) }),
    'Folder berhasil dibuat');
  if (ok) {
    input.value = '';
    afterChange();
  }
}

async function renameItem(item) {
  const newName = await askText({
    title: item.IsDir ? 'Ubah nama folder' : 'Ubah nama file',
    input: { value: item.Name },
    okText: 'Simpan',
    validate: (value) => {
      const error = validateName(value);
      if (error) return error;
      if (value !== item.Name && state.rawItems.some((i) => i.Name === value)) {
        return 'Sudah ada item dengan nama itu';
      }
      return null;
    },
  });
  if (newName === null || newName === item.Name) return;

  const newPath = joinPath(parentOf(item.Path), newName);

  let target;
  try {
    target = await api('/operations/stat', { fs: REMOTE, remote: newPath });
  } catch (err) {
    return showToast(`Gagal memeriksa nama tujuan: ${err.message}`, 'error');
  }
  if (target.item) {
    const ok = await askConfirm({
      title: 'Timpa item yang ada?',
      message: `"${newName}" sudah ada di lokasi ini dan akan diganti.`,
      okText: 'Timpa',
      danger: true,
    });
    if (!ok) return;
  }

  const done = await run(null, () => api('/operations/moveto', {
    srcFs: REMOTE, srcRemote: item.Path, dstFs: REMOTE, dstRemote: newPath,
  }), 'Nama berhasil diubah');
  if (done) {
    favoritesAfterMove(item.Path, newPath);
    afterChange();
  }
}


async function deleteItem(item) {
  const ok = await askConfirm({
    title: item.IsDir ? 'Hapus folder?' : 'Hapus file?',
    message: `"${item.Name}" akan dipindah ke tempat sampah dan bisa dipulihkan selama ${TRASH_DAYS} hari.`,
    okText: 'Hapus',
    danger: true,
  });
  if (!ok) return;

  const done = await run(null, async () => {
    await api('/trash/move', { remote: item.Path });
    favoritesAfterRemove(item.Path);
    state.selected.delete(item.Path);
  }, 'Dipindah ke tempat sampah');
  if (done) afterChange();
  else refresh();
}

/* ==========================================================================
   Upload
   ========================================================================== */
function updateFileHint() {
  const n = $('fileInput').files.length + $('folderInput').files.length;
  $('fileHint').textContent = n ? `${n} file dipilih` : 'Pilih file/folder atau seret file ke halaman';
}

function setUploadStatus(label, ratio) {
  const pct = Math.round(Math.max(0, Math.min(1, ratio)) * 100);
  $('uploadLabel').textContent = label;
  $('uploadPct').textContent = `${pct}%`;
  $('uploadBar').style.width = `${pct}%`;
}


async function uploadSelectedFiles() {
  const input = $('fileInput');
  const folderInput = $('folderInput');
  const files = [...Array.from(input.files || []), ...Array.from(folderInput.files || [])];
  if (files.length === 0) return showToast('Pilih file atau folder dulu', 'error');
  const attempted = await uploadFiles(files, state.currentPath);
  if (attempted) { input.value = ''; folderInput.value = ''; updateFileHint(); }
}

/** Mengunggah daftar file ke folder aktif. Mengembalikan false jika dibatalkan atau tidak valid. */
async function uploadFiles(files, dir) {
  for (const file of files) {
    const rel = file.webkitRelativePath || file.name;
    const parts = rel.split('/').filter(Boolean);
    if (!parts.length || parts.some((part) => validateName(part))) {
      showToast(`"${rel}": nama file/folder tidak valid`, 'error');
      return false;
    }
  }

  const existing = new Set(state.rawItems.filter((i) => !i.IsDir).map((i) => i.Name));
  const conflicts = files.filter((f) => existing.has(f.name));
  if (conflicts.length) {
    const names = conflicts.slice(0, 3).map((f) => f.name).join(', ')
      + (conflicts.length > 3 ? ` dan ${conflicts.length - 3} lainnya` : '');
    const ok = await askConfirm({
      title: 'Timpa file yang sudah ada?',
      message: `${names} sudah ada di folder ini dan akan diganti.`,
      okText: 'Timpa',
      danger: true,
    });
    if (!ok) return false;
  }

  const btn = $('uploadBtn');
  setBusy(btn, true);
  $('uploadProgress').classList.remove('hidden');
  const failed = [];
  let succeeded = 0;

  for (let i = 0; i < files.length; i++) {
    const file = files[i];
    try {
      const rel = file.webkitRelativePath || file.name;
      const parts = rel.split('/').filter(Boolean);
      // Untuk folder yang dipilih, buat semua subfolder tujuan sebelum mengunggah file.
      let targetDir = dir;
      if (parts.length > 1) {
        for (const folderName of parts.slice(0, -1)) {
          targetDir = joinPath(targetDir, folderName);
          const stat = await api('/operations/stat', { remote: targetDir });
          if (!stat.item) await api('/operations/mkdir', { remote: targetDir });
        }
      }
      await uploadBlob(targetDir, parts[parts.length - 1], file, (ratio) =>
        setUploadStatus(`(${i + 1}/${files.length}) ${rel}`, (i + ratio) / files.length));
      succeeded++;
    } catch (err) {
      failed.push(`${file.name} (${err.message})`);
    }
  }

  setUploadStatus('Selesai', 1);
  setBusy(btn, false);
  setTimeout(() => $('uploadProgress').classList.add('hidden'), 1500);

  if (failed.length) {
    showToast(`${succeeded} berhasil, ${failed.length} gagal: ${failed[0]}${failed.length > 1 ? ' …' : ''}`, 'error');
  } else {
    showToast(`${succeeded} file berhasil diunggah`, 'success');
  }
  afterChange();
  return true;
}

/* ==========================================================================
   Preview (gambar, video, audio, PDF, teks, dan lainnya)
   ========================================================================== */
function previewFile(item) {
  const files = getVisibleItems().filter((i) => !i.IsDir);
  const index = files.findIndex((f) => f.Path === item.Path);
  if (index < 0) return;

  state.previewList = files;
  state.previewIndex = index;
  state.previewDirty = false;
  if ($('previewOverlay').classList.contains('hidden')) openOverlay('previewOverlay');
  renderPreview();
}

async function confirmDiscard() {
  if (!state.previewDirty) return true;
  const ok = await askConfirm({
    title: 'Perubahan belum disimpan',
    message: 'Ada perubahan yang belum disimpan. Buang perubahan ini?',
    okText: 'Buang',
    danger: true,
  });
  if (ok) state.previewDirty = false;
  return ok;
}

async function gotoPreview(index) {
  if (index < 0 || index >= state.previewList.length) return;
  if (!(await confirmDiscard())) return;
  state.previewIndex = index;
  renderPreview();
}

async function closePreview() {
  if (!(await confirmDiscard())) return;
  state.previewToken++;
  const content = $('previewContent');
  content.querySelectorAll('video, audio').forEach((m) => m.pause());
  content.replaceChildren();
  closeOverlay('previewOverlay');
}

function setPreviewLoading(on) {
  $('previewLoader').classList.toggle('hidden', !on);
}

function renderPreview() {
  const item = state.previewList[state.previewIndex];
  if (!item) return;

  const token = ++state.previewToken;
  const kind = fileKind(item.Name);
  const total = state.previewList.length;
  const content = $('previewContent');

  $('previewTitle').textContent = item.Name;
  $('previewMeta').textContent = `${KIND_LABEL[kind]} · ${formatBytes(item.Size)}`;
  $('previewCounter').textContent = `${state.previewIndex + 1} / ${total}`;
  $('prevBtn').disabled = state.previewIndex <= 0;
  $('nextBtn').disabled = state.previewIndex >= total - 1;
  $('downloadBtn').href = streamUrl(item.Path, true);
  $('downloadBtn').setAttribute('download', item.Name);
  $('openTabBtn').href = streamUrl(item.Path, false);
  $('openTabBtn').classList.toggle('hidden', kind === 'image' || kind === 'video' || kind === 'audio');
  $('saveBtn').classList.add('hidden');

  content.replaceChildren();
  setPreviewLoading(true);

  const done = () => { if (token === state.previewToken) setPreviewLoading(false); };
  const fail = (message) => {
    if (token !== state.previewToken) return;
    setPreviewLoading(false);
    content.replaceChildren(infoBlock(item, kind, message, true));
  };

  switch (kind) {
    case 'image': {
      const img = el('img', {
        class: 'max-w-full max-h-[70vh] object-contain rounded-xl select-none',
        alt: item.Name,
        src: streamUrl(item.Path),
      });
      img.addEventListener('load', done, { once: true });
      img.addEventListener('error', () => fail('Gambar tidak bisa ditampilkan.'), { once: true });
      content.append(img);
      break;
    }

    case 'video': {
      const video = el('video', {
        controls: true,
        preload: 'metadata',
        class: 'max-w-full max-h-[70vh] rounded-xl bg-black',
        src: streamUrl(item.Path),
      });
      video.addEventListener('loadedmetadata', done, { once: true });
      video.addEventListener('error', () => fail(
        `Video ini tidak bisa diputar di browser ini (format .${item.Name.split('.').pop()} mungkin tidak didukung). Silakan unduh lalu putar dengan pemutar lain.`
      ), { once: true });
      content.append(video);
      break;
    }

    case 'audio': {
      const audio = el('audio', { controls: true, preload: 'metadata', class: 'w-full max-w-md', src: streamUrl(item.Path) });
      audio.addEventListener('loadedmetadata', done, { once: true });
      audio.addEventListener('error', () => fail('Audio ini tidak bisa diputar di browser ini.'), { once: true });
      content.append(el('div', { class: 'w-full flex flex-col items-center gap-5 py-6' }, [
        el('div', { class: 'text-6xl', text: '🎵' }),
        el('p', { class: 'text-sm font-semibold text-slate-200 break-all text-center', text: item.Name }),
        audio,
      ]));
      break;
    }

    case 'pdf': {
      const frame = el('iframe', {
        class: 'w-full h-[70vh] rounded-xl bg-white border-0',
        src: streamUrl(item.Path),
        title: item.Name,
      });
      frame.addEventListener('load', done, { once: true });
      content.append(frame);
      break;
    }

    case 'text':
      renderTextPreview(item, token, content, done, fail);
      break;

    default:
      content.append(infoBlock(item, kind));
      done();
  }
}

async function renderTextPreview(item, token, content, done, fail) {
  if ((item.Size || 0) > TEXT_EDIT_LIMIT) {
    content.append(infoBlock(item, 'text',
      'File terlalu besar untuk ditampilkan dan diedit di browser (lebih dari 2 MB). Silakan unduh.'));
    return done();
  }
  try {
    const res = await fetch(streamUrl(item.Path), { cache: 'no-store' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const text = await res.text();
    if (token !== state.previewToken) return;

    const textarea = el('textarea', {
      class: 'w-full h-[60vh] bg-slate-900 text-slate-200 border border-slate-800 rounded-xl p-3 font-mono text-xs focus:outline-none focus:border-blue-500',
      spellcheck: false,
      value: text,
    });
    content.append(textarea);
    $('saveBtn').classList.remove('hidden');
    done();
  } catch (err) {
    fail(`Gagal memuat isi file: ${err.message}`);
  }
}

function infoBlock(item, kind, note, isError = false) {
  const defaultNote = 'Preview tidak tersedia untuk jenis file ini. Gunakan tombol Unduh atau Buka tab baru.';
  return el('div', { class: 'w-full text-center py-8 px-4 space-y-3' }, [
    el('div', { class: 'text-6xl', text: isError ? '⚠️' : KIND_ICON[kind] }),
    el('p', { class: 'text-sm font-semibold text-slate-200 break-all', text: item.Name }),
    el('p', { class: 'text-xs text-slate-400', text: `${KIND_LABEL[kind]} · ${formatBytes(item.Size)}` }),
    el('p', {
      class: `text-xs max-w-sm mx-auto ${isError ? 'text-red-400' : 'text-slate-500'}`,
      text: note || defaultNote,
    }),
  ]);
}

async function saveCurrentText(btn) {
  const item = state.previewList[state.previewIndex];
  const textarea = $('previewContent').querySelector('textarea');
  if (!item || !textarea) return;

  const blob = new Blob([textarea.value], { type: 'text/plain;charset=utf-8' });
  const done = await run(btn, () => uploadBlob(parentOf(item.Path), item.Name, blob),
    'File berhasil disimpan');
  if (done) {
    item.Size = blob.size;
    state.previewDirty = false;
    $('previewMeta').textContent = `${KIND_LABEL[fileKind(item.Name)]} · ${formatBytes(item.Size)}`;
    afterChange();
  }
}

/* ==========================================================================
   Pencarian (debounce) & sinkronisasi tombol
   ========================================================================== */
let searchTimer = null;
function onSearchInput() {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(renderList, 150);
}


/* ==========================================================================
   Pindahkan (pemilih folder tujuan)
   ========================================================================== */
let moveCtx = null;     // { items, summary, path, list, loading } selama dialog pindah terbuka

function selectedItems() {
  return [...state.selected].map((p) => state.items.get(p)).filter(Boolean);
}

/** Alasan tujuan tidak valid, atau null jika boleh. */
function moveTargetError(dest) {
  const inside = moveCtx.items.find((it) => dest === it.Path || dest.startsWith(it.Path + '/'));
  if (inside) return `"${inside.Name}" tidak bisa dipindah ke dalam dirinya sendiri`;
  if (moveCtx.items.every((it) => parentOf(it.Path) === dest)) return 'Item sudah ada di folder ini';
  return null;
}

function isInsideMoved(folderPath) {
  return moveCtx.items.some((it) => folderPath === it.Path || folderPath.startsWith(it.Path + '/'));
}

function openMoveDialog(items) {
  if (items.length === 0) return;
  moveCtx = {
    items,
    summary: items.length === 1 ? `Memindahkan "${items[0].Name}"` : `Memindahkan ${items.length} item`,
    path: state.currentPath,
    list: [],
    loading: true,
  };
  openOverlay('moveOverlay');
  loadMoveFolder(state.currentPath);
}

function closeMoveDialog() {
  moveCtx = null;
  closeOverlay('moveOverlay');
}

function renderMoveFooter() {
  if (!moveCtx) return;
  const err = moveTargetError(moveCtx.path);
  $('moveSummary').textContent = err || moveCtx.summary;
  $('moveSummary').classList.toggle('text-red-400', !!err);
  $('moveConfirmBtn').disabled = !!err || moveCtx.loading;
}

async function loadMoveFolder(path) {
  if (!moveCtx) return;
  const requestId = (moveCtx.requestId || 0) + 1;
  moveCtx.requestId = requestId;
  moveCtx.path = path;
  moveCtx.list = [];
  moveCtx.loading = true;
  $('movePath').textContent = path ? `/${path}` : 'Root (My PC Drive)';
  $('moveUpBtn').disabled = !path;
  $('moveList').replaceChildren(el('p', { class: 'text-xs text-slate-500 text-center py-8', text: 'Memuat folder...' }));
  renderMoveFooter();

  // Tampilkan cache folder lebih dulu agar navigasi antar-folder terasa instan.
  const cached = await fetchCached(`/cache/list?remote=${encodeURIComponent(path)}`);
  if (!moveCtx || moveCtx.requestId !== requestId || moveCtx.path !== path) return;
  if (cached && Array.isArray(cached.list)) {
    moveCtx.list = cached.list;
    moveCtx.loading = false;
    renderMoveList();
    renderMoveFooter();
  }

  // Tetap sinkronisasi ke Rclone di latar belakang dan perbarui cache.
  try {
    const data = await api('/operations/list', { fs: REMOTE, remote: path });
    if (!moveCtx || moveCtx.requestId !== requestId || moveCtx.path !== path) return;
    const list = Array.isArray(data.list) ? data.list : [];
    if (JSON.stringify(list) !== JSON.stringify(moveCtx.list)) {
      moveCtx.list = list;
      renderMoveList();
    }
    moveCtx.loading = false;
    renderMoveFooter();
  } catch (err) {
    if (!moveCtx || moveCtx.requestId !== requestId || moveCtx.path !== path) return;
    moveCtx.loading = false;
    renderMoveFooter();
    if (!cached || !Array.isArray(cached.list)) {
      $('moveList').replaceChildren(el('p', {
        class: 'text-xs text-red-400 text-center py-8',
        text: `Gagal memuat folder: ${err.message}`,
      }));
    } else {
      showToast(`Memakai cache folder; gagal memperbarui dari Drive: ${err.message}`, 'error');
    }
  }
}

function renderMoveList() {
  const folders = moveCtx.list
    .filter((i) => i.IsDir)
    .sort((a, b) => a.Name.localeCompare(b.Name, 'id', { numeric: true }));
  const wrap = $('moveList');
  if (folders.length === 0) {
    wrap.replaceChildren(el('p', { class: 'text-xs text-slate-500 text-center py-8', text: 'Tidak ada subfolder di sini' }));
  } else {
    wrap.replaceChildren(...folders.map((f) => {
      const blocked = isInsideMoved(f.Path);
      return el('button', {
        type: 'button',
        class: 'move-row',
        disabled: blocked,
        title: blocked ? 'Tidak bisa masuk ke folder yang sedang dipindah' : f.Name,
        onclick: () => loadMoveFolder(f.Path),
      }, [
        el('span', { text: '📁' }),
        el('span', { class: 'truncate', text: f.Name }),
      ]);
    }));
  }
  renderMoveFooter();
}

async function confirmMove() {
  if (!moveCtx || moveCtx.loading) return;
  const dest = moveCtx.path;
  const err = moveTargetError(dest);
  if (err) return showToast(err, 'error');

  const todo = moveCtx.items.filter((it) => parentOf(it.Path) !== dest);
  const byName = new Map(moveCtx.list.map((i) => [i.Name, i]));
  const clashes = todo.filter((it) => byName.has(it.Name));

  const folderClash = clashes.find((it) => it.IsDir || byName.get(it.Name).IsDir);
  if (folderClash) {
    return showToast(`Sudah ada folder bernama "${folderClash.Name}" di tujuan. Ubah namanya dulu.`, 'error');
  }
  if (clashes.length) {
    const names = clashes.slice(0, 3).map((i) => `"${i.Name}"`).join(', ')
      + (clashes.length > 3 ? ` dan ${clashes.length - 3} lainnya` : '');
    const ok = await askConfirm({
      title: 'Timpa file yang sudah ada?',
      message: `${names} sudah ada di folder tujuan dan akan diganti.`,
      okText: 'Timpa',
      danger: true,
    });
    if (!ok) return;
  }

  const btn = $('moveConfirmBtn');
  setBusy(btn, true);
  let done = 0;
  let failed = 0;
  for (const it of todo) {
    const newPath = joinPath(dest, it.Name);
    try {
      await api('/operations/moveto', {
        srcFs: REMOTE, srcRemote: it.Path, dstFs: REMOTE, dstRemote: newPath,
      });
      favoritesAfterMove(it.Path, newPath);     // favorit ikut pindah otomatis
      state.selected.delete(it.Path);
      done++;
    } catch {
      failed++;
    }
  }
  setBusy(btn, false);
  closeMoveDialog();
  showToast(
    failed ? `${done} dipindah, ${failed} gagal` : `${done} item berhasil dipindah`,
    failed ? 'error' : 'success'
  );
  afterChange();
}

/* ==========================================================================
   Bagikan (tautan publik dari Drive)
   ========================================================================== */
let shareCtx = null;

async function shareItem(item) {
  shareCtx = item;
  $('shareName').textContent = item.Name;
  $('shareUrl').value = '';
  $('shareBody').classList.add('hidden');
  $('shareStopBtn').classList.add('hidden');
  $('shareError').textContent = '';
  $('shareLoader').classList.remove('hidden');
  openOverlay('shareOverlay');

  try {
    const data = await api('/operations/publiclink', { fs: REMOTE, remote: item.Path });
    if (shareCtx !== item) return;
    if (!data.url) throw new ApiError('Drive tidak mengembalikan tautan');
    $('shareLoader').classList.add('hidden');
    $('shareUrl').value = data.url;
    $('shareBody').classList.remove('hidden');
    $('shareStopBtn').classList.remove('hidden');
  } catch (err) {
    if (shareCtx !== item) return;
    $('shareLoader').classList.add('hidden');
    $('shareError').textContent = `Gagal membuat tautan: ${err.message}`;
  }
}

function closeShare() {
  shareCtx = null;
  closeOverlay('shareOverlay');
}

async function copyShareUrl() {
  const url = $('shareUrl').value;
  if (!url) return;
  try {
    await navigator.clipboard.writeText(url);
    showToast('Tautan disalin', 'success');
  } catch {
    $('shareUrl').select();
    showToast('Salin tautan secara manual (Ctrl+C)', 'info');
  }
}

async function stopSharing() {
  const item = shareCtx;
  if (!item) return;
  const ok = await askConfirm({
    title: 'Hentikan berbagi?',
    message: 'Tautan ini tidak akan bisa dibuka lagi oleh siapa pun.',
    okText: 'Hentikan',
    danger: true,
  });
  if (!ok) return;
  const done = await run($('shareStopBtn'),
    () => api('/operations/publiclink', { fs: REMOTE, remote: item.Path, unlink: true }),
    'Berbagi dihentikan');
  if (done) closeShare();
}

/* ==========================================================================
   Tempat sampah
   ========================================================================== */
let trashItems = [];

async function apiGet(endpoint) {
  let res;
  try {
    res = await fetch(API + endpoint, { cache: 'no-store' });
  } catch {
    throw new ApiError('Tidak bisa terhubung ke server lokal');
  }
  return readJson(res);
}

function openTrash() {
  openOverlay('trashOverlay');
  loadTrash();
}

function closeTrash() {
  closeOverlay('trashOverlay');
}

async function loadTrash() {
  if (trashItems.length === 0) {
    $('trashList').replaceChildren(el('p', { class: 'text-xs text-slate-500 text-center py-10', text: 'Memuat...' }));
  }
  try {
    const data = await apiGet('/trash/list');
    trashItems = Array.isArray(data.items) ? data.items : [];
  } catch (err) {
    $('trashList').replaceChildren(el('p', {
      class: 'text-xs text-red-400 text-center py-10',
      text: `Gagal memuat tempat sampah: ${err.message}`,
    }));
    return;
  }
  renderTrash();
}

function renderTrash() {
  const wrap = $('trashList');
  const count = trashItems.length;
  $('trashInfo').textContent = count
    ? `${count} item · dihapus permanen otomatis setelah ${TRASH_DAYS} hari`
    : `Kosong · item yang dihapus disimpan ${TRASH_DAYS} hari`;
  $('trashEmptyBtn').disabled = count === 0;

  if (count === 0) {
    wrap.replaceChildren(el('div', { class: 'text-center py-12 text-xs text-slate-500' }, [
      el('div', { class: 'text-4xl mb-2', text: '🗑️' }),
      el('p', { text: 'Tempat sampah kosong' }),
    ]));
    return;
  }
  wrap.replaceChildren(...trashItems.map(buildTrashRow));
}

function buildTrashRow(rec) {
  const daysLeft = Math.max(0, Math.ceil((rec.expiresAt * 1000 - Date.now()) / 86400000));
  const from = parentOf(rec.origPath) ? `/${parentOf(rec.origPath)}` : 'Root';
  const meta = `${rec.isDir ? 'Folder' : formatBytes(rec.size)} · Asal: ${from} · sisa ${daysLeft} hari`;
  return el('div', { class: 'trash-row', dataset: { id: rec.id } }, [
    el('span', { class: 'card-icon', text: rec.isDir ? '📁' : KIND_ICON[fileKind(rec.name)] }),
    el('div', { class: 'min-w-0 flex-1' }, [
      el('p', { class: 'card-name', text: rec.name, title: rec.name }),
      el('p', { class: 'card-meta', text: meta }),
    ]),
    el('button', { type: 'button', class: 'btn-ghost whitespace-nowrap', text: '↩ Pulihkan', dataset: { trash: 'restore' } }),
    el('button', {
      type: 'button',
      class: 'icon-btn text-red-400',
      text: '✕',
      title: 'Hapus permanen',
      attrs: { 'aria-label': `Hapus permanen ${rec.name}` },
      dataset: { trash: 'purge' },
    }),
  ]);
}

async function restoreTrash(rec, btn) {
  setBusy(btn, true);
  let result;
  try {
    result = await api('/trash/restore', { id: rec.id });
  } catch (err) {
    setBusy(btn, false);
    showToast(`Gagal memulihkan: ${err.message}`, 'error');
    return loadTrash();
  }
  if (result.toRoot) showToast('Folder asal sudah tidak ada, dipulihkan ke Root', 'info');
  else if (result.renamed) showToast('Dipulihkan dengan nama baru karena nama lama sudah dipakai', 'info');
  else showToast('Item berhasil dipulihkan', 'success');
  await loadTrash();
  afterChange();
}

async function purgeTrash(rec, btn) {
  const ok = await askConfirm({
    title: 'Hapus permanen?',
    message: `"${rec.name}" akan dihapus selamanya dan tidak bisa dipulihkan.`,
    okText: 'Hapus permanen',
    danger: true,
  });
  if (!ok) return;
  await run(btn, () => api('/trash/delete', { id: rec.id }), 'Dihapus permanen');
  await loadTrash();
  loadStorage();
}

async function emptyTrash() {
  if (trashItems.length === 0) return;
  const ok = await askConfirm({
    title: 'Kosongkan tempat sampah?',
    message: `Semua ${trashItems.length} item di tempat sampah akan dihapus permanen.`,
    okText: 'Kosongkan',
    danger: true,
  });
  if (!ok) return;
  let result = null;
  await run($('trashEmptyBtn'), async () => {
    result = await api('/trash/empty', {});
  }, null);
  if (result) {
    showToast(
      result.failed ? `${result.deleted} dihapus, ${result.failed} gagal` : 'Tempat sampah dikosongkan',
      result.failed ? 'error' : 'success'
    );
  }
  await loadTrash();
  loadStorage();
}

/* ==========================================================================
   Seret & lepas untuk unggah
   ========================================================================== */
let dropDepth = 0;

function isModalOpen() {
  return ['dialogOverlay', 'moveOverlay', 'shareOverlay', 'trashOverlay', 'previewOverlay']
    .some((id) => !$(id).classList.contains('hidden'));
}

function canDropHere() {
  return state.category !== 'favorites' && !isModalOpen();
}

function carriesFiles(e) {
  return !!e.dataTransfer && Array.from(e.dataTransfer.types || []).includes('Files');
}

function setDropVisible(on) {
  if (on) {
    $('dropTarget').textContent = state.currentPath ? `Ke folder /${state.currentPath}` : 'Ke Root (My PC Drive)';
  }
  $('dropOverlay').classList.toggle('hidden', !on);
}

/** Mengambil file dari event drop. Folder dilewati karena belum didukung. */
function collectDroppedFiles(dt) {
  const files = [];
  let skipped = 0;
  const items = dt.items ? Array.from(dt.items) : [];
  if (items.length && typeof items[0].webkitGetAsEntry === 'function') {
    for (const item of items) {
      if (item.kind !== 'file') continue;
      const entry = item.webkitGetAsEntry();
      if (entry && entry.isDirectory) {
        skipped++;
        continue;
      }
      const file = item.getAsFile();
      if (file) files.push(file);
    }
  } else {
    files.push(...Array.from(dt.files || []));
  }
  return { files, skipped };
}

function initDragAndDrop() {
  window.addEventListener('dragenter', (e) => {
    if (!carriesFiles(e)) return;
    e.preventDefault();
    dropDepth++;
    if (canDropHere()) setDropVisible(true);
  });

  window.addEventListener('dragover', (e) => {
    if (!carriesFiles(e)) return;
    e.preventDefault();                       // tanpa ini browser membuka file yang dijatuhkan
    e.dataTransfer.dropEffect = canDropHere() ? 'copy' : 'none';
  });

  window.addEventListener('dragleave', (e) => {
    if (!carriesFiles(e)) return;
    dropDepth = Math.max(0, dropDepth - 1);
    if (dropDepth === 0) setDropVisible(false);
  });

  window.addEventListener('drop', (e) => {
    if (!carriesFiles(e)) return;
    e.preventDefault();
    dropDepth = 0;
    setDropVisible(false);
    if (isModalOpen()) return;
    if (state.category === 'favorites') {
      showToast('Buka sebuah folder dulu untuk mengunggah', 'info');
      return;
    }
    const { files, skipped } = collectDroppedFiles(e.dataTransfer);
    if (skipped) showToast(`${skipped} folder dilewati (unggah folder belum didukung)`, 'info');
    if (files.length) uploadFiles(files, state.currentPath);
  });
}

/* ==========================================================================
   Event untuk modal baru
   ========================================================================== */
function bindModalEvents() {
  $('nav-trash').addEventListener('click', openTrash);
  $('moveSelBtn').addEventListener('click', () => openMoveDialog(selectedItems()));

  $('moveUpBtn').addEventListener('click', () => {
    if (moveCtx && moveCtx.path) loadMoveFolder(parentOf(moveCtx.path));
  });
  $('moveCancelBtn').addEventListener('click', closeMoveDialog);
  $('moveConfirmBtn').addEventListener('click', confirmMove);
  $('moveOverlay').addEventListener('click', (e) => {
    if (e.target === $('moveOverlay')) closeMoveDialog();
  });

  $('shareCopyBtn').addEventListener('click', copyShareUrl);
  $('shareStopBtn').addEventListener('click', stopSharing);
  $('shareCloseBtn').addEventListener('click', closeShare);
  $('shareOverlay').addEventListener('click', (e) => {
    if (e.target === $('shareOverlay')) closeShare();
  });

  $('trashCloseBtn').addEventListener('click', closeTrash);
  $('trashEmptyBtn').addEventListener('click', emptyTrash);
  $('trashOverlay').addEventListener('click', (e) => {
    if (e.target === $('trashOverlay')) closeTrash();
  });
  $('trashList').addEventListener('click', (e) => {
    const btn = e.target.closest('[data-trash]');
    if (!btn) return;
    const row = btn.closest('[data-id]');
    const rec = row && trashItems.find((r) => r.id === row.dataset.id);
    if (!rec) return;
    if (btn.dataset.trash === 'restore') restoreTrash(rec, btn);
    else purgeTrash(rec, btn);
  });
}

/* ==========================================================================
   Inisialisasi & event
   ========================================================================== */
function bindEvents() {
  $('nav-all').addEventListener('click', () => setCategory('all'));
  $('nav-fav').addEventListener('click', () => setCategory('favorites'));

  $('searchInput').addEventListener('input', onSearchInput);
  $('sortSelect').addEventListener('change', renderList);
  $('viewToggleBtn').addEventListener('click', () => {
    state.isGridView = !state.isGridView;
    saveViewPref();
    updateViewButton();
    renderList();
  });
  $('refreshBtn').addEventListener('click', () => { loadStorage(); refresh(); });
  $('quotaRefresh').addEventListener('click', loadStorage);

  $('btnBack').addEventListener('click', goBack);
  $('btnUp').addEventListener('click', goUp);
  $('breadcrumb').addEventListener('click', (e) => {
    const btn = e.target.closest('[data-nav]');
    if (btn) navigate(btn.dataset.nav);
  });

  $('newFolder').addEventListener('keydown', (e) => { if (e.key === 'Enter') createFolder(); });
  $('createFolderBtn').addEventListener('click', createFolder);
  $('fileInput').addEventListener('change', updateFileHint);
  $('folderInput').addEventListener('change', updateFileHint);
  $('uploadFolderBtn').addEventListener('click', () => $('folderInput').click());
  $('uploadBtn').addEventListener('click', uploadSelectedFiles);

  $('selectAllCb').addEventListener('change', (e) => (e.target.checked ? selectAllVisible() : clearSelection()));
  $('clearSelBtn').addEventListener('click', clearSelection);
  $('deleteSelBtn').addEventListener('click', deleteSelected);

  const list = $('fileContainer');
  list.addEventListener('click', (e) => {
    const target = e.target.closest('[data-action]');
    if (!target || target.dataset.action === 'select') return;
    handleCardAction(target);
  });
  list.addEventListener('change', (e) => {
    const box = e.target;
    if (!box.matches('input[data-action="select"]')) return;
    const card = box.closest('[data-path]');
    const item = card && state.items.get(card.dataset.path);
    if (item) toggleSelect(item, box.checked);
  });
  list.addEventListener('keydown', (e) => {
    if ((e.key === 'Enter' || e.key === ' ') && e.target.matches('[data-action="open"]')) {
      e.preventDefault();
      handleCardAction(e.target);
    }
  });

  $('closePreviewBtn').addEventListener('click', closePreview);
  $('prevBtn').addEventListener('click', () => gotoPreview(state.previewIndex - 1));
  $('nextBtn').addEventListener('click', () => gotoPreview(state.previewIndex + 1));
  $('saveBtn').addEventListener('click', (e) => saveCurrentText(e.currentTarget));
  $('previewOverlay').addEventListener('click', (e) => {
    if (e.target === $('previewOverlay')) closePreview();
  });
  $('previewContent').addEventListener('input', (e) => {
    if (e.target.tagName === 'TEXTAREA') state.previewDirty = true;
  });

  $('dialogOkBtn').addEventListener('click', dialogSubmit);
  $('dialogCancelBtn').addEventListener('click', onDialogCancel);
  $('dialogInput').addEventListener('keydown', (e) => { if (e.key === 'Enter') dialogSubmit(); });
  $('dialogOverlay').addEventListener('click', (e) => {
    if (e.target === $('dialogOverlay')) onDialogCancel();
  });

  document.addEventListener('keydown', (e) => {
    const isOpen = (id) => !$(id).classList.contains('hidden');
    if (isOpen('dialogOverlay')) {
      if (e.key === 'Escape') { e.preventDefault(); onDialogCancel(); }
      return;
    }
    if (e.key === 'Escape') {
      if (isOpen('moveOverlay')) return closeMoveDialog();
      if (isOpen('shareOverlay')) return closeShare();
      if (isOpen('trashOverlay')) return closeTrash();
    }
    if (isOpen('previewOverlay')) {
      if (e.key === 'Escape') closePreview();
      const typing = e.target.tagName === 'TEXTAREA' || e.target.tagName === 'INPUT';
      if (!typing && e.key === 'ArrowLeft') gotoPreview(state.previewIndex - 1);
      if (!typing && e.key === 'ArrowRight') gotoPreview(state.previewIndex + 1);
    }
  });
}

async function setStorageMode(mode) {
  if (mode === activeStorageMode) return;
  const buttons = [$('storageAndroid'), $('storageDrive')];
  buttons.forEach((b) => { if (b) b.disabled = true; });
  try {
    const result = await api('/storage', { mode });
    activeStorageMode = result.mode || mode;
    REMOTE = activeStorageMode === 'drive' ? 'cloud:' : 'local:';
    $('storageModeHint').textContent = activeStorageMode === 'drive'
      ? 'Google Drive aktif · perubahan tersimpan ke Drive'
      : 'Penyimpanan HP aktif · file lokal Android';
    const active = activeStorageMode === 'drive' ? $('storageDrive') : $('storageAndroid');
    const inactive = activeStorageMode === 'drive' ? $('storageAndroid') : $('storageDrive');
    active.className = 'px-3 py-2 rounded-xl text-xs font-semibold border border-blue-500/40 bg-blue-600/20 text-blue-300';
    inactive.className = 'px-3 py-2 rounded-xl text-xs font-semibold border border-slate-700 bg-slate-800 text-slate-300';
    state.currentPath = ''; state.loaded = false; state.rawItems = []; state.selected.clear();
    await loadStorage(); await loadFiles('');
    showToast(activeStorageMode === 'drive' ? 'Google Drive aktif' : 'Penyimpanan HP aktif', 'success');
  } catch (err) {
    showToast(`Gagal mengganti penyimpanan: ${err.message}`, 'error');
  } finally {
    buttons.forEach((b) => { if (b) b.disabled = false; });
  }
}

async function initStorageMode() {
  try {
    const res = await fetch(API + '/storage', { cache: 'no-store' });
    const data = await readJson(res);
    activeStorageMode = data.mode || 'android';
    REMOTE = activeStorageMode === 'drive' ? 'cloud:' : 'local:';
    $('storageModeHint').textContent = activeStorageMode === 'drive'
      ? 'Google Drive aktif · perubahan tersimpan ke Drive'
      : 'Penyimpanan HP aktif · file lokal Android';
    const active = activeStorageMode === 'drive' ? $('storageDrive') : $('storageAndroid');
    const inactive = activeStorageMode === 'drive' ? $('storageAndroid') : $('storageDrive');
    active.className = 'px-3 py-2 rounded-xl text-xs font-semibold border border-blue-500/40 bg-blue-600/20 text-blue-300';
    inactive.className = 'px-3 py-2 rounded-xl text-xs font-semibold border border-slate-700 bg-slate-800 text-slate-300';
  } catch { /* default Android remains selected */ }
  $('storageAndroid').addEventListener('click', () => setStorageMode('android'));
  $('storageDrive').addEventListener('click', () => setStorageMode('drive'));
}

async function init() {
  state.isGridView = loadViewPref();
  updateViewButton();
  styleNav();
  bindEvents();
  bindModalEvents();
  await initStorageMode();
  initDragAndDrop();
  renderBreadcrumb();
  updateNavButtons();
  loadStorage();
  loadFiles('');
}

document.addEventListener('DOMContentLoaded', init);
