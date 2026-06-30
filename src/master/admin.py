from datetime import UTC, datetime

from fastapi import FastAPI

from src.master.service import MasterService


class MasterAdmin:
    def __init__(self, service: MasterService):
        self.service = service

    def register_routes(self, app: FastAPI) -> None:
        service = self.service

        @app.get("/api/master")
        async def status():
            return {
                "configured": service.master_id is not None,
                "master_id": service.master_id,
            }

        @app.post("/api/master/code")
        async def generate_code():
            code, expires_at = service.generate_code()
            return {
                "code": code,
                "command": f"/set-master {code}",
                "expires_at": expires_at.isoformat(),
                "expires_in": max(
                    0,
                    int((expires_at - datetime.now(UTC)).total_seconds()),
                ),
            }

    @property
    def view_html(self) -> str:
        return """
<section id="view-master" class="view" hidden>
  <div class="page-head">
    <div><h1>主人管理</h1><p class="hint">绑定机器人主人，为聊天中的管理操作提供身份认证。</p></div>
  </div>
  <article class="feature expanded">
    <div class="feature-head">
      <div class="feature-summary">
        <h2>主人身份</h2>
        <p class="master-summary">正在读取主人绑定状态...</p>
      </div>
    </div>
    <div class="feature-details">
      <div class="feature-extra">
    <div class="token-guide">
      <h3>绑定或更换主人</h3>
      <ol>
        <li>点击下方按钮生成一次性鉴别码。</li>
        <li>使用准备绑定的 QQ 账号，把完整指令发送给机器人。</li>
        <li>绑定成功后鉴别码立即失效；新主人 ID 会持久保存。</li>
      </ol>
      <p><strong>安全提示：</strong>不要把鉴别码发送给其他人。生成新码会立即废弃旧码。</p>
    </div>
    <button class="generate-master-code" type="button">生成鉴别码</button>
    <button class="secondary copy-master-command" type="button" disabled>复制指令</button>
    <span class="master-status status"></span>
    <input class="master-command" type="text" readonly placeholder="生成后将在这里显示 /set-master 指令">
      </div>
    </div>
  </article>
</section>
"""

    @property
    def javascript(self) -> str:
        return r"""
async function initMasterView() {
  const row = document.querySelector("#view-master");
  const summary = row.querySelector(".master-summary");
  const generate = row.querySelector(".generate-master-code");
  const copy = row.querySelector(".copy-master-command");
  const command = row.querySelector(".master-command");
  const status = row.querySelector(".master-status");
  const response = await fetch("/api/master");
  if (!response.ok) throw new Error("加载主人绑定状态失败");
  const data = await response.json();
  summary.textContent = data.configured
    ? `当前主人 QQ ID：${data.master_id}`
    : "当前尚未绑定主人。";
  if (row.dataset.initialized !== "true") {
    row.dataset.initialized = "true";
    generate.addEventListener("click", async () => {
      generate.disabled = true;
      status.textContent = "正在生成...";
      const result = await fetch("/api/master/code", {method: "POST"});
      generate.disabled = false;
      const body = await result.json().catch(() => ({}));
      if (!result.ok) { status.textContent = body.detail || "生成失败"; return; }
      command.value = body.command;
      copy.disabled = false;
      status.textContent = `鉴别码将在 ${body.expires_in} 秒后失效`;
    });
    copy.addEventListener("click", async () => {
      await navigator.clipboard.writeText(command.value);
      status.textContent = "指令已复制";
    });
  }
};
"""
