import json
from collections.abc import Iterable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from src.admin_auth import AdminAuth, COOKIE_NAME, SESSION_TTL_SECONDS
from src.feature.atom import FeaturePlugin
from src.feature.manager import FeatureManager
from src.logging_service import application_logs
from src.master import MasterService
from src.master.admin import MasterAdmin


class ToggleRequest(BaseModel):
    enabled: bool


class LoginRequest(BaseModel):
    password: str


def create_admin_app(
    feature_manager: FeatureManager,
    master_service: MasterService,
    plugins: Iterable[FeaturePlugin] = (),
) -> FastAPI:
    plugins = list(plugins)
    app = FastAPI(title="MyQQbot Admin")
    master_admin = MasterAdmin(master_service)
    master_admin.register_routes(app)

    for plugin in plugins:
        if plugin.admin is not None:
            plugin.admin.register_routes(app)

    auth = AdminAuth()

    def cookie_is_valid(request: Request) -> bool:
        return auth.validate(request.cookies.get(COOKIE_NAME))

    def _secure_cookie(request: Request) -> bool:
        # 经 nginx 反代时用 X-Forwarded-Proto 判断；直连回环 HTTP 时允许不加密 cookie 便于调试。
        return (request.headers.get("x-forwarded-proto") == "https") or (request.url.scheme == "https")

    @app.middleware("http")
    async def require_login(request: Request, call_next):
        if (request.method, request.url.path) in {("POST", "/api/login"), ("GET", "/api/session"), ("POST", "/api/logout")}:
            return await call_next(request)
        if request.method == "GET" and request.url.path == "/":
            # 登录页外壳公开；所有数据端点（/api/*、/memory/* 等）由下方鉴权保护。
            return await call_next(request)
        if not cookie_is_valid(request):
            return JSONResponse(status_code=401, content={"detail": "未登录或会话已过期"})
        return await call_next(request)

    @app.post("/api/login")
    async def login(body: LoginRequest, request: Request):
        if not auth.verify(body.password):
            raise HTTPException(status_code=401, detail="密码错误")
        response = JSONResponse(content={"authenticated": True})
        response.set_cookie(
            COOKIE_NAME,
            auth.issue(),
            max_age=SESSION_TTL_SECONDS,
            httponly=True,
            samesite="lax",
            secure=_secure_cookie(request),
            path="/",
        )
        return response

    @app.get("/api/session")
    async def session(request: Request):
        return {"authenticated": cookie_is_valid(request)}

    @app.post("/api/logout")
    async def logout():
        response = JSONResponse(content={"ok": True})
        response.delete_cookie(COOKIE_NAME, path="/")
        return response

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
        return (
            HTML.replace("<!-- MASTER_VIEW -->", master_admin.view_html)
            .replace("<!-- FEATURE_PANELS -->", panels)
            .replace("/* FEATURE_SCRIPTS */", scripts)
            .replace("/* MASTER_SCRIPT */", master_admin.javascript)
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
    .settings-section { margin: 12px 0; border: 1px solid #dbe3ef; border-radius: 9px; background: #f8fafc; overflow: hidden; }
    .settings-section[open] { background: white; border-color: #bfdbfe; }
    .settings-section > summary { display: flex; align-items: center; justify-content: space-between; gap: 16px; padding: 15px 16px; cursor: pointer; list-style: none; user-select: none; }
    .settings-section > summary::-webkit-details-marker { display: none; }
    .settings-section > summary span { font-size: 16px; font-weight: 700; }
    .settings-section > summary small { margin-left: auto; color: #667085; }
    .settings-section > summary:after { content: "展开"; min-width: 32px; color: #2563eb; font-size: 13px; font-weight: 600; }
    .settings-section[open] > summary { border-bottom: 1px solid #dbeafe; background: #eff6ff; }
    .settings-section[open] > summary:after { content: "收起"; }
    .settings-section-body { padding: 15px 16px 17px; }
    .settings-section-body > h3 { margin: 14px 0 8px; font-size: 16px; }
    .settings-section-body > h3:first-child { margin-top: 0; }
    .settings-subsection { margin-top: 18px; padding-top: 16px; border-top: 1px solid #e5e7eb; }
    .settings-subsection h3 { margin: 0 0 8px; font-size: 16px; }
    .settings-save-bar { display: flex; align-items: center; flex-wrap: wrap; padding-top: 4px; }
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
    #login-mask { position: fixed; inset: 0; z-index: 100; display: flex; align-items: center; justify-content: center; background: rgba(15, 23, 42, .62); backdrop-filter: blur(2px); }
    #login-mask[hidden] { display: none; }
    .login-card { width: min(360px, calc(100vw - 40px)); background: white; border-radius: 14px; padding: 26px 24px 22px; box-shadow: 0 18px 50px rgba(0, 0, 0, .35); }
    .login-card h1 { font-size: 20px; margin: 0 0 6px; }
    .login-card p { color: #667085; margin: 0 0 4px; font-size: 14px; }
    .login-card input { margin-top: 14px; }
    .login-card button { width: 100%; margin-top: 14px; }
    .login-error { color: #b42318; font-size: 13px; min-height: 18px; margin-top: 10px; }
    .logout-button { background: #667085; margin-left: auto; }
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
  <div id="login-mask" hidden>
    <form class="login-card" id="login-form">
      <h1>🔐 MyQQbot 管理中心</h1>
      <p>请输入管理密码以继续</p>
      <input id="login-password" type="password" placeholder="管理密码" autocomplete="current-password" autofocus>
      <button type="submit" id="login-button">登录</button>
      <div id="login-error" class="login-error"></div>
    </form>
  </div>
  <header class="topbar">
    <div class="brand">MyQQbot 管理中心</div>
    <nav aria-label="管理页面导航">
      <a href="#features" data-view-link="features">功能管理</a>
      <a href="#logs" data-view-link="logs">实时日志</a>
      <a href="#master" data-view-link="master">主人管理</a>
    </nav>
    <button id="logout-button" class="logout-button" type="button">退出登录</button>
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
    <!-- MASTER_VIEW -->
    <div id="error" class="error"></div>
  </main>
  <!-- FEATURE_PANELS -->
  <script>
    const featureAdminControls = {};
    /* FEATURE_SCRIPTS */
    /* MASTER_SCRIPT */

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
      const view = ["logs", "master"].includes(name) ? name : "features";
      document.querySelectorAll(".view").forEach(el => { el.hidden = el.id !== `view-${view}`; });
      document.querySelectorAll("[data-view-link]").forEach(link => link.classList.toggle("active", link.dataset.viewLink === view));
      if (view === "logs") startLogs().catch(error => { errorEl.textContent = error.message; });
      else {
        stopLogs();
        if (view === "master") initMasterView().catch(error => { errorEl.textContent = error.message; });
      }
    }

    document.querySelector("#clear-logs").addEventListener("click", () => logConsole.replaceChildren());
    window.addEventListener("hashchange", () => showView(location.hash.slice(1)));
    window.addEventListener("beforeunload", stopLogs);
    window.addEventListener("unhandledrejection", event => {
      errorEl.textContent = event.reason instanceof Error ? event.reason.message : "操作失败";
    });

    const loginMask = document.querySelector("#login-mask");
    const loginForm = document.querySelector("#login-form");
    const loginPassword = document.querySelector("#login-password");
    const loginButton = document.querySelector("#login-button");
    const loginError = document.querySelector("#login-error");

    function showLogin(message) {
      stopLogs();
      document.querySelectorAll(".view").forEach(el => { el.hidden = true; });
      loginMask.hidden = false;
      loginError.textContent = message || "";
      loginPassword.focus();
    }

    function hideLogin() { loginMask.hidden = true; }

    // 拦截任意 API 401（会话过期/未登录）：直接弹回登录界面。
    const originalFetch = window.fetch.bind(window);
    window.fetch = async (...args) => {
      const response = await originalFetch(...args);
      if (response.status === 401 && !String(args[0]).endsWith("/api/session") && !String(args[0]).endsWith("/api/login")) {
        showLogin("登录已过期，请重新输入密码");
      }
      return response;
    };

    loginForm.addEventListener("submit", async event => {
      event.preventDefault();
      const password = loginPassword.value;
      loginButton.disabled = true;
      loginError.textContent = "";
      try {
        const response = await originalFetch("/api/login", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({password}),
        });
        if (response.ok) { location.reload(); return; }
        const data = await response.json().catch(() => ({}));
        loginError.textContent = data.detail || "密码错误";
      } catch (error) { loginError.textContent = error.message; }
      finally { loginButton.disabled = false; loginPassword.select(); }
    });

    document.querySelector("#logout-button").addEventListener("click", async () => {
      try { await originalFetch("/api/logout", {method: "POST"}); } finally { showLogin(); }
    });

    async function boot() {
      try {
        const session = await (await originalFetch("/api/session")).json();
        if (!session.authenticated) { showLogin(); return; }
        hideLogin();
      } catch { showLogin(); return; }
      try { await loadFeatures(); } catch (error) { errorEl.textContent = error.message; }
      showView(location.hash.slice(1));
    }
    boot();
  </script>
</body>
</html>
"""
