// Cygnus desktop shell: a tray icon, Ctrl+Alt+J, and one window that switches
// between a sticky panel, the full HUD and a mini orb (modelled on Sticky
// Brain). The backend stays in the repo: this process starts, watches and
// stops it, or attaches to one already running (ELECTRON_PLAN.md, phase 2
// and "Development workflow").
const { app, BrowserWindow, Tray, Menu, globalShortcut, ipcMain, nativeImage, powerMonitor, screen, session, shell } = require('electron')
const { spawn, execFileSync } = require('child_process')
const fs = require('fs')
const http = require('http')
const net = require('net')
const path = require('path')

// A child's exit handler can still write to a console that has gone away and
// throw EPIPE, which kills the main process with an error dialog (Sticky Brain
// hit exactly this). Must be in place before anything is spawned.
for (const stream of [process.stdout, process.stderr]) {
  stream.on('error', err => { if (err && err.code === 'EPIPE') return; throw err })
}

const REPO = path.resolve(__dirname, '..')
const PYTHON = path.join(REPO, '.venv', 'Scripts', 'python.exe')
const PORT = Number(readEnv('JARVIS_PORT') || 8000)
const HUD_URL = `http://127.0.0.1:${PORT}/`
const HUD_ORIGIN = HUD_URL.slice(0, -1)
const HOTKEY = 'Control+Alt+J'
// A ring on a transparent background: the app icon shrunk to 16px was a dark
// square that vanished on a dark taskbar. tray@2x.png is picked on scaled
// displays.
const TRAY_ICON = path.join(__dirname, 'assets', 'tray.png')
const LOG_FILE = path.join(REPO, 'logs', 'desktop-backend.log')
const READY_TIMEOUT_MS = 180000
// What the tray checks besides the backend: Chrome started with its debugging
// port (clicks inside the browser) and Ollama (the offline fallback), read
// from the same settings the backend uses.
const CHROME_PORT = Number(readEnv('JARVIS_BROWSER_PORT') || 9223)
const OLLAMA_PORT = Number(new URL(readEnv('OLLAMA_BASE_URL') || 'http://localhost:11434/v1').port || 11434)
const HEALTH_EVERY_MS = 10000
const CHECKS_EVERY_MS = 60000
const MAX_RESTARTS = 3
const RESTART_WINDOW_MS = 10 * 60 * 1000
// 560 tall since the transcript is trimmed to the last exchange (owner's
// choice, CYGNUS_UI_PLAN.md step 8).
const STICKY = { width: 380, height: 560, margin: 20, minWidth: 320, minHeight: 420 }
const MINI = { size: 96, margin: 24 }
const MODES = ['sticky', 'expanded', 'mini']

const startHidden = process.argv.includes('--hidden')
const isDev = process.argv.includes('--dev')
// A dev run gets its own profile and single-instance lock, so `npm run dev`
// next to an installed copy starts instead of only focusing that copy.
if (isDev) app.setPath('userData', app.getPath('userData') + ' (dev)')

let win = null
let tray = null
let mode = 'sticky'
let stickyBounds = null
let dragOrigin = null
let hotkeyOk = false
let quitting = false
// state: starting | ready | stopped | error. attached: the backend is someone
// else's (a dev server started by hand), so it is never restarted or killed.
let backend = { proc: null, attached: false, state: 'starting', note: '' }
let checks = null          // tray warnings; null until the first check ran
let restartTimes = []      // when an owned backend was restarted after dying

// ---------------------------------------------------------------- backend

// A setting from the environment, else from the repo's .env (as the backend
// reads it), else ''.
function readEnv (name) {
  if (process.env[name]) return process.env[name]
  try {
    const m = fs.readFileSync(path.join(REPO, '.env'), 'utf8').match(new RegExp(`^\\s*${name}\\s*=\\s*(.+?)\\s*$`, 'm'))
    if (m) return m[1].replace(/^["']|["']$/g, '')
  } catch {}
  return ''
}

// What answers on the port: 'jarvis' (the HUD page), 'other', or 'free'.
function probe () {
  return new Promise(resolve => {
    const req = http.get(HUD_URL, { timeout: 1500 }, res => {
      let body = ''
      res.on('data', chunk => { if (body.length < 4096) body += chunk })
      // The HUD's own shell element, not its title: the title is branding.
      res.on('end', () => resolve(res.statusCode === 200 && body.includes('id="jv-shell"') ? 'jarvis' : 'other'))
    })
    req.on('timeout', () => req.destroy())
    req.on('error', () => resolve('free'))
  })
}

const pidFile = () => path.join(app.getPath('userData'), 'backend.pid')

function isPython (pid) {
  try {
    return /python/i.test(execFileSync('tasklist', ['/FI', `PID eq ${pid}`, '/FO', 'CSV', '/NH'], { encoding: 'utf8' }))
  } catch { return false }
}

function killTree (pid) {
  try { execFileSync('taskkill', ['/PID', String(pid), '/T', '/F'], { stdio: 'ignore' }) } catch {}
}

// A backend left behind by a crashed shell still holds the port and the mic.
// Only a python process is killed, in case Windows reused the pid.
function killStaleBackend () {
  let pid = 0
  try { pid = Number(fs.readFileSync(pidFile(), 'utf8')) } catch { return }
  try { fs.unlinkSync(pidFile()) } catch {}
  if (pid && isPython(pid)) killTree(pid)
}

function setBackend (state, note = '') {
  backend.state = state
  backend.note = note
  console.log(`[desktop] backend ${state}${backend.attached ? ' (attached)' : ''}${note ? ': ' + note : ''}`)
  buildTrayMenu()
  if (state === 'error') showStatusPage(note)
}

async function startBackend () {
  killStaleBackend()
  const found = await probe()
  if (found === 'jarvis') {
    backend.attached = true
    return onBackendReady()
  }
  if (found === 'other') return setBackend('error', `Port ${PORT} is used by another program.`)
  if (!fs.existsSync(PYTHON)) return setBackend('error', 'No .venv in the repo (ELECTRON_PLAN.md, phase 0).')
  const proc = spawnBackend()
  await waitForReady(proc)
}

function spawnBackend () {
  fs.mkdirSync(path.dirname(LOG_FILE), { recursive: true })
  const log = fs.openSync(LOG_FILE, 'a')
  const proc = spawn(PYTHON, ['jarvis_web.py'], {
    cwd: REPO,
    windowsHide: true,
    stdio: ['ignore', log, log],
    env: { ...process.env, JARVIS_PORT: String(PORT), JARVIS_HOST: '127.0.0.1', PYTHONUNBUFFERED: '1', PYTHONIOENCODING: 'utf-8' }
  })
  fs.writeFileSync(pidFile(), String(proc.pid))
  backend = { proc, attached: false, state: 'starting', note: '' }
  proc.on('exit', code => {
    try { fs.closeSync(log) } catch {}
    if (backend.proc !== proc) return
    try { fs.unlinkSync(pidFile()) } catch {}
    backend.proc = null
    onBackendExit(code)
  })
  buildTrayMenu()
  return proc
}

// An owned backend that dies on its own is started again, but at most three
// times in ten minutes: one that keeps crashing needs a person, not a loop.
// A deliberate stop never gets here (stopBackend lets go of the process first).
function onBackendExit (code) {
  const now = Date.now()
  restartTimes = restartTimes.filter(t => now - t < RESTART_WINDOW_MS)
  if (restartTimes.length >= MAX_RESTARTS) {
    return setBackend('stopped', `The backend keeps exiting (code ${code}). See the backend log.`)
  }
  restartTimes.push(now)
  setBackend('starting', `The backend exited (code ${code}); starting it again.`)
  showStatusPage('Cygnus stopped unexpectedly. Starting it again…')
  setTimeout(startBackend, 2000)
}

// "Start backend" in the tray: a fresh start, with the crash budget reset.
function startFresh () {
  restartTimes = []
  setBackend('starting')
  showStatusPage('Starting Cygnus…')
  startBackend()
}

// Whisper loads before the port opens, so "the page answers" means ready.
async function waitForReady (proc) {
  const deadline = Date.now() + READY_TIMEOUT_MS
  while (Date.now() < deadline && backend.proc === proc) {
    if (await probe() === 'jarvis') return onBackendReady()
    await new Promise(resolve => setTimeout(resolve, 1000))
  }
  if (backend.proc === proc) setBackend('error', 'The backend did not answer within 3 minutes. See the backend log.')
}

function onBackendReady () {
  setBackend('ready')
  if (win && !win.isDestroyed()) win.loadURL(HUD_URL)
  runChecks()
}

function stopBackend () {
  const { proc } = backend
  backend.proc = null
  if (proc && proc.exitCode === null) killTree(proc.pid)
  try { fs.unlinkSync(pidFile()) } catch {}
}

async function restartBackend () {
  if (backend.attached) return
  stopBackend()
  setBackend('starting')
  showStatusPage('Restarting Cygnus…')
  await new Promise(resolve => setTimeout(resolve, 1000))
  await startBackend()
}

// ---------------------------------------------------------------- health

function fetchStatus () {
  return new Promise(resolve => {
    const req = http.get(HUD_URL + 'status', { timeout: 3000 }, res => {
      let body = ''
      res.on('data', chunk => { body += chunk })
      res.on('end', () => { try { resolve(JSON.parse(body)) } catch { resolve(null) } })
    })
    req.on('timeout', () => req.destroy())
    req.on('error', () => resolve(null))
  })
}

// An attached dev server that goes away shows in the tray, and one that
// answers again is picked up. An owned backend reports its own exit instead.
async function healthTick () {
  if (backend.state === 'starting' || backend.proc) return
  const alive = await probe() === 'jarvis'
  if (alive && backend.state !== 'ready') {
    backend.attached = true
    onBackendReady()
  } else if (!alive && backend.state === 'ready') {
    setBackend('stopped', 'Your dev server stopped.')
  }
}

function portOpen (port) {
  return new Promise(resolve => {
    const socket = net.connect({ host: '127.0.0.1', port, timeout: 1000 })
    socket.once('connect', () => { socket.destroy(); resolve(true) })
    socket.once('timeout', () => { socket.destroy(); resolve(false) })
    socket.once('error', () => resolve(false))
  })
}

// Windows can switch the microphone off for all desktop apps, and the HUD
// then only shows a dead mic. "Deny" at the machine, user or desktop-app
// level blocks it.
const MIC_STORE = 'Microsoft\\Windows\\CurrentVersion\\CapabilityAccessManager\\ConsentStore\\microphone'
function micBlocked () {
  const keys = [`HKLM\\SOFTWARE\\${MIC_STORE}`, `HKCU\\Software\\${MIC_STORE}`, `HKCU\\Software\\${MIC_STORE}\\NonPackaged`]
  return keys.some(key => {
    try { return /\bDeny\b/.test(execFileSync('reg', ['query', key, '/v', 'Value'], { encoding: 'utf8' })) } catch { return false }
  })
}

async function runChecks () {
  const found = []
  if (micBlocked()) found.push('Microphone blocked for desktop apps (Settings > Privacy > Microphone)')
  if (!await portOpen(OLLAMA_PORT)) found.push('Ollama is not running: no offline fallback')
  if (!await portOpen(CHROME_PORT)) found.push('Chrome (JARVIS) is not open: no clicks inside the browser')
  const wake = backend.state === 'ready' ? ((await fetchStatus()) || {}).wake_server : null
  if (wake && !wake.healthy) found.push(`Wake word off: ${wake.error || 'microphone unavailable'}`)
  if (checks === null || found.join('|') !== checks.join('|')) {
    console.log(`[desktop] checks: ${found.join(' | ') || 'all clear'}`)
  }
  checks = found
  buildTrayMenu()
}

// ---------------------------------------------------------------- window

function showStatusPage (text) {
  if (win && !win.isDestroyed()) win.loadFile(path.join(__dirname, 'starting.html'), { query: { msg: text } })
}

function createWindow () {
  const { workArea } = screen.getPrimaryDisplay()
  stickyBounds = {
    width: STICKY.width,
    height: Math.min(STICKY.height, workArea.height - 2 * STICKY.margin),
    x: workArea.x + workArea.width - STICKY.width - STICKY.margin,
    y: workArea.y + STICKY.margin
  }
  win = new BrowserWindow({
    ...stickyBounds,
    minWidth: STICKY.minWidth,
    minHeight: STICKY.minHeight,
    title: 'Cygnus',
    frame: false,
    transparent: true,
    backgroundColor: '#00000000',
    alwaysOnTop: true,
    skipTaskbar: true,
    show: false,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      backgroundThrottling: false,
      autoplayPolicy: 'no-user-gesture-required'
    }
  })
  win.setAlwaysOnTop(true, 'floating')
  guardWindow()
  // A reload rebuilds the page laid out for the default mode; tell it again.
  win.webContents.on('did-finish-load', () => win.webContents.send('desk:mode', mode))
  // Closing (Alt+F4) shrinks to the orb, as Sticky Brain's close shrinks to its
  // pet; quitting is a tray decision.
  win.on('close', event => { if (!quitting) { event.preventDefault(); setMode('mini') } })
  win.once('ready-to-show', () => { if (!startHidden) win.show() })
  showStatusPage('Starting Cygnus…')
}

// The window only ever shows JARVIS. Other pages open in the default browser,
// and the one permission granted is the microphone, to the HUD's own origin.
function guardWindow () {
  const openOutside = url => { if (/^https?:/i.test(url)) shell.openExternal(url) }
  win.webContents.setWindowOpenHandler(({ url }) => { openOutside(url); return { action: 'deny' } })
  win.webContents.on('will-navigate', (event, url) => {
    if (url.startsWith(HUD_URL)) return
    event.preventDefault()
    openOutside(url)
  })
  session.defaultSession.setPermissionRequestHandler((_wc, permission, callback, details) =>
    callback(permission === 'media' && (details.requestingUrl || '').startsWith(HUD_URL)))
  session.defaultSession.setPermissionCheckHandler((_wc, permission, origin) =>
    permission === 'media' && origin === HUD_ORIGIN)
}

// The page is told first, then the window resized, so the frame the resize
// lands on already has the new layout. Minimum size is lowered before the
// bounds and resizability dropped after them: Windows clamps silently
// otherwise (both learned in Sticky Brain).
function setMode (next) {
  if (!win || win.isDestroyed() || !MODES.includes(next)) return
  if (mode === 'sticky') stickyBounds = win.getBounds()
  mode = next
  win.webContents.send('desk:mode', mode)
  const { workArea } = screen.getDisplayMatching(stickyBounds)
  if (mode === 'mini') {
    win.setMinimumSize(MINI.size, MINI.size)
    win.setBounds({
      x: workArea.x + workArea.width - MINI.size - MINI.margin,
      y: workArea.y + workArea.height - MINI.size - MINI.margin,
      width: MINI.size,
      height: MINI.size
    })
    win.setResizable(false)
  } else {
    win.setResizable(true)
    win.setMinimumSize(STICKY.minWidth, STICKY.minHeight)
    win.setBounds(mode === 'expanded' ? workArea : stickyBounds)
  }
  // The full HUD is a normal window; the panel and the orb float.
  win.setAlwaysOnTop(mode !== 'expanded', 'floating')
  buildTrayMenu()
}

function showMode (next, focus = true) {
  setMode(next)
  if (focus) { win.show(); win.focus() } else if (!win.isVisible()) win.showInactive()
}

function onHotkey () {
  if (!win || win.isDestroyed()) return
  showMode(mode === 'mini' ? 'sticky' : mode)
  win.webContents.send('desk:hotkey')
}

ipcMain.on('desk:setMode', (_e, next) => showMode(next, next !== 'mini'))

// A wake word heard while JARVIS is an orb or hidden brings the panel back
// without taking focus from whatever the owner is typing in.
ipcMain.on('desk:surface', () => {
  if (!win || win.isDestroyed()) return
  if (mode === 'mini') setMode('sticky')
  if (!win.isVisible()) win.showInactive()
})

// The orb is dragged by hand, not with a drag region: a drag region swallows
// the click that opens the panel (Sticky Brain's pet works the same way).
ipcMain.on('desk:dragStart', () => { dragOrigin = win && !win.isDestroyed() ? win.getPosition() : null })
ipcMain.on('desk:dragMove', (_e, d) => {
  if (!dragOrigin || mode !== 'mini') return
  win.setPosition(Math.round(dragOrigin[0] + (Number(d && d.dx) || 0)), Math.round(dragOrigin[1] + (Number(d && d.dy) || 0)))
})
ipcMain.on('desk:dragEnd', () => { dragOrigin = null })

// ---------------------------------------------------------------- tray

function trayStatus () {
  if (backend.state === 'ready') return backend.attached ? 'Attached to your dev server' : 'Running'
  if (backend.state === 'starting') return 'Starting…'
  return backend.note || 'Backend stopped'
}

function buildTrayMenu () {
  if (!tray) return
  const status = trayStatus()
  const warnings = checks || []
  const ready = backend.state === 'ready'
  tray.setToolTip(`Cygnus (${status})${warnings.length ? `, ${warnings.length} warning(s)` : ''}${isDev ? ' [dev]' : ''}`)
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: status, enabled: false },
    ...warnings.map(w => ({ label: `⚠ ${w}`, enabled: false })),
    ...(hotkeyOk ? [] : [{ label: 'Ctrl+Alt+J is taken by another app', enabled: false }]),
    { type: 'separator' },
    { label: 'Panel', type: 'radio', checked: mode === 'sticky', click: () => showMode('sticky') },
    { label: 'Full HUD', type: 'radio', checked: mode === 'expanded', click: () => showMode('expanded') },
    { label: 'Mini orb', type: 'radio', checked: mode === 'mini', click: () => showMode('mini', false) },
    { label: 'Hide', click: () => win.hide() },
    { type: 'separator' },
    {
      label: ready ? 'Restart backend' : 'Start backend',
      enabled: backend.state !== 'starting' && !(ready && backend.attached),
      click: ready ? restartBackend : startFresh
    },
    { label: 'Open backend log', click: () => shell.openPath(LOG_FILE) },
    { type: 'separator' },
    { label: 'Quit Cygnus', click: () => { quitting = true; app.quit() } }
  ]))
}

// An empty image is an invisible tray entry: started hidden, that would leave
// no way into the app at all (Sticky Brain's first build had this).
function createTray () {
  const image = nativeImage.createFromPath(TRAY_ICON)
  if (image.isEmpty()) console.error(`[desktop] tray icon missing: ${TRAY_ICON}`)
  tray = new Tray(image)
  tray.on('click', () => showMode(mode === 'mini' ? 'sticky' : mode))
  buildTrayMenu()
  showTrayHintOnce()
}

// Windows 11 puts new tray icons under the ^ arrow, so on first start JARVIS
// says where it went. Once per profile, behind a marker file (the same
// pattern Sticky Brain uses for autostart).
function showTrayHintOnce () {
  const marker = path.join(app.getPath('userData'), 'tray-hint-shown')
  if (fs.existsSync(marker)) return
  tray.displayBalloon({
    title: 'Cygnus is running',
    content: 'Ctrl+Alt+J opens the panel. The tray icon may be under the ^ arrow on the taskbar; drag it onto the taskbar to keep it visible.'
  })
  try {
    fs.mkdirSync(path.dirname(marker), { recursive: true })
    fs.writeFileSync(marker, new Date().toISOString(), 'utf8')
  } catch {}
}

// ---------------------------------------------------------------- app

if (!app.requestSingleInstanceLock()) {
  app.quit()
} else {
  app.on('second-instance', () => { if (win) showMode(mode === 'mini' ? 'sticky' : mode) })
  app.whenReady().then(() => {
    createWindow()
    createTray()
    hotkeyOk = globalShortcut.register(HOTKEY, onHotkey)
    if (!hotkeyOk) console.error(`[desktop] ${HOTKEY} could not be registered`)
    startBackend()
    setInterval(healthTick, HEALTH_EVERY_MS)
    setInterval(runChecks, CHECKS_EVERY_MS)
    // After sleep the page's microphone stream is dead; a reload gives it a
    // new one (the backend's wake engine reopens its own). Checks rerun too.
    powerMonitor.on('resume', () => setTimeout(() => {
      if (backend.state === 'ready' && win && !win.isDestroyed()) win.loadURL(HUD_URL)
      runChecks()
    }, 5000))
  })
  app.on('before-quit', () => { quitting = true })
  app.on('will-quit', () => {
    globalShortcut.unregisterAll()
    if (!backend.attached) stopBackend()
  })
}
