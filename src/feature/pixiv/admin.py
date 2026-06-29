import asyncio

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from src.feature.atom import FeatureBusyError
from src.feature.pixiv.feature import PixivFeature
from src.feature.pixiv.oauth import PixivOAuth, PixivOAuthError


class PixivTokenRequest(BaseModel):
    refresh_token: str


class PixivOAuthCompleteRequest(BaseModel):
    flow_id: str
    callback: str


class PixivAdmin:
    def __init__(self, feature: PixivFeature, oauth: PixivOAuth):
        self.feature = feature
        self.oauth = oauth

    def register_routes(self, app: FastAPI) -> None:
        feature = self.feature
        oauth = self.oauth

        @app.get("/api/pixiv/token")
        async def token_status():
            return {"configured": feature.has_refresh_token}

        @app.post("/api/pixiv/token")
        async def save_token(body: PixivTokenRequest):
            token = body.refresh_token.strip()
            if not token:
                raise HTTPException(status_code=400, detail="refresh token 不能为空")
            feature.save_refresh_token(token)
            return {"configured": True}

        @app.delete("/api/pixiv/token")
        async def clear_token():
            feature.clear_refresh_token()
            return {"configured": False}

        @app.post("/api/pixiv/oauth/start")
        async def start_oauth():
            flow = oauth.start()
            return {
                "flow_id": flow.flow_id,
                "login_url": flow.login_url,
                "expires_in": flow.expires_in,
            }

        @app.post("/api/pixiv/oauth/complete")
        async def complete_oauth(body: PixivOAuthCompleteRequest):
            try:
                refresh_token = await asyncio.to_thread(
                    oauth.exchange,
                    body.flow_id,
                    body.callback,
                )
            except PixivOAuthError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            feature.save_refresh_token(refresh_token)
            return {"configured": True}

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
    <input type="password" autocomplete="new-password" placeholder="粘贴新的 Pixiv refresh token">
    <div>
      <button class="save-token" type="button">保存 token</button>
      <button class="secondary clear-token" type="button">清除 token</button>
      <span class="token-status status"></span>
    </div>
    <div class="oauth-box">
      <p>也可以使用 PKCE 辅助获取：先打开浏览器开发者工具并保留网络日志，生成授权链接后在 Pixiv 官方页面登录，从 callback 请求中复制完整 URL。</p>
      <button class="start-oauth" type="button">1. 生成并打开 Pixiv 授权链接</button>
      <a class="oauth-link" target="_blank" rel="noopener noreferrer" hidden>浏览器未自动打开？点击这里</a>
      <input class="oauth-callback" type="text" placeholder="2. 粘贴 callback URL 或其中的 code">
      <button class="complete-oauth" type="button" disabled>3. 换取并保存 token</button>
      <span class="oauth-status status"></span>
    </div>
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
  const input = row.querySelector('input[type="password"]');
  const summary = row.querySelector(".token-summary");
  const save = row.querySelector(".save-token");
  const clear = row.querySelector(".clear-token");
  const tokenStatus = row.querySelector(".token-status");
  const startOAuth = row.querySelector(".start-oauth");
  const completeOAuth = row.querySelector(".complete-oauth");
  const oauthLink = row.querySelector(".oauth-link");
  const callbackInput = row.querySelector(".oauth-callback");
  const oauthStatus = row.querySelector(".oauth-status");
  const cleanup = row.querySelector(".cleanup");
  const cleanupStatus = row.querySelector(".cleanup-status");
  let oauthFlowId = null;
  const refreshStatus = async () => {
    const response = await fetch("/api/pixiv/token");
    if (!response.ok) throw new Error("加载 Pixiv token 状态失败");
    const configured = (await response.json()).configured;
    summary.textContent = `Pixiv refresh token：${configured ? "已配置" : "未配置"}。后端不会回传 token。`;
    clear.disabled = !configured;
  };
  await refreshStatus();
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
  startOAuth.addEventListener("click", async () => {
    startOAuth.disabled = true;
    oauthStatus.textContent = "正在生成一次性授权链接...";
    const response = await fetch("/api/pixiv/oauth/start", {method: "POST"});
    startOAuth.disabled = false;
    const data = await response.json().catch(() => ({}));
    if (!response.ok) { oauthStatus.textContent = data.detail || "生成失败"; return; }
    oauthFlowId = data.flow_id;
    completeOAuth.disabled = false;
    oauthLink.href = data.login_url;
    oauthLink.hidden = false;
    oauthStatus.textContent = `授权链接将在 ${data.expires_in} 秒后失效；登录后请尽快粘贴 callback URL。若没有自动打开，请点击旁边的链接。`;
    window.open(data.login_url, "_blank", "noopener,noreferrer");
  });
  completeOAuth.addEventListener("click", async () => {
    const callback = callbackInput.value.trim();
    if (!oauthFlowId || !callback) { oauthStatus.textContent = "请先生成链接并粘贴 callback URL"; return; }
    completeOAuth.disabled = true;
    oauthStatus.textContent = "正在向 Pixiv 换取 token...";
    const response = await fetch("/api/pixiv/oauth/complete", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({flow_id: oauthFlowId, callback}),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) { oauthFlowId = null; oauthStatus.textContent = data.detail || "授权失败，请重新生成链接"; return; }
    oauthFlowId = null; callbackInput.value = "";
    oauthStatus.textContent = "refresh token 已获取、保存并立即生效";
    await refreshStatus();
  });
  cleanup.addEventListener("click", async () => {
    cleanup.disabled = true; cleanupStatus.textContent = "清理中...";
    const response = await fetch("/api/pixiv/cleanup", {method: "POST"});
    cleanup.disabled = false;
    const data = await response.json().catch(() => ({}));
    cleanupStatus.textContent = response.ok ? "已清理" : (data.detail || "清理失败");
  });
};
"""
