featureAdminControls.find_chicken = async row => {
  const status = row.querySelector('.status');
  const fields = [...row.querySelectorAll('[data-setting]')];
  const engines = [...row.querySelectorAll('[data-engine]')];
  const button = row.querySelector('.chicken-save');
  const fill = data => {
    fields.forEach(field => {
      if (field.type === 'checkbox') field.checked = data[field.dataset.setting];
      else field.value = data[field.dataset.setting];
    });
    engines.forEach(field => { field.checked = data.engines.includes(field.dataset.engine); });
  };
  const response = await fetch('/api/find-chicken');
  if (!response.ok) throw new Error('加载搜图配置失败');
  fill(await response.json());
  button.addEventListener('click', async () => {
    const data = {engines: engines.filter(field => field.checked).map(field => field.dataset.engine)};
    if (!data.engines.length) { status.textContent = '请至少选择一个引擎'; return; }
    if (fields.some(field => !field.reportValidity())) return;
    fields.forEach(field => {
      data[field.dataset.setting] = field.type === 'checkbox' ? field.checked
        : field.type === 'number' ? Number(field.value) : field.value.trim();
    });
    status.textContent = '保存中…';
    button.disabled = true;
    try {
      const saved = await fetch('/api/find-chicken', {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data),
      });
      if (!saved.ok) throw new Error('保存失败，请检查引擎、代理格式和数值范围');
      fill(await saved.json());
      status.textContent = '已保存，新任务使用新配置';
    } catch (error) {
      status.textContent = error.message || '保存失败';
    } finally {
      button.disabled = false;
    }
  });
};
