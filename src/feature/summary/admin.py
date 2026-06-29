from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from src.feature.summary.feature import SummaryFeature


class SummaryConfigRequest(BaseModel):
    api_url: str
    model: str
    api_key: str | None = None


class SummaryAdmin:
    def __init__(self, feature: SummaryFeature):
        self.feature = feature

    def register_routes(self, app: FastAPI) -> None:
        feature = self.feature

        @app.get("/api/summary/config")
        async def get_config():
            return feature.config_status

        @app.post("/api/summary/config")
        async def save_config(body: SummaryConfigRequest):
            try:
                feature.config.save(body.api_url, body.model, body.api_key)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            return feature.config_status

        @app.delete("/api/summary/key")
        async def clear_key():
            feature.config.clear_api_key()
            return feature.config_status

    @property
    def panel_html(self) -> str:
        return """
<template data-feature-panel="summary">
  <div class="feature-extra">
    <p class="summary-config-status">正在读取模型配置...</p>
    <label>API URL
      <input class="summary-api-url" type="text" placeholder="https://api.deepseek.com">
    </label>
    <label>模型名
      <input class="summary-model" type="text" placeholder="deepseek-chat">
    </label>
    <label>API Key（留空则保留现有 Key）
      <input class="summary-api-key" type="password" autocomplete="new-password" placeholder="sk-...">
    </label>
    <div>
      <button class="summary-save" type="button">保存模型配置</button>
      <button class="secondary summary-clear-key" type="button">清除 API Key</button>
      <span class="summary-save-status status"></span>
    </div>
    <p>URL 可填写 OpenAI-compatible 服务根地址，或完整的 <code>/chat/completions</code> 地址。API Key 不会回传到浏览器。</p>
  </div>
</template>
"""

    @property
    def javascript(self) -> str:
        return r"""
featureAdminControls.summary = async row => {
  const apiUrl = row.querySelector(".summary-api-url");
  const model = row.querySelector(".summary-model");
  const apiKey = row.querySelector(".summary-api-key");
  const summary = row.querySelector(".summary-config-status");
  const save = row.querySelector(".summary-save");
  const clearKey = row.querySelector(".summary-clear-key");
  const status = row.querySelector(".summary-save-status");
  const refresh = async () => {
    const response = await fetch("/api/summary/config");
    if (!response.ok) throw new Error("加载 AI 总结配置失败");
    const data = await response.json();
    apiUrl.value = data.api_url;
    model.value = data.model;
    summary.textContent = `API Key：${data.api_key_configured ? "已配置" : "未配置"}。`;
    clearKey.disabled = !data.api_key_configured;
  };
  await refresh();
  save.addEventListener("click", async () => {
    save.disabled = true; status.textContent = "保存中...";
    const body = {api_url: apiUrl.value.trim(), model: model.value.trim()};
    if (apiKey.value.trim()) body.api_key = apiKey.value.trim();
    const response = await fetch("/api/summary/config", {
      method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body),
    });
    save.disabled = false;
    const data = await response.json().catch(() => ({}));
    status.textContent = response.ok ? "已保存并立即生效" : (data.detail || "保存失败");
    if (response.ok) { apiKey.value = ""; await refresh(); }
  });
  clearKey.addEventListener("click", async () => {
    clearKey.disabled = true;
    const response = await fetch("/api/summary/key", {method: "DELETE"});
    status.textContent = response.ok ? "API Key 已清除" : "清除失败";
    await refresh();
  });
};
"""
