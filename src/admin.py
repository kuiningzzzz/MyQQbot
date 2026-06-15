from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from src.feature.manager import FeatureManager


class ToggleRequest(BaseModel):
    enabled: bool


class HelpRequest(BaseModel):
    messages: list[str]


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
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 20px;
      margin-bottom: 12px;
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
    .panel {
      background: white;
      border: 1px solid #e5e7eb;
      border-radius: 8px;
      padding: 18px 20px;
      margin-top: 24px;
    }
    .panel h2 {
      margin: 0 0 8px;
      font-size: 20px;
    }
    .panel p {
      margin: 0 0 14px;
      color: #667085;
      line-height: 1.5;
    }
    textarea {
      width: 100%;
      min-height: 180px;
      box-sizing: border-box;
      resize: vertical;
      border: 1px solid #d0d5dd;
      border-radius: 8px;
      padding: 12px;
      color: #1f2937;
      font: 14px/1.5 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
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
  </style>
</head>
<body>
  <main>
    <h1>MyQQbot Feature 管理</h1>
    <div class="hint">动态开关机器人功能，状态会保存到 data/features.json。</div>
    <section id="features"></section>
    <section class="panel">
      <h2>/help 返回内容</h2>
      <p>每一行会作为 QQ 聊天记录中的一条消息发送，空行会被忽略。</p>
      <textarea id="helpMessages" spellcheck="false"></textarea>
      <div>
        <button id="saveHelp" type="button">保存 /help 文本</button>
        <span id="helpStatus" class="status"></span>
      </div>
    </section>
    <div id="error" class="error"></div>
  </main>
  <script>
    const featuresEl = document.querySelector("#features");
    const errorEl = document.querySelector("#error");
    const helpMessagesEl = document.querySelector("#helpMessages");
    const saveHelpEl = document.querySelector("#saveHelp");
    const helpStatusEl = document.querySelector("#helpStatus");

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
          <div>
            <h2>${feature.title}</h2>
            <p>${feature.description}</p>
          </div>
          <label class="switch" title="开关 ${feature.title}">
            <input type="checkbox" ${feature.enabled ? "checked" : ""}>
            <span class="slider"></span>
          </label>
        `;
        const input = row.querySelector("input");
        input.addEventListener("change", () => toggleFeature(feature.name, input.checked));
        featuresEl.appendChild(row);
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
      helpStatusEl.textContent = "";
      const res = await fetch("/api/help");
      if (!res.ok) throw new Error("加载 /help 文本失败");
      const data = await res.json();
      helpMessagesEl.value = data.messages.join("\\n");
    }

    async function saveHelpMessages() {
      helpStatusEl.textContent = "保存中...";
      saveHelpEl.disabled = true;
      const messages = helpMessagesEl.value
        .split("\\n")
        .map(line => line.trim())
        .filter(Boolean);
      const res = await fetch("/api/help", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({messages}),
      });
      saveHelpEl.disabled = false;
      if (!res.ok) {
        helpStatusEl.textContent = "保存失败，请稍后重试。";
        return;
      }
      const data = await res.json();
      helpMessagesEl.value = data.messages.join("\\n");
      helpStatusEl.textContent = "已保存";
    }

    saveHelpEl.addEventListener("click", () => {
      saveHelpMessages().catch(err => {
        saveHelpEl.disabled = false;
        helpStatusEl.textContent = err.message;
      });
    });

    loadFeatures().catch(err => {
      errorEl.textContent = err.message;
    });
    loadHelpMessages().catch(err => {
      errorEl.textContent = err.message;
    });
  </script>
</body>
</html>
"""
