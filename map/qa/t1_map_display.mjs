// Part 1: map display. Leaflet 1.9.4 + OSM tiles + OSRM (today) vs MapKit JS + the map node (Apple), at desktop and phone width.
// Needs lab_server.py running on :8765. Writes qa/out/t1_map_display.json and four screenshots.  node qa/t1_map_display.mjs
import { writeFileSync } from 'node:fs';
import { init } from './cdp.mjs';

const BASE = process.env.LAB || 'http://localhost:8765';
const VIEWS = [['desktop', 1100, 760, false], ['phone', 390, 844, true]];
const out = { generated: new Date().toISOString().slice(0, 16).replace('T', ' '), runs: [] };

for (const [vname, w, h, mobile] of VIEWS) {
  const page = await init({ width: w, height: h });
  await page.viewport(w, h, mobile);
  for (const kind of ['leaflet', 'mapkit']) {
    page.resetLogs();
    const t0 = Date.now();
    const loadMs = await page.goto(`${BASE}/lab/${kind}.html`);
    const readyMs = await page.waitFor('window.QA && window.QA.ready', 40000);
    const total = Date.now() - t0;
    const qa = JSON.parse(await page.eval('JSON.stringify({ ...window.QA, line: undefined })'));
    const reqs = [...page.requests.values()];
    const hosts = {};
    for (const r of reqs) { try { const u = new URL(r.url); if (u.protocol.startsWith('http')) hosts[u.host] = (hosts[u.host] || 0) + 1; } catch { /* data: url */ } }
    const external = Object.entries(hosts).filter(([host]) => !host.startsWith('localhost'));
    const sri = kind === 'leaflet' ? await page.eval(`[...document.scripts].some(s => s.integrity) && typeof L === 'object'`) : null;
    const tileHosts = kind === 'leaflet' ? ['tile.openstreetmap.org'] : external.map(([h]) => h).filter((h) => /apple-mapkit|apple\.com/.test(h));
    const tileCount = reqs.filter((r) => tileHosts.some((h) => r.url.includes(h)) && (kind === 'leaflet' ? /\.png/.test(r.url) : r.type !== 'Script')).length;
    const bytes = reqs.reduce((s, r) => s + (r.bytes || 0), 0);
    const shot = `qa/out/shots/t1-${kind}-${vname}.png`;
    await page.shot(shot);
    const failed = reqs.filter((r) => r.failed || (r.status && r.status >= 400));
    out.runs.push({
      view: vname, width: w, kind, load_ms: loadMs, ready_ms: total, route_ms: qa.routeMs ?? null, route_ok: !!qa.routeOk, km: qa.km ? +qa.km.toFixed(1) : null,
      min: qa.min ? +qa.min.toFixed(1) : null, line_points: qa.linePoints ?? null, requests: reqs.length, tile_requests: tileCount, kb: Math.round(bytes / 1024),
      hosts: Object.fromEntries(external), sri_checked_and_loaded: sri, token_ok: qa.tokenOk ?? null, mapkit_error: qa.mapkitError ?? null,
      console_errors: page.console.filter((c) => c.level === 'error' && !/favicon/.test(c.text)).map((c) => c.text.slice(0, 160)), exceptions: page.errors.slice(0, 3),
      failed_requests: failed.filter((r) => !/favicon/.test(r.url)).map((r) => `${r.status || r.failed} ${r.url.slice(0, 80)}`).slice(0, 5), screenshot: shot,
    });
    console.log(vname, kind, 'ready', total, 'ms | tiles', tileCount, '| kb', Math.round(bytes / 1024), '| errors', out.runs.at(-1).console_errors.length, '| route', qa.routeOk, qa.km?.toFixed(1));
  }
  await page.close();
}
writeFileSync('qa/out/t1_map_display.json', JSON.stringify(out, null, 1));
console.log('t1 done');
