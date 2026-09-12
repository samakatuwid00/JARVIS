// The HUD's only door to the shell. The page stays a plain web page (no
// Node) and gets exactly these calls; see ELECTRON_PLAN.md phase 2.
const { contextBridge, ipcRenderer } = require('electron')

contextBridge.exposeInMainWorld('jarvisDesktop', {
  onHotkey: fn => ipcRenderer.on('desk:hotkey', () => fn()),
  onMode: fn => ipcRenderer.on('desk:mode', (_e, mode) => fn(mode)),
  setMode: mode => ipcRenderer.send('desk:setMode', mode),
  surface: () => ipcRenderer.send('desk:surface'),
  dragStart: () => ipcRenderer.send('desk:dragStart'),
  dragMove: (dx, dy) => ipcRenderer.send('desk:dragMove', { dx, dy }),
  dragEnd: () => ipcRenderer.send('desk:dragEnd')
})
