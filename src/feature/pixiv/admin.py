from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from src.feature.atom import FeatureBusyError
from src.feature.pixiv.feature import PixivFeature


class PixivTokenRequest(BaseModel):
    refresh_token: str


class PixivAdmin:
    def __init__(self, feature: PixivFeature):
        self.feature = feature

    def register_routes(self, app: FastAPI) -> None:
        feature = self.feature

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
  const cleanup = row.querySelector(".cleanup");
  const cleanupStatus = row.querySelector(".cleanup-status");
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
  cleanup.addEventListener("click", async () => {
    cleanup.disabled = true; cleanupStatus.textContent = "清理中...";
    const response = await fetch("/api/pixiv/cleanup", {method: "POST"});
    cleanup.disabled = false;
    const data = await response.json().catch(() => ({}));
    cleanupStatus.textContent = response.ok ? "已清理" : (data.detail || "清理失败");
  });
};
"""
