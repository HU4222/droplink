// 真机 UI 冒烟：通过 WebView 调试端口驱动页面，验证 R-01 / R-02 / R-03
const http = require('http');

function targets() {
  return new Promise((resolve, reject) => {
    http.get('http://127.0.0.1:9333/json/list', (r) => {
      let s = '';
      r.on('data', (d) => (s += d));
      r.on('end', () => resolve(JSON.parse(s)));
    }).on('error', reject);
  });
}

(async () => {
  const list = await targets();
  const page = list.find((x) => x.type === 'page' && x.url.includes('8848'));
  if (!page) {
    console.error('没找到页面目标');
    process.exit(2);
  }
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  let id = 0;
  const pend = new Map();
  const events = [];
  ws.onmessage = (m) => {
    const j = JSON.parse(m.data);
    if (j.id && pend.has(j.id)) {
      pend.get(j.id)(j.result);
      pend.delete(j.id);
    } else if (j.method) events.push(j);
  };
  await new Promise((r) => (ws.onopen = r));

  const send = (method, params = {}) =>
    new Promise((res) => {
      const i = ++id;
      pend.set(i, res);
      ws.send(JSON.stringify({ id: i, method, params }));
    });
  const q = async (expr) => {
    const r = await send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true });
    if (r && r.exceptionDetails) return '__ERR__ ' + JSON.stringify(r.exceptionDetails.text);
    return r && r.result ? r.result.value : undefined;
  };
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const pass = [];
  const fail = [];
  const check = (name, ok, detail = '') => {
    (ok ? pass : fail).push(name + (detail ? '  <- ' + detail : ''));
    console.log((ok ? '  PASS  ' : '  FAIL  ') + name + (detail ? '   <- ' + detail : ''));
  };

  await send('Runtime.enable');
  await send('Page.enable');

  // 回到浏览页并等列表
  await q(`document.querySelector('[data-view="browse"]').click()`);
  await sleep(600);
  const nav = async (name) => {
    const ok = await q(`(()=>{const r=[...document.querySelectorAll('.row')].find(x=>x.innerText.startsWith(${JSON.stringify(name)})); if(!r) return false; r.click(); return true})()`);
    await sleep(900);
    return ok;
  };

  // 先回到存储根：SPA 会记住上次的路径
  const atRoot = await q(`[...document.querySelectorAll('.row')].some(r=>r.innerText.startsWith('Documents'))`);
  if (!atRoot) {
    await q(`(()=>{const c=document.querySelector('.crumbs, .crumb, .pathbar'); if(!c) return false; const b=c.querySelector('button, a, span'); if(b){b.click(); return true} return false})()`);
    await sleep(800);
    // 兜底：直接点面包屑里的第一段（根）
    const back = await q(`(()=>{const el=[...document.querySelectorAll('.crumb, .crumbs > *')][0]; if(el){el.click(); return true} return false})()`);
    if (!back) console.log('  INFO  面包屑根节点未找到');
    await sleep(900);
  }

  // 进入 Documents
  let inDocs = await q(`[...document.querySelectorAll('.row')].some(r=>r.innerText.startsWith('预览测试'))`);
  if (!inDocs) {
    const entered = await nav('Documents');
    check('可进入 Documents 目录', entered, entered ? '' : 'row 未找到');
    await sleep(400);
  }

  const names = await q(`[...document.querySelectorAll('.row')].map(r=>r.innerText.split('\\n')[0])`);
  check('列表包含测试文件', Array.isArray(names) && names.some((n) => n.includes('预览测试')), JSON.stringify(names && names.slice(0, 6)));

  // R-01：打开文本预览，正文应为文件内容
  await nav('预览测试');
  await sleep(900);
  const body = await q(`document.querySelector('#textPreview') ? document.querySelector('#textPreview').textContent : '(无预览节点)'`);
  check('R-01 文本预览显示正文', typeof body === 'string' && body.includes('第一行 中文内容'), String(body).slice(0, 60));

  // R-02：桥可用 + 点击下载会走原生确认
  const bridgeOk = await q(`!!(window.androidBridge && typeof window.androidBridge.downloadFile === 'function')`);
  check('R-02 下载桥可用', bridgeOk === true, String(bridgeOk));

  // R-02：点击「下载」→ 原生流程（Java 桥方法无法在 JS 侧打桩，这里只验证不抛异常 + 有原生界面）
  const beforeClick = await q(`document.querySelector('#sheet').hidden`);
  await q(`document.querySelector('[data-act="sheetDownload"]').click()`);
  await sleep(1200);
  const stillAlive = await q(`typeof window.androidBridge.downloadFile === 'function'`);
  check('R-02 点击下载后应用仍在运行', beforeClick === false && stillAlive === true, 'sheetOpen=' + !beforeClick);

  // R-03：空剪贴板按钮应为禁用且有样式
  const clip = await q(`(()=>{
    document.querySelector('[data-view="clip"]').click();
    const el = document.querySelector('#clipInput');
    if (el) { el.value = ''; el.dispatchEvent(new Event('input', {bubbles:true})); }
    const b = document.querySelector('[data-act="clipSend"]');
    if (!b) return {missing:true};
    const cs = getComputedStyle(b);
    return {disabled: b.disabled, opacity: cs.opacity, pointer: cs.pointerEvents};
  })()`);
  check('R-03 空内容时按钮禁用', clip && clip.disabled === true, JSON.stringify(clip));
  check('R-03 禁用态有视觉差异', clip && parseFloat(clip.opacity) < 0.9, JSON.stringify(clip));

  // 收尾：回到浏览页
  await q(`document.querySelector('[data-view="browse"]').click()`);

  console.log(`\n通过 ${pass.length}/${pass.length + fail.length}`);
  if (fail.length) console.log('失败：\n' + fail.join('\n'));
  ws.close();
  process.exit(fail.length ? 1 : 0);
})();
