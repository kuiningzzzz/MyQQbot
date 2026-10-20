let backupTimer = null;
let backupSelection = null;
let backupBusy = false;
let backupSupported = false;

async function backupRequest(url, options) {
  const response = await fetch(url, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || `操作失败（${response.status}）`);
  return data;
}

function backupMessage(message) { document.querySelector('#backup-progress').textContent = message; }
function backupBytes(size) {
  for (const unit of ['B', 'KiB', 'MiB', 'GiB', 'TiB']) {
    if (size < 1024 || unit === 'TiB') return `${size.toFixed(unit === 'B' ? 0 : 1)} ${unit}`;
    size /= 1024;
  }
}

function backupButtons() {
  document.querySelector('#backup-create').disabled = backupBusy || !backupSupported;
  document.querySelector('#backup-import').disabled = backupBusy;
  document.querySelector('#backup-restore').disabled = backupBusy || !backupSupported;
  document.querySelectorAll('#backup-list button').forEach(button => { button.disabled = backupBusy; });
}

function stopBackupPolling() { clearTimeout(backupTimer); backupTimer = null; }

async function initBackupView() {
  stopBackupPolling();
  try {
    const data = await backupRequest('/api/backups');
    backupSupported = data.supported;
    backupBusy = ['queued', 'running'].includes(data.status.state);
    backupMessage(data.status.message + (!data.supported ? '\n当前启动方式不支持自动维护，请使用 python -m src.main。' : ''));
    const list = document.querySelector('#backup-list');
    list.replaceChildren();
    for (const item of data.packages) {
      const row = document.createElement('tr');
      const values = [item.name + (item.imported ? '（导入）' : ''), item.created_at ? new Date(item.created_at).toLocaleString('zh-CN', {timeZone: 'Asia/Shanghai', hour12: false}) : '未知', backupBytes(item.size)];
      for (const value of values) { const cell = document.createElement('td'); cell.textContent = value; row.append(cell); }
      const actions = document.createElement('td');
      actions.className = 'backup-actions';
      const download = document.createElement('a');
      download.textContent = '下载'; download.href = `/api/backups/${encodeURIComponent(item.name)}/download`; download.download = item.name;
      actions.append(download);
      const restore = document.createElement('button'); restore.type = 'button'; restore.className = 'secondary'; restore.textContent = '还原';
      restore.addEventListener('click', () => backupRun(async () => {
        const preview = await backupRequest(`/api/backups/${encodeURIComponent(item.name)}/preview`);
        backupSelection = item.name;
        document.querySelector('#backup-preview').textContent = `${item.name}\n${preview.files} 个文件，展开大小 ${backupBytes(preview.bytes)}\n${preview.notice}`;
        document.querySelector('#backup-restore-confirm').value = '';
        const panel = document.querySelector('#backup-restore-panel'); panel.hidden = false; panel.scrollIntoView({behavior: 'smooth', block: 'start'});
      }));
      const del = document.createElement('button'); del.type = 'button'; del.className = 'danger'; del.textContent = '删除';
      del.addEventListener('click', () => {
        if (confirm(`永久删除备份包 ${item.name}？`)) backupRun(async () => {
          await backupRequest(`/api/backups/${encodeURIComponent(item.name)}`, {method: 'DELETE'}); await initBackupView();
        });
      });
      actions.append(restore, del); row.append(actions); list.append(row);
    }
    if (!data.packages.length) {
      const row = document.createElement('tr'); const cell = document.createElement('td'); cell.colSpan = 4; cell.textContent = '还没有备份包'; row.append(cell); list.append(row);
    }
    backupButtons();
  } catch (error) {
    // A stopped backend is expected while the supervisor snapshots/restores.
    backupMessage(backupBusy ? '服务正在维护，等待重新连接…较大备份可能需要几分钟。若端口改变，请使用还原后的地址访问。' : error.message);
  }
  if (!document.querySelector('#view-backups').hidden) backupTimer = setTimeout(initBackupView, backupBusy ? 3000 : 15000);
}

async function backupRun(action) {
  try { await action(); } catch (error) { backupMessage(error.message); }
}

document.querySelector('#backup-refresh').addEventListener('click', () => initBackupView());
document.querySelector('#backup-create').addEventListener('click', () => backupRun(async () => {
  if (!confirm('即将暂停机器人并生成含密钥的完整备份，完成后自动重启。继续？')) return;
  const data = await backupRequest('/api/backups', {method: 'POST'});
  backupBusy = true; backupButtons(); backupMessage(data.message); stopBackupPolling(); backupTimer = setTimeout(initBackupView, 3000);
}));
document.querySelector('#backup-import').addEventListener('click', () => backupRun(async () => {
  const file = document.querySelector('#backup-upload').files[0];
  if (!file) throw new Error('请先选择 ZIP 备份包');
  backupBusy = true; backupButtons(); stopBackupPolling(); backupMessage('正在上传和校验，请保持页面打开…');
  try {
    const data = await backupRequest('/api/backups/import', {method: 'POST', headers: {'Content-Type': 'application/zip'}, body: file});
    await initBackupView(); backupMessage(`上传并校验成功：${data.name}。请在列表中选择还原。`);
  } finally { backupBusy = false; backupButtons(); }
}));
document.querySelector('#backup-cancel').addEventListener('click', () => { document.querySelector('#backup-restore-panel').hidden = true; backupSelection = null; });
document.querySelector('#backup-restore').addEventListener('click', () => backupRun(async () => {
  if (!backupSelection) throw new Error('请先选择备份包');
  const mode = document.querySelector('#backup-mode').value;
  const confirmation = document.querySelector('#backup-restore-confirm').value.trim();
  const required = mode === 'overwrite' ? '覆盖还原' : '增量还原';
  if (confirmation !== required) throw new Error(`请输入“${required}”以确认`);
  const data = await backupRequest('/api/backups/restore', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({name: backupSelection, mode, confirmation})});
  backupBusy = true; backupButtons(); backupMessage(data.message); document.querySelector('#backup-restore-panel').hidden = true;
  stopBackupPolling(); backupTimer = setTimeout(initBackupView, 3000);
}));
