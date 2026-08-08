const { chromium } = require('C:\\Users\\deped\\AppData\\Local\\npm-cache\\_npx\\e41f203b7505f1fb\\node_modules\\playwright');

const shot = process.argv[2] || 'C:\\Users\\deped\\Documents\\jarvis-demo\\playwright_shots\\jarvis_measure.png';

(async () => {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  await page.goto('http://localhost:8000/', { waitUntil: 'networkidle' });
  await page.waitForTimeout(2500);
  await page.screenshot({ path: shot, fullPage: true });

  const frames = page.frames().map(f => f.name() + ' | ' + f.url());
  console.log('FRAMES:', JSON.stringify(frames, null, 1));

  const fl = page.frameLocator('#hud');
  const data = await fl.locator('canvas').first().evaluate((cv) => {
    const host = cv.parentElement;
    const main = host.parentElement;
    const r = host.getBoundingClientRect();
    const mr = main.getBoundingClientRect();
    const W = Math.max(20, r.width), H = Math.max(20, r.height);
    const off = (window.innerWidth / 2) - (r.left + W / 2);
    return {
      innerWidth: window.innerWidth, innerHeight: window.innerHeight,
      mainTag: main.tagName, mainLeft: mr.left, mainRight: mr.right,
      hostLeft: r.left, hostTop: r.top, W, H,
      hostTransform: getComputedStyle(host).transform,
      canvasPx: { w: cv.width, h: cv.height },
      canvasRect: cv.getBoundingClientRect().toJSON(),
    };
  });
  console.log('MEASURE:', JSON.stringify(data, null, 1));
  const R = Math.min(data.W, data.H) / 2 * 0.97;
  console.log('R =', R.toFixed(1));
  await browser.close();
})();
