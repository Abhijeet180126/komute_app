// Screenshots of the built dashboard at desktop and phone width, to check layout.  node qa/dashshot.mjs
import { resolve } from 'node:path';
import { init, sleep } from './cdp.mjs';

const file = 'file:///' + resolve('qa/out/komute-map-dashboard.html').split('\\').join('/');
const page = await init({ width: 1180, height: 900 });
await page.goto(file); await sleep(2500);
const h = await page.eval('document.documentElement.scrollHeight');
const overflow = await page.eval('document.documentElement.scrollWidth > document.documentElement.clientWidth');
console.log('desktop height', h, 'horizontal overflow', overflow);
await page.shot('qa/out/shots/dash-desktop-top.png');
await page.eval('window.scrollTo(0, 2300)'); await sleep(600); await page.shot('qa/out/shots/dash-desktop-mid.png');
await page.viewport(390, 844, false); await page.goto(file); await sleep(2500);
console.log('phone horizontal overflow', await page.eval('document.documentElement.scrollWidth > document.documentElement.clientWidth'), 'scrollWidth', await page.eval('document.documentElement.scrollWidth'));
await page.shot('qa/out/shots/dash-phone-top.png');
await page.close();
