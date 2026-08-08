import json, sys

JS = r"""
() => {
  const lines = [];
  const desc = (el) => {
    if (!el) return 'null';
    const cs = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    return [
      el.tagName.toLowerCase(),
      el.id ? '#'+el.id : '',
      (el.className && typeof el.className === 'string' && el.className.trim()) ? '.'+el.className.trim().split(/\s+/).join('.') : '',
      ' bg='+cs.backgroundColor,
      ' bgImg='+(cs.backgroundImage === 'none' ? 'none' : cs.backgroundImage.slice(0,90)),
      ' z='+cs.zIndex,
      ' op='+cs.opacity,
      ' pos='+cs.position,
      ' disp='+cs.display,
      ' blend='+cs.mixBlendMode,
      ' filter='+cs.filter,
      ' backdrop='+cs.backdropFilter,
      ' rect='+JSON.stringify({x:Math.round(r.x),y:Math.round(r.y),w:Math.round(r.width),h:Math.round(r.height)}),
      ' inline="'+(el.getAttribute('style')||'')+'"'
    ].join('');
  };
  const pathOf = (el) => {
    if (!el) return 'null';
    const p = []; let n = el;
    while (n && n !== document.documentElement) {
      p.unshift(n.tagName.toLowerCase() + (n.id ? '#'+n.id : '') +
        ((n.className && typeof n.className === 'string' && n.className.trim()) ? '.'+n.className.trim().split(/\s+/).join('.') : ''));
      n = n.parentElement;
    }
    return p.join(' > ');
  };

  const canvases = Array.from(document.querySelectorAll('canvas'));
  lines.push('=== CANVASES ===');
  canvases.forEach((c,i)=>{
    lines.push('canvas['+i+'] '+desc(c));
    lines.push('   path: '+pathOf(c));
    lines.push('   parent: '+desc(c.parentElement));
  });

  const main = document.querySelector('main');
  lines.push('');
  lines.push('=== MAIN CHILDREN ===');
  if (main) {
    lines.push('main: '+desc(main));
    Array.from(main.children).forEach((ch,i)=>lines.push('main.child['+i+'] '+desc(ch)));
  }

  const orbCanvas = canvases[0];
  const host = orbCanvas ? orbCanvas.parentElement : null;
  lines.push('');
  lines.push('=== ORB HOST SUBTREE ===');
  if (host) {
    lines.push('host: '+desc(host));
    lines.push('host path: '+pathOf(host));
    Array.from(host.querySelectorAll('*')).forEach((el,i)=>lines.push('  host.desc['+i+'] '+desc(el)));
    lines.push('host siblings:');
    Array.from(host.parentElement ? host.parentElement.children : []).forEach((s,i)=>{ if (s!==host) lines.push('  sib['+i+'] '+desc(s)); });
  }

  const orbRect = orbCanvas ? orbCanvas.getBoundingClientRect() : null;
  lines.push('');
  lines.push('=== ORB CANVAS RECT === '+JSON.stringify(orbRect ? {x:Math.round(orbRect.x),y:Math.round(orbRect.y),w:Math.round(orbRect.width),h:Math.round(orbRect.height)} : null));

  const pts = [];
  if (orbRect) {
    const cx = orbRect.left + orbRect.width/2, cy = orbRect.top + orbRect.height/2;
    for (let dy=-0.45; dy<=0.46; dy+=0.15)
      for (let dx=-0.45; dx<=0.46; dx+=0.15)
        pts.push([Math.round(cx+dx*orbRect.width), Math.round(cy+dy*orbRect.height), 'orb']);
  }
  for (let y=690; y<=800; y+=10)
    for (let x=680; x<=1040; x+=40)
      pts.push([x,y,'bar']);

  lines.push('');
  lines.push('=== ELEMENT FROM POINT (unique hits) ===');
  const seen = new Map();
  for (const pt of pts) {
    const x=pt[0], y=pt[1], tag=pt[2];
    const el = document.elementFromPoint(x,y);
    const key = tag+'|'+pathOf(el);
    if (!seen.has(key)) {
      seen.set(key, []);
      lines.push('['+tag+'] ('+x+','+y+') -> '+pathOf(el));
      lines.push('      '+desc(el));
      document.elementsFromPoint(x,y).slice(0,6).forEach((s,i)=>lines.push('      stack['+i+'] '+desc(s)));
    }
    seen.get(key).push([x,y]);
  }
  lines.push('');
  lines.push('=== POINT GROUPS ===');
  for (const kv of seen) lines.push(kv[0]+'  hits='+kv[1].length+' first='+JSON.stringify(kv[1][0])+' last='+JSON.stringify(kv[1][kv[1].length-1]));

  lines.push('');
  lines.push('=== IMAGES ===');
  Array.from(document.querySelectorAll('img')).forEach((im,i)=>{
    lines.push('img['+i+'] src='+String(im.currentSrc||im.src||'').slice(0,140)+' complete='+im.complete+' nw='+im.naturalWidth+' nh='+im.naturalHeight);
    lines.push('      '+desc(im));
    lines.push('      path='+pathOf(im));
  });
  lines.push('window.__resources='+(typeof window.__resources !== 'undefined' && window.__resources ? JSON.stringify(Object.keys(window.__resources)) : 'undefined'));
  try {
    if (window.__resources && window.__resources.crateImg) {
      const c = window.__resources.crateImg;
      lines.push('crateImg: complete='+c.complete+' nw='+c.naturalWidth+' src='+String(c.src).slice(0,140));
    }
  } catch(e) { lines.push('crateImg err '+e); }

  lines.push('');
  lines.push('=== ALL VISIBLE ELEMENTS WITH DARK OPAQUE BG (alpha>0.5, lum<40) ===');
  Array.from(document.querySelectorAll('*')).forEach((el)=>{
    const cs = getComputedStyle(el);
    const m = cs.backgroundColor.match(/rgba?\(([^)]+)\)/);
    if (!m) return;
    const p = m[1].split(',').map(s=>parseFloat(s));
    const a = p.length>3 ? p[3] : 1;
    const lum = 0.299*p[0]+0.587*p[1]+0.114*p[2];
    if (a>0.5 && lum<40) {
      const r = el.getBoundingClientRect();
      if (r.width>4 && r.height>4 && cs.display!=='none' && cs.visibility!=='hidden' && parseFloat(cs.opacity)>0.05) {
        lines.push('DARK '+pathOf(el));
        lines.push('     '+desc(el));
      }
    }
  });
  return lines.join('\n');
}
"""

from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    pg = b.new_page(viewport={'width':1440,'height':900})
    pg.goto('http://localhost:8000/', wait_until='networkidle')
    pg.wait_for_timeout(2500)
    fr = None
    for f in pg.frames:
        if f.name == 'hud' or (f.frame_element and False):
            fr = f
    if fr is None:
        el = pg.query_selector('#hud')
        fr = el.content_frame() if el else None
    if fr is None:
        print('NO IFRAME FOUND; frames=' + str([f.url for f in pg.frames]))
        b.close(); sys.exit(1)
    out = fr.evaluate(JS)
    with open(r'C:\Users\deped\Documents\jarvis-demo\playwright_shots\_blackbox_raw.txt','w',encoding='utf-8') as fh:
        fh.write(out)
    pg.screenshot(path=r'C:\Users\deped\Documents\jarvis-demo\playwright_shots\_blackbox_shot.png')
    print(out)
    b.close()
