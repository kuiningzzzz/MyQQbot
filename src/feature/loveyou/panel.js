featureAdminControls.loveyou = async row => {
  const status = row.querySelector('.love-status');
  const rows = row.querySelector('.love-rows');
  const image = row.querySelector('.love-image');
  const request = async (url, method = 'GET', body) => {
    const response = await fetch('/api/loveyou' + url, {
      method, ...(body === undefined ? {} : {headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)}),
    });
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(typeof error.detail === 'string' ? error.detail : '操作失败，请检查输入或刷新后重试');
    }
    return response.json();
  };
  const act = async (button, task) => {
    button.disabled = true;
    status.textContent = '处理中…';
    try { await task(); status.textContent = '已保存'; }
    catch (error) { status.textContent = error.message; }
    finally { button.disabled = false; }
  };
  const renderEntry = entry => {
    const line = document.createElement('div'); line.className = 'love-row';
    const name = document.createElement('input'); name.placeholder = '人名'; name.setAttribute('aria-label', '赞助人姓名'); name.maxLength = 80; name.value = entry?.name || '';
    const amount = document.createElement('input'); amount.placeholder = '累计金额（两位小数）'; amount.setAttribute('aria-label', '累计金额'); amount.inputMode = 'decimal'; amount.value = entry?.amount ?? '0.00';
    const save = document.createElement('button'); save.type = 'button'; save.textContent = '保存';
    const remove = document.createElement('button'); remove.type = 'button'; remove.className = 'secondary'; remove.textContent = entry ? '删除' : '取消';
    save.onclick = () => act(save, async () => {
      await request('/entries', 'POST', {name: name.value, amount: amount.value, previous_name: entry?.name ?? null, version: entry?.version ?? null});
      await load();
    });
    remove.onclick = () => {
      if (!entry) { line.remove(); return; }
      if (!window.confirm(`确定删除“${entry.name}”的赞助记录吗？`)) return;
      act(remove, async () => { await request('/entries/delete', 'POST', {name: entry.name, version: entry.version}); await load(); });
    };
    line.append(name, amount, save, remove); rows.append(line);
  };
  const load = async () => {
    const data = await request('');
    rows.replaceChildren(); data.entries.forEach(renderEntry);
    row.querySelector('.love-empty').hidden = data.entries.length > 0;
    image.hidden = !data.has_image;
    if (data.has_image) { image.src = '/api/loveyou/image?t=' + Date.now(); image.alt = '当前收款码'; }
    else image.removeAttribute('src');
  };
  row.querySelector('.love-new').onclick = () => { row.querySelector('.love-empty').hidden = true; renderEntry(null); };
  const refresh = row.querySelector('.love-refresh'); refresh.onclick = () => act(refresh, load);
  const upload = row.querySelector('.love-upload-save');
  upload.onclick = () => act(upload, async () => {
    const file = row.querySelector('.love-upload').files[0];
    if (!file || file.size > 8 * 1024 * 1024) throw new Error('请选择不超过8 MiB的图片');
    const data = await new Promise((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(reader.result); reader.onerror = () => reject(new Error('图片读取失败')); reader.readAsDataURL(file); });
    await request('/image', 'POST', {data_url: data}); await load();
  });
  const removeImage = row.querySelector('.love-image-delete');
  removeImage.onclick = () => {
    if (window.confirm('确定移除收款码图片吗？')) act(removeImage, async () => { await request('/image', 'DELETE'); await load(); });
  };
  await load();
};
