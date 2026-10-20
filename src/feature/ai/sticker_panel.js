function initStickerPanel(row) {
  const $ = selector => row.querySelector(selector);
  let offset = 0, next = null, loading = false, loaded = false;
  const status = text => { $('[data-sticker-status]').textContent = text; };
  async function request(path, body) {
    const response = await fetch(path, body === undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const data = await response.json();
    if (!response.ok || data.success === false) throw new Error(data.error || data.detail || data.code || '操作失败');
    return data;
  }
  async function run(work, button) {
    if (loading) return;
    loading = true;
    if (button) button.disabled = true;
    try { await work(); } catch (error) { status(error.message); }
    finally { loading = false; if (button) button.disabled = false; }
  }
  function account() {
    const value = $('[data-sticker-account]').value;
    if (!value) throw new Error('请先连接NapCat并刷新账号列表');
    return value;
  }
  async function manage(action, body) {
    return request('/api/ai/stickers/manage', {account:account(), action, body});
  }
  function field(parent, title, value, tag='input') {
    const label = document.createElement('label'); label.className = 'ai-field'; label.append(document.createTextNode(title));
    const input = document.createElement(tag); if (tag === 'input') input.type = 'text'; else input.rows = 3;
    input.value = value || ''; label.append(input); parent.append(label); return input;
  }
  function button(parent, text, work, danger=false) {
    const element = document.createElement('button'); element.type = 'button'; element.textContent = text;
    element.className = danger ? 'ai-danger' : 'ai-secondary'; element.onclick = () => run(work, element); parent.append(element);
  }
  async function refresh() {
    const chosen = $('[data-sticker-account]').value;
    const params = new URLSearchParams({offset:String(offset),limit:'30',query:$('[data-sticker-query]').value});
    if (chosen) params.set('account', chosen);
    const data = await request('/api/ai/stickers?' + params);
    const select = $('[data-sticker-account]'); select.replaceChildren();
    for (const qq of data.accounts) {
      const option = document.createElement('option'); option.value = qq; option.textContent = qq + (data.connected_accounts.includes(qq) ? '（在线）' : '（离线）'); select.append(option);
    }
    if (data.accounts.includes(chosen)) select.value = chosen;
    // With multiple accounts, fetch an explicitly scoped page after selecting one.
    if (!chosen && data.accounts.length) { offset = 0; return refresh(); }
    const grid = $('[data-sticker-grid]'); grid.replaceChildren();
    for (const item of data.stickers) {
      const card = document.createElement('article'); card.className = 'sticker-card';
      if (item.has_image) { const image = document.createElement('img'); image.src = `/api/ai/stickers/${encodeURIComponent(item.id)}/image`; image.alt = item.name || '表情预览'; image.loading = 'lazy'; card.append(image); }
      const meta = document.createElement('p'); meta.className = 'ai-muted'; meta.textContent = `${item.id} · ${item.state} · QQ${item.remote_present ? '已收藏' : '未见收藏'}`; card.append(meta);
      const name = field(card, '名称', item.name), tags = field(card, '标签（逗号分隔）', (item.tags || []).join('，'));
      const description = field(card, '本地描述／自动转述', item.description, 'textarea');
      const enabledLabel = document.createElement('label'); enabledLabel.className = 'ai-check';
      const enabled = document.createElement('input'); enabled.type = 'checkbox'; enabled.checked = item.enabled; enabledLabel.append(enabled, document.createTextNode('允许Agent使用')); card.append(enabledLabel);
      const qqLabel = document.createElement('label'); qqLabel.className = 'ai-check'; const qq = document.createElement('input'); qq.type = 'checkbox'; qqLabel.append(qq, document.createTextNode('保存时同步QQ描述')); card.append(qqLabel);
      if (item.error || item.pending_operation) { const error = document.createElement('p'); error.className = 'ai-muted'; error.textContent = item.error || `QQ操作 ${item.pending_operation} 结果待核对，请同步收藏`; card.append(error); }
      const actions = document.createElement('div'); actions.className = 'ai-toolbar'; card.append(actions);
      button(actions, '保存', async () => {
        const result = await manage('update', {id:item.id, name:name.value, tags:tags.value.split(/[,，]/).map(t=>t.trim()).filter(Boolean), description:description.value, enabled:enabled.checked, qq:qq.checked, expected_revision:item.revision});
        Object.assign(item, result.sticker); qq.checked = false;
        status('表情设置已保存。');
      });
      button(actions, '重试标注', async () => { await manage('retry', {id:item.id}); status('已加入后台处理队列，请稍后刷新。'); });
      button(actions, '测试发送', async () => {
        const scene = $('[data-sticker-test-id]').value.trim(), kind = $('[data-sticker-test-type]').value;
        if (!/^[1-9][0-9]*$/.test(scene)) throw new Error('请在下方填写测试目标群号或QQ号');
        if (!confirm(`向${kind === 'group' ? '群' : 'QQ'} ${scene} 发送这个表情？`)) return;
        await request('/api/ai/stickers/test-send', {account:account(), id:item.id, scene_type:kind, scene_id:scene}); status('已收到发送成功回执。');
      });
      button(actions, '本地移除', async () => {
        if (!confirm('从机器人本地库移除，保留QQ收藏？后续同步不会自动重新启用。')) return;
        await manage('delete', {id:item.id, scope:'local'}); await refresh(); status('已移出本地库。');
      }, true);
      button(actions, '仅删QQ', async () => {
        if (!confirm('删除QQ账号里的收藏，保留机器人本地副本？')) return;
        await manage('delete', {id:item.id, scope:'qq'}); await refresh(); status('已确认QQ收藏删除，本地副本保留。');
      }, true);
      button(actions, '两边删除', async () => {
        if (!confirm('删除QQ收藏并移出机器人本地库？')) return;
        await manage('delete', {id:item.id, scope:'both'}); await refresh(); status('已删除。');
      }, true);
      grid.append(card);
    }
    if (!data.stickers.length) { const empty = document.createElement('p'); empty.textContent = '暂无匹配的表情，可同步QQ收藏或上传图片。'; grid.append(empty); }
    next = data.next_offset;
    $('[data-sticker-prev]').disabled = offset === 0; $('[data-sticker-next]').disabled = next === null;
    $('[data-sticker-count]').textContent = `共 ${data.total} 张 · 第 ${Math.floor(offset / 30) + 1} 页`;
    const sync = data.sync[select.value];
    loaded = true;
    status((!data.enabled ? 'AI或表情功能已关闭，Agent暂不能发送。\n' : '') + (sync?.error || (sync?.at ? `上次同步：${new Date(sync.at * 1000).toLocaleString()}，${sync.count}张${sync.complete ? '' : '（可能未取全，已保留原目录）'}` : '账号连接后会后台同步，也可点击“同步QQ收藏”。')));
  }
  for (const [selector, work] of [
    ['[data-sticker-refresh]', refresh],
    ['[data-sticker-search]', async () => { offset = 0; await refresh(); }],
    ['[data-sticker-sync]', async () => { await request('/api/ai/stickers/sync', {account:account()}); status('已加入同步队列，请稍后刷新。'); }],
    ['[data-sticker-prev]', async () => { offset = Math.max(0, offset - 30); await refresh(); }],
    ['[data-sticker-next]', async () => { if (next !== null) { offset = next; await refresh(); } }],
    ['[data-sticker-add-url]', async () => {
      const result = await manage('add', {url:$('[data-sticker-url]').value.trim(), name:$('[data-sticker-name]').value, qq:$('[data-sticker-add-qq]').checked});
      await refresh(); status(`已保存 ${result.sticker.id}，等待后台标注。`);
    }],
    ['[data-sticker-add-file]', async () => {
      const file = $('[data-sticker-upload]').files[0]; if (!file) throw new Error('请先选择图片');
      if (file.size > 8 * 1024 * 1024) throw new Error('表情图片最多8MiB');
      const params = new URLSearchParams({account:account(), qq:String($('[data-sticker-add-qq]').checked),name:$('[data-sticker-name]').value});
      const response = await fetch('/api/ai/stickers/upload?' + params, {method:'POST',headers:{'Content-Type':'application/octet-stream'},body:file});
      const result = await response.json(); if (!response.ok || result.success === false) throw new Error(result.error || result.detail || result.code);
      await refresh(); status(`已保存 ${result.sticker.id}，等待后台标注。`);
    }]
  ]) { const element = $(selector); element.onclick = () => run(work, element); }
  $('[data-sticker-account]').onchange = () => run(async () => { offset = 0; await refresh(); });
  // Lazy-load when this tab is opened. Never replace inputs on a timer while
  // the administrator is editing a name, description or deletion choice.
  row.addEventListener('ai-tab-open', event => { if (event.detail === 'stickers' && !loaded) run(refresh); });
}
