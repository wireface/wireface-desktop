// The face page's only bridge to the desktop shell (contextIsolation: no Node in the page).
'use strict';
const { contextBridge, ipcRenderer, webUtils } = require('electron');

contextBridge.exposeInMainWorld('faceShell', {
  init: () => ipcRenderer.invoke('face:init'),
  setMode: mode => ipcRenderer.send('face:mode', mode),
  setIgnoreMouse: ignore => ipcRenderer.send('face:ignore-mouse', ignore),
  setOnTop: on => ipcRenderer.send('face:on-top', on),
  setVisible: on => ipcRenderer.send('face:visible', on),
  focus: () => ipcRenderer.send('face:focus'),
  drag: on => ipcRenderer.send('face:drag', on),
  resize: on => ipcRenderer.send('face:resize', on),
  zoom: (factor, absWidth) => ipcRenderer.send('face:zoom', factor, absWidth),
  menu: template => ipcRenderer.invoke('face:menu', template),
  pathForFile: file => { try { return webUtils.getPathForFile(file); } catch { return ''; } },
  chooseFolder: current => ipcRenderer.invoke('face:choose-folder', current),
  quit: () => ipcRenderer.send('face:quit'),
  openExternal: url => ipcRenderer.send('face:open-external', url),     // a web page, in the user's browser
  // the activity panel (a second window); each call is only honoured from the window it belongs to
  togglePanel: (tab, toggle) => ipcRenderer.send('face:panel', tab, !!toggle),
  panelInit: () => ipcRenderer.invoke('panel:init'),
  panelPin: on => ipcRenderer.send('panel:pin', on),
  panelClose: () => ipcRenderer.send('panel:close'),
  panelMinimize: () => ipcRenderer.send('panel:minimize'),
  panelMaximize: () => ipcRenderer.send('panel:maximize'),
  panelChooseFolder: current => ipcRenderer.invoke('panel:choose-folder', current),
  // the panel as a drawer: dock('left' | 'right' | null), fold it into a tab at the edge, and hear when that changes
  panelDock: side => ipcRenderer.send('panel:dock', side),
  panelCollapse: on => ipcRenderer.send('panel:collapse', !!on),
  onPanelDock: fn => ipcRenderer.on('panel:dock', (_e, s) => fn(s)),
  onPanelTab: fn => ipcRenderer.on('panel:tab', (_e, tab) => fn(tab)),
  onCursor: fn => ipcRenderer.on('face:cursor', (_e, p) => fn(p)),
  onShow: fn => ipcRenderer.on('face:show', (_e, on) => fn(!!on)),          // the tray icon: show or hide the face
});
