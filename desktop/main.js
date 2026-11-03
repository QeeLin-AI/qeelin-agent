const { app, BrowserWindow, ipcMain } = require('electron');
const { spawn } = require('child_process');
const path = require('path');
const fs = require('fs');
const http = require('http');
const net = require('net');

// 诊断日志：写到 exe 同目录，方便定位启动问题
const LOG_FILE = path.join(path.dirname(process.resourcesPath || __dirname), 'qeelin-debug.log');
function log(msg) {
  try { fs.appendFileSync(LOG_FILE, new Date().toISOString() + ' ' + msg + '\n'); } catch (e) { /* ignore */ }
}
log('==== 启动 ====  argv=' + JSON.stringify(process.argv));

let backend = null;
let win = null;
let backendPort = 0;

// 单实例锁：每次启动都会 spawn 一个后端，重复双击会累积孤儿进程
const gotLock = app.requestSingleInstanceLock();
log('gotLock=' + gotLock);
if (!gotLock) {
  log('未拿到单实例锁，退出');
  app.quit();
}
app.on('second-instance', () => {
  log('second-instance 事件');
  if (win) { if (win.isMinimized()) win.restore(); win.focus(); }
});

/* ---------- 空闲端口 ---------- */
function findFreePort() {
  return new Promise((resolve) => {
    const srv = net.createServer();
    srv.listen(0, '127.0.0.1', () => {
      const port = srv.address().port;
      srv.close(() => resolve(port));
    });
  });
}

/* ---------- 后端 sidecar ---------- */
function backendCommand() {
  const candidates = [
    path.join(process.resourcesPath, 'backend', 'qeelin-backend.exe'),
    path.join(__dirname, 'backend', 'qeelin-backend.exe'),
  ];
  for (const exe of candidates) {
    if (fs.existsSync(exe)) return { cmd: exe, args: [] };
  }
  // 纯开发模式：python 跑 desktop_entry.py
  const devEntry = path.join(__dirname, '..', 'desktop_entry.py');
  return { cmd: process.env.PYTHON || 'python', args: [devEntry] };
}

function startBackend(port) {
  const { cmd, args } = backendCommand();
  log('startBackend cmd=' + cmd + ' port=' + port);
  backend = spawn(cmd, args, {
    env: { ...process.env, QEELIN_PORT: String(port) },
    stdio: ['ignore', 'pipe', 'pipe'],
    windowsHide: true,
  });
  log('backend spawned pid=' + (backend.pid || '?'));
  backend.stdout.on('data', (d) => log('[backend] ' + String(d).trim()));
  backend.stderr.on('data', (d) => log('[backend-err] ' + String(d).trim()));
  backend.on('error', (e) => log('[backend] spawn error: ' + e.message));
  backend.on('exit', (code) => {
    log('[backend] exited with code=' + code);
    if (win && !win.isDestroyed()) win.close();
  });
}

function stopBackend() {
  if (backend && !backend.killed) {
    try {
      // 杀整个进程树：PyInstaller onefile 有 bootloader + Python 子进程，只 kill 父进程会残留子进程
      if (process.platform === 'win32') {
        spawn('taskkill', ['/f', '/t', '/pid', String(backend.pid)], { windowsHide: true });
      } else {
        backend.kill();
      }
    } catch (e) { /* ignore */ }
  }
  backend = null;
}

function waitForBackend(port, timeout = 30000) {
  return new Promise((resolve, reject) => {
    const start = Date.now();
    const tryOnce = () => {
      const req = http.get(`http://127.0.0.1:${port}/api/status`, (res) => {
        res.resume();
        resolve();
      });
      req.on('error', () => retry());
      req.setTimeout(1000, () => { req.destroy(); retry(); });
    };
    const retry = () => {
      if (Date.now() - start > timeout) reject(new Error('后端启动超时'));
      else setTimeout(tryOnce, 300);
    };
    tryOnce();
  });
}

/* ---------- 窗口 ---------- */
function createWindow(url) {
  win = new BrowserWindow({
    width: 1360,
    height: 860,
    minWidth: 1040,
    minHeight: 660,
    frame: false,
    backgroundColor: '#0b0e14',
    titleBarStyle: 'hidden',
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
      // preload 放在 asar 外（extraResources），否则 Electron 从 asar 内加载 preload 会失败
      preload: fs.existsSync(path.join(process.resourcesPath, 'preload.js'))
        ? path.join(process.resourcesPath, 'preload.js')
        : path.join(__dirname, 'preload.js'),
    },
  });
  win.loadURL(url);
  win.on('closed', () => { win = null; });

  ipcMain.on('win:minimize', () => win && win.minimize());
  ipcMain.on('win:maximize', () => {
    if (!win) return;
    if (win.isMaximized()) win.unmaximize(); else win.maximize();
  });
  ipcMain.on('win:close', () => win && win.close());
  ipcMain.handle('win:isMaximized', () => (win ? win.isMaximized() : false));
}

app.whenReady().then(async () => {
  log('whenReady fired, gotLock=' + gotLock);
  if (!gotLock) return;
  backendPort = await findFreePort();
  log('findFreePort=' + backendPort);
  startBackend(backendPort);
  try {
    await waitForBackend(backendPort);
    log('waitForBackend 成功');
  } catch (e) {
    log('[main] 后端启动超时: ' + e.message);
    stopBackend();  // 后端没起来，杀掉残留进程再退出
    return;
  }
  createWindow(`http://127.0.0.1:${backendPort}`);
  log('createWindow 完成');
});

app.on('window-all-closed', () => {
  stopBackend();
  if (process.platform !== 'darwin') app.quit();
});

app.on('before-quit', stopBackend);
