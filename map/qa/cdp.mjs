// Minimal Chrome driver over the DevTools protocol. No dependencies: Node 22+ has WebSocket and fetch built in.
// Used by the qa tests to load pages, type, fake GPS, read the network log and take screenshots.
import { spawn } from 'node:child_process';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';

const CHROME = process.env.CHROME || 'C:/Program Files/Google/Chrome/Application/chrome.exe';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

export async function launch({ width = 1280, height = 900, port = 9333 } = {}) {
  const dir = mkdtempSync(path.join(tmpdir(), 'komute-qa-'));
  const proc = spawn(CHROME, [
    '--headless=new', `--remote-debugging-port=${port}`, `--user-data-dir=${dir}`, '--no-first-run',
    '--no-default-browser-check', '--disable-extensions', '--use-angle=swiftshader', '--enable-unsafe-swiftshader',
    '--ignore-gpu-blocklist', `--window-size=${width},${height}`, 'about:blank',
  ], { stdio: 'ignore' });
  let target = null;
  for (let i = 0; i < 60 && !target; i++) {
    try {
      const list = await (await fetch(`http://127.0.0.1:${port}/json`)).json();
      target = list.find((t) => t.type === 'page');
    } catch { /* browser still starting */ }
    if (!target) await sleep(250);
  }
  if (!target) { proc.kill(); throw new Error('Chrome did not start'); }
  const ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = () => rej(new Error('devtools socket failed')); });
  return new Page(ws, proc, dir, width, height);
}

class Page {
  constructor(ws, proc, dir, width, height) {
    this.ws = ws; this.proc = proc; this.dir = dir; this.id = 0; this.pending = new Map(); this.handlers = {};
    this.width = width; this.height = height;
    this.requests = new Map(); this.console = []; this.errors = [];
    ws.onmessage = (m) => {
      const msg = JSON.parse(m.data);
      if (msg.id && this.pending.has(msg.id)) {
        const { res, rej } = this.pending.get(msg.id); this.pending.delete(msg.id);
        msg.error ? rej(new Error(`${msg.error.message}`)) : res(msg.result);
      } else if (msg.method) {
        this._event(msg.method, msg.params);
        (this.handlers[msg.method] || []).forEach((h) => h(msg.params));
      }
    };
  }
  _event(method, p) {
    if (method === 'Network.requestWillBeSent') this.requests.set(p.requestId, { url: p.request.url, start: p.timestamp, status: null, bytes: 0, failed: null, type: p.type });
    else if (method === 'Network.responseReceived') { const r = this.requests.get(p.requestId); if (r) { r.status = p.response.status; r.mime = p.response.mimeType; r.fromCache = p.response.fromDiskCache || false; } }
    else if (method === 'Network.loadingFinished') { const r = this.requests.get(p.requestId); if (r) { r.bytes = p.encodedDataLength; r.end = p.timestamp; } }
    else if (method === 'Network.loadingFailed') { const r = this.requests.get(p.requestId); if (r) { r.failed = p.errorText; r.end = p.timestamp; } }
    else if (method === 'Runtime.consoleAPICalled') this.console.push({ level: p.type, text: p.args.map((a) => a.value ?? a.description ?? '').join(' ').slice(0, 300) });
    else if (method === 'Runtime.exceptionThrown') this.errors.push((p.exceptionDetails.exception?.description || p.exceptionDetails.text || '').slice(0, 300));
    else if (method === 'Log.entryAdded' && p.entry.level === 'error') this.console.push({ level: 'error', text: `${p.entry.text} ${p.entry.url || ''}`.slice(0, 300) });
  }
  on(method, h) { (this.handlers[method] ||= []).push(h); }
  send(method, params = {}) {
    const id = ++this.id;
    return new Promise((res, rej) => { this.pending.set(id, { res, rej }); this.ws.send(JSON.stringify({ id, method, params })); });
  }
  async init() {
    for (const d of ['Page', 'Runtime', 'Network', 'Log']) await this.send(`${d}.enable`);
    // Cache stays on: OSM's tile policy forbids sending no-cache headers. Each launch uses a fresh profile, so it starts empty anyway.
    return this;
  }
  resetLogs() { this.requests.clear(); this.console = []; this.errors = []; }
  async viewport(width, height, mobile = false) {
    this.width = width; this.height = height;
    await this.send('Emulation.setDeviceMetricsOverride', { width, height, deviceScaleFactor: mobile ? 2 : 1, mobile });
  }
  async goto(url, { wait = 'load', timeout = 30000 } = {}) {
    const loaded = new Promise((res) => this.on(wait === 'dom' ? 'Page.domContentEventFired' : 'Page.loadEventFired', res));
    const t0 = Date.now();
    await this.send('Page.navigate', { url });
    await Promise.race([loaded, sleep(timeout)]);
    return Date.now() - t0;
  }
  async eval(expr) {
    const r = await this.send('Runtime.evaluate', { expression: expr, awaitPromise: true, returnByValue: true });
    if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || r.exceptionDetails.text);
    return r.result.value;
  }
  async waitFor(expr, timeout = 15000, step = 150) {
    const t0 = Date.now();
    while (Date.now() - t0 < timeout) {
      try { if (await this.eval(`!!(${expr})`)) return Date.now() - t0; } catch { /* page not ready */ }
      await sleep(step);
    }
    return null;
  }
  async rect(selector) { return this.eval(`(() => { const e = document.querySelector(${JSON.stringify(selector)}); if (!e) return null; const r = e.getBoundingClientRect(); return { x: r.x + r.width / 2, y: r.y + r.height / 2 }; })()`); }
  async click(selector) {
    const r = await this.rect(selector);
    if (!r) throw new Error(`click: ${selector} not found`);
    for (const type of ['mousePressed', 'mouseReleased']) await this.send('Input.dispatchMouseEvent', { type, x: r.x, y: r.y, button: 'left', clickCount: 1 });
  }
  async type(selector, text, delay = 60) {
    await this.eval(`document.querySelector(${JSON.stringify(selector)}).focus()`);
    for (const ch of text) {
      await this.send('Input.dispatchKeyEvent', { type: 'keyDown', text: ch, key: ch });
      await this.send('Input.dispatchKeyEvent', { type: 'keyUp', key: ch });
      await sleep(delay);
    }
  }
  async geo(lat, lon, accuracy = 20) {
    await this.send('Browser.grantPermissions', { permissions: ['geolocation'] }).catch(() => {});
    await this.send('Emulation.setGeolocationOverride', { latitude: lat, longitude: lon, accuracy });
  }
  async shot(file, { full = false } = {}) {
    const r = await this.send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: full });
    (await import('node:fs')).writeFileSync(file, Buffer.from(r.data, 'base64'));
  }
  async close() {
    try { this.ws.close(); } catch { /* already closed */ }
    this.proc.kill();
    await sleep(500);
    try { rmSync(this.dir, { recursive: true, force: true }); } catch { /* chrome still releasing files */ }
  }
}

export { sleep };
export const init = async (opts) => (await launch(opts)).init();
