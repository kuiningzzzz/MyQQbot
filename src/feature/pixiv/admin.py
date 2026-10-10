from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, SecretStr

from src.feature.atom import FeatureBusyError
from src.feature.pixiv.feature import PixivFeature
from src.feature.atom.diagnostics import DiagnosticRequest
from src.feature.atom.diagnostic_panel import diagnostic_panel, diagnostic_javascript


class PixivTokenRequest(BaseModel):
    refresh_token: str


class PixivAccountRequest(BaseModel):
    username: str = Field(min_length=1, max_length=256)
    password: SecretStr
    totp_secret: SecretStr = SecretStr('')


class PixivAdmin:
    def __init__(self, feature: PixivFeature):
        self.feature = feature

    def register_routes(self, app: FastAPI) -> None:
        feature = self.feature

        @app.get('/api/pixiv/account')
        async def account_status():
            return feature.auth.snapshot()

        @app.post('/api/pixiv/account', status_code=202)
        async def save_account(body: PixivAccountRequest):
            try:
                feature.auth.ensure_idle()
                username = body.username.strip()
                password = body.password.get_secret_value()
                totp = body.totp_secret.get_secret_value().strip()
                if not username or not password or len(password) > 256 or len(totp) > 512:
                    raise ValueError('请输入有效账号和密码；密码最长 256 字符，TOTP 密钥最长 512 字符。')
                feature.config.save_credentials(username, password, totp)
                feature.auth.reset()
                feature.auth.start()
                return feature.auth.snapshot()
            except FeatureBusyError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except Exception:
                raise HTTPException(status_code=500, detail='账号配置保存失败，请检查加密依赖与目录权限。') from None

        @app.post('/api/pixiv/account/login', status_code=202)
        async def login_account():
            try:
                feature.auth.start()
                return feature.auth.snapshot()
            except FeatureBusyError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

        @app.delete('/api/pixiv/account')
        async def clear_account():
            try:
                feature.auth.ensure_idle()
                feature.config.clear_credentials()
                feature.auth.reset()
                return feature.auth.snapshot()
            except FeatureBusyError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc

        @app.get('/api/pixiv/test')
        async def test_status():
            return feature.diagnostics.snapshot()

        @app.post('/api/pixiv/test', status_code=202)
        async def test_availability(body: DiagnosticRequest | None = None):
            try:
                return feature.diagnostics.start({
                    'sample_id': body.sample_id if body else '',
                    'refresh_token': feature.client.refresh_token or '',
                    'proxy_url': feature.client.proxy_url,
                })
            except FeatureBusyError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc

        @app.get("/api/pixiv/token")
        async def token_status():
            return {"configured": feature.has_refresh_token}

        @app.post("/api/pixiv/token")
        async def save_token(body: PixivTokenRequest):
            try:
                feature.auth.ensure_idle()
            except FeatureBusyError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            token = body.refresh_token.strip()
            if not token:
                raise HTTPException(status_code=400, detail="refresh token 不能为空")
            feature.save_refresh_token(token)
            feature.auth.reset()
            return {"configured": True}

        @app.delete("/api/pixiv/token")
        async def clear_token():
            try:
                feature.auth.ensure_idle()
            except FeatureBusyError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            feature.clear_refresh_token()
            feature.auth.reset()
            return {"configured": False}

        @app.post("/api/pixiv/cleanup")
        async def cleanup():
            try:
                await feature.cleanup_storage()
            except FeatureBusyError as exc:
                raise HTTPException(
                    status_code=409,
                    detail="Pixiv 功能仍有任务，请稍后再清理",
                ) from exc
            return {"ok": True}

    @property
    def panel_html(self) -> str:
        return """
<template data-feature-panel="pixiv">
  <div class="feature-extra">
    <p class="token-summary">正在读取 Pixiv token 状态...</p>
    <section class="diagnostic-panel">
      <h3>账号自动登录与认证恢复</h3>
      <p>保存后自动启动一次浏览器登录并获取 token。之后只有认证失效才自动重新登录，成功后继续原下载任务一次；网络故障、作品不可见或 QQ 发送失败不会触发账号重登。</p>
      <label>Pixiv 邮箱或账号<input class="pixiv-username" type="text" autocomplete="off" maxlength="256" placeholder="输入 Pixiv 邮箱或账号"></label>
      <label>Pixiv 密码<input class="pixiv-password" type="password" autocomplete="new-password" maxlength="256" placeholder="输入确认过的密码，每次保存都需填写"></label>
      <label>TOTP 密钥（可选）<input class="pixiv-totp" type="password" autocomplete="off" maxlength="512" placeholder="开启两步验证时填写认证器密钥，不是 6 位临时验证码"></label>
      <p>账号密码和 TOTP 密钥加密保存在服务器本地，接口不回传秘密。服务器仍需保护配置和密钥文件。验证码、邮箱验证或风控可能需要在本地手动登录；系统不会猜测密码或绕过验证。</p>
      <div>
        <button class="save-account" type="button">保存账号并获取 token</button>
        <button class="secondary login-account" type="button">使用已保存账号重新登录</button>
        <button class="secondary clear-account" type="button">清除账号（停止自动登录）</button>
      </div>
      <p class="account-summary status" aria-live="polite">正在读取账号状态...</p>
      <p>自动重登最长 180 秒；自动尝试间隔至少 10 分钟。清除账号会保留现有 token；仅清除 token 后，已保存的账号仍可自动重新登录。</p>
    </section>
    <details class="token-guide">
      <summary>手动获取与配置 refresh token（备用方式）</summary>
    <div class="token-guide">
      <h3>在本地获取 refresh token</h3>
      <ol>
        <li>在能够正常访问 Pixiv 的个人电脑上准备 Python 环境。</li>
        <li>打开 <a href="https://gist.github.com/ZipFile/c9ebedb224406f4f11845ab700124362" target="_blank" rel="noopener noreferrer">PixivPy 推荐的 OAuth 获取脚本</a>，保存其中的 <code>pixiv_auth.py</code>。</li>
        <li>在脚本目录执行 <code>pip install requests</code>，然后执行 <code>python pixiv_auth.py login</code>。</li>
        <li>按照脚本页面的说明完成 Pixiv 登录，并及时把 callback 请求中的 <code>code</code> 交给脚本。</li>
        <li>复制脚本输出的 <code>refresh_token</code>，粘贴到下方输入框并保存。</li>
      </ol>
      <p><strong>安全提示：</strong>refresh token 等同于账号授权凭据，不要发给他人，也不要提交到 Git。</p>
    </div>
    <input class="pixiv-token" type="password" autocomplete="new-password" placeholder="粘贴本地获取的 Pixiv refresh token">
    <div>
      <button class="save-token" type="button">保存 token</button>
      <button class="secondary clear-token" type="button">清除 token</button>
      <span class="token-status status"></span>
    </div>
    </details>
""" + diagnostic_panel('检查当前保存的 token、作品接口及首张原图下载。留空时自动选择日榜中的普通图片作品；也可指定已知可访问的作品。', '留空自动选择日榜作品') + """
    <p>删除空闲任务目录；正在处理或等待处理的任务不会被清理。</p>
    <button class="danger cleanup" type="button">手动清理 Pixiv 存储垃圾</button>
    <span class="cleanup-status status"></span>
  </div>
</template>
"""

    @property
    def javascript(self) -> str:
        return r"""
featureAdminControls.pixiv = async row => {
  const input = row.querySelector('.pixiv-token');
  const summary = row.querySelector(".token-summary");
  const save = row.querySelector(".save-token");
  const clear = row.querySelector(".clear-token");
  const tokenStatus = row.querySelector(".token-status");
  const cleanup = row.querySelector(".cleanup");
  const cleanupStatus = row.querySelector(".cleanup-status");
  const username = row.querySelector('.pixiv-username');
  const password = row.querySelector('.pixiv-password');
  const totp = row.querySelector('.pixiv-totp');
  const accountSummary = row.querySelector('.account-summary');
  const saveAccount = row.querySelector('.save-account');
  const loginAccount = row.querySelector('.login-account');
  const clearAccount = row.querySelector('.clear-account');
  const refreshStatus = async () => {
    const response = await fetch("/api/pixiv/token");
    if (!response.ok) throw new Error("加载 Pixiv token 状态失败");
    const configured = (await response.json()).configured;
    summary.textContent = `Pixiv refresh token：${configured ? "已配置" : "未配置"}。后端不会回传 token。`;
    clear.disabled = !configured;
  };
  await refreshStatus();
  let pollAccount;
  const accountStatus = async () => {
    const response = await fetch('/api/pixiv/account');
    if (!response.ok) throw new Error('读取账号登录状态失败');
    const data = await response.json();
    if (!username.value) username.value = data.username || '';
    accountSummary.textContent = `${data.configured ? '已保存账号' : '未保存账号'}${data.totp_configured ? '（含 TOTP）' : ''}。${data.configuration_error || data.message || ''}`;
    [saveAccount, clearAccount, save, clear, username, password, totp].forEach(el => el.disabled = !!data.running);
    loginAccount.disabled = !!data.running || !data.configured;
    if (!data.running) { await refreshStatus(); clearAccount.disabled = !data.configured; }
    if (row.isConnected) pollAccount = setTimeout(() => accountStatus().catch(err => { accountSummary.textContent = err.message; }), data.running ? 1000 : 5000);
    return data;
  };
  const changeAccount = async (url, method, body) => {
    clearTimeout(pollAccount);
    [saveAccount, loginAccount, clearAccount].forEach(el => el.disabled = true);
    accountSummary.textContent = '正在提交...';
    try {
      const response = await fetch(url, {method, headers: {'Content-Type': 'application/json'}, ...(body ? {body: JSON.stringify(body)} : {})});
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '操作失败');
      password.value = ''; totp.value = '';
      if (method === 'DELETE') username.value = '';
      await accountStatus();
    } catch (err) {
      await accountStatus().catch(() => {});
      accountSummary.textContent = err.message;
      [saveAccount, loginAccount, clearAccount].forEach(el => el.disabled = false);
    }
  };
  saveAccount.addEventListener('click', () => {
    if (!username.value.trim() || !password.value) { accountSummary.textContent = '请输入账号和确认过的密码。'; return; }
    changeAccount('/api/pixiv/account', 'POST', {username: username.value.trim(), password: password.value, totp_secret: totp.value.trim()});
  });
  loginAccount.addEventListener('click', () => changeAccount('/api/pixiv/account/login', 'POST'));
  clearAccount.addEventListener('click', () => changeAccount('/api/pixiv/account', 'DELETE'));
  await accountStatus();
  save.addEventListener("click", async () => {
    const refresh_token = input.value.trim();
    if (!refresh_token) { tokenStatus.textContent = "请输入 refresh token"; return; }
    save.disabled = true;
    const response = await fetch("/api/pixiv/token", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({refresh_token}),
    });
    save.disabled = false;
    const data = await response.json().catch(() => ({}));
    tokenStatus.textContent = response.ok ? "已保存并立即生效" : (data.detail || "保存失败");
    if (response.ok) { input.value = ""; await refreshStatus(); }
  });
  clear.addEventListener("click", async () => {
    clear.disabled = true;
    const response = await fetch("/api/pixiv/token", {method: "DELETE"});
    tokenStatus.textContent = response.ok ? "已清除" : "清除失败";
    await refreshStatus();
  });
  cleanup.addEventListener("click", async () => {
    cleanup.disabled = true; cleanupStatus.textContent = "清理中...";
    const response = await fetch("/api/pixiv/cleanup", {method: "POST"});
    cleanup.disabled = false;
    const data = await response.json().catch(() => ({}));
    cleanupStatus.textContent = response.ok ? "已清理" : (data.detail || "清理失败");
  });
""" + diagnostic_javascript('pixiv') + r"""
};
"""
