from fastapi import FastAPI
from pydantic import BaseModel

from src.feature.help.feature import HelpFeature


class HelpRequest(BaseModel):
    messages: list[str]


class HelpAdmin:
    def __init__(self, feature: HelpFeature):
        self.feature = feature

    def register_routes(self, app: FastAPI) -> None:
        feature = self.feature

        @app.get("/api/help")
        async def get_config():
            return {"messages": feature.messages.copy()}

        @app.post("/api/help")
        async def save_config(body: HelpRequest):
            feature.save_messages(body.messages)
            return {"messages": feature.messages.copy()}

        @app.post("/api/help/reset")
        async def reset_config():
            return {"messages": feature.reset_messages()}

    @property
    def panel_html(self) -> str:
        return """
<template data-feature-panel="help">
  <div class="feature-extra">
    <p>每一行会作为 QQ 聊天记录中的一条消息发送，空行会被忽略。</p>
    <textarea spellcheck="false"></textarea>
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
  const textarea = row.querySelector("textarea");
  const button = row.querySelector(".save-help");
  const resetButton = row.querySelector(".reset-help");
  const status = row.querySelector(".status");
  const response = await fetch("/api/help");
  if (!response.ok) throw new Error("加载 /help 文本失败");
  textarea.value = (await response.json()).messages.join("\n");
  button.addEventListener("click", async () => {
    status.textContent = "保存中...";
    button.disabled = true;
    const messages = textarea.value.split("\n").map(line => line.trim()).filter(Boolean);
    const result = await fetch("/api/help", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({messages}),
    });
    button.disabled = false;
    if (!result.ok) { status.textContent = "保存失败"; return; }
    textarea.value = (await result.json()).messages.join("\n");
    status.textContent = "已保存";
  });
  resetButton.addEventListener("click", async () => {
    if (!window.confirm("确定恢复并保存代码中的默认帮助文本吗？")) return;
    resetButton.disabled = true;
    status.textContent = "正在恢复...";
    const result = await fetch("/api/help/reset", {method: "POST"});
    resetButton.disabled = false;
    if (!result.ok) { status.textContent = "恢复失败"; return; }
    textarea.value = (await result.json()).messages.join("\n");
    status.textContent = "已恢复并保存默认文本";
  });
};
"""
