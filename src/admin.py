from collections.abc import Iterable

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from src.feature.atom import FeaturePlugin
from src.feature.manager import FeatureManager


class ToggleRequest(BaseModel):
    enabled: bool


def create_admin_app(
    feature_manager: FeatureManager,
    plugins: Iterable[FeaturePlugin] = (),
) -> FastAPI:
    plugins = list(plugins)
    app = FastAPI(title="MyQQbot Admin")

    for plugin in plugins:
        if plugin.admin is not None:
            plugin.admin.register_routes(app)

    @app.get("/", response_class=HTMLResponse)
    async def index():
        panels = "\n".join(
            plugin.admin.panel_html
            for plugin in plugins
            if plugin.admin is not None
        )
        scripts = "\n".join(
            plugin.admin.javascript
            for plugin in plugins
            if plugin.admin is not None
        )
        return HTML.replace("<!-- FEATURE_PANELS -->", panels).replace(
            "/* FEATURE_SCRIPTS */",
            scripts,
        )

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
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>MyQQbot Feature 管理</title>
  <style>
    body { margin: 0; background: #f6f7f9; color: #1f2937; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
    main { max-width: 880px; margin: 48px auto; padding: 0 20px; }
    h1 { margin: 0 0 8px; font-size: 28px; }
    .hint, .feature p, .status { color: #667085; }
    .hint { margin-bottom: 24px; }
    .feature { background: white; border: 1px solid #e5e7eb; border-radius: 8px; padding: 18px 20px; margin-bottom: 12px; }
    .feature-head { display: flex; align-items: center; justify-content: space-between; gap: 20px; }
    .feature h2 { margin: 0 0 6px; font-size: 18px; }
    .feature p { margin: 0; line-height: 1.5; }
    .switch { position: relative; width: 54px; height: 30px; flex: 0 0 auto; }
    .switch input { opacity: 0; width: 0; height: 0; }
    .slider { position: absolute; cursor: pointer; inset: 0; background: #d0d5dd; border-radius: 999px; transition: .2s; }
    .slider:before { position: absolute; content: ""; height: 24px; width: 24px; left: 3px; top: 3px; background: white; border-radius: 50%; transition: .2s; box-shadow: 0 1px 3px rgba(16,24,40,.2); }
    input:checked + .slider { background: #2563eb; }
    input:checked + .slider:before { transform: translateX(24px); }
    .feature-extra { margin-top: 16px; padding-top: 16px; border-top: 1px solid #e5e7eb; }
    textarea, input[type="password"], input[type="text"] { width: 100%; box-sizing: border-box; border: 1px solid #d0d5dd; border-radius: 8px; padding: 12px; color: #1f2937; font: 14px/1.5 ui-monospace, monospace; }
    textarea { min-height: 180px; resize: vertical; }
    input[type="password"], input[type="text"] { margin-top: 10px; }
    button { margin-top: 12px; border: 0; border-radius: 8px; background: #2563eb; color: white; padding: 10px 16px; cursor: pointer; font-weight: 600; }
    button:disabled { background: #98a2b3; cursor: not-allowed; }
    .status { margin-left: 12px; }
    .danger { background: #b42318; }
    .secondary { background: #667085; margin-left: 8px; }
    .oauth-link { display: inline-block; margin: 12px 0 0 10px; }
    .error { color: #b42318; margin-top: 16px; }
  </style>
</head>
<body>
  <main>
    <h1>MyQQbot Feature 管理</h1>
    <div class="hint">动态开关机器人功能，功能自己的设置由对应模块提供。</div>
    <section id="features"></section>
    <div id="error" class="error"></div>
  </main>
  <!-- FEATURE_PANELS -->
  <script>
    const featureAdminControls = {};
    /* FEATURE_SCRIPTS */

    const featuresEl = document.querySelector("#features");
    const errorEl = document.querySelector("#error");

    async function toggleFeature(name, enabled) {
      const response = await fetch(`/api/features/${name}`, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({enabled}),
      });
      if (!response.ok) throw new Error("保存 Feature 开关失败");
    }

    async function renderFeature(feature) {
      const row = document.createElement("article");
      row.className = "feature";
      const head = document.createElement("div");
      head.className = "feature-head";
      const summary = document.createElement("div");
      const title = document.createElement("h2");
      const description = document.createElement("p");
      title.textContent = feature.title;
      description.textContent = feature.description;
      summary.append(title, description);
      const label = document.createElement("label");
      label.className = "switch";
      const input = document.createElement("input");
      input.type = "checkbox";
      input.checked = feature.enabled;
      input.addEventListener("change", async () => {
        try { await toggleFeature(feature.name, input.checked); }
        catch (error) { input.checked = !input.checked; errorEl.textContent = error.message; }
      });
      const slider = document.createElement("span");
      slider.className = "slider";
      label.append(input, slider);
      head.append(summary, label);
      row.appendChild(head);

      const template = document.querySelector(`template[data-feature-panel="${feature.name}"]`);
      if (template) row.appendChild(template.content.cloneNode(true));
      featuresEl.appendChild(row);
      if (featureAdminControls[feature.name]) await featureAdminControls[feature.name](row);
    }

    async function init() {
      const response = await fetch("/api/features");
      if (!response.ok) throw new Error("加载 Feature 列表失败");
      for (const feature of await response.json()) await renderFeature(feature);
    }

    init().catch(error => { errorEl.textContent = error.message; });
    window.addEventListener("unhandledrejection", event => {
      errorEl.textContent = event.reason instanceof Error ? event.reason.message : "操作失败";
    });
  </script>
</body>
</html>
"""
