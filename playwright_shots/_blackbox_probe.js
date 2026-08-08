const { chromium } = require('C:/Users/deped/AppData/Local/npm-cache/_npx/e41f203b7505f1fb/node_modules/playwright');

(async () => {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  await page.goto('http://localhost:8000/', { waitUntil: 'networkidle' });
  await page.waitForTimeout(2500);

  const frame = page.frames().find(f => f.name() === 'hud') ||
                (await (await page.$('#hud')).contentFrame());

  const out = await frame.evaluate(() => {
    const lines = [];
    const desc = (el) => {
      if (!el) return 'null';
      const cs = getComputedStyle(el);
      return [
        el.tagName.toLowerCase(),
        el.id ? '#' + el.id : '',
        el.className && typeof el.className === 'string' ? '.' + el.className.trim().split(/\s+/).join('.') : '',
        ' bg=' + cs.backgroundColor,
        ' bgImg=' + (cs.backgroundImage === 'none' ? 'none' : cs.backgroundImage.slice(0, 80)),
        ' z=' + cs.zIndex,
        ' op=' + cs.opacity,
        ' pos=' + cs.position,
        ' disp=' + cs.display,
        ' mixBlend=' + cs.mixBlendMode,
        ' filter=' + cs.filter,
        ' backdrop=' + cs.backdropFilter,
        ' rect=' + JSON.stringify(el.getBoundingClientRect().toJSON()),
        ' inline="' + (el.getAttribute('style') || '') + '"',
      ].join('');
    };

    // path to root
    const pathOf = (el) => {
      const p = [];
      let n = el;
      while (n && n !== document.documentElement) {
        p.unshift(n.tagName.toLowerCase() + (n.id ? '#' + n.id : '') +
          (n.className && typeof n.className === 'string' && n.className.trim() ? '.' + n.className.trim().split(/\s+/).join('.') : ''));
        n = n.parentElement;
      }
      return p.join(' > ');
    };

    // Locate the orb canvas
    const canvases = Array.from(document.querySelectorAll('canvas'));
    lines.push('=== CANVASES ===');
    canvases.forEach((c, i) => {
      lines.push(`canvas[${i}] ${desc(c)}`);
      lines.push(`   path: ${pathOf(c)}`);
      lines.push(`   parent: ${desc(c.parentElement)}`);
    });

    // main children
    const main = document.querySelector('main');
    lines.push('');
    lines.push('=== MAIN CHILDREN ===');
    if (main) {
      lines.push('main itself: ' + desc(main));
      Array.from(main.children).forEach((ch, i) => {
        lines.push(`main.child[${i}] ${desc(ch)}`);
      });
    }

    // orb host = canvas parent; dump all descendants
    const orbCanvas = canvases[0];
    const host = orbCanvas ? orbCanvas.parentElement : null;
    lines.push('');
    lines.push('=== ORB HOST SUBTREE ===');
    if (host) {
      lines.push('host: ' + desc(host));
      Array.from(host.querySelectorAll('*')).forEach((el, i) => {
        lines.push(`  host.desc[${i}] ${desc(el)}`);
      });
      lines.push('host siblings:');
      Array.from(host.parentElement ? host.parentElement.children : []).forEach((s, i) => {
        if (s !== host) lines.push(`  sib[${i}] ${desc(s)}`);
      });
    }

    // Grid sampling
    const orbRect = orbCanvas ? orbCanvas.getBoundingClientRect() : null;
    lines.push('');
    lines.push('=== ORB CANVAS RECT === ' + JSON.stringify(orbRect ? orbRect.toJSON() : null));

    const pts = [];
    if (orbRect) {
      const cx = orbRect.left + orbRect.width / 2;
      const cy = orbRect.top + orbRect.height / 2;
      for (let dy = -0.45; dy <= 0.46; dy += 0.15) {
        for (let dx = -0.45; dx <= 0.46; dx += 0.15) {
          pts.push([Math.round(cx + dx * orbRect.width), Math.round(cy + dy * orbRect.height), 'orb']);
        }
      }
    }
    // bar region beneath orb
    for (let y = 690; y <= 800; y += 10) {
      for (let x = 680; x <= 1040; x += 40) {
        pts.push([x, y, 'bar']);
      }
    }

    lines.push('');
    lines.push('=== ELEMENT FROM POINT ===');
    const seen = new Map();
    for (const [x, y, tag] of pts) {
      const el = document.elementFromPoint(x, y);
      const key = tag + '|' + (el ? pathOf(el) : 'null');
      if (!seen.has(key)) {
        seen.set(key, []);
        lines.push(`[${tag}] (${x},${y}) -> ${pathOf(el)}`);
        lines.push(`      ${desc(el)}`);
        // also full stack at that point
        const stack = document.elementsFromPoint(x, y).slice(0, 6);
        stack.forEach((s, i) => lines.push(`      stack[${i}] ${desc(s)}`));
      }
      seen.get(key).push([x, y]);
    }
    lines.push('');
    lines.push('=== POINT GROUPS (hit counts) ===');
    for (const [k, v] of seen) lines.push(`${k}  hits=${v.length} first=${JSON.stringify(v[0])} last=${JSON.stringify(v[v.length - 1])}`);

    // resources / images
    lines.push('');
    lines.push('=== IMAGES ===');
    Array.from(document.querySelectorAll('img')).forEach((im, i) => {
      lines.push(`img[${i}] src=${(im.currentSrc || im.src || '').slice(0, 120)} complete=${im.complete} nw=${im.naturalWidth} nh=${im.naturalHeight}`);
      lines.push(`      ${desc(im)}`);
    });
    lines.push('window.__resources=' + (typeof window.__resources !== 'undefined' ? JSON.stringify(Object.keys(window.__resources)) : 'undefined'));
    if (typeof window.__resources !== 'undefined' && window.__resources.crateImg) {
      const c = window.__resources.crateImg;
      lines.push('crateImg: complete=' + c.complete + ' nw=' + c.naturalWidth + ' src=' + String(c.src).slice(0, 120));
    }

    // Any element in doc with an opaque dark background
    lines.push('');
    lines.push('=== ALL ELEMENTS WITH DARK OPAQUE BG (alpha>0.5, luminance<40) ===');
    Array.from(document.querySelectorAll('*')).forEach((el) => {
      const cs = getComputedStyle(el);
      const m = cs.backgroundColor.match(/rgba?\(([^)]+)\)/);
      if (!m) return;
      const p = m[1].split(',').map(s => parseFloat(s));
      const a = p.length > 3 ? p[3] : 1;
      const lum = 0.299 * p[0] + 0.587 * p[1] + 0.114 * p[2];
      if (a > 0.5 && lum < 40) {
        const r = el.getBoundingClientRect();
        if (r.width > 4 && r.height > 4 && cs.display !== 'none' && cs.visibility !== 'hidden' && parseFloat(cs.opacity) > 0.05) {
          lines.push(`DARK ${pathOf(el)}`);
          lines.push(`     ${desc(el)}`);
        }
      }
    });
    return lines.join('\n');
  });

  require('fs').writeFileSync('C:/Users/deped/Documents/jarvis-demo/playwright_shots/_blackbox_raw.txt', out, 'utf8');
  console.log(out);
  await page.screenshot({ path: 'C:/Users/deped/Documents/jarvis-demo/playwright_shots/_blackbox_shot.png' });
  await browser.close();
})();
