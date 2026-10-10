"""Reusable admin panel for a feature's live diagnostic endpoint."""
from html import escape


def diagnostic_panel(description, placeholder):
    return f'''
    <section class="diagnostic-panel">
      <h3>可用性自检</h3>
      <p>{escape(description)}</p>
      <label>测试作品 ID（可选）
        <input class="diagnostic-sample" type="text" inputmode="numeric" pattern="[0-9]*" maxlength="20" placeholder="{escape(placeholder, quote=True)}">
      </label>
      <p class="diagnostic-note">使用点击时已保存的配置；修改配置后请重新测试。功能关闭时也能测试。最多运行 180 秒，不发送 QQ 消息，样本文件自动清理。</p>
      <button class="diagnostic-run" type="button">一键测试可用性</button>
      <p class="diagnostic-summary" role="status" aria-live="polite">尚未运行测试。</p>
      <ol class="diagnostic-checks"></ol>
    </section>
'''


def diagnostic_javascript(kind):
    # This fragment runs inside the feature's row initializer.
    return r'''
  const testButton = row.querySelector('.diagnostic-run');
  const testInput = row.querySelector('.diagnostic-sample');
  const testSummary = row.querySelector('.diagnostic-summary');
  const testChecks = row.querySelector('.diagnostic-checks');
  const testURL = '/api/''' + kind + r'''/test';
  const labels = {pending: '待检查', running: '检查中', passed: '通过', failed: '失败', skipped: '未检查'};
  const renderTest = data => {
    testButton.disabled = data.running;
    testInput.disabled = data.running;
    testChecks.replaceChildren();
    for (const check of data.checks || []) {
      const item = document.createElement('li');
      item.className = 'diagnostic-check ' + check.status;
      const title = document.createElement('strong');
      title.textContent = `${labels[check.status] || check.status} · ${check.label}`;
      item.append(title);
      if (check.message) {
        const message = document.createElement('p'); message.textContent = check.message; item.append(message);
      }
      if (check.hint) {
        const hint = document.createElement('p'); hint.className = 'diagnostic-hint'; hint.textContent = '建议：' + check.hint; item.append(hint);
      }
      testChecks.append(item);
    }
    const elapsed = ((data.duration_ms || 0) / 1000).toFixed(1);
    const when = data.started_at ? new Date(data.started_at).toLocaleString() + ' · ' : '';
    testSummary.textContent = data.running ? `测试中，请稍候（最长 ${data.timeout_seconds} 秒）…`
      : data.status === 'passed' ? `${when}检查通过 · 耗时 ${elapsed} 秒。下载链路可用；QQ 发送和其他作品未测试。`
      : data.status === 'failed' ? `${when}检查未通过 · 耗时 ${elapsed} 秒。请按失败阶段的建议排查。`
      : '当前进程尚未运行测试。';
  };
  const fetchTest = async () => {
    const response = await fetch(testURL);
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || '读取测试结果失败');
    renderTest(data); return data;
  };
  const followTest = async data => {
    const runID = data.run_id;
    while (data.running && row.isConnected) {
      await new Promise(resolve => setTimeout(resolve, 1000));
      if (!row.isConnected) return;
      data = await fetchTest();
      if (data.run_id !== runID) return;
    }
  };
  testButton.addEventListener('click', async () => {
    const sample_id = testInput.value.trim();
    if (sample_id && !/^[0-9]{1,20}$/.test(sample_id)) { testSummary.textContent = '测试作品 ID 必须是数字。'; return; }
    testButton.disabled = true; testSummary.textContent = '正在启动测试…';
    try {
      const response = await fetch(testURL, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({sample_id})});
      const data = await response.json();
      if (response.status === 409) { await followTest(await fetchTest()); return; }
      if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '无法启动测试，请检查作品 ID 或登录状态');
      renderTest(data); await followTest(data);
    } catch (error) { testSummary.textContent = error.message + '；可刷新页面查看仍在运行的测试。'; }
    finally { testButton.disabled = false; testInput.disabled = false; }
  });
  try { const initial = await fetchTest(); followTest(initial).catch(error => { testSummary.textContent = error.message; }); }
  catch (error) { testSummary.textContent = error.message; }
'''
