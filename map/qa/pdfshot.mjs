// Renders pages of a PDF to PNG with Chrome's built-in viewer, to check layout.  node qa/pdfshot.mjs <pdf> <outdir> <pages>
// One fresh browser per page: Chrome's viewer ignores a changed #page fragment on the same tab.
import { mkdirSync } from 'node:fs';
import { init, sleep } from './cdp.mjs';

const [pdf, outdir, pages = '1'] = process.argv.slice(2);
mkdirSync(outdir, { recursive: true });
for (const n of pages.split(',')) {
  const page = await init({ width: 900, height: 1100 });
  await page.goto(`file:///${pdf.split('\\').join('/')}#page=${n}&zoom=page-fit&toolbar=0&navpanes=0`);
  await sleep(3500);
  await page.shot(`${outdir}/pdf-p${n}.png`);
  await page.close();
  console.log('page', n);
}
