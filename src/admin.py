from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from src.feature.manager import FeatureManager


class ToggleRequest(BaseModel):
    enabled: bool


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

    loadFeatures().catch(err => {
      errorEl.textContent = err.message;
    });
  </script>
</body>
</html>
"""
