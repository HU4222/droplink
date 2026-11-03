/* ============================================================
   DropLink 互传 · 前端逻辑
   ============================================================ */
(() => {
  'use strict';

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  /* 本机界面令牌：由程序打开窗口时通过 ?k= 传入，只存在 sessionStorage 里。
     没有它，服务端不会因为「请求来自 127.0.0.1」就放行。 */
  const DL_TOKEN = (() => {
    try {
      const u = new URL(location.href);
      const k = u.searchParams.get('k');
      if (k) {
        sessionStorage.setItem('dl.token', k);
        u.searchParams.delete('k');
        const q = u.searchParams.toString();
        history.replaceState(null, '', u.pathname + (q ? '?' + q : '') + u.hash);
        return k;
      }
      return sessionStorage.getItem('dl.token') || '';
    } catch (e) {
      return '';
    }
  })();

  const state = {
    session: null,
    authed: false,
    view: 'browse',
    path: '',
    parent: null,
    name: '',
    list: [],
    roots: [],
    recvDir: '',
    writable: false,
    mode: localStorage.getItem('dl.mode') === 'grid' ? 'grid' : 'list',
    selectMode: false,
    sel: new Map(),
    searching: false,
    searchQuery: '',
    searchOpen: false,
    uploads: [],
    sheetEntry: null,
    sharedUrl: '',
    clipTimer: 0,
    suppressClick: 0,
    animateList: false,
    networks: [],        // 本机所有网卡 IP（网络切换）
    selectedIp: localStorage.getItem('dl.ip') || '',
    peers: [],           // 已接入的终端
    sendFiles: [],       // 电脑端已选的待发送文件
  };

  /* ---------------- 小工具 ---------------- */

  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));

  const uid = () => Math.random().toString(36).slice(2, 10);

  function fmtSize(n) {
    n = Number(n) || 0;
    if (n < 1024) return `${n} B`;
    const units = ['KB', 'MB', 'GB', 'TB'];
    let i = -1;
    do { n /= 1024; i++; } while (n >= 1024 && i < units.length - 1);
    return `${n < 10 ? n.toFixed(1) : Math.round(n)} ${units[i]}`;
  }

  function fmtTime(ts) {
    if (!ts) return '';
    const d = new Date(ts * 1000);
    const now = new Date();
    const p = (x) => String(x).padStart(2, '0');
    const hm = `${p(d.getHours())}:${p(d.getMinutes())}`;
    if (d.toDateString() === now.toDateString()) return `今天 ${hm}`;
    const y = d.getFullYear() === now.getFullYear() ? '' : `${d.getFullYear()}/`;
    return `${y}${p(d.getMonth() + 1)}/${p(d.getDate())} ${hm}`;
  }

  const SVGS = {
    folder: '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z"/>',
    file: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/>',
    image: '<rect x="3" y="4" width="18" height="16" rx="2.5"/><circle cx="8.5" cy="9.5" r="1.8"/><path d="m4 18 5-5 4 4 3-2 4 4"/>',
    video: '<rect x="3" y="5" width="13" height="14" rx="2.5"/><path d="m16 10 5-3v10l-5-3z"/>',
    audio: '<path d="M9 18V6l10-2v12"/><circle cx="6.5" cy="18" r="2.6"/><circle cx="16.5" cy="16" r="2.6"/>',
    text: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/><path d="M9 13h6M9 17h4"/>',
    doc: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/><path d="M9 12h6M9 16h6"/>',
    archive: '<path d="M4 8h16v11a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2z"/><path d="M3 4h18v4H3z"/><path d="M10 12h4"/>',
    chevron: '<path d="m9 5 7 7-7 7"/>',
    check: '<path d="m4 12 5 5L20 6"/>',
    down: '<path d="M12 4v11"/><path d="m7 10 5 5 5-5"/><path d="M5 20h14"/>',
    up: '<path d="M12 20V9"/><path d="m7 14 5-5 5 5"/><path d="M5 4h14"/>',
    close: '<path d="M6 6l12 12M18 6 6 18"/>',
    trash: '<path d="M4 7h16"/><path d="M9 7V5h6v2"/><path d="M6 7l1 13h10l1-13"/>',
    share: '<path d="M8.6 13.4 15.4 17"/><path d="M15.4 7 8.6 10.6"/><circle cx="18" cy="6" r="2.6"/><circle cx="18" cy="18" r="2.6"/><circle cx="6" cy="12" r="2.6"/>',
    qr: '<rect x="3" y="3" width="7" height="7" rx="1.4"/><rect x="14" y="3" width="7" height="7" rx="1.4"/><rect x="3" y="14" width="7" height="7" rx="1.4"/><path d="M14 14h3v3h-3zM20 14v7h-7"/>',
    drive: '<rect x="3" y="5" width="18" height="7" rx="2"/><rect x="3" y="12" width="18" height="7" rx="2"/><path d="M7 8.5h.01M7 15.5h.01"/>',
    search: '<circle cx="11" cy="11" r="6.5"/><path d="m16 16 4 4"/>',
    copy: '<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M15 5H6a2 2 0 0 0-2 2v9"/>',
    folderOpen: '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v1H3z"/><path d="M3 10h18l-2 9H5z"/>',
  };

  function ico(name, extra = '') {
    return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"
      stroke-linecap="round" stroke-linejoin="round" class="${extra}">${SVGS[name] || SVGS.file}</svg>`;
  }

  const KIND_ICON = { folder: 'folder', image: 'image', video: 'video', audio: 'audio', pdf: 'doc', doc: 'doc', archive: 'archive', text: 'text', other: 'file' };

  /* ---------------- 网络 ---------------- */

  async function api(path, opts = {}) {
    const init = { credentials: 'same-origin', ...opts };
    init.headers = Object.assign({}, init.headers || {});
    if (DL_TOKEN) init.headers['X-DL-Token'] = DL_TOKEN;
    if (init.body && typeof init.body === 'string') {
      init.headers['Content-Type'] = init.headers['Content-Type'] || 'application/json';
    }
    const res = await fetch(path, init);
    let data = null;
    const ct = res.headers.get('content-type') || '';
    if (ct.includes('application/json')) {
      try { data = await res.json(); } catch (e) { data = null; }
    }
    if (res.status === 401) {
      showLogin();
      const err = new Error((data && data.error) || '需要配对码');
      err.needLogin = true;
      throw err;
    }
    if (!res.ok || (data && data.ok === false)) {
      throw new Error((data && data.error) || `请求失败（${res.status}）`);
    }
    return data || {};
  }

  function toast(msg, type = 'info') {
    const el = $('#toast');
    el.textContent = msg;
    const isErr = type === 'err' || type === true;
    const isOk = type === 'ok';
    el.classList.toggle('err', isErr);
    el.classList.toggle('ok', isOk);
    el.hidden = false;
    requestAnimationFrame(() => el.classList.add('show'));
    clearTimeout(toast._t);
    const ms = isErr ? 2800 : (isOk ? 2200 : 1800);
    toast._t = setTimeout(() => {
      el.classList.remove('show');
      setTimeout(() => { el.hidden = true; }, 220);
    }, ms);
  }

  // 安卓原生桥在，就交给它：确认弹窗、.part、改名、通知整条链路都由原生实现。
  // 浏览器里（电脑本机界面）继续用 <a download>。
  function nativeBridge() {
    const b = window.androidBridge;
    return b && typeof b.downloadFile === 'function' ? b : null;
  }

  function triggerDownload(url, name) {
    const bridge = nativeBridge();
    if (bridge && name) {
      try {
        // 桥只接受绝对地址；dlUrl() 给的是 /api/download?...，先按当前页面解析
        bridge.downloadFile(new URL(url, location.href).href, name, '');
        return true;
      } catch (e) {
        // 桥调用失败就退回浏览器下载，不要让按钮变成死的
      }
    }
    const a = document.createElement('a');
    a.href = url;
    a.download = name || '';
    a.rel = 'noopener';
    document.body.appendChild(a);
    a.click();
    setTimeout(() => a.remove(), 1000);
    return false;
  }

  const dlUrl = (p, log = true) =>
    `/api/download?path=${encodeURIComponent(p)}${log ? '&log=1' : ''}`;

  /* ---------------- 主题 ---------------- */

  const THEME_KEY = 'dl.theme';
  const ONBOARD_KEY = 'dl.onboarded';
  const TIPS_KEY = 'dl.tips';   // 'off' = 用户已关闭「怎么用」提示，之后不再显示

  function tipsEnabled() {
    return localStorage.getItem(TIPS_KEY) !== 'off';
  }

  // 给每个「怎么用」提示条挂一个关闭按钮；关闭后全局不再显示
  function initTips() {
    if (!tipsEnabled()) { document.body.classList.add('tips-off'); return; }
    $$('.inline-tip').forEach((tip) => {
      if (tip.querySelector('.tip-x')) return;
      const btn = document.createElement('button');
      btn.className = 'tip-x';
      btn.type = 'button';
      btn.title = '不再显示这类提示';
      btn.setAttribute('aria-label', '关闭使用提示');
      btn.textContent = '✕';
      btn.addEventListener('click', (ev) => {
        ev.preventDefault();
        ev.stopPropagation();
        localStorage.setItem(TIPS_KEY, 'off');
        document.body.classList.add('tips-off');
        toast('已关闭使用提示，可在「我的」页重新打开');
      });
      tip.appendChild(btn);
    });
  }

  function effectiveTheme() {
    const t = document.documentElement.dataset.theme;
    if (t === 'light' || t === 'dark') return t;
    return window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  }

  function applyTheme(t) {
    const root = document.documentElement;
    if (t === 'light' || t === 'dark') root.dataset.theme = t;
    else delete root.dataset.theme;
    const btn = $('#themeBtn');
    if (btn) btn.title = `切换主题（当前：${effectiveTheme() === 'dark' ? '深色' : '浅色'}）`;
  }

  function initTheme() {
    const qs = new URLSearchParams(location.search).get('theme');
    const saved = localStorage.getItem(THEME_KEY);
    applyTheme(qs === 'light' || qs === 'dark' ? qs : (saved || 'auto'));
  }

  /* ---------------- 传输可视化（Canvas） ---------------- */

  const viz = {
    canvas: null, ctx: null, w: 0, h: 0,
    parts: [], pool: [], spawnAcc: 0, speed: 0,
    hist: new Array(56).fill(0),
    lastT: 0, lastSample: 0, lastMeta: 0,
    burstStart: -99,
    // 自适应画质：按每帧真实绘制耗时（不是帧间隔，避免被 60Hz 屏幕误判）降级
    cost: 0, quality: 0, lastAdjust: 0, partCap: 180, trail: true,
    // 每帧复用的缓存，避免在 120fps 下反复分配对象 / 拼字符串
    g: { x0: 68, x1: 68, y: 0, cx: 0, cy: 0 },
    dark: false,
    c1: '79,70,229', c2: '124,58,237',
    c1str: 'rgb(79,70,229)', c2str: 'rgb(124,58,237)',
  };

  function roundRect(ctx, x, y, w, h, r) {
    const rr = Math.min(r, w / 2, h / 2);
    ctx.beginPath();
    ctx.moveTo(x + rr, y);
    ctx.arcTo(x + w, y, x + w, y + h, rr);
    ctx.arcTo(x + w, y + h, x, y + h, rr);
    ctx.arcTo(x, y + h, x, y, rr);
    ctx.arcTo(x, y, x + w, y, rr);
    ctx.closePath();
  }

  function initViz() {
    const canvas = $('#vizCanvas');
    if (!canvas || !canvas.getContext) return;
    viz.canvas = canvas;
    viz.ctx = canvas.getContext('2d');

    const resize = () => {
      const stage = canvas.parentElement;
      if (!stage) return;
      const rect = stage.getBoundingClientRect();
      const dpr = Math.min(2, window.devicePixelRatio || 1);
      viz.w = Math.max(1, rect.width);
      viz.h = Math.max(1, rect.height);
      canvas.width = Math.round(viz.w * dpr);
      canvas.height = Math.round(viz.h * dpr);
      canvas.style.width = `${viz.w}px`;
      canvas.style.height = `${viz.h}px`;
      viz.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      // 几何随尺寸更新，后续每帧直接复用
      viz.g.x0 = viz.w < 340 ? 58 : 68;
      viz.g.x1 = viz.w - viz.g.x0;
      viz.g.y = Math.round(viz.h * 0.44);
      viz.g.cx = viz.w / 2;
      viz.g.cy = viz.g.y - 17;
    };

    resize();
    if (window.ResizeObserver) new ResizeObserver(resize).observe(canvas.parentElement);
    else window.addEventListener('resize', resize);
    requestAnimationFrame(loopViz);
  }

  const activeUploads = () => state.uploads.filter((u) => u.status === 'up');
  const uploadSpeed = () => activeUploads().reduce((a, u) => a + (u.speed || 0), 0);

  function loopViz(ts) {
    const now = ts / 1000;
    let dt = viz.lastT ? now - viz.lastT : 0.016;
    viz.lastT = now;
    dt = Math.min(0.06, Math.max(0.001, dt));

    if (!document.hidden && state.view === 'send') {
      const raw = activeUploads().length ? uploadSpeed() : 0;
      viz.speed += (raw - viz.speed) * Math.min(1, dt * 4);
      if (!raw && viz.speed < 2000) viz.speed = Math.max(0, viz.speed - dt * 4000);

      const t0 = performance.now();
      stepViz(dt);
      drawViz(now);
      // 绘制耗时 EMA：>6.5ms 说明逼近 120fps 的 8.3ms 预算，主动降级
      const cost = performance.now() - t0;
      viz.cost = viz.cost ? viz.cost * 0.9 + cost * 0.1 : cost;
      if (now - viz.lastAdjust > 1.5) {
        if (viz.cost > 6.5 && viz.quality < 2) {
          viz.quality++; viz.lastAdjust = now; applyVizQuality();
        } else if (viz.cost < 2.6 && viz.quality > 0) {
          viz.quality--; viz.lastAdjust = now; applyVizQuality();
        }
      }
      if (now - viz.lastSample > 0.26) {
        viz.lastSample = now;
        viz.hist.push(viz.speed);
        viz.hist.shift();
      }
      if (now - viz.lastMeta > 0.3) {
        viz.lastMeta = now;
        updateVizMeta();
      }
    }
    requestAnimationFrame(loopViz);
  }

  function applyVizQuality() {
    if (viz.quality === 0) { viz.partCap = 180; viz.trail = true; }
    else if (viz.quality === 1) { viz.partCap = 100; viz.trail = false; }
    else { viz.partCap = 56; viz.trail = false; }
  }

  function stepViz(dt) {
    const mb = viz.speed / 1048576;
    const rate = viz.speed > 30000 ? 5 + Math.min(30, mb * 10) : 1.45;
    viz.spawnAcc += dt * rate;
    while (viz.spawnAcc >= 1) {
      viz.spawnAcc -= 1;
      let p = viz.pool.pop();
      if (!p) p = {};
      p.t = 0;
      p.rate = 0.52 + Math.random() * 0.3;
      p.size = 1 + Math.random() * 2.1;
      p.alpha = viz.speed > 30000 ? 1 : 0.45;
      p.hue = Math.random() > 0.55 ? 1 : 0;
      viz.parts.push(p);
    }
    // 原地推进 + 压缩，超时的粒子回收到池，全程不产生新数组
    const parts = viz.parts;
    let n = 0;
    for (let i = 0; i < parts.length; i++) {
      const p = parts[i];
      p.t += dt * p.rate;
      if (p.t <= 1.02) {
        if (n !== i) parts[n] = p;
        n++;
      } else if (viz.pool.length < 220) {
        viz.pool.push(p);
      }
    }
    parts.length = n;
    if (parts.length > viz.partCap) parts.length = viz.partCap;
  }

  function drawViz(now) {
    const ctx = viz.ctx;
    if (!ctx || viz.w < 20) return;
    const w = viz.w, h = viz.h, g = viz.g;
    ctx.clearRect(0, 0, w, h);

    // 主题变化时才重建调色板，其余帧复用缓存字符串
    const dark = effectiveTheme() === 'dark';
    if (dark !== viz.dark) {
      viz.dark = dark;
      viz.c1 = dark ? '150,146,255' : '79,70,229';
      viz.c2 = dark ? '196,160,255' : '124,58,237';
      viz.c1str = 'rgb(' + viz.c1 + ')';
      viz.c2str = 'rgb(' + viz.c2 + ')';
    }
    const c1 = viz.c1, c1str = viz.c1str, c2str = viz.c2str;
    const live = viz.speed > 30000;

    // 虚线轨道
    ctx.setLineDash([3, 7]);
    ctx.lineDashOffset = -(now * 24) % 10;
    ctx.strokeStyle = 'rgba(' + c1 + ',' + (live ? 0.34 : 0.17) + ')';
    ctx.lineWidth = 1.3;
    ctx.beginPath();
    ctx.moveTo(g.x0, g.y);
    ctx.quadraticCurveTo(g.cx, g.cy, g.x1, g.y);
    ctx.stroke();
    ctx.setLineDash([]);

    // 底部速度波形（用 globalAlpha 控透明度，避免每根拼 rgba）
    const bars = 30;
    const span = g.x1 - g.x0;
    const bw = span / bars;
    const base = h - 7;
    const hist = viz.hist;
    const hoff = hist.length - bars;
    ctx.fillStyle = c1str;
    for (let i = 0; i < bars; i++) {
      const v = Math.min(1, hist[hoff + i] / 8388608); // 8 MB/s 满格
      const bh = 2 + v * 24;
      ctx.globalAlpha = 0.09 + v * 0.42;
      roundRect(ctx, g.x0 + i * bw, base - bh, Math.max(2, bw - 3), bh, 1.6);
      ctx.fill();
    }

    // 数据粒子（内联二次贝塞尔，不分配坐标数组）
    ctx.lineCap = 'round';
    const parts = viz.parts;
    for (let i = 0; i < parts.length; i++) {
      const p = parts[i];
      const t = p.t;
      const mt = 1 - t;
      const x = mt * mt * g.x0 + 2 * mt * t * g.cx + t * t * g.x1;
      const y = mt * mt * g.y + 2 * mt * t * g.cy + t * t * g.y;
      const fade = t < 1 ? Math.sin(Math.PI * t) : 0;
      const a = p.alpha * (0.25 + fade * 0.75);
      if (a <= 0.02) continue;
      const str = p.hue ? c2str : c1str;
      if (viz.trail) {
        const t0 = t > 0.055 ? t - 0.055 : 0;
        const mt0 = 1 - t0;
        const px = mt0 * mt0 * g.x0 + 2 * mt0 * t0 * g.cx + t0 * t0 * g.x1;
        const py = mt0 * mt0 * g.y + 2 * mt0 * t0 * g.cy + t0 * t0 * g.y;
        ctx.globalAlpha = a * 0.5;
        ctx.strokeStyle = str;
        ctx.lineWidth = p.size * 0.9;
        ctx.beginPath();
        ctx.moveTo(px, py);
        ctx.lineTo(x, y);
        ctx.stroke();
      }
      ctx.globalAlpha = a;
      ctx.fillStyle = str;
      ctx.beginPath();
      ctx.arc(x, y, p.size, 0, 6.2831853);
      ctx.fill();
    }
    ctx.globalAlpha = 1;

    // 完成波纹
    const k = now - viz.burstStart;
    if (k >= 0 && k < 1) {
      const e = 1 - Math.pow(1 - k, 3);
      ctx.strokeStyle = 'rgba(16,185,129,' + (0.5 * (1 - k)) + ')';
      ctx.lineWidth = 2 * (1 - k) + 0.5;
      ctx.beginPath();
      ctx.arc(g.cx, g.y, 16 + e * 96, 0, 6.2831853);
      ctx.stroke();
      ctx.strokeStyle = 'rgba(79,70,229,' + (0.35 * (1 - k)) + ')';
      ctx.beginPath();
      ctx.arc(g.cx, g.y, 8 + e * 54, 0, 6.2831853);
      ctx.stroke();
    }
  }

  function updateVizMeta() {
    if (!viz.canvas) return;
    const running = activeUploads();
    const done = state.uploads.filter((u) => u.status === 'done');
    const bytes = state.uploads.reduce(
      (a, u) => a + Math.min(u.loaded, u.total || u.size || 0), 0);
    const v = viz.speed / 1048576;
    $('#vizSpeed').textContent = v >= 1 ? v.toFixed(1) : v.toFixed(2);
    $('#vizCount').textContent = String(done.length);
    $('#vizBytes').textContent = fmtSize(bytes);
    $('#vizCard').classList.toggle('is-live', running.length > 0);
    $('#vizState').textContent = running.length
      ? `传输中 · ${running.length} 个任务`
      : (done.length ? '上一次传输已完成' : '待机中');
  }

  function vizBurst() {
    const el = $('#vizBurst');
    if (!el) return;
    const now = performance.now();
    // 波纹时间总是刷新（canvas 完成波纹跟随最新一次完成）
    viz.burstStart = now / 1000;
    // 去重：短时间内的多次完成合并为一次 DOM 动画，避免批量传输时连续重播
    if (now - (viz._burstAt || 0) < 800) return;
    viz._burstAt = now;
    el.classList.remove('show');
    void el.offsetWidth;
    el.classList.add('show');
  }

  /* ---------------- 登录 ---------------- */

  function showLogin() {
    $('#login').hidden = false;
    document.body.style.overflow = 'hidden';
    setTimeout(() => $('#pinInput').focus(), 150);
  }

  function hideLogin() {
    $('#login').hidden = true;
    document.body.style.overflow = '';
    state.authed = true;
  }

  function deviceDisplayName() {
    const ua = navigator.userAgent || '';
    if (window.androidBridge && window.androidBridge.getDeviceInfo) {
      try { const m = JSON.parse(window.androidBridge.getDeviceInfo()).model; if (m) return m; } catch (e) {}
    }
    if (ua.includes('Android')) {
      const m = ua.match(/Android\s+([\d.]+)/);
      return '安卓 ' + (m ? m[1] : '设备');
    }
    if (ua.includes('iPhone') || ua.includes('iPad')) return '苹果设备';
    return '手机设备';
  }

  async function doLogin() {
    const pin = $('#pinInput').value.trim();
    const errEl = $('#loginError');
    if (!pin) { errEl.textContent = '请输入配对码'; return; }
    errEl.textContent = '';
    try {
      await api('/api/login', { method: 'POST', body: JSON.stringify({ pin, name: deviceDisplayName() }) });
      hideLogin();
      await boot();
    } catch (e) {
      errEl.textContent = e.message || '连接失败';
      $('#pinInput').select();
    }
  }

  /* ---------------- 启动 ---------------- */

  async function boot() {
    try {
      const s = await api('/api/session');
      state.session = s;
      state.recvDir = s.recv_dir;
      state.authed = !!s.authed;
      renderDevice();
      if (!s.authed) {
        // 扫码即连：二维码带 pin 参数，打开时自动配对
        const autoPin = new URLSearchParams(location.search).get('pin');
        if (autoPin) {
          try {
            await api('/api/login', { method: 'POST', body: JSON.stringify({ pin: autoPin }) });
            return boot();
          } catch (e) { /* 配对码错，落到登录页 */ }
        }
        showLogin(); return;
      }
      hideLogin();
      const last = localStorage.getItem('dl.path');
      await loadList(last || s.recv_dir);
      const initial = (location.hash || '').replace('#', '') || 'browse';
      switchView(['browse', 'send', 'clip', 'about'].includes(initial) ? initial : 'browse');
      loadTransfers();
      pollLive();
      loadClip();
      startClipPoll();
      renderAbout();
      loadNetworks();
      pollPeers();
      pollInbox();
      renderSendPanel();
      // 电脑本机首次打开时显示引导；手机端不弹
      if (s.local && !localStorage.getItem(ONBOARD_KEY)) showGuide();
    } catch (e) {
      if (e && e.needLogin) {
        // 扫码即连：二维码带 pin 参数，打开时自动配对
        const autoPin = new URLSearchParams(location.search).get('pin');
        if (autoPin) {
          try {
            await api('/api/login', { method: 'POST', body: JSON.stringify({ pin: autoPin }) });
            return boot();
          } catch (err) { /* 配对码错，留在登录页 */ }
        }
        return;
      }
      $('#deviceName').textContent = '未连接';
      $('#deviceSub').textContent = e.message || '请确认电脑端已启动';
      $('#netDot').classList.add('is-off');
    }
  }

  function renderDevice() {
    const s = state.session || {};
    $('#deviceName').textContent = s.local ? `${s.device}（本机）` : `已连接 ${s.device || '电脑'}`;
    $('#deviceSub').textContent = s.local
      ? '电脑端 · 手机可扫码连接'
      : '局域网直连 · 不消耗流量';
    $('#netDot').classList.remove('is-off');
    const foot = $('#navFoot');
    if (foot) foot.innerHTML = `${esc(s.device || '')}<br>v${esc(s.version || '')} · 局域网直连`;
  }

  /* ---------------- 视图切换 ---------------- */

  const shownViews = new Set();
  function switchView(view) {
    state.view = view;
    $$('.view').forEach((v) => {
      const active = v.id === `view-${view}`;
      v.classList.toggle('is-active', active);
      // 入场动画只在首次展示时播放，避免每次切回来都重播
      if (active && !shownViews.has(view)) {
        shownViews.add(view);
        v.classList.add('first-show');
      }
    });
    $$('.nav-item').forEach((b) => b.classList.toggle('is-active', b.dataset.view === view));
    if (location.hash !== `#${view}`) history.replaceState(null, '', `#${view}`);
    window.scrollTo({ top: 0 });
    if (view === 'about') renderAbout();
    if (view === 'send') loadTransfers();
    if (view === 'clip') loadClip();
  }

  /* ---------------- 文件浏览 ---------------- */

  function crumbsFor(p) {
    const out = [];
    if (!p) return out;
    const win = p.match(/^([A-Za-z]:)[\\/]?(.*)$/);
    if (win) {
      out.push({ label: win[1] + '\\', path: win[1] + '\\' });
      let cur = win[1] + '\\';
      (win[2] || '').split(/[\\/]/).filter(Boolean).forEach((seg) => {
        cur = cur.endsWith('\\') ? cur + seg : cur + '\\' + seg;
        out.push({ label: seg, path: cur });
      });
    } else {
      out.push({ label: '/', path: '/' });
      let cur = '';
      p.split('/').filter(Boolean).forEach((seg) => {
        cur += '/' + seg;
        out.push({ label: seg, path: cur });
      });
    }
    return out;
  }

  async function loadList(path) {
    if (state.searchOpen) {
      const bar = $('#searchBar');
      if (bar) bar.remove();
      state.searchOpen = false;
    }
    const data = await api(`/api/list?path=${encodeURIComponent(path || '')}`);
    state.path = data.path;
    state.parent = data.parent;
    state.name = data.name;
    state.list = data.entries || [];
    state.roots = data.roots || [];
    state.recvDir = data.recv_dir || state.recvDir;
    state.writable = !!data.writable;
    state.searching = false;
    state.searchQuery = '';
    state.animateList = true;
    localStorage.setItem('dl.path', data.path);
    renderBrowse();
  }

  function renderBrowse() {
    // 面包屑
    const parts = crumbsFor(state.path);
    $('#crumbs').innerHTML = parts.map((c, i) => {
      const last = i === parts.length - 1;
      return `${i ? '<span class="crumb-sep">›</span>' : ''}
        <button class="crumb${last ? ' is-last' : ''}" data-crumb="${i}">${esc(c.label)}</button>`;
    }).join('') || '<button class="crumb is-last">文件</button>';
    const cb = $('#crumbs');
    cb.scrollLeft = cb.scrollWidth;

    // 快捷入口
    $('#roots').innerHTML = state.roots.map((r, i) => {
      const active = state.path && r.path.toLowerCase() === state.path.toLowerCase();
      return `<button class="root${active ? ' is-active' : ''}" data-root="${i}">
        ${ico(r.icon === 'drive' ? 'drive' : 'folder')}<span>${esc(r.label)}</span>
      </button>`;
    }).join('');

    // 标题
    const count = state.list.length;
    $('#listTitle').textContent = state.searching
      ? `“${state.searchQuery}” · ${count} 个结果`
      : `${state.name || '文件'} · ${count} 项`;

    // 按钮状态
    $('#modeBtn').classList.toggle('is-on', state.mode === 'grid');
    $('#selBtn').classList.toggle('is-on', state.selectMode);
    $('#modeBtn').querySelector('span').textContent = state.mode === 'grid' ? '列表' : '网格';

    renderList();
  }

  function renderList() {
    const list = state.list;
    const box = $('#fileList');
    const anim = state.animateList;
    state.animateList = false;
    box.className = 'filelist' + (state.mode === 'grid' ? ' grid' : '') + (anim ? ' anim' : '');

    if (!list.length) {
      box.innerHTML = '';
      const empty = $('#emptyState');
      empty.classList.remove('hidden');
      empty.innerHTML = state.searching
        ? `${ico('search')}<b>没有找到匹配的文件</b><span>换个关键词试试</span>`
        : `${ico('folderOpen')}<b>这里是空的</b><span>${state.writable ? '点「传文件」把手机里的内容放进来' : '换一个文件夹看看'}</span>`;
      $('#listFoot').textContent = '';
      return;
    }
    $('#emptyState').classList.add('hidden');

    const wide = state.mode === 'grid' ? 400 : 160;
    box.innerHTML = list.map((e, i) => {
      const selected = state.sel.has(e.path);
      let thumb = ico(KIND_ICON[e.kind] || 'file');
      if (e.kind === 'image' && state.session && state.session.thumb_available) {
        thumb = `<img loading="lazy" alt="" src="/api/thumb?w=${wide}&path=${encodeURIComponent(e.path)}" onerror="this.remove()">`;
      }
      const meta = e.dir
        ? (state.searching ? esc(e.rel || '文件夹') : '文件夹')
        : `${fmtSize(e.size)}<i>·</i>${fmtTime(e.mtime)}`;
      return `<div class="row${selected ? ' is-sel' : ''}" data-i="${i}" style="--d:${Math.min(i, 18) * 16}ms">
        ${state.selectMode ? `<span class="check">${ico('check')}</span>` : ''}
        <div class="thumb k-${e.kind}">${thumb}</div>
        <div class="row-body">
          <div class="row-name">${esc(e.name)}</div>
          <div class="row-meta">${meta}</div>
        </div>
        <div class="row-side">${e.dir ? ico('chevron') : ''}</div>
      </div>`;
    }).join('');

    $('#listFoot').textContent = state.searching
      ? '最多显示 300 条结果'
      : `${list.length} 个项目 · 点击文件夹进入，点击文件预览`;
  }

  function toggleSel(entry) {
    if (state.sel.has(entry.path)) state.sel.delete(entry.path);
    else state.sel.set(entry.path, entry);
    updateSelBar();
    renderList();
  }

  function updateSelBar() {
    const n = state.sel.size;
    $('#selCount').textContent = String(n);
    $('#selbar').hidden = !state.selectMode;
    $$('.row').forEach((row) => {
      const i = Number(row.dataset.i);
      const e = state.list[i];
      if (e) row.classList.toggle('is-sel', state.sel.has(e.path));
    });
    if (state.selectMode && n === 0) $('#selCount').textContent = '0';
  }

  function exitSelect() {
    state.selectMode = false;
    state.sel.clear();
    $('#selbar').hidden = true;
    $('#selBtn').classList.remove('is-on');
    renderList();
  }

  /* ---------------- 打开 / 预览 ---------------- */

  async function openEntry(entry) {
    if (entry.dir) { await loadList(entry.path); return; }
    const kind = entry.kind;
    if (['image', 'video', 'audio', 'pdf', 'text'].includes(kind)) {
      showPreview(entry);
    } else {
      // BUG-04：不能预览的类型不再「单击即静默下载」，先给操作单让用户明确选择
      showUnknownSheet(entry);
    }
  }

  /** 无法预览的文件：显示大小 + 下载 / 分享 / 取消，避免误触和重复下载。 */
  function showUnknownSheet(e) {
    state.sheetEntry = e;
    sheetFrame(`
      <div class="sheet-head">
        <div class="thumb k-${e.kind}">${ico(KIND_ICON[e.kind] || 'file')}</div>
        <div class="sheet-title">
          <b>${esc(e.name)}</b>
          <span>${fmtSize(e.size)} · ${fmtTime(e.mtime)}</span>
        </div>
        <button class="icon-btn" data-act="closeSheet">${ico('close')}</button>
      </div>
      <div class="sheet-body">
        <div class="empty">
          ${ico('file')}
          <b>这种文件无法预览</b>
          <span>可以下载到手机，或生成扫码分享链接。</span>
        </div>
      </div>
      <div class="sheet-actions">
        <button class="btn btn-primary" data-act="sheetDownload">${ico('down')}下载</button>
        <button class="btn" data-act="sheetShare">${ico('qr')}分享</button>
        <button class="btn btn-plain" data-act="closeSheet">取消</button>
      </div>
    `);
  }

  function sheetFrame(inner) {
    const card = $('#sheetCard');
    card.innerHTML = `<div class="sheet-grab"></div>${inner}`;
    $('#sheet').hidden = false;
    document.body.style.overflow = 'hidden';
  }

  function closeSheet() {
    $('#sheet').hidden = true;
    $('#sheetCard').innerHTML = '';
    document.body.style.overflow = '';
    state.sheetEntry = null;
  }

  function showPreview(e) {
    state.sheetEntry = e;
    const inline = `/api/download?inline=1&path=${encodeURIComponent(e.path)}`;
    const isEmpty = !e.size;
    let body = '';
    if (isEmpty) {
      // BUG-10：0 字节文件给出明确空态，而不是一个空白框
      body = `<div class="empty">${ico('file')}<b>文件为空（0 B）</b><span>这个文件没有任何内容可以预览。</span></div>`;
    } else if (e.kind === 'image') body = `<img class="preview" src="${inline}" alt="">`;
    else if (e.kind === 'video') body = `<video src="${inline}" controls playsinline preload="metadata"></video>`;
    else if (e.kind === 'audio') body = `<audio src="${inline}" controls></audio>`;
    else if (e.kind === 'pdf') body = `<iframe src="${inline}" title="PDF"></iframe>`;
    else body = `<pre id="textPreview">正在读取…</pre>`;

    sheetFrame(`
      <div class="sheet-head">
        <div class="thumb k-${e.kind}">${ico(KIND_ICON[e.kind] || 'file')}</div>
        <div class="sheet-title">
          <b>${esc(e.name)}</b>
          <span>${fmtSize(e.size)} · ${fmtTime(e.mtime)}</span>
        </div>
        <button class="icon-btn" data-act="closeSheet">${ico('close')}</button>
      </div>
      <div class="sheet-body">${body}</div>
      <div class="sheet-actions">
        <button class="btn btn-primary" data-act="sheetDownload">${ico('down')}下载</button>
        <button class="btn" data-act="sheetShare" ${isEmpty ? 'disabled title="空文件无需分享"' : ''}>${ico('qr')}分享</button>
        ${state.session && state.session.local ? `<button class="btn" data-act="sheetOpenLocal">${ico('folderOpen')}在电脑打开</button>` : ''}
        <button class="btn btn-plain" data-act="closeSheet">关闭</button>
      </div>
    `);

    if (!isEmpty && e.kind === 'text') {
      api(`/api/preview?path=${encodeURIComponent(e.path)}`).then((d) => {
        const el = $('#textPreview');
        if (el) el.textContent = d.text + (d.truncated ? '\n\n…（仅显示前 256 KB）' : '');
      }).catch((err) => {
        const el = $('#textPreview');
        if (el) el.textContent = `无法预览：${err.message}`;
      });
    }
  }

  async function shareEntry(e) {
    if (!e.size) { toast('空文件无需分享', true); return; }
    try {
      const body = { path: e.path };
      if (state.selectedIp) body.host = state.selectedIp;  // 用选中的网络生成链接
      const d = await api('/api/share', { method: 'POST', body: JSON.stringify(body) });
      state.sheetEntry = e;
      const qrAvailable = state.session && state.session.qr_available;
      sheetFrame(`
        <div class="sheet-head">
          <div class="sheet-title"><b>扫码下载</b><span>${esc(e.name)}</span></div>
          <button class="icon-btn" data-act="closeSheet">${ico('close')}</button>
        </div>
        <div class="share-box">
          ${qrAvailable ? `<img src="/qr.png?text=${encodeURIComponent(d.url)}" alt="二维码">` : ''}
          <div class="share-url">${esc(d.url)}</div>
          <div class="sheet-actions" style="margin-top:14px">
            <button class="btn btn-primary" data-act="copyShared">复制链接</button>
            <button class="btn" data-act="closeSheet">关闭</button>
          </div>
          <p class="muted" style="font-size:12px;margin:12px 0 0">
            手机连同一 Wi-Fi，扫码即可直接下载这个文件。链接 24 小时内有效。
          </p>
        </div>
      `);
      state.sharedUrl = d.url;
    } catch (err) {
      toast(err.message, true);
    }
  }

  /* ---------------- 上传 ---------------- */

  const queue = [];
  let activeCount = 0;
  let qRaf = 0;

  function scheduleQueueRender() {
    if (qRaf) return;
    qRaf = requestAnimationFrame(() => { qRaf = 0; renderQueue(); });
  }

  function renderQueue() {
    const box = $('#queue');
    const section = $('#queueSection');
    if (!state.uploads.length) { section.hidden = true; box.innerHTML = ''; return; }
    section.hidden = false;

    const retryBar = failedTasks.length
      ? `<div class="queue-retry"><span>${failedTasks.length} 个失败</span>` +
        `<button class="btn btn-sm" data-act="retryFailed">重试失败项</button></div>`
      : '';

    box.innerHTML = retryBar + state.uploads.slice(0, 40).map((u) => {
      const pct = u.total ? Math.min(100, (u.loaded / u.total) * 100) : (u.status === 'done' ? 100 : 0);
      const label = u.status === 'done' ? '已完成'
        : u.status === 'error' ? (u.error || '失败')
        : u.status === 'cancel' ? '已取消'
        : u.status === 'wait' ? '等待中'
        : `${fmtSize(u.speed || 0)}/s`;
      return `<div class="qitem ${u.status === 'done' ? 'ok' : ''} ${u.status === 'error' ? 'err' : ''}" data-status="${u.status}">
        <div class="qitem-top">
          <div class="qitem-name">${esc(u.name)}</div>
          <div class="qitem-pct">${u.status === 'up' ? Math.round(pct) + '%' : esc(label)}</div>
        </div>
        <div class="bar"><i style="width:${pct}%"></i></div>
        <div class="qitem-sub"><span>${fmtSize(u.loaded)} / ${fmtSize(u.total)}</span><span>${u.status === 'up' ? esc(label) : ''}</span></div>
      </div>`;
    }).join('');
  }

  function enqueue(file, rel) {
    const item = {
      id: uid(), name: file.name, size: file.size, loaded: 0, total: file.size,
      status: 'wait', speed: 0, rel: rel || '',
    };
    state.uploads.unshift(item);
    queue.push({ file, rel: rel || '', item });
    renderQueue();
    pump();
  }

  function pump() {
    while (activeCount < 2 && queue.length) {
      const task = queue.shift();
      activeCount += 1;
      runUpload(task).finally(() => { activeCount -= 1; pump(); });
    }
  }

  function runUpload({ file, rel, item }) {
    return new Promise((resolve) => {
      const xhr = new XMLHttpRequest();
      item.xhr = xhr;
      item.status = 'up';

      const qs = new URLSearchParams({ name: file.name });
      if (rel) qs.set('rel', rel);
      xhr.open('POST', `/api/upload?${qs.toString()}`);
      xhr.setRequestHeader('X-File-Mtime', String(file.lastModified || Date.now()));

      let lastT = performance.now();
      let lastB = 0;
      xhr.upload.onprogress = (ev) => {
        item.loaded = ev.loaded;
        if (ev.total) item.total = ev.total;
        const now = performance.now();
        const dt = (now - lastT) / 1000;
        if (dt > 0.25) {
          item.speed = Math.max(0, (ev.loaded - lastB) / dt);
          lastT = now;
          lastB = ev.loaded;
        }
        scheduleQueueRender();
      };

      xhr.onload = () => {
        let data = {};
        try { data = JSON.parse(xhr.responseText || '{}'); } catch (e) { /* ignore */ }
        if (xhr.status === 200 && data.ok) {
          item.status = 'done';
          item.loaded = item.total = item.size;
          item.speed = 0;
          toast(`已保存：${data.name || file.name}`);
          finishOne(true);
          vizBurst();
          loadTransfers();
          if (state.view === 'browse' && state.path &&
              state.path.toLowerCase().startsWith(state.recvDir.toLowerCase())) {
            loadList(state.path).catch(() => {});
          }
        } else {
          item.status = 'error';
          item.error = data.error || `失败 ${xhr.status}`;
          failedTasks.push({ file, rel, item });
          finishOne(false);
          toast(item.error, true);
        }
        scheduleQueueRender();
        resolve();
      };
      xhr.onerror = () => {
        item.status = 'error';
        item.error = '网络中断';
        failedTasks.push({ file, rel, item });
        finishOne(false);
        scheduleQueueRender();
        resolve();
      };
      xhr.onabort = () => {
        item.status = 'cancel';
        scheduleQueueRender();
        resolve();
      };
      xhr.send(file);
    });
  }

  function handleFiles(fileList) {
    const files = Array.from(fileList || []);
    if (!files.length) return;
    files.forEach((f) => enqueue(f, f.webkitRelativePath || ''));
    toast(`开始传输 ${files.length} 个文件`);
  }

  /* ---------------- 文件夹传输 ---------------- */

  // 一次文件夹传输的统计：用于结束时给出「文件 / 空目录 / 失败」汇总
  const batch = { active: false, total: 0, done: 0, failed: 0, emptyDirs: 0 };
  let failedTasks = [];

  function startBatch() {
    batch.active = true;
    batch.total = 0;
    batch.done = 0;
    batch.failed = 0;
    batch.emptyDirs = 0;
  }

  function finishOne(ok) {
    if (!batch.active) return;
    batch.total = Math.max(batch.total, batch.done + batch.failed + 1);
    if (ok) batch.done++; else batch.failed++;
    if (batch.done + batch.failed >= batch.total && !queue.length && activeCount === 0) {
      batch.active = false;
      const parts = [`完成 ${batch.done} 个文件`];
      if (batch.emptyDirs) parts.push(`${batch.emptyDirs} 个空文件夹`);
      if (batch.failed) parts.push(`失败 ${batch.failed}`);
      toast(parts.join('，'), batch.failed > 0);
    }
  }

  async function createRemoteDir(rel) {
    try {
      await api('/api/mkdir', { method: 'POST', body: JSON.stringify({ rel }) });
      batch.emptyDirs++;
    } catch (e) { /* 文件上传时会自动建目录，这里失败不阻塞 */ }
  }

  // 桌面 Chromium 的目录选择器：能拿到完整的树（含空文件夹）
  async function collectDirHandle(handle, prefix) {
    const rel = prefix ? `${prefix}/${handle.name}` : handle.name;
    let children = 0;
    for await (const [name, child] of handle.entries()) {
      children++;
      if (child.kind === 'directory') {
        await collectDirHandle(child, rel);
      } else {
        const file = await child.getFile();
        batch.total++;
        enqueue(file, `${rel}/${name}`);
      }
    }
    if (!children) await createRemoteDir(rel);
  }

  async function pickFolder() {
    if (window.showDirectoryPicker) {
      try {
        const handle = await window.showDirectoryPicker({ mode: 'read' });
        startBatch();
        await collectDirHandle(handle, '');
        if (!batch.total && !batch.emptyDirs) { batch.active = false; toast('这个文件夹是空的'); }
        return;
      } catch (e) {
        if (e && e.name === 'AbortError') return;   // 用户取消
      }
    }
    // 回退：webkitdirectory（拿不到空文件夹）
    startBatch();
    $('#folderInput').click();
  }

  // 拖拽进来的文件夹：webkitGetAsEntry 能枚举到空目录
  function walkEntry(entry, prefix) {
    return new Promise((resolve) => {
      if (entry.isFile) {
        entry.file((f) => {
          batch.total++;
          enqueue(f, prefix ? `${prefix}/${entry.name}` : entry.name);
          resolve();
        }, () => resolve());
        return;
      }
      const rel = prefix ? `${prefix}/${entry.name}` : entry.name;
      const reader = entry.createReader();
      let count = 0;
      const readBatch = () => {
        reader.readEntries(async (entries) => {
          if (!entries.length) {
            if (!count) await createRemoteDir(rel);
            resolve();
            return;
          }
          count += entries.length;
          for (const e of entries) await walkEntry(e, rel);
          readBatch();
        }, () => resolve());
      };
      readBatch();
    });
  }

  async function handleDropItems(dt) {
    const entries = Array.from(dt.items || [])
      .map((it) => (it.webkitGetAsEntry ? it.webkitGetAsEntry() : null))
      .filter(Boolean);
    if (!entries.length) { handleFiles(dt.files); return; }
    startBatch();
    for (const en of entries) await walkEntry(en, '');
    if (!batch.total && !batch.emptyDirs) { batch.active = false; toast('没有可传输的文件'); }
  }

  function retryFailed() {
    if (!failedTasks.length) return;
    const tasks = failedTasks;
    failedTasks = [];
    startBatch();
    tasks.forEach((t) => {
      t.item.status = 'wait';
      t.item.loaded = 0;
      queue.push(t);
      batch.total++;
    });
    renderQueue();
    pump();
  }

  async function loadTransfers() {
    try {
      const d = await api('/api/transfers');
      const box = $('#transfers');
      if (!d.items.length) {
        box.innerHTML = `<div class="empty" style="padding:32px 10px">${ico('share')}<b>还没有传输记录</b><span>传一次文件就会出现在这里</span></div>`;
        return;
      }
      box.innerHTML = d.items.slice(0, 20).map((t) => `
        <div class="titem">
          <div class="badge ${t.direction === 'up' ? 'up' : 'down'}">${ico(t.direction === 'up' ? 'up' : 'down')}</div>
          <div class="titem-body">
            <div class="titem-name">${esc(t.name)}</div>
            <div class="titem-sub">${t.direction === 'up' ? '手机 → 电脑' : '电脑 → 手机'} · ${fmtSize(t.size)}${t.speed ? ' · ' + fmtSize(t.speed) + '/s' : ''}</div>
          </div>
          <div class="titem-sub">${fmtTime(t.ts)}</div>
        </div>`).join('');
    } catch (e) { /* ignore */ }
  }

  /* ---------------- 剪贴板 ---------------- */

  function startClipPoll() {
    if (state.clipTimer) clearInterval(state.clipTimer);
    state.clipTimer = setInterval(() => {
      if (state.view === 'clip' && document.visibilityState === 'visible') loadClip();
    }, 5000);
  }

  async function loadClip() {
    try {
      const d = await api('/api/clip');
      const box = $('#clipList');
      if (!d.items.length) {
        box.innerHTML = `<div class="empty" style="padding:28px 10px">${ico('copy')}<b>还没有内容</b><span>在上面输入文字，另一端马上能看到</span></div>`;
        return;
      }
      box.innerHTML = d.items.map((c) => `
        <div class="clip-item">
          <p>${esc(c.text)}</p>
          <div class="meta">
            <span>${esc(c.from || '')} · ${fmtTime(c.ts)}</span>
            <div class="meta-actions">
              <button class="link-btn" data-act="clipCopy" data-id="${c.id}">复制</button>
              <button class="link-btn" data-act="clipDelete" data-id="${c.id}">删除</button>
            </div>
          </div>
        </div>`).join('');
    } catch (e) { /* ignore */ }
  }

  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      toast('已复制');
      return;
    } catch (e) { /* 回退 */ }
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.cssText = 'position:fixed;top:0;left:0;opacity:0';
    document.body.appendChild(ta);
    ta.focus();
    ta.select();
    try {
      document.execCommand('copy');
      toast('已复制');
    } catch (err) {
      toast('复制失败，请手动选择', true);
    }
    ta.remove();
  }

  /* ---------------- 我的 / 连接信息 ---------------- */

  function loadNetworks() {
    if (!state.session || !state.session.local) return;
    api('/api/networks').then((d) => {
      if (d && d.networks) {
        state.networks = d.networks;
        // 之前选中的 IP 已经不存在了，就清掉
        if (state.selectedIp && !d.networks.some((n) => n.ip === state.selectedIp)) {
          state.selectedIp = '';
          localStorage.removeItem('dl.ip');
        }
        renderAbout();
      }
    }).catch(() => {});
  }

  // 轮询已接入设备，有新设备连上时提示
  let knownPeerIps = new Set();
  let peerSig = '';

  function pollPeers() {
    if (!state.session || !state.session.local) { setTimeout(pollPeers, 3000); return; }
    // 后台标签页降频，避免长期占用 CPU
    if (document.hidden) { setTimeout(pollPeers, 8000); return; }
    api('/api/peers').then((d) => {
      const peers = (d && d.peers) || [];
      state.peers = peers;
      for (const p of peers) {
        if (!knownPeerIps.has(p.ip)) {
          toast('有设备连上来了');
          break;
        }
      }
      knownPeerIps = new Set(peers.map((p) => p.ip));
      const badge = $('#peerCount');
      if (badge) badge.textContent = peers.length ? `已接入 ${peers.length} 台` : '';
      // 设备列表没变化就不重绘，避免 2.5s 一次的 innerHTML 重建（会打断勾选、触发重排）
      const sig = peers.map((p) => p.ip + '|' + (p.name || '')).join(',');
      if (sig !== peerSig) { peerSig = sig; renderSendDevices(); }
    }).catch(() => {});
    setTimeout(pollPeers, 2500);
  }

  /* ---------------- 进行中的发送（服务端实时速率） ---------------- */

  let liveItems = [];

  function renderLive() {
    const section = $('#liveSection');
    const box = $('#liveBox');
    if (!section || !box) return;
    if (!liveItems.length) { section.hidden = true; box.innerHTML = ''; return; }
    section.hidden = false;
    box.innerHTML = liveItems.map((t) => {
      const pct = t.total ? Math.min(100, (t.sent / t.total) * 100) : 0;
      return `<div class="qitem" data-status="up">
        <div class="qitem-top">
          <div class="qitem-name">${esc(t.name)}</div>
          <div class="qitem-pct">${fmtSize(t.speed || 0)}/s</div>
        </div>
        <div class="bar"><i style="width:${pct}%"></i></div>
        <div class="qitem-sub"><span>${fmtSize(t.sent || 0)} / ${fmtSize(t.total || 0)}</span><span>${esc(t.peer || '')}</span></div>
      </div>`;
    }).join('');
  }

  function pollLive() {
    if (!state.authed) { setTimeout(pollLive, 1500); return; }
    if (document.hidden) { setTimeout(pollLive, 2000); return; }
    api('/api/live').then((d) => {
      const items = (d && d.items) || [];
      const changed = items.length !== liveItems.length ||
        items.some((it, i) => !liveItems[i] || liveItems[i].sent !== it.sent);
      liveItems = items;
      if (changed) renderLive();
      // 有其它设备在拉文件时，也让传输动画/速率跑起来
      if (items.length && state.view === 'send') {
        const sp = items.reduce((a, it) => a + (it.speed || 0), 0);
        if (sp > viz.speed) viz.speed = sp;
      }
    }).catch(() => {}).then(() => setTimeout(pollLive, 900));
  }

  /* ---------------- 电脑端：发送到手机 ---------------- */

  function renderSendPanel() {
    const local = !!(state.session && state.session.local);
    const panel = $('#sendPanel');
    const drop = $('#dropzone');
    if (panel) panel.hidden = !local;
    if (drop) drop.hidden = local;  // 电脑端隐藏「上传到电脑」，显示「发送到手机」
    const hero = document.querySelector('#view-send .hero h1');
    if (hero) hero.textContent = local ? '把文件发给手机' : '把文件丢过来';
    if (local) renderSendDevices();
  }

  function renderSendDevices() {
    const list = $('#sendDeviceList');
    const hint = $('#sendDeviceHint');
    if (!list) return;
    const peers = state.peers || [];
    if (!peers.length) {
      list.innerHTML = '';
      if (hint) hint.textContent = '还没有手机连接，请先用手机扫码连接。';
      return;
    }
    if (hint) hint.textContent = `已连接 ${peers.length} 台设备，勾选要发送的目标。`;
    // 轮询会周期性调用本函数，必须保留用户已勾选的目标，否则选择会被清空
    const checked = new Set(
      Array.from(list.querySelectorAll('.dev-check:checked')).map((b) => b.value));
    const allBox = $('#sendAll');
    const allChecked = allBox ? allBox.checked : true;
    list.innerHTML = peers.map((p) => {
      const on = checked.size ? checked.has(p.ip) : allChecked;
      return `
      <label class="dev-item">
        <input type="checkbox" class="dev-check" value="${esc(p.ip)}" ${on ? 'checked' : ''}>
        <span>${esc(p.name || p.ip)}</span>
        <span class="dev-sub">${esc(p.ip)}</span>
      </label>`;
    }).join('');
    // 全部设备勾选联动
    const all = $('#sendAll');
    const boxes = () => Array.from(list.querySelectorAll('.dev-check'));
    if (all) {
      all.onchange = () => {
        boxes().forEach((b) => { b.checked = all.checked; });
      };
    }
  }

  function renderSendFiles() {
    const box = $('#sendFileList');
    if (!box) return;
    if (!state.sendFiles.length) {
      box.innerHTML = '<p class="muted" style="font-size:12px">还没有选择文件</p>';
      return;
    }
    box.innerHTML = state.sendFiles.map((f, i) => `
      <div class="send-file">
        <span class="sf-name">${esc(f.name)}</span>
        <span class="sf-size">${fmtSize(f.size)}</span>
        <span class="sf-x" data-act="removeSendFile" data-i="${i}">✕</span>
      </div>`).join('');
  }

  async function doSend() {
    const files = state.sendFiles;
    if (!files.length) { toast('请先选择文件', true); return; }
    // 目标
    const all = $('#sendAll');
    let targets;
    if (all && all.checked) {
      targets = 'all';
    } else {
      const boxes = Array.from(document.querySelectorAll('#sendDeviceList .dev-check:checked'));
      targets = boxes.map((b) => b.value).join(',');
      if (!targets) { toast('请选择至少一台设备', true); return; }
    }

    // 发送文件接入上传队列，复用传输中心的速率与粒子动画
    files.forEach((f) => {
      const item = {
        id: uid(), name: f.name, size: f.size, loaded: 0, total: f.size,
        status: 'up', speed: 0,
      };
      state.uploads.unshift(item);
      sendOne(f, targets, item);
    });
    renderQueue();
    state.sendFiles = [];
    renderSendFiles();
    toast(`正在发送 ${files.length} 个文件…`);
  }

  function sendOne(file, targets, item) {
    const xhr = new XMLHttpRequest();
    const qs = new URLSearchParams({ name: file.name, targets });
    xhr.open('POST', `/api/send?${qs.toString()}`);

    let lastT = performance.now();
    let lastB = 0;
    xhr.upload.onprogress = (ev) => {
      item.loaded = ev.loaded;
      if (ev.total) item.total = ev.total;
      const now = performance.now();
      const dt = (now - lastT) / 1000;
      if (dt > 0.25) {
        item.speed = Math.max(0, (ev.loaded - lastB) / dt);
        lastT = now;
        lastB = ev.loaded;
      }
      scheduleQueueRender();
    };

    xhr.onload = () => {
      let data = {};
      try { data = JSON.parse(xhr.responseText || '{}'); } catch (e) { /* ignore */ }
      if (xhr.status === 200 && data.ok) {
        item.status = 'done';
        item.loaded = item.total = item.size;
        item.speed = 0;
        toast(`已发送：${data.name || file.name}`);
        vizBurst();
        loadTransfers();
      } else {
        item.status = 'error';
        item.error = data.error || `失败 ${xhr.status}`;
        toast(item.error, true);
      }
      scheduleQueueRender();
    };
    xhr.onerror = () => {
      item.status = 'error';
      item.error = '网络中断';
      scheduleQueueRender();
    };
    xhr.onabort = () => {
      item.status = 'cancel';
      scheduleQueueRender();
    };
    xhr.send(file);
  }

  /* ---------------- 手机端：自动接收 ---------------- */

  let knownInbox = new Set();
  function pollInbox() {
    if (!state.session || state.session.local) { setTimeout(pollInbox, 3000); return; }
    api('/api/inbox').then((d) => {
      const items = (d && d.items) || [];
      for (const it of items) {
        if (knownInbox.has(it.id)) continue;
        knownInbox.add(it.id);
        toast(`收到来自电脑的文件：${it.name}`);
        // 自动下载
        const a = document.createElement('a');
        a.href = it.url;
        a.download = it.name;
        document.body.appendChild(a);
        a.click();
        a.remove();
        // 确认接收
        api('/api/inbox/ack', { method: 'POST', body: JSON.stringify({ id: it.id }) }).catch(() => {});
      }
    }).catch(() => {});
    setTimeout(pollInbox, 2500);
  }

  const isAndroid = () => !!(state.session && state.session.platform === 'android');

  function renderAbout() {
    const s = state.session || {};
    const urls = s.lan_urls || [];
    const port = location.port || '8848';
    const android = isAndroid();
    const mainUrl = state.selectedIp
      ? `http://${state.selectedIp}:${port}`
      : (urls[0] || (location.origin.includes('127.0.0.1') ? '' : location.origin));
    // 安卓端二维码带上配对码，扫码即可自动配对（扫码即连）
    const qrText = android && s.pin ? `${mainUrl}?pin=${s.pin}` : mainUrl;
    const qrAvailable = s.qr_available && mainUrl;

    const html = [
      `<div class="card">
        <h3>${android ? '让对方设备连接' : '手机连接方式'}</h3>
        <div class="qr-wrap">
          ${qrAvailable ? `<img src="/qr.png?text=${encodeURIComponent(qrText)}" alt="连接二维码">` : ''}
          <div class="qr-side">
            <div class="url-pill">${esc(mainUrl || '未检测到局域网地址')}</div>
            <p class="muted" style="font-size:12.5px;margin:10px 0 0">
              ${android
                ? '让对方设备连同一个 Wi-Fi 或你的热点，扫二维码或打开上面的地址。'
                : '电脑和手机连同一个 Wi-Fi，手机浏览器打开上面的地址即可。'}
              ${qrAvailable ? '也可以直接扫左边的二维码。' : ''}
            </p>
            <div style="display:flex;gap:8px;margin-top:12px;flex-wrap:wrap">
              ${mainUrl ? `<button class="btn btn-sm" data-act="copyUrl">复制地址</button>` : ''}
              ${android ? `<button class="btn btn-sm btn-primary" data-act="scanConnect">扫码连接</button>` : ''}
            </div>
          </div>
        </div>
      </div>`,
    ];

    // 多网络切换（有多个网卡/IP 时，选对对方能连的那个；电脑端和手机端都显示）
    if (s.local && state.networks.length > 1) {
      const opts = state.networks.map((n) =>
        `<option value="${esc(n.ip)}" ${n.ip === state.selectedIp ? 'selected' : ''}>${esc(n.ip)}（${esc(n.alias)}）</option>`).join('');
      html.push(`<div class="card">
        <h3>切换网络</h3>
        <p class="muted" style="font-size:12.5px;margin:0 0 10px">
          ${android ? '有多个网络地址时，二维码会跟随这里选中的网络。对方连不上就换一个试试。' : '电脑有多个网卡时，二维码和地址会跟随这里选中的网络。手机连不上就换一个试试。'}
        </p>
        <div class="searchbar" style="padding:0">
          <select id="netSelect" style="width:100%;padding:12px;border:none;background:transparent;font-size:14px;color:var(--text)">
            <option value="" ${!state.selectedIp ? 'selected' : ''}>自动（推荐）</option>
            ${opts}
          </select>
        </div>
      </div>`);
    }

    if (s.local && s.pin) {
      html.push(`<div class="card">
        <h3>配对码</h3>
        <div class="pin-big">${esc(s.pin)}</div>
        <p class="muted" style="font-size:12.5px;margin:8px 0 0">
          手机第一次打开网页时需要输入它。只保存在本次运行中，重启电脑端后会更新。
        </p>
      </div>`);
    }

    html.push(`<div class="card">
      <h3>设备信息</h3>
      <div class="kv"><span>电脑名称</span><b>${esc(s.device || '-')}</b></div>
      <div class="kv"><span>接收目录</span><b class="mono">${esc(s.recv_dir || '-')}</b></div>
      <div class="kv"><span>服务版本</span><b>${esc(s.app || '')} v${esc(s.version || '')}</b></div>
      <div class="kv"><span>已运行</span><b>${Math.floor((s.uptime || 0) / 60)} 分钟</b></div>
      <div class="kv"><span>当前设备</span><b>${s.local ? '电脑本机' : '手机 / 其他设备'}</b></div>
      ${s.local ? `<div class="kv"><span>已接入设备</span><b>${state.peers.length ? `${state.peers.length} 台` : '暂无'}</b></div>` : ''}
      <div class="kv"><span>使用提示</span><b>${tipsEnabled() ? '显示中' : '已关闭'}</b></div>
      <div class="sheet-actions" style="margin-top:10px">
        <button class="btn" data-act="toggleTips">${tipsEnabled() ? '关闭使用提示' : '重新显示使用提示'}</button>
      </div>
    </div>`);

    if (s.local) {
      if (android) {
        // 浏览范围：授权「所有文件访问」后可以浏览整机（下载、相册、文档…）
        const allFiles = !!s.all_files;
        html.push(`<div class="card">
          <h3>浏览范围</h3>
          <div class="kv"><span>当前可以浏览</span>
            <b style="color:${allFiles ? 'var(--ok)' : 'var(--muted)'}">${allFiles ? '手机全部文件' : '仅 DropLink 目录'}</b>
          </div>
          <p class="muted" style="font-size:12.5px;margin:8px 0 0">
            ${allFiles
              ? '已获得「所有文件访问」权限，「浏览」页可以直接翻看下载、相册、文档等目录。'
              : '想让对方在「浏览」页看到手机的下载、相册、文档？点下面的按钮，在系统设置里允许「所有文件访问」即可。'}
          </p>
          ${allFiles ? '' : '<button class="btn btn-primary" style="margin-top:10px" data-act="openAllFiles">开启全部文件访问</button>'}
        </div>`);
        html.push(`<div class="card">
          <h3>接收目录</h3>
          <p class="muted mono" style="font-size:12px;margin:0;word-break:break-all">${esc(s.recv_dir || '')}</p>
          <p class="muted" style="font-size:12px;margin:8px 0 0">对方传来的文件会保存在这里。</p>
        </div>`);
      } else {
        html.push(`<div class="card">
          <h3>接收设置</h3>
          <p class="muted" style="font-size:12.5px;margin:0 0 10px">
            手机传来的文件会保存到这里。可以改成任意文件夹。
          </p>
          <div class="searchbar" style="padding:0">
            <input id="recvInput" value="${esc(s.recv_dir || '')}" spellcheck="false">
          </div>
          <div class="sheet-actions" style="margin-top:10px">
            <button class="btn btn-primary" data-act="saveRecv">保存目录</button>
            <button class="btn" data-act="openRecv">打开文件夹</button>
          </div>
        </div>`);
      }

      if (android) {
        html.push(`<div class="card">
          <h3>移动热点</h3>
          <p class="muted" style="font-size:12.5px;margin:0 0 10px">
            没有 Wi-Fi 时，开启热点让另一台设备连上来即可互传，不耗流量。
          </p>
          <div class="sheet-actions">
            <button class="btn btn-primary" data-act="toggleHotspot">开启热点</button>
            <button class="btn" data-act="diag">检测连接</button>
          </div>
        </div>`);
      } else {
        html.push(`<div class="card">
          <h3>防火墙</h3>
          <p class="muted" style="font-size:12.5px;margin:0 0 10px">
            手机连不上电脑，多半是 Windows 防火墙拦了入站连接。点「设置防火墙规则」放行
            （会弹一次系统授权确认，点「是」即可）；不用了可以随时「删除规则」。
          </p>
          <div class="sheet-actions">
            <button class="btn btn-primary" data-act="firewallSet">设置防火墙规则</button>
            <button class="btn" data-act="firewallRemove">删除规则</button>
          </div>
          <div class="sheet-actions" style="margin-top:8px">
            <button class="btn" data-act="diag">检测连接问题</button>
          </div>
        </div>`);
      }
    } else {
      html.push(`<div class="card">
        <h3>在手机上使用</h3>
        <ol class="steps">
          <li>点浏览器菜单里的「<b>添加到主屏幕</b>」，就能像 App 一样打开。</li>
          <li>「浏览」里可以看电脑上的文件，点开即预览。</li>
          <li>「传文件」把手机照片、视频、文档传到电脑。</li>
          <li>「剪贴板」可以在两端互发文字和链接。</li>
        </ol>
      </div>`);
    }

    html.push(`<div class="card">
      <h3>为什么不消耗流量</h3>
      <ol class="steps">
        <li>文件只在你家的局域网里传输，<b>不经过互联网、不上传云端</b>。</li>
        <li>没有 Wi-Fi 时，可以让电脑开启「<b>移动热点</b>」，手机连上电脑的热点即可（同样不走流量）。</li>
        <li>传输速度取决于路由器，一般 10–60 MB/s，比网盘快很多。</li>
      </ol>
    </div>`);

    if (s.local) {
      html.push(`<div class="card">
        <h3>第一次用？</h3>
        <p class="muted" style="font-size:12.5px;margin:0 0 10px">
          花 30 秒看一遍图文引导，手机马上就能连上来。
        </p>
        <button class="btn" data-act="showGuide">查看使用引导</button>
      </div>`);
    }

    $('#aboutBody').innerHTML = html.join('');
  }

  /* ---------------- 使用引导 ---------------- */

  const guideState = { step: 0, count: 3 };

  function showGuide() {
    guideState.step = 0;
    $('#guide').hidden = false;
    document.body.style.overflow = 'hidden';
    guideRender();
  }

  function hideGuide() {
    $('#guide').hidden = true;
    document.body.style.overflow = '';
    localStorage.setItem(ONBOARD_KEY, '1');
  }

  function guideRender() {
    $$('#guide .guide-slide').forEach((s, i) =>
      s.classList.toggle('is-on', i === guideState.step));
    $$('#guideDots i').forEach((d, i) =>
      d.classList.toggle('is-on', i === guideState.step));
    const next = $('#guide .guide-actions .btn-primary');
    if (next) next.textContent = guideState.step === guideState.count - 1 ? '开始使用' : '下一步';
  }

  function guideStep(delta) {
    const n = guideState.step + delta;
    if (n >= guideState.count) { hideGuide(); return; }
    if (n < 0) return;
    guideState.step = n;
    guideRender();
  }

  /* ---------------- 连接检测 ---------------- */

  function showDiag(d) {
    const cats = { 0: '公用', 1: '专用', 2: '域' };
    const urls = (d.urls || []).map((u, i) =>
      `<div class="kv"><span>地址 ${i + 1}</span><b class="mono">${esc(u)}</b></div>`).join('');
    const prof = (d.profiles || []).map((p) =>
      `<div class="kv"><span>${esc(p.name)}</span><b>${cats[p.cat] || p.cat}</b></div>`).join('')
      || '<p class="muted" style="font-size:12px">未检测到网络配置</p>';
    const pub = (d.profiles || []).filter((p) => p.cat === 0);

    // 防火墙规则是否覆盖了当前网络类型
    const detail = d.fw_detail || {};
    const profStr = detail.profiles || '';
    const hasPub = (d.profiles || []).some((p) => p.cat === 0);
    const hasPriv = (d.profiles || []).some((p) => p.cat === 1);
    const coversAny = /任何|Any/.test(profStr);
    const coversPub = coversAny || /公用|Public/.test(profStr);
    const coversPriv = coversAny || /专用|Private/.test(profStr);
    const fwMismatch = (hasPub && !coversPub) || (hasPriv && !coversPriv);
    const android = isAndroid();

    const fwCard = android ? '' : `
        <div class="card">
          <h3>防火墙</h3>
          <div class="kv"><span>DropLink 放行规则</span>
            <b style="color:${d.fw_rule ? 'var(--ok)' : 'var(--danger)'}">${d.fw_rule ? '已添加' : '未添加'}</b>
          </div>
          ${detail.profiles ? `<div class="kv"><span>覆盖网络</span><b>${esc(detail.profiles)}</b></div>` : ''}
          ${detail.port ? `<div class="kv"><span>端口</span><b>${esc(detail.port)}</b></div>` : ''}
          ${fwMismatch
            ? '<p class="muted" style="font-size:12px;margin:8px 0 0;color:var(--danger)">规则没覆盖你当前的网络类型（网络是「公用」但规则只放了「专用」之类）。点下面的「设置防火墙规则」重新放行一次即可。</p>'
            : (d.fw_rule ? '<p class="muted" style="font-size:12px;margin:8px 0 0">规则配置正确，覆盖了当前网络。若还连不上，看下面的排查。</p>'
              : '<p class="muted" style="font-size:12px;margin:8px 0 0">点上面的「设置防火墙规则」即可放行。</p>')}
        </div>`;

    const netCard = android ? '' : `
        <div class="card">
          <h3>网络类型</h3>
          ${prof}
          ${pub.length ? '<p class="muted" style="font-size:12px;margin:8px 0 0">检测到「公用」网络：若手机连的是这个网络，放行规则可能对不上号。建议在系统设置里把它改成「专用网络」。</p>' : ''}
        </div>`;

    const hotspotCard = android ? `
        <div class="card">
          <h3>没有 Wi-Fi？</h3>
          <p class="muted" style="font-size:12.5px;margin:0 0 10px">
            让对方设备连你的<b>移动热点</b>，或两台设备都连同一个 Wi-Fi，即可互传。
          </p>
          <button class="btn btn-primary" data-act="toggleHotspot">开启热点</button>
        </div>` : `
        <div class="card">
          <h3>宿舍 / 校园网大概率连不上？</h3>
          <p class="muted" style="font-size:12.5px;margin:0 0 10px">
            宿舍和校园网几乎都开了「<b>AP 隔离 / 客户端隔离</b>」——同一 Wi-Fi 下设备之间互相看不到，
            防火墙和「专用网络」都解决不了它。最省事的办法是让<b>电脑开「移动热点」</b>，手机连电脑的热点，
            直接绕开宿舍路由器。
          </p>
          <button class="btn btn-primary" data-act="openHotspot">打开移动热点设置</button>
        </div>`;

    sheetFrame(`
      <div class="sheet-head">
        <div class="sheet-title">
          <b>连接检测</b>
          <span>按下面的结果逐项排查，对方就能连上</span>
        </div>
        <button class="icon-btn" data-act="closeSheet">${ico('close')}</button>
      </div>
      <div class="sheet-body">
        <div class="card">
          <h3>服务地址（对方浏览器打开这个）</h3>
          ${urls || '<p class="muted" style="font-size:12px">未检测到局域网地址</p>'}
          <p class="muted" style="font-size:12px;margin:8px 0 0">地址若不止一个，请逐个试；第一个通常是正确的。</p>
        </div>
        ${fwCard}
        ${netCard}
        ${hotspotCard}
        <div class="card">
          <h3>还连不上的话，按顺序查</h3>
          <ol class="steps">
            <li>确认两台设备连<b>同一个 Wi-Fi</b>（不要一个连 5G、一个连 2.4G 或访客）。</li>
            <li>关掉设备上的 <b>VPN / 代理 / 加速器</b>。</li>
            <li>设备若有多个网卡，换个地址试，或用热点。</li>
          </ol>
        </div>
      </div>
      <div class="sheet-actions">
        <button class="btn btn-primary" data-act="openHotspot">打开移动热点设置</button>
        <button class="btn btn-plain" data-act="closeSheet">关闭</button>
      </div>
    `);
  }

  /* ---------------- 事件绑定 ---------------- */

  document.addEventListener('click', async (ev) => {
    const navBtn = ev.target.closest('.nav-item[data-view]');
    if (navBtn) { switchView(navBtn.dataset.view); return; }

    const crumb = ev.target.closest('[data-crumb]');
    if (crumb) {
      const parts = crumbsFor(state.path);
      const c = parts[Number(crumb.dataset.crumb)];
      if (c) loadList(c.path).catch((e) => toast(e.message, true));
      return;
    }

    const root = ev.target.closest('[data-root]');
    if (root) {
      const r = state.roots[Number(root.dataset.root)];
      if (r) loadList(r.path).catch((e) => toast(e.message, true));
      return;
    }

    const row = ev.target.closest('.row[data-i]');
    if (row) {
      if (Date.now() - state.suppressClick < 700) return;
      const e = state.list[Number(row.dataset.i)];
      if (!e) return;
      if (state.selectMode) toggleSel(e);
      else openEntry(e).catch((err) => toast(err.message, true));
      return;
    }

    const actEl = ev.target.closest('[data-act]');
    if (!actEl) return;
    const act = actEl.dataset.act;

    switch (act) {
      case 'login': doLogin(); break;
      case 'theme': {
        const next = effectiveTheme() === 'dark' ? 'light' : 'dark';
        localStorage.setItem(THEME_KEY, next);
        applyTheme(next);
        break;
      }
      case 'refresh':
        if (state.view === 'browse') {
          if (state.searching) doSearch(state.searchQuery);
          else loadList(state.path).catch((e) => toast(e.message, true));
        }
        if (state.view === 'send') loadTransfers();
        toast('已刷新');
        break;
      case 'goAbout': switchView('about'); break;
      case 'guideNext': guideStep(1); break;
      case 'guideSkip': hideGuide(); break;
      case 'showGuide': showGuide(); break;
      case 'toggleSelect':
        state.selectMode = !state.selectMode;
        state.sel.clear();
        $('#selbar').hidden = !state.selectMode;
        $('#selBtn').classList.toggle('is-on', state.selectMode);
        renderList();
        break;
      case 'toggleMode':
        state.mode = state.mode === 'grid' ? 'list' : 'grid';
        localStorage.setItem('dl.mode', state.mode);
        renderBrowse();
        break;
      case 'search': toggleSearch(); break;
      case 'closeSearch': closeSearch(); break;
      case 'pickFiles': $('#fileInput').click(); break;
      case 'pickPhotos': $('#photoInput').click(); break;
      case 'pickCamera': $('#cameraInput').click(); break;
      case 'pickFolder': pickFolder(); break;
      case 'retryFailed': retryFailed(); break;
      case 'clearQueue':
        state.uploads = state.uploads.filter((u) => u.status === 'up' || u.status === 'wait');
        renderQueue();
        break;
      case 'loadTransfers': loadTransfers(); break;
      case 'clipSend': sendClip(); break;
      case 'clipCopy': {
        const id = actEl.dataset.id;
        const d = await api('/api/clip').catch(() => null);
        const item = d && d.items.find((x) => x.id === id);
        if (item) copyText(item.text);
        break;
      }
      case 'clipDelete':
        api('/api/clip/delete', { method: 'POST', body: JSON.stringify({ id: actEl.dataset.id }) })
          .then(loadClip).catch((e) => toast(e.message, true));
        break;
      case 'closeSheet': closeSheet(); break;
      case 'sheetDownload':
        if (state.sheetEntry) {
          if (!confirmLarge(state.sheetEntry.size, '下载')) break;
          const started = triggerDownload(dlUrl(state.sheetEntry.path), state.sheetEntry.name);
          if (!started) toast(state.sheetEntry.size ? '开始下载' : '空文件：仅创建一个 0 字节文件');
        }
        break;
      case 'sheetShare':
        if (state.sheetEntry) shareEntry(state.sheetEntry);
        break;
      case 'sheetOpenLocal':
        if (state.sheetEntry) {
          api('/api/open', { method: 'POST', body: JSON.stringify({ path: state.sheetEntry.path }) })
            .then(() => toast('已在电脑上打开')).catch((e) => toast(e.message, true));
        }
        break;
      case 'copyShared':
        if (state.sharedUrl) copyText(state.sharedUrl);
        break;
      case 'copyUrl':
        if (state.session && state.session.lan_urls && state.session.lan_urls[0]) {
          copyText(state.session.lan_urls[0]);
        }
        break;
      case 'openAllFiles': {
        const bridge = window.androidBridge;
        if (bridge && bridge.openAllFilesSettings) {
          bridge.openAllFilesSettings();
          toast('请在系统设置里允许「所有文件访问」，回来后即可浏览整机');
        } else {
          toast('当前环境不支持', true);
        }
        break;
      }
      case 'toggleTips': {
        if (tipsEnabled()) {
          localStorage.setItem(TIPS_KEY, 'off');
          document.body.classList.add('tips-off');
          toast('已关闭使用提示');
        } else {
          localStorage.removeItem(TIPS_KEY);
          document.body.classList.remove('tips-off');
          toast('已重新显示使用提示');
        }
        renderAbout();
        break;
      }
      case 'saveRecv': {
        const val = ($('#recvInput') || {}).value || '';
        api('/api/config', { method: 'POST', body: JSON.stringify({ recv_dir: val }) })
          .then((d) => {
            state.recvDir = d.recv_dir;
            if (state.session) state.session.recv_dir = d.recv_dir;
            toast('接收目录已更新');
            renderAbout();
            loadList(d.recv_dir).catch(() => {});
          })
          .catch((e) => toast(e.message, true));
        break;
      }
      case 'openRecv':
        api('/api/open', { method: 'POST', body: JSON.stringify({ path: state.recvDir }) })
          .catch((e) => toast(e.message, true));
        break;
      case 'firewallSet': {
        const d = await api('/api/firewall/set', { method: 'POST', body: '{}' })
          .catch((e) => ({ ok: false, msg: e.message }));
        if (d.ok) toast(d.msg || '防火墙规则已添加');
        else toast(d.cancelled ? '已取消，未做改动' : (d.msg || '设置失败'), true);
        break;
      }
      case 'firewallRemove': {
        const d = await api('/api/firewall/remove', { method: 'POST', body: '{}' })
          .catch((e) => ({ ok: false, msg: e.message }));
        if (d.ok) toast(d.msg || '防火墙规则已删除');
        else toast(d.cancelled ? '已取消，未做改动' : (d.msg || '删除失败'), true);
        break;
      }
      case 'diag': {
        const d = await api('/api/diag').catch(() => null);
        if (d) showDiag(d);
        else toast('检测失败', true);
        break;
      }
      case 'openHotspot':
        api('/api/open-hotspot', { method: 'POST', body: '{}' })
          .catch((e) => toast(e.message, true));
        break;
      case 'scanConnect': {
        const bridge = window.androidBridge;
        if (bridge && bridge.scanQrCode) {
          window.__qrResult = (url) => {
            if (!url) { toast('未识别到二维码，请重试', 'err'); return; }
            // AD-05：只接受 http 且指向局域网/本机的地址，防止恶意二维码把界面带到钓鱼页
            let ok = false;
            try {
              const u = new URL(url);
              const h = (u.hostname || '').toLowerCase();
              ok = u.protocol === 'http:' && (
                h === 'localhost' || h === '127.0.0.1' || h === '[::1]' ||
                /^10\./.test(h) || /^192\.168\./.test(h) ||
                /^172\.(1[6-9]|2\d|3[01])\./.test(h) || /^169\.254\./.test(h)
              );
            } catch (e) { ok = false; }
            if (!ok) { toast('不是有效的 DropLink 地址', 'err'); return; }
            toast('扫码成功，正在连接…', 'ok');
            window.location.href = url;
          };
          bridge.scanQrCode();
        } else {
          toast('当前环境不支持扫码', 'err');
        }
        break;
      }
      case 'toggleHotspot': {
        const bridge = window.androidBridge;
        if (bridge && bridge.enableHotspot) {
          const ok = bridge.enableHotspot();
          toast(ok ? '热点已开启，等对方连上来' : '开启失败，请到系统设置手动开热点');
          if (ok) setTimeout(renderAbout, 1500);
        } else {
          toast('当前环境不支持热点');
        }
        break;
      }
      case 'pickSendFiles': $('#sendFileInput').click(); break;
      case 'removeSendFile': {
        const i = Number(actEl.dataset.i);
        state.sendFiles.splice(i, 1);
        renderSendFiles();
        break;
      }
      case 'doSend': doSend(); break;
      case 'selCancel': exitSelect(); break;
      case 'selDownload': {
        const items = Array.from(state.sel.values()).filter((x) => !x.dir);
        if (!items.length) { toast('请选择文件'); break; }
        if (items.length === 1) {
          if (confirmLarge(items[0].size, '下载')) triggerDownload(dlUrl(items[0].path), items[0].name);
        } else zipSelected(items);
        break;
      }
      case 'selZip': {
        const items = Array.from(state.sel.values());
        if (!items.length) { toast('请选择内容'); break; }
        zipSelected(items.filter((x) => !x.dir));
        break;
      }
      case 'selDelete': deleteSelected(); break;
      default: break;
    }
  });

  // 长按进入多选
  let pressTimer = 0;
  document.addEventListener('pointerdown', (ev) => {
    const row = ev.target.closest('.row[data-i]');
    if (!row || state.selectMode || !matchMedia('(hover: none)').matches) return;
    pressTimer = setTimeout(() => {
      const e = state.list[Number(row.dataset.i)];
      if (!e) return;
      state.suppressClick = Date.now();
      state.selectMode = true;
      state.sel.clear();
      state.sel.set(e.path, e);
      $('#selbar').hidden = false;
      $('#selBtn').classList.add('is-on');
      renderList();
      if (navigator.vibrate) navigator.vibrate(12);
    }, 480);
  }, { passive: true });
  ['pointerup', 'pointercancel', 'pointermove', 'scroll'].forEach((t) => {
    document.addEventListener(t, () => clearTimeout(pressTimer), { passive: true });
  });

  $('#sheet').addEventListener('click', (ev) => {
    if (ev.target === $('#sheet')) closeSheet();
  });

  document.addEventListener('keydown', (ev) => {
    if (ev.key === 'Escape') {
      if (!$('#sheet').hidden) closeSheet();
      if (!$('#login').hidden) return;
    }
    if (ev.key === 'Enter' && !$('#login').hidden) doLogin();
  });

  /* 安卓返回键先交给页面：返回 true 表示已消费（关弹层/退出多选/关搜索）。 */
  window.__dlBack = function () {
    if (!$('#sheet').hidden) { closeSheet(); return true; }
    if (state.selectMode) { exitSelect(); return true; }
    if (state.searchOpen) { closeSearch(); return true; }
    return false;
  };

  // 网络切换下拉
  document.addEventListener('change', (ev) => {
    if (ev.target && ev.target.id === 'netSelect') {
      state.selectedIp = ev.target.value;
      if (state.selectedIp) localStorage.setItem('dl.ip', state.selectedIp);
      else localStorage.removeItem('dl.ip');
      renderAbout();
      toast(state.selectedIp ? `已切换到 ${state.selectedIp}` : '已恢复自动选择');
    }
  });

  $('#pinInput').addEventListener('input', (ev) => {
    ev.target.value = ev.target.value.replace(/\D/g, '').slice(0, 8);
    $('#loginError').textContent = '';
  });

  function clipTextOk(v) {
    // 只有空白或只有分隔线（—— / ----）都算空内容，避免生成垃圾记录
    return !!v && !/^[—\-–_\s]+$/.test(v);
  }

  $('#clipInput').addEventListener('input', (ev) => {
    const v = ev.target.value;
    $('#clipCount').textContent = `${v.length} 字`;
    const btn = document.querySelector('[data-act="clipSend"]');
    if (btn) btn.disabled = !clipTextOk(v.trim());
  });

  function sendClip() {
    const el = $('#clipInput');
    const text = el.value.trim();
    if (!clipTextOk(text)) { toast('请输入内容', true); return; }
    const from = state.session && state.session.local ? '电脑' : '手机';
    api('/api/clip', { method: 'POST', body: JSON.stringify({ text, from }) })
      .then(() => {
        el.value = '';
        $('#clipCount').textContent = '0 字';
        const btn = document.querySelector('[data-act="clipSend"]');
        if (btn) btn.disabled = true;
        toast('已发送');
        loadClip();
      })
      .catch((e) => toast(e.message, true));
  }

  /* ---------------- 多选操作 ---------------- */

  /** 安卓桥能拿到剩余空间；桌面浏览器没有这个能力就返回 0。 */
  function freeBytes() {
    try {
      const b = window.androidBridge;
      if (b && b.freeBytes) return Number(b.freeBytes()) || 0;
    } catch (e) { /* ignore */ }
    return 0;
  }

  /** 大文件/批量操作前确认；返回 true 表示继续。 */
  function confirmLarge(size, action) {
    const free = freeBytes();
    if (!size || size < (1 << 30)) return true;
    let msg = `${action} ${fmtSize(size)} 的文件？`;
    if (free) msg += `\n手机剩余空间 ${fmtSize(free)}`;
    if (free && size > free) msg += '\n\n剩余空间不足，可能写到一半失败。';
    return confirm(msg);
  }

  function zipSelected(items) {
    if (!items.length) { toast('没有可打包的文件', true); return; }
    const free = freeBytes();
    const HUGE = 2 * 1024 * 1024 * 1024;   // 2 GiB；JS 的 2<<30 会溢出成负数
    const huge = items.filter((x) => (x.size || 0) > HUGE);
    let chosen = items;
    if (huge.length) {
      // 超过 2 GB 的文件不参与打包：既避免服务端 8 GB 上限，也避免长时间卡住
      chosen = items.filter((x) => (x.size || 0) <= HUGE);
      if (!chosen.length) { toast('选中的文件都超过 2 GB，请单独下载', true); return; }
      if (!confirm(`有 ${huge.length} 个文件超过 2 GB，将跳过它们，只打包其余 ${chosen.length} 个。\n\n继续？`)) return;
    }
    if (chosen.length > 100) { toast('一次最多打包 100 项，请分批操作', true); return; }
    const total = chosen.reduce((s, x) => s + (x.size || 0), 0);
    let msg = `打包 ${chosen.length} 个文件，共 ${fmtSize(total)}？`;
    if (free) msg += `\n手机剩余空间 ${fmtSize(free)}`;
    if (free && total > free) msg += '\n\n剩余空间可能不足。';
    if (!confirm(msg)) return;

    // 走导航下载而不是 fetch+blob：安卓 WebView 的 DownloadListener 只认 http 地址，
    // blob 地址会「点了没反应」，这也是之前误触后直接下载原文件的原因之一。
    const url = '/api/zip?paths=' + encodeURIComponent(JSON.stringify(chosen.map((x) => x.path)));
    triggerDownload(url);
    toast('正在打包，稍候…');
  }

  async function deleteSelected() {
    const items = Array.from(state.sel.values());
    if (!items.length) return;
    if (!confirm(`删除 ${items.length} 项到回收站？\n（可在电脑回收站里还原）`)) return;
    try {
      await api('/api/delete', { method: 'POST', body: JSON.stringify({ paths: items.map((x) => x.path) }) });
      toast('已移入回收站');
      exitSelect();
      await loadList(state.path);
    } catch (e) {
      toast(e.message, true);
    }
  }

  /* ---------------- 搜索 ---------------- */

  function toggleSearch() {
    if (state.searchOpen) { closeSearch(); return; }
    state.searchOpen = true;
    const bar = document.createElement('div');
    bar.className = 'searchbar';
    bar.id = 'searchBar';
    bar.innerHTML = `
      <input id="searchInput" placeholder="搜索当前文件夹（含子文件夹）" autocomplete="off" spellcheck="false">
      <button class="chip is-on" data-act="closeSearch">取消</button>`;
    $('#fileList').before(bar);
    $('#searchInput').focus();
    $('#searchInput').addEventListener('input', debounce((ev) => {
      const q = ev.target.value.trim();
      if (!q) { closeSearch(); return; }
      doSearch(q);
    }, 320));
  }

  function closeSearch() {
    state.searchOpen = false;
    const bar = $('#searchBar');
    if (bar) bar.remove();
    if (state.searching) loadList(state.path).catch(() => {});
  }

  function debounce(fn, ms) {
    let t = 0;
    return (...args) => {
      clearTimeout(t);
      t = setTimeout(() => fn(...args), ms);
    };
  }

  async function doSearch(q) {
    state.searchQuery = q;
    state.searching = true;
    try {
      const d = await api(`/api/search?q=${encodeURIComponent(q)}&path=${encodeURIComponent(state.path)}`);
      state.list = d.entries;
      state.animateList = true;
      renderBrowse();
    } catch (e) {
      toast(e.message, true);
    }
  }

  /* ---------------- 容器初始化 ---------------- */

  ['fileInput', 'photoInput', 'cameraInput', 'folderInput'].forEach((id) => {
    const el = document.getElementById(id);
    el.addEventListener('change', () => {
      if (id === 'folderInput') startBatch();
      handleFiles(el.files);
      el.value = '';
      switchView('send');
    });
  });

  // 电脑端「发送到手机」的文件选择
  document.getElementById('sendFileInput').addEventListener('change', (ev) => {
    const files = Array.from(ev.target.files || []);
    if (files.length) {
      state.sendFiles.push(...files);
      renderSendFiles();
    }
    ev.target.value = '';
  });

  ['dragenter', 'dragover'].forEach((t) => {
    $('#dropzone').addEventListener(t, (ev) => {
      ev.preventDefault();
      $('#dropzone').classList.add('is-over');
    });
  });
  ['dragleave', 'drop'].forEach((t) => {
    $('#dropzone').addEventListener(t, (ev) => {
      ev.preventDefault();
      $('#dropzone').classList.remove('is-over');
    });
  });
  $('#dropzone').addEventListener('drop', (ev) => {
    ev.preventDefault();
    if (ev.dataTransfer) handleDropItems(ev.dataTransfer);
  });

  // 桌面端可把文件拖到任意位置
  window.addEventListener('dragover', (ev) => ev.preventDefault());
  window.addEventListener('drop', (ev) => {
    ev.preventDefault();
    if (ev.target.closest('#dropzone')) return;
    if (ev.dataTransfer) {
      handleDropItems(ev.dataTransfer);
      switchView('send');
    }
  });

  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState !== 'visible' || !state.authed) return;
    if (state.view === 'clip') loadClip();
    // 从系统设置返回：刷新会话（可能刚授予了「所有文件访问」）
    if (window.androidBridge && state.view === 'about') boot();
  });

  window.addEventListener('hashchange', () => {
    const v = location.hash.replace('#', '');
    if (['browse', 'send', 'clip', 'about'].includes(v) && v !== state.view) switchView(v);
  });

  // 原生（安卓 App）回调：显示 toast / 接收扫码结果
  window.__toast = (m) => toast(m);

  initTheme();
  initTips();
  initViz();
  boot();
})();
