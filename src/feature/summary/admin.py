from dataclasses import asdict

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from src.feature.summary.character import Character
from src.feature.summary.feature import SummaryFeature


class AiConfigRequest(BaseModel):
    api_url: str
    model: str
    api_key: str | None = None
    summary_enabled: bool
    chat_enabled: bool
    chat_window_min: int
    chat_window_max: int
    thinking_mode: str
    reasoning_effort: str
    context_token_limit: int
    max_tokens: int


class CharacterRequest(BaseModel):
    original_name: str | None = None
    name: str
    trigger_name: str
    description: str
    prompt: str


class SummaryAdmin:
    def __init__(self, feature: SummaryFeature):
        self.feature = feature

    def register_routes(self, app: FastAPI) -> None:
        feature = self.feature

        @app.get("/api/summary/config")
        async def get_config():
            return feature.config_status

        @app.post("/api/summary/config")
        async def save_config(body: AiConfigRequest):
            if not 10 <= body.chat_window_min <= body.chat_window_max <= 200:
                raise HTTPException(
                    status_code=400,
                    detail="动态窗口必须满足 10 ≤ 起始消息数 ≤ 最大消息数 ≤ 200",
                )
            if body.thinking_mode not in {"inherit", "enabled", "disabled"}:
                raise HTTPException(status_code=400, detail="思考模式配置无效")
            if body.reasoning_effort not in {"high", "max"}:
                raise HTTPException(status_code=400, detail="思考强度配置无效")
            if not 1024 <= body.context_token_limit <= 128000:
                raise HTTPException(status_code=400, detail="上下文 Token 预算必须在1024至128000之间")
            if not 256 <= body.max_tokens <= 65536:
                raise HTTPException(status_code=400, detail="最大生成 Token 必须在256至65536之间")
            try:
                feature.config.save(
                    body.api_url,
                    body.model,
                    body.api_key,
                    body.summary_enabled,
                    body.chat_enabled,
                    body.chat_window_min,
                    body.chat_window_max,
                    body.thinking_mode,
                    body.reasoning_effort,
                    body.context_token_limit,
                    body.max_tokens,
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            return feature.config_status

        @app.delete("/api/summary/key")
        async def clear_key():
            feature.config.clear_api_key()
            return feature.config_status

        @app.get("/api/summary/characters")
        async def list_characters():
            return [asdict(item) for item in feature.characters.list()]

        @app.post("/api/summary/characters")
        async def save_character(body: CharacterRequest):
            try:
                character = feature.characters.save(
                    Character(body.name, body.trigger_name, body.description, body.prompt),
                    body.original_name,
                )
                if body.original_name and body.original_name.casefold() != character.name.casefold():
                    feature.selections.rename(body.original_name, character.name)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            return asdict(character)

        @app.delete("/api/summary/characters")
        async def delete_character(name: str):
            characters = feature.characters.delete(name)
            feature.selections.remove(name)
            return [asdict(item) for item in characters]

    @property
    def panel_html(self) -> str:
        return """
<template data-feature-panel="summary">
  <div class="feature-extra">
    <h3>共用模型配置</h3>
    <p class="ai-config-status">正在读取模型配置...</p>
    <label>API URL<input class="ai-api-url" type="text" placeholder="https://api.deepseek.com"></label>
    <label>模型名<input class="ai-model" type="text" placeholder="deepseek-chat"></label>
    <label>API Key（留空保留现有 Key）<input class="ai-api-key" type="password" autocomplete="new-password" placeholder="sk-..."></label>
    <label>思考模式
      <select class="ai-thinking-mode">
        <option value="inherit">跟随模型或服务默认</option>
        <option value="enabled">启用</option>
        <option value="disabled">禁用</option>
      </select>
    </label>
    <label>思考强度
      <select class="ai-reasoning-effort">
        <option value="high">high</option>
        <option value="max">max</option>
      </select>
    </label>
    <label>输入上下文 Token 预算（1024-128000）
      <input class="ai-context-token-limit" type="text" inputmode="numeric" placeholder="16000">
    </label>
    <label>最大生成 Token（思维链与最终回答合计，256-65536）
      <input class="ai-max-tokens" type="text" inputmode="numeric" placeholder="8192">
    </label>
    <div>
      <button class="ai-save" type="button">保存 AI 设置</button>
      <button class="secondary ai-clear-key" type="button">清除 API Key</button>
      <span class="ai-save-status status"></span>
    </div>
    <p>URL 可填写 OpenAI-compatible 服务根地址，或完整的 <code>/chat/completions</code> 地址。思考模式下 temperature 不会发送；输入 Token 预算使用保守估算裁剪最旧消息。API Key 不会回传到浏览器。</p>

    <div class="token-guide">
      <h3>群聊总结</h3>
      <label><input class="ai-summary-enabled" type="checkbox"> 启用 <code>/short 10-200</code></label>
    </div>

    <div class="token-guide">
      <h3>AI 聊天</h3>
      <label><input class="ai-chat-enabled" type="checkbox"> 启用 AI 聊天、人设指令和自动回复</label>
      <label>动态窗口起始消息数（10-200）
        <input class="ai-window-min" type="text" inputmode="numeric" placeholder="150">
      </label>
      <label>动态窗口最大消息数（10-200）
        <input class="ai-window-max" type="text" inputmode="numeric" placeholder="200">
      </label>
      <p>窗口从起始数量开始，只在尾部追加新消息；超过最大数量时重置为最新的起始数量，以提高前缀缓存命中率。</p>
    </div>

    <div class="token-guide ai-character-editor">
      <h3>聊天人设</h3>
      <label>选择已有的人设<select class="ai-character-select"></select></label>
      <label>名字<input class="ai-character-name" type="text"></label>
      <label>姓名（用于在消息中唤醒该人设）<input class="ai-character-trigger-name" type="text"></label>
      <label>描述<input class="ai-character-description" type="text"></label>
      <label>正文提示词<textarea class="ai-character-prompt" spellcheck="false"></textarea></label>
      <div>
        <button class="ai-character-new secondary" type="button">新建</button>
        <button class="ai-character-save" type="button">保存人设</button>
        <button class="ai-character-delete danger" type="button">删除人设</button>
        <span class="ai-character-status status"></span>
      </div>
    </div>
  </div>
</template>
"""

    @property
    def javascript(self) -> str:
        return r"""
featureAdminControls.summary = async row => {
  const q = selector => row.querySelector(selector);
  const apiUrl = q(".ai-api-url"), model = q(".ai-model"), apiKey = q(".ai-api-key");
  const summaryEnabled = q(".ai-summary-enabled"), chatEnabled = q(".ai-chat-enabled");
  const windowMin = q(".ai-window-min"), windowMax = q(".ai-window-max"), configStatus = q(".ai-config-status");
  const thinkingMode = q(".ai-thinking-mode"), reasoningEffort = q(".ai-reasoning-effort");
  const contextTokenLimit = q(".ai-context-token-limit"), maxTokens = q(".ai-max-tokens");
  const save = q(".ai-save"), clearKey = q(".ai-clear-key"), saveStatus = q(".ai-save-status");
  const characterSelect = q(".ai-character-select"), characterName = q(".ai-character-name");
  const characterTriggerName = q(".ai-character-trigger-name");
  const characterDescription = q(".ai-character-description"), characterPrompt = q(".ai-character-prompt");
  const characterStatus = q(".ai-character-status"), deleteCharacter = q(".ai-character-delete");
  let characters = [], originalName = null;

  const loadConfig = async () => {
    const response = await fetch("/api/summary/config");
    if (!response.ok) throw new Error("加载 AI 配置失败");
    const data = await response.json();
    apiUrl.value = data.api_url; model.value = data.model;
    windowMin.value = data.chat_window_min; windowMax.value = data.chat_window_max;
    summaryEnabled.checked = data.summary_enabled; chatEnabled.checked = data.chat_enabled;
    thinkingMode.value = data.thinking_mode; reasoningEffort.value = data.reasoning_effort;
    contextTokenLimit.value = data.context_token_limit; maxTokens.value = data.max_tokens;
    reasoningEffort.disabled = thinkingMode.value !== "enabled";
    configStatus.textContent = `API Key：${data.api_key_configured ? "已配置" : "未配置"}`;
    clearKey.disabled = !data.api_key_configured;
  };
  const showCharacter = character => {
    originalName = character ? character.name : null;
    characterName.value = character?.name || "";
    characterTriggerName.value = character?.trigger_name || "";
    characterDescription.value = character?.description || "";
    characterPrompt.value = character?.prompt || "";
    characterSelect.value = character?.name || "";
    deleteCharacter.disabled = !character;
  };
  const loadCharacters = async selectedName => {
    const response = await fetch("/api/summary/characters");
    if (!response.ok) throw new Error("加载 AI 人设失败");
    characters = await response.json();
    characterSelect.replaceChildren(...characters.map(character => {
      const option = document.createElement("option"); option.value = character.name; option.textContent = character.name; return option;
    }));
    showCharacter(characters.find(item => item.name === selectedName) || characters[0] || null);
  };
  await Promise.all([loadConfig(), loadCharacters()]);
  characterSelect.addEventListener("change", () => showCharacter(characters.find(item => item.name === characterSelect.value)));
  q(".ai-character-new").addEventListener("click", () => showCharacter(null));
  thinkingMode.addEventListener("change", () => { reasoningEffort.disabled = thinkingMode.value !== "enabled"; });
  save.addEventListener("click", async () => {
    const minimum = Number(windowMin.value), maximum = Number(windowMax.value);
    const contextLimit = Number(contextTokenLimit.value), outputLimit = Number(maxTokens.value);
    if (!Number.isInteger(minimum) || !Number.isInteger(maximum) || minimum < 10 || maximum > 200 || minimum > maximum) { saveStatus.textContent = "动态窗口需满足 10 ≤ 起始数 ≤ 最大数 ≤ 200"; return; }
    if (!Number.isInteger(contextLimit) || contextLimit < 1024 || contextLimit > 128000) { saveStatus.textContent = "上下文 Token 预算必须是1024至128000的整数"; return; }
    if (!Number.isInteger(outputLimit) || outputLimit < 256 || outputLimit > 65536) { saveStatus.textContent = "最大生成 Token 必须是256至65536的整数"; return; }
    save.disabled = true; saveStatus.textContent = "保存中...";
    const body = {api_url: apiUrl.value.trim(), model: model.value.trim(), summary_enabled: summaryEnabled.checked, chat_enabled: chatEnabled.checked, chat_window_min: minimum, chat_window_max: maximum, thinking_mode: thinkingMode.value, reasoning_effort: reasoningEffort.value, context_token_limit: contextLimit, max_tokens: outputLimit};
    if (apiKey.value.trim()) body.api_key = apiKey.value.trim();
    const response = await fetch("/api/summary/config", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
    save.disabled = false; const data = await response.json().catch(() => ({}));
    saveStatus.textContent = response.ok ? "已保存并立即生效" : (data.detail || "保存失败");
    if (response.ok) { apiKey.value = ""; await loadConfig(); }
  });
  clearKey.addEventListener("click", async () => {
    clearKey.disabled = true; const response = await fetch("/api/summary/key", {method: "DELETE"});
    saveStatus.textContent = response.ok ? "API Key 已清除" : "清除失败"; await loadConfig();
  });
  q(".ai-character-save").addEventListener("click", async () => {
    const response = await fetch("/api/summary/characters", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({original_name: originalName, name: characterName.value.trim(), trigger_name: characterTriggerName.value.trim(), description: characterDescription.value.trim(), prompt: characterPrompt.value.trim()})});
    const data = await response.json().catch(() => ({}));
    characterStatus.textContent = response.ok ? "人设已保存" : (data.detail || "保存失败");
    if (response.ok) await loadCharacters(data.name);
  });
  deleteCharacter.addEventListener("click", async () => {
    if (!originalName) return;
    const response = await fetch(`/api/summary/characters?name=${encodeURIComponent(originalName)}`, {method: "DELETE"});
    characterStatus.textContent = response.ok ? "人设已删除" : "删除失败";
    if (response.ok) await loadCharacters();
  });
};
"""
