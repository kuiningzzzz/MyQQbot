from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from src.feature.manager import FeatureManager


class ToggleRequest(BaseModel):
    enabled: bool


class HelpRequest(BaseModel):
    messages: list[str]


class PixivTokenRequest(BaseModel):
    refresh_token: str


def create_admin_app(feature_manager: FeatureManager) -> FastAPI:
    app = FastAPI(title="MyQQbot Admin")

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return HTML

    @app.get("/api/features")
    async def list_features():
        return [info.__dict__ for info in feature_manager.list_features()]

    @app.post("/api/features/{name}")
    async def toggle_feature(name: str, body: ToggleRequest):
        try:
            info = feature_manager.set_enabled(name, body.enabled)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="feature not found") from exc
        return info.__dict__

    @app.get("/api/help")
    async def get_help_messages():
        return {"messages": feature_manager.get_help_messages()}

    @app.post("/api/help")
    async def save_help_messages(body: HelpRequest):
        try:
            messages = feature_manager.set_help_messages(body.messages)
        except RuntimeError as exc:
            raise HTTPException(status_code=404, detail="help feature not found") from exc
        return {"messages": messages}

    @app.post("/api/jm/cleanup")
    async def cleanup_jm_storage():
        try:
            await feature_manager.cleanup_jm_storage()
        except RuntimeError as exc:
            message = str(exc)
            if message == "jm feature is busy":
                raise HTTPException(status_code=409, detail="JM 功能正在执行任务，请稍后再清理") from exc
            raise HTTPException(status_code=404, detail="jm feature not found") from exc
        return {"ok": True}

    @app.post("/api/pixiv/cleanup")
    async def cleanup_pixiv_storage():
        try:
            await feature_manager.cleanup_pixiv_storage()
        except RuntimeError as exc:
            message = str(exc)
            if message == "pixiv feature is busy":
                raise HTTPException(
                    status_code=409,
                    detail="Pixiv 功能正在执行任务，请稍后再清理",
                ) from exc
            raise HTTPException(status_code=404, detail="pixiv feature not found") from exc
        return {"ok": True}

    @app.get("/api/pixiv/token")
    async def get_pixiv_token_status():
        try:
            configured = feature_manager.get_pixiv_token_status()
        except RuntimeError as exc:
            raise HTTPException(status_code=404, detail="pixiv feature not found") from exc
        return {"configured": configured}

    @app.post("/api/pixiv/token")
    async def save_pixiv_token(body: PixivTokenRequest):
        token = body.refresh_token.strip()
        if not token:
            raise HTTPException(status_code=400, detail="refresh token 不能为空")
        try:
            feature_manager.set_pixiv_refresh_token(token)
        except RuntimeError as exc:
            raise HTTPException(status_code=404, detail="pixiv feature not found") from exc
        return {"configured": True}

    @app.delete("/api/pixiv/token")
    async def clear_pixiv_token():
        try:
            feature_manager.clear_pixiv_refresh_token()
        except RuntimeError as exc:
            raise HTTPException(status_code=404, detail="pixiv feature not found") from exc
        return {"configured": False}

    return app


HTML = """
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>MyQQbot Feature 管理</title>
  <style>
    body {
      margin: 0;
      background: #f6f7f9;
      color: #1f2937;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    main {
      max-width: 880px;
      margin: 48px auto;
      padding: 0 20px;
    }
    h1 {
      margin: 0 0 8px;
      font-size: 28px;
    }
    .hint {
      color: #667085;
      margin-bottom: 24px;
    }
    .feature {
      background: white;
      border: 1px solid #e5e7eb;
      border-radius: 8px;
      padding: 18px 20px;
      margin-bottom: 12px;
    }
    .feature-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 20px;
    }
    .feature h2 {
      margin: 0 0 6px;
      font-size: 18px;
    }
    .feature p {
      margin: 0;
      color: #667085;
      line-height: 1.5;
    }
    .switch {
      position: relative;
      width: 54px;
      height: 30px;
      flex: 0 0 auto;
    }
    .switch input {
      opacity: 0;
      width: 0;
      height: 0;
    }
    .slider {
      position: absolute;
      cursor: pointer;
      inset: 0;
      background: #d0d5dd;
      border-radius: 999px;
      transition: .2s;
    }
    .slider:before {
      position: absolute;
      content: "";
      height: 24px;
      width: 24px;
      left: 3px;
      top: 3px;
      background: white;
      border-radius: 50%;
      transition: .2s;
      box-shadow: 0 1px 3px rgba(16, 24, 40, .2);
    }
    input:checked + .slider {
      background: #2563eb;
    }
    input:checked + .slider:before {
      transform: translateX(24px);
    }
    .error {
      color: #b42318;
      margin-top: 16px;
    }
    .feature-extra {
      margin-top: 16px;
      padding-top: 16px;
      border-top: 1px solid #e5e7eb;
    }
    textarea, input[type="password"] {
      width: 100%;
      box-sizing: border-box;
      border: 1px solid #d0d5dd;
      border-radius: 8px;
      padding: 12px;
      color: #1f2937;
      font: 14px/1.5 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    }
    textarea {
      min-height: 180px;
      resize: vertical;
    }
    input[type="password"] {
      margin-top: 10px;
    }
    button {
      margin-top: 12px;
      border: 0;
      border-radius: 8px;
      background: #2563eb;
      color: white;
      padding: 10px 16px;
      cursor: pointer;
      font-weight: 600;
    }
    button:disabled {
      background: #98a2b3;
      cursor: not-allowed;
    }
    .status {
      margin-left: 12px;
      color: #667085;
    }
    .danger {
      background: #b42318;
    }
    .secondary {
      background: #667085;
      margin-left: 8px;
    }
  </style>
</head>
<body>
  <main>
    <h1>MyQQbot Feature 管理</h1>
    <div class="hint">动态开关机器人功能，状态会保存到 data/features.json。</div>
    <section id="features"></section>
    <div id="error" class="error"></div>
  </main>
  <script>
    const featuresEl = document.querySelector("#features");
    const errorEl = document.querySelector("#error");
    let helpMessages = [];
    let pixivTokenConfigured = false;

    async function loadFeatures() {
      errorEl.textContent = "";
      const res = await fetch("/api/features");
      if (!res.ok) throw new Error("加载 feature 列表失败");
      render(await res.json());
    }

    function render(features) {
      featuresEl.innerHTML = "";
      for (const feature of features) {
        const row = document.createElement("article");
        row.className = "feature";
        row.innerHTML = `
          <div class="feature-head">
            <div>
              <h2>${feature.title}</h2>
              <p>${feature.description}</p>
            </div>
            <label class="switch" title="开关 ${feature.title}">
              <input type="checkbox" ${feature.enabled ? "checked" : ""}>
              <span class="slider"></span>
            </label>
          </div>
        `;
        const input = row.querySelector("input");
        input.addEventListener("change", () => toggleFeature(feature.name, input.checked));
        appendFeatureControls(row, feature);
        featuresEl.appendChild(row);
      }
    }

    function appendFeatureControls(row, feature) {
      if (feature.name === "help") {
        const extra = document.createElement("div");
        extra.className = "feature-extra";
        extra.innerHTML = `
          <p>每一行会作为 QQ 聊天记录中的一条消息发送，空行会被忽略。</p>
          <textarea spellcheck="false"></textarea>
          <div>
            <button type="button">保存 /help 文本</button>
            <span class="status"></span>
          </div>
        `;
        const textarea = extra.querySelector("textarea");
        const button = extra.querySelector("button");
        const status = extra.querySelector(".status");
        textarea.value = helpMessages.join("\\n");
        button.addEventListener("click", () => saveHelpMessages(textarea, button, status));
        row.appendChild(extra);
      }

      if (feature.name === "jm_pdf") {
        const extra = document.createElement("div");
        extra.className = "feature-extra";
        extra.innerHTML = `
          <p>清理会删除 data/jm/tasks 中已完成或失败的空闲任务目录，不会删除正在处理或等待处理的任务。</p>
          <button class="danger" type="button">手动清理存储垃圾</button>
          <span class="status"></span>
        `;
        const button = extra.querySelector("button");
        const status = extra.querySelector(".status");
        button.addEventListener("click", () => cleanupJmStorage(button, status));
        row.appendChild(extra);
      }

      if (feature.name === "pixiv") {
        const extra = document.createElement("div");
        extra.className = "feature-extra";
        extra.innerHTML = `
          <p>Pixiv refresh token：${pixivTokenConfigured ? "已配置" : "未配置"}。后端不会向页面回传已保存的 token。</p>
          <input type="password" autocomplete="new-password" placeholder="粘贴新的 Pixiv refresh token">
          <div>
            <button class="save-token" type="button">保存 token</button>
            <button class="secondary clear-token" type="button">清除 token</button>
            <span class="token-status status"></span>
          </div>
          <p>清理会删除 data/pixiv/tasks 中的空闲任务目录，不会删除正在处理或等待处理的任务。</p>
          <button class="danger" type="button">手动清理 Pixiv 存储垃圾</button>
          <span class="cleanup-status status"></span>
        `;
        const tokenInput = extra.querySelector('input[type="password"]');
        const saveButton = extra.querySelector(".save-token");
        const clearButton = extra.querySelector(".clear-token");
        const tokenStatus = extra.querySelector(".token-status");
        const cleanupButton = extra.querySelector(".danger");
        const cleanupStatus = extra.querySelector(".cleanup-status");
        clearButton.disabled = !pixivTokenConfigured;
        saveButton.addEventListener("click", () => savePixivToken(tokenInput, saveButton, clearButton, tokenStatus));
        clearButton.addEventListener("click", () => clearPixivToken(tokenInput, clearButton, tokenStatus));
        cleanupButton.addEventListener("click", () => cleanupPixivStorage(cleanupButton, cleanupStatus));
        row.appendChild(extra);
      }
    }

    async function toggleFeature(name, enabled) {
      errorEl.textContent = "";
      const res = await fetch(`/api/features/${name}`, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({enabled}),
      });
      if (!res.ok) {
        errorEl.textContent = "保存失败，请刷新后重试。";
        await loadFeatures();
      }
    }

    async function loadHelpMessages() {
      const res = await fetch("/api/help");
      if (!res.ok) throw new Error("加载 /help 文本失败");
      const data = await res.json();
      helpMessages = data.messages;
    }

    async function loadPixivTokenStatus() {
      const res = await fetch("/api/pixiv/token");
      if (!res.ok) throw new Error("加载 Pixiv token 状态失败");
      const data = await res.json();
      pixivTokenConfigured = data.configured;
    }

    async function savePixivToken(input, saveButton, clearButton, status) {
      const refreshToken = input.value.trim();
      if (!refreshToken) {
        status.textContent = "请输入 refresh token";
        return;
      }
      status.textContent = "保存中...";
      saveButton.disabled = true;
      const res = await fetch("/api/pixiv/token", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({refresh_token: refreshToken}),
      });
      saveButton.disabled = false;
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        status.textContent = data.detail || "保存失败，请稍后重试。";
        return;
      }
      input.value = "";
      pixivTokenConfigured = true;
      clearButton.disabled = false;
      status.textContent = "已保存并立即生效";
    }

    async function clearPixivToken(input, clearButton, status) {
      status.textContent = "清除中...";
      clearButton.disabled = true;
      const res = await fetch("/api/pixiv/token", {method: "DELETE"});
      if (!res.ok) {
        clearButton.disabled = false;
        const data = await res.json().catch(() => ({}));
        status.textContent = data.detail || "清除失败，请稍后重试。";
        return;
      }
      input.value = "";
      pixivTokenConfigured = false;
      status.textContent = "已清除";
    }

    async function saveHelpMessages(textarea, button, status) {
      status.textContent = "保存中...";
      button.disabled = true;
      const messages = textarea.value
        .split("\\n")
        .map(line => line.trim())
        .filter(Boolean);
      const res = await fetch("/api/help", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({messages}),
      });
      button.disabled = false;
      if (!res.ok) {
        status.textContent = "保存失败，请稍后重试。";
        return;
      }
      const data = await res.json();
      helpMessages = data.messages;
      textarea.value = data.messages.join("\\n");
      status.textContent = "已保存";
    }

    async function cleanupJmStorage(button, status) {
      status.textContent = "清理中...";
      button.disabled = true;
      const res = await fetch("/api/jm/cleanup", {method: "POST"});
      button.disabled = false;
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        status.textContent = data.detail || "清理失败，请稍后重试。";
        return;
      }
      status.textContent = "已清理";
    }

    async function cleanupPixivStorage(button, status) {
      status.textContent = "清理中...";
      button.disabled = true;
      const res = await fetch("/api/pixiv/cleanup", {method: "POST"});
      button.disabled = false;
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        status.textContent = data.detail || "清理失败，请稍后重试。";
        return;
      }
      status.textContent = "已清理";
    }

    async function init() {
      await loadHelpMessages();
      await loadPixivTokenStatus();
      await loadFeatures();
    }

    init().catch(err => {
      errorEl.textContent = err.message;
    });

    window.addEventListener("unhandledrejection", event => {
      const reason = event.reason;
      if (reason instanceof Error) {
        errorEl.textContent = reason.message;
      } else {
        errorEl.textContent = "操作失败，请稍后重试。";
      }
    });
  </script>
</body>
</html>
"""
