import io, sys

P = r'C:\Users\deped\Documents\jarvis-demo\hud_artifact.html'
s = io.open(P, encoding='utf-8', newline='').read()
orig = s

# 1. grid: 3 cols -> 2 cols
old_grid = 'grid-template-columns:clamp(216px,23vw,300px) minmax(0,1fr) clamp(228px,24vw,320px)'
new_grid = 'grid-template-columns:minmax(0,1fr) clamp(228px,24vw,320px)'
assert s.count(old_grid) == 1, 'grid count %d' % s.count(old_grid)
s = s.replace(old_grid, new_grid)

# 2. delete left aside (border-right one) through its closing tag + trailing blank line
open_tag = '<aside style=\\"display:flex;flex-direction:column;border-right:2px solid color-mix(in srgb,var(--holo,#ffb454) 45%,transparent);position:relative;z-index:3;min-height:0;overflow-y:auto;overflow-x:hidden;scrollbar-width:none\\">'
assert s.count(open_tag) == 1, 'open aside count %d' % s.count(open_tag)
start = s.index(open_tag)
close = '<\\u002Faside>'
end = s.index(close, start) + len(close)
tail = '\\n\\n  '
if s[end:end + len(tail)] == tail:
    end += len(tail)
removed = s[start:end]
s = s[:start] + s[end:]

# 3. main gets left border
old_main = '<main style=\\"position:relative;display:flex;align-items:center;justify-content:center;overflow:hidden\\">'
new_main = '<main style=\\"position:relative;display:flex;align-items:center;justify-content:center;overflow:hidden;border-left:2px solid color-mix(in srgb,var(--holo,#ffb454) 45%,transparent)\\">'
assert s.count(old_main) == 1, 'main count %d' % s.count(old_main)
s = s.replace(old_main, new_main)

io.open(P, 'w', encoding='utf-8', newline='').write(s)
print('removed_chars', len(removed))
print('removed_head', removed[:120])
print('removed_tail', removed[-120:])
print('aside_open_count', s.count('<aside'))
print('aside_close_count', s.count('<\\u002Faside>'))
print('header_main_join', s[s.index('<\\u002Fheader>'):s.index('<\\u002Fheader>') + 200])
