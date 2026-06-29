import json
from collections.abc import Iterable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

from src.feature.atom import FeaturePlugin
from src.feature.manager import FeatureManager
from src.logging_service import application_logs


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

    @app.get("/api/logs/history")
    async def log_history(limit: int = 500):
        entries = application_logs.history(limit)
        return {
            "entries": [entry.as_dict() for entry in entries],
            "last_id": entries[-1].id if entries else 0,
        }

    @app.get("/api/logs/stream")
    async def log_stream(request: Request, after_id: int = 0):
        last_event_id = request.headers.get("last-event-id")
        if last_event_id and last_event_id.isdigit():
            after_id = max(after_id, int(last_event_id))

        async def events():
            async for entry in application_logs.subscribe(after_id):
                if entry is None:
                    yield ": keepalive\n\n"
                    continue
                data = json.dumps(entry.as_dict(), ensure_ascii=False)
                yield f"id: {entry.id}\nevent: log\ndata: {data}\n\n"

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    return app


HTML = """
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>MyQQbot 管理中心</title>
  <style>
    :root { color-scheme: light; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
    * { box-sizing: border-box; }
    body { margin: 0; background: #f6f7f9; color: #1f2937; }
    .topbar { position: sticky; top: 0; z-index: 10; display: flex; align-items: center; gap: 28px; padding: 0 max(20px, calc((100vw - 1080px) / 2)); min-height: 64px; background: #111827; color: white; box-shadow: 0 1px 4px rgba(0,0,0,.18); }
    .brand { font-size: 18px; font-weight: 700; white-space: nowrap; }
    nav { display: flex; align-self: stretch; }
    nav a { display: flex; align-items: center; padding: 0 18px; color: #cbd5e1; text-decoration: none; border-bottom: 3px solid transparent; }
    nav a:hover, nav a.active { color: white; background: #1f2937; border-bottom-color: #60a5fa; }
    main { max-width: 1080px; margin: 36px auto; padding: 0 20px 48px; }
    .page-head { display: flex; justify-content: space-between; align-items: end; gap: 20px; margin-bottom: 22px; }
    h1 { margin: 0 0 7px; font-size: 28px; }
    .hint, .feature p, .status { color: #667085; }
    .hint { margin: 0; }
    .view[hidden] { display: none; }
    .feature { background: white; border: 1px solid #e5e7eb; border-radius: 10px; margin-bottom: 12px; overflow: hidden; transition: box-shadow .2s, border-color .2s; }
    .feature.expanded { border-color: #bfdbfe; box-shadow: 0 5px 18px rgba(37,99,235,.08); }
    .feature-head { display: flex; align-items: center; justify-content: space-between; gap: 20px; padding: 18px 20px; }
    .feature-summary { min-width: 0; }
    .feature h2 { margin: 0 0 6px; font-size: 18px; }
    .feature p { margin: 0; line-height: 1.5; }
    .feature-actions { display: flex; align-items: center; gap: 14px; flex: 0 0 auto; }
    .expand-button { margin: 0; min-width: 72px; background: #eef2ff; color: #3730a3; }
    .feature-details { padding: 0 20px 20px; }
    .feature-details[hidden] { display: none; }
    .switch { position: relative; width: 54px; height: 30px; flex: 0 0 auto; }
    .switch input { opacity: 0; width: 0; height: 0; }
    .slider { position: absolute; cursor: pointer; inset: 0; background: #d0d5dd; border-radius: 999px; transition: .2s; }
    .slider:before { position: absolute; content: ""; height: 24px; width: 24px; left: 3px; top: 3px; background: white; border-radius: 50%; transition: .2s; box-shadow: 0 1px 3px rgba(16,24,40,.2); }
    input:checked + .slider { background: #2563eb; }
    input:checked + .slider:before { transform: translateX(24px); }
    .feature-extra { padding-top: 16px; border-top: 1px solid #e5e7eb; }
    textarea, select, input[type="password"], input[type="text"] { width: 100%; border: 1px solid #d0d5dd; border-radius: 8px; padding: 12px; color: #1f2937; font: 14px/1.5 ui-monospace, monospace; }
    textarea { min-height: 180px; resize: vertical; }
    select, input[type="password"], input[type="text"] { margin-top: 10px; }
    button { margin-top: 12px; border: 0; border-radius: 8px; background: #2563eb; color: white; padding: 10px 16px; cursor: pointer; font-weight: 600; }
    button:disabled { background: #98a2b3; cursor: not-allowed; }
    .status { margin-left: 12px; }
    .danger { background: #b42318; }
    .secondary { background: #667085; margin-left: 8px; }
    .token-guide { margin: 14px 0; padding: 14px 16px; border: 1px solid #bfdbfe; border-radius: 8px; background: #eff6ff; }
    .token-guide h3 { margin: 0 0 8px; font-size: 16px; }
    .token-guide ol { margin: 8px 0; padding-left: 22px; line-height: 1.7; }
    .token-guide code { padding: 2px 5px; border-radius: 4px; background: #dbeafe; }
    .error { color: #b42318; margin-top: 16px; }
    .log-toolbar { display: flex; align-items: center; flex-wrap: wrap; gap: 14px; }
    .log-toolbar button { margin: 0; }
    .stream-status { font-size: 14px; color: #667085; }
    .stream-status.live { color: #067647; }
    .log-console { min-height: 520px; max-height: calc(100vh - 230px); overflow: auto; padding: 10px 0; border: 1px solid #334155; border-radius: 10px; background: #0f172a; color: #e2e8f0; box-shadow: inset 0 1px 8px rgba(0,0,0,.25); font: 13px/1.55 ui-monospace, SFMono-Regular, Consolas, monospace; }
    .log-row { display: grid; grid-template-columns: 190px 70px 170px minmax(0,1fr); gap: 10px; padding: 5px 14px; border-bottom: 1px solid rgba(148,163,184,.08); }
    .log-row:hover { background: rgba(148,163,184,.08); }
    .log-time, .log-name { color: #94a3b8; }
    .log-message { min-width: 0; white-space: pre-wrap; overflow-wrap: anywhere; }
    .level-DEBUG { color: #94a3b8; }
    .level-INFO { color: #7dd3fc; }
    .level-WARNING { color: #fbbf24; }
    .level-ERROR, .level-CRITICAL { color: #fda4af; font-weight: 700; }
    .empty-setting { padding-top: 16px; border-top: 1px solid #e5e7eb; color: #98a2b3; }
    @media (max-width: 720px) {
      .topbar { gap: 10px; padding: 0 12px; }
      nav a { padding: 0 10px; }
      .feature-head { align-items: flex-start; }
      .feature-actions { flex-direction: column-reverse; align-items: flex-end; }
      .log-row { grid-template-columns: 1fr; gap: 1px; padding: 9px 12px; }
    }
  </style>
</head>
<body>
  <header class="topbar">
    <div class="brand">MyQQbot 管理中心</div>
    <nav aria-label="管理页面导航">
      <a href="#features" data-view-link="features">功能管理</a>
      <a href="#logs" data-view-link="logs">实时日志</a>
    </nav>
  </header>
  <main>
    <section id="view-features" class="view">
      <div class="page-head"><div><h1>功能管理</h1><p class="hint">卡片默认折叠；开关始终可用，展开后可以修改详细设置。</p></div></div>
      <div id="features"></div>
    </section>
    <section id="view-logs" class="view" hidden>
      <div class="page-head">
        <div><h1>实时日志</h1><p class="hint">显示当前进程最近 1000 条日志，并实时接收新日志。</p></div>
        <div class="log-toolbar">
          <label><input id="auto-scroll" type="checkbox" checked> 自动滚动</label>
          <button id="clear-logs" class="secondary" type="button">清空当前显示</button>
          <span id="stream-status" class="stream-status">未连接</span>
        </div>
      </div>
      <div id="log-console" class="log-console" role="log" aria-live="polite"></div>
    </section>
    <div id="error" class="error"></div>
  </main>
  <!-- FEATURE_PANELS -->
  <script>
    const featureAdminControls = {};
    /* FEATURE_SCRIPTS */

    const featuresEl = document.querySelector("#features");
    const errorEl = document.querySelector("#error");
    const logConsole = document.querySelector("#log-console");
    const streamStatus = document.querySelector("#stream-status");
    const autoScroll = document.querySelector("#auto-scroll");
    let logSource = null;
    let lastRenderedLogId = 0;

    async function toggleFeature(name, enabled) {
      const response = await fetch(`/api/features/${name}`, {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({enabled}),
      });
      if (!response.ok) throw new Error("保存 Feature 开关失败");
    }

    function renderFeature(feature) {
      const row = document.createElement("article");
      row.className = "feature";
      const head = document.createElement("div");
      head.className = "feature-head";
      const summary = document.createElement("div");
      summary.className = "feature-summary";
      const title = document.createElement("h2");
      const description = document.createElement("p");
      title.textContent = feature.title;
      description.textContent = feature.description;
      summary.append(title, description);

      const actions = document.createElement("div");
      actions.className = "feature-actions";
      const expand = document.createElement("button");
      expand.type = "button";
      expand.className = "expand-button";
      expand.textContent = "展开";
      expand.setAttribute("aria-expanded", "false");
      const label = document.createElement("label");
      label.className = "switch";
      label.title = `启用或关闭 ${feature.title}`;
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
      actions.append(expand, label);
      head.append(summary, actions);

      const details = document.createElement("div");
      details.className = "feature-details";
      details.hidden = true;
      const template = document.querySelector(`template[data-feature-panel="${feature.name}"]`);
      if (template) details.appendChild(template.content.cloneNode(true));
      else {
        const empty = document.createElement("div");
        empty.className = "empty-setting";
        empty.textContent = "此功能暂无详细设置。";
        details.appendChild(empty);
      }
      row.append(head, details);
      featuresEl.appendChild(row);

      let initialized = false;
      expand.addEventListener("click", async () => {
        const opening = details.hidden;
        details.hidden = !opening;
        row.classList.toggle("expanded", opening);
        expand.textContent = opening ? "收起" : "展开";
        expand.setAttribute("aria-expanded", String(opening));
        if (opening && !initialized && featureAdminControls[feature.name]) {
          initialized = true;
          try { await featureAdminControls[feature.name](row); }
          catch (error) { initialized = false; errorEl.textContent = error.message; }
        }
      });
    }

    async function loadFeatures() {
      const response = await fetch("/api/features");
      if (!response.ok) throw new Error("加载 Feature 列表失败");
      for (const feature of await response.json()) renderFeature(feature);
    }

    function appendLog(entry) {
      if (entry.id <= lastRenderedLogId) return;
      lastRenderedLogId = entry.id;
      const row = document.createElement("div");
      row.className = "log-row";
      const time = document.createElement("span");
      const level = document.createElement("span");
      const name = document.createElement("span");
      const message = document.createElement("span");
      time.className = "log-time";
      level.className = `level-${entry.level}`;
      name.className = "log-name";
      message.className = "log-message";
      time.textContent = new Date(entry.timestamp).toLocaleString();
      level.textContent = entry.level;
      name.textContent = entry.logger;
      message.textContent = entry.message;
      row.append(time, level, name, message);
      logConsole.appendChild(row);
      while (logConsole.children.length > 2000) logConsole.firstElementChild.remove();
      if (autoScroll.checked) logConsole.scrollTop = logConsole.scrollHeight;
    }

    function stopLogs() {
      if (logSource) { logSource.close(); logSource = null; }
      streamStatus.textContent = "未连接";
      streamStatus.classList.remove("live");
    }

    async function startLogs() {
      stopLogs();
      logConsole.replaceChildren();
      lastRenderedLogId = 0;
      streamStatus.textContent = "正在加载历史日志...";
      const response = await fetch("/api/logs/history?limit=500");
      if (!response.ok) throw new Error("加载历史日志失败");
      const history = await response.json();
      for (const entry of history.entries) appendLog(entry);
      logSource = new EventSource(`/api/logs/stream?after_id=${history.last_id}`);
      logSource.addEventListener("log", event => appendLog(JSON.parse(event.data)));
      logSource.onopen = () => {
        streamStatus.textContent = "实时连接正常";
        streamStatus.classList.add("live");
      };
      logSource.onerror = () => {
        streamStatus.textContent = "连接中断，正在自动重连...";
        streamStatus.classList.remove("live");
      };
    }

    function showView(name) {
      const view = name === "logs" ? "logs" : "features";
      document.querySelectorAll(".view").forEach(el => { el.hidden = el.id !== `view-${view}`; });
      document.querySelectorAll("[data-view-link]").forEach(link => link.classList.toggle("active", link.dataset.viewLink === view));
      if (view === "logs") startLogs().catch(error => { errorEl.textContent = error.message; });
      else stopLogs();
    }

    document.querySelector("#clear-logs").addEventListener("click", () => logConsole.replaceChildren());
    window.addEventListener("hashchange", () => showView(location.hash.slice(1)));
    window.addEventListener("beforeunload", stopLogs);
    window.addEventListener("unhandledrejection", event => {
      errorEl.textContent = event.reason instanceof Error ? event.reason.message : "操作失败";
    });
    loadFeatures().catch(error => { errorEl.textContent = error.message; });
    showView(location.hash.slice(1));
  </script>
</body>
</html>
"""
