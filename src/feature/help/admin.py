from fastapi import FastAPI
from pydantic import BaseModel

from src.feature.help.feature import HelpFeature


class HelpRequest(BaseModel):
    public_messages: list[str]
    master_messages: list[str]


class HelpAdmin:
    def __init__(self, feature: HelpFeature):
        self.feature = feature

    def register_routes(self, app: FastAPI) -> None:
        feature = self.feature

        @app.get("/api/help")
        async def get_config():
            return self._response()

        @app.post("/api/help")
        async def save_config(body: HelpRequest):
            feature.save_messages(body.public_messages, body.master_messages)
            return self._response()

        @app.post("/api/help/reset")
        async def reset_config():
            feature.reset_messages()
            return self._response()

    def _response(self) -> dict[str, list[str]]:
        return {
            "public_messages": self.feature.public_messages.copy(),
            "master_messages": self.feature.master_messages.copy(),
        }

    @property
    def panel_html(self) -> str:
        return """
<template data-feature-panel="help">
  <div class="feature-extra">
    <p>使用单独一行的 <code>---</code> 分隔聊天记录消息；普通换行会保留在同一条消息中。</p>
    <h3>普通用户帮助文本</h3>
    <p>非主人发送 <code>/help</code> 时使用。</p>
    <textarea class="public-help" spellcheck="false"></textarea>
    <h3>主人帮助文本</h3>
    <p>当前主人发送 <code>/help</code> 时使用，可包含主人专属指令。</p>
    <textarea class="master-help" spellcheck="false"></textarea>
    <div>
      <button class="save-help" type="button">保存 /help 文本</button>
      <button class="secondary reset-help" type="button">一键恢复默认文本</button>
      <span class="status"></span>
    </div>
  </div>
</template>
"""

    @property
    def javascript(self) -> str:
        return r"""
featureAdminControls.help = async row => {
  const separator = "\n---\n";
  const formatMessages = messages => messages.join(separator);
  const parseMessages = text => text
    .split(/^[ \t]*---[ \t]*$/m)
    .map(message => message.trim())
    .filter(Boolean);
  const publicTextarea = row.querySelector(".public-help");
  const masterTextarea = row.querySelector(".master-help");
  const button = row.querySelector(".save-help");
  const resetButton = row.querySelector(".reset-help");
  const status = row.querySelector(".status");
  const response = await fetch("/api/help");
  if (!response.ok) throw new Error("加载 /help 文本失败");
  const initial = await response.json();
  publicTextarea.value = formatMessages(initial.public_messages);
  masterTextarea.value = formatMessages(initial.master_messages);
  button.addEventListener("click", async () => {
    status.textContent = "保存中...";
    button.disabled = true;
    const result = await fetch("/api/help", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        public_messages: parseMessages(publicTextarea.value),
        master_messages: parseMessages(masterTextarea.value),
      }),
    });
    button.disabled = false;
    if (!result.ok) { status.textContent = "保存失败"; return; }
    const data = await result.json();
    publicTextarea.value = formatMessages(data.public_messages);
    masterTextarea.value = formatMessages(data.master_messages);
    status.textContent = "已保存";
  });
  resetButton.addEventListener("click", async () => {
    if (!window.confirm("确定恢复并保存代码中的默认帮助文本吗？")) return;
    resetButton.disabled = true;
    status.textContent = "正在恢复...";
    const result = await fetch("/api/help/reset", {method: "POST"});
    resetButton.disabled = false;
    if (!result.ok) { status.textContent = "恢复失败"; return; }
    const data = await result.json();
    publicTextarea.value = formatMessages(data.public_messages);
    masterTextarea.value = formatMessages(data.master_messages);
    status.textContent = "已恢复并保存默认文本";
  });
};
"""
