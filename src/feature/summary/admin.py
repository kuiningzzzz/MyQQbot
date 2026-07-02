from dataclasses import asdict

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from src.feature.summary.character import Character
from src.feature.summary.feature import SummaryFeature
from src.feature.summary.memory_store import MemoryCandidate


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
    memory_enabled: bool
    memory_auto_learn: bool
    memory_batch_size: int
    memory_retrieval_limit: int
    memory_min_confidence: float


class CharacterRequest(BaseModel):
    original_name: str | None = None
    name: str
    trigger_name: str
    description: str
    prompt: str


class MemoryRequest(BaseModel):
    kind: str
    content: str
    normalized_key: str
    subject_user_id: int | None = None
    scope_type: str
    scope_id: str | None = None
    confidence: float
    importance: float
    status: str = "active"
    expires_at: str | None = None


class WebSearchConfigRequest(BaseModel):
    enabled: bool
    provider: str
    api_url: str
    api_key: str | None = None
    max_results: int
    timeout_seconds: int


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
            if not 5 <= body.memory_batch_size <= 100:
                raise HTTPException(status_code=400, detail="记忆提取批次必须在5至100之间")
            if not 1 <= body.memory_retrieval_limit <= 20:
                raise HTTPException(status_code=400, detail="记忆检索数量必须在1至20之间")
            if not 0 <= body.memory_min_confidence <= 1:
                raise HTTPException(status_code=400, detail="最低置信度必须在0至1之间")
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
                    body.memory_enabled,
                    body.memory_auto_learn,
                    body.memory_batch_size,
                    body.memory_retrieval_limit,
                    body.memory_min_confidence,
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            return feature.config_status

        @app.delete("/api/summary/key")
        async def clear_key():
            feature.config.clear_api_key()
            return feature.config_status

        @app.get("/api/web-search/config")
        async def get_web_search_config():
            return feature.web_search.config.status

        @app.post("/api/web-search/config")
        async def save_web_search_config(body: WebSearchConfigRequest):
            if not 1 <= body.max_results <= 10:
                raise HTTPException(status_code=400, detail="搜索结果数必须在1至10之间")
            if not 5 <= body.timeout_seconds <= 60:
                raise HTTPException(status_code=400, detail="搜索超时必须在5至60秒之间")
            try:
                feature.web_search.config.save(
                    body.enabled,
                    body.provider,
                    body.api_url,
                    body.api_key,
                    body.max_results,
                    body.timeout_seconds,
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            return feature.web_search.config.status

        @app.delete("/api/web-search/key")
        async def clear_web_search_key():
            feature.web_search.config.clear_api_key()
            return feature.web_search.config.status

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

        @app.get("/api/summary/memories")
        async def list_memories(query: str = "", status: str = "", limit: int = 100):
            records = feature.memory_store.list(query, status or None, limit)
            return {
                "memories": [record.as_dict() for record in records],
                "stats": feature.memory_store.stats(),
            }

        @app.get("/api/summary/memories/{memory_id}/sources")
        async def memory_sources(memory_id: int):
            return {"sources": feature.memory_store.sources(memory_id)}

        @app.post("/api/summary/memories")
        async def create_memory(body: MemoryRequest):
            try:
                record = feature.memory_store.upsert_candidate(
                    MemoryCandidate(
                        kind=body.kind,
                        content=body.content,
                        normalized_key=body.normalized_key,
                        subject_user_id=body.subject_user_id,
                        confidence=body.confidence,
                        importance=body.importance,
                    ),
                    body.scope_type,
                    body.scope_id,
                    "webui",
                    body.content,
                    explicit=True,
                )
                if body.status != "active" or body.expires_at:
                    record = feature.memory_store.update(
                        record.id,
                        status=body.status,
                        expires_at=body.expires_at,
                    )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            return record.as_dict()

        @app.put("/api/summary/memories/{memory_id}")
        async def update_memory(memory_id: int, body: MemoryRequest):
            try:
                record = feature.memory_store.update(
                    memory_id,
                    **body.dict(),
                )
            except KeyError as exc:
                raise HTTPException(status_code=404, detail="memory not found") from exc
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            return record.as_dict()

        @app.delete("/api/summary/memories/{memory_id}")
        async def forget_memory(memory_id: int):
            try:
                return feature.memory_store.forget(memory_id).as_dict()
            except KeyError as exc:
                raise HTTPException(status_code=404, detail="memory not found") from exc

    @property
    def panel_html(self) -> str:
        return """
<template data-feature-panel="summary">
  <div class="feature-extra">
    <p class="ai-config-status">正在读取模型配置...</p>
    <details class="settings-section" open>
      <summary><span>API 基础设置</span><small>模型、密钥与 Token 参数</small></summary>
      <div class="settings-section-body">
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
      <button class="secondary ai-clear-key" type="button">清除 API Key</button>
      <p>URL 可填写 OpenAI-compatible 服务根地址，或完整的 <code>/chat/completions</code> 地址。思考模式下 temperature 不会发送；输入 Token 预算使用保守估算裁剪最旧消息。API Key 不会回传到浏览器。</p>
      </div>
    </details>

    <details class="settings-section">
      <summary><span>功能设置</span><small>群聊总结与 AI 聊天</small></summary>
      <div class="settings-section-body">
      <h3>群聊总结</h3>
      <label><input class="ai-summary-enabled" type="checkbox"> 启用 <code>/short 10-200</code></label>
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
    </details>

    <details class="settings-section">
      <summary><span>联网搜索</span><small>供聊天及未来功能复用的实时搜索工具</small></summary>
      <div class="settings-section-body">
      <label><input class="web-search-enabled" type="checkbox"> 启用联网搜索工具</label>
      <label>搜索服务
        <select class="web-search-provider"><option value="tavily">Tavily</option><option value="searxng">SearXNG</option></select>
      </label>
      <label>搜索 API URL<input class="web-search-url" type="text" placeholder="https://api.tavily.com/search"></label>
      <label>搜索 API Key（SearXNG 通常留空）<input class="web-search-key" type="password" autocomplete="new-password" placeholder="tvly-..."></label>
      <label>每次返回结果数（1-10）<input class="web-search-max-results" type="text" inputmode="numeric" placeholder="5"></label>
      <label>搜索超时秒数（5-60）<input class="web-search-timeout" type="text" inputmode="numeric" placeholder="20"></label>
      <button class="secondary web-search-clear-key" type="button">清除搜索 API Key</button>
      <p class="web-search-status">正在读取联网搜索配置...</p>
      <p>模型只在需要实时或外部资料时自主调用。Tavily 使用托管 API；SearXNG 可填写自建实例地址，并需在实例中启用 JSON 输出。</p>
      </div>
    </details>

    <details class="settings-section">
      <summary><span>每日新闻播报</span><small>订阅、整点播报与内容偏好</small></summary>
      <div class="settings-section-body">
      <label><input class="news-enabled" type="checkbox"> 启用 AI 每日新闻调度</label>
      <label>每天北京时间几点整播报（0-23）<input class="news-hour" type="text" inputmode="numeric" placeholder="8"></label>
      <label>每次播报新闻条数（5-50）<input class="news-count" type="text" inputmode="numeric" placeholder="10"></label>
      <label>希望获取的新闻内容需求<textarea class="news-requirements" spellcheck="false"></textarea></label>
      <p class="news-summary">正在读取新闻播报设置...</p>
      <p>各群聊或私聊使用 <code>/open-news</code> 订阅、<code>/close-news</code> 退订。没有订阅时不会搜索或调用模型。</p>
      </div>
    </details>

    <details class="settings-section">
      <summary><span>记忆设置</span><small>长期记忆与记忆管理</small></summary>
      <div class="settings-section-body">
      <h3>长期记忆</h3>
      <label><input class="ai-memory-enabled" type="checkbox"> 启用聊天时长期记忆检索</label>
      <label><input class="ai-memory-auto-learn" type="checkbox"> 启用后台自动学习</label>
      <label>每累计多少条消息后台提取一次（5-100）<input class="ai-memory-batch" type="text" inputmode="numeric" placeholder="20"></label>
      <label>每次最多检索记忆数（1-20）<input class="ai-memory-retrieval" type="text" inputmode="numeric" placeholder="8"></label>
      <label>自动记忆最低置信度（0-1）<input class="ai-memory-confidence" type="text" inputmode="decimal" placeholder="0.72"></label>
      <p>自动提取在后台运行。私聊记忆只用于对应私聊，群记忆只用于对应群；显式标记为全局的记忆才会跨会话。</p>

    <div class="settings-subsection ai-memory-editor">
      <h3>记忆管理</h3>
      <p class="ai-memory-stats">正在读取记忆统计...</p>
      <input class="ai-memory-search" type="text" placeholder="搜索内容、语义键或来源">
      <select class="ai-memory-list" size="8"></select>
      <label>类型<select class="ai-memory-kind"><option value="user_fact">用户信息</option><option value="topic">历史话题</option><option value="instruction">命令要求</option><option value="reminder">待办记忆</option><option value="group_fact">群体事实</option><option value="explicit">显式记忆</option></select></label>
      <label>语义键<input class="ai-memory-key" type="text" placeholder="例如 favorite_food"></label>
      <label>主体 QQ ID（可空）<input class="ai-memory-subject" type="text" inputmode="numeric"></label>
      <label>范围<select class="ai-memory-scope"><option value="group">群聊</option><option value="private">私聊</option><option value="global">全局</option></select></label>
      <label>范围 ID（群号或 QQ 号；全局可空）<input class="ai-memory-scope-id" type="text"></label>
      <label>状态<select class="ai-memory-status"><option value="active">有效</option><option value="conflicted">冲突旧版本</option><option value="forgotten">已遗忘</option><option value="expired">已过期</option></select></label>
      <label>置信度（0-1）<input class="ai-memory-record-confidence" type="text" inputmode="decimal"></label>
      <label>重要度（0-1）<input class="ai-memory-importance" type="text" inputmode="decimal"></label>
      <label>有效期 ISO 时间（可空）<input class="ai-memory-expires" type="text" placeholder="2027-01-01T00:00:00+00:00"></label>
      <label>内容<textarea class="ai-memory-content" spellcheck="false"></textarea></label>
      <div><button class="secondary ai-memory-new" type="button">新建</button><button class="ai-memory-save" type="button">保存</button><button class="danger ai-memory-delete" type="button">标记遗忘</button><button class="secondary ai-memory-sources" type="button">查看来源</button><span class="ai-memory-record-status status"></span></div>
    </div>
      </div>
    </details>

    <details class="settings-section">
      <summary><span>人设设置</span><small>管理聊天人格与触发姓名</small></summary>
      <div class="settings-section-body ai-character-editor">
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
    </details>

    <div class="settings-save-bar">
      <button class="ai-save" type="button">保存 AI 设置</button>
      <span class="ai-save-status status"></span>
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
  const memoryEnabled = q(".ai-memory-enabled"), memoryBatch = q(".ai-memory-batch");
  const memoryAutoLearn = q(".ai-memory-auto-learn");
  const memoryRetrieval = q(".ai-memory-retrieval"), memoryConfidence = q(".ai-memory-confidence");
  const searchEnabled = q(".web-search-enabled"), searchProvider = q(".web-search-provider"), searchUrl = q(".web-search-url"), searchKey = q(".web-search-key");
  const searchMaxResults = q(".web-search-max-results"), searchTimeout = q(".web-search-timeout"), searchStatus = q(".web-search-status"), clearSearchKey = q(".web-search-clear-key");
  const newsEnabled = q(".news-enabled"), newsHour = q(".news-hour"), newsCount = q(".news-count"), newsRequirements = q(".news-requirements"), newsSummary = q(".news-summary");
  const save = q(".ai-save"), clearKey = q(".ai-clear-key"), saveStatus = q(".ai-save-status");
  const characterSelect = q(".ai-character-select"), characterName = q(".ai-character-name");
  const characterTriggerName = q(".ai-character-trigger-name");
  const characterDescription = q(".ai-character-description"), characterPrompt = q(".ai-character-prompt");
  const characterStatus = q(".ai-character-status"), deleteCharacter = q(".ai-character-delete");
  let characters = [], originalName = null;
  let memories = [], selectedMemoryId = null;

  const loadConfig = async () => {
    const response = await fetch("/api/summary/config");
    if (!response.ok) throw new Error("加载 AI 配置失败");
    const data = await response.json();
    apiUrl.value = data.api_url; model.value = data.model;
    windowMin.value = data.chat_window_min; windowMax.value = data.chat_window_max;
    summaryEnabled.checked = data.summary_enabled; chatEnabled.checked = data.chat_enabled;
    thinkingMode.value = data.thinking_mode; reasoningEffort.value = data.reasoning_effort;
    contextTokenLimit.value = data.context_token_limit; maxTokens.value = data.max_tokens;
    memoryEnabled.checked = data.memory_enabled; memoryBatch.value = data.memory_batch_size;
    memoryAutoLearn.checked = data.memory_auto_learn;
    memoryRetrieval.value = data.memory_retrieval_limit; memoryConfidence.value = data.memory_min_confidence;
    reasoningEffort.disabled = thinkingMode.value !== "enabled";
    configStatus.textContent = `API Key：${data.api_key_configured ? "已配置" : "未配置"}`;
    clearKey.disabled = !data.api_key_configured;
  };
  const loadSearchConfig = async () => {
    const response = await fetch("/api/web-search/config");
    if (!response.ok) throw new Error("加载联网搜索配置失败");
    const data = await response.json();
    searchEnabled.checked = data.enabled; searchProvider.value = data.provider;
    searchUrl.value = data.api_url; searchMaxResults.value = data.max_results; searchTimeout.value = data.timeout_seconds;
    clearSearchKey.disabled = !data.has_api_key;
    searchStatus.textContent = `搜索 API Key：${data.has_api_key ? "已配置" : "未配置"}`;
  };
  const loadNewsConfig = async () => {
    const response = await fetch("/api/news/config");
    if (!response.ok) throw new Error("加载新闻播报设置失败");
    const data = await response.json();
    newsEnabled.checked = data.enabled; newsHour.value = data.broadcast_hour;
    newsCount.value = data.news_count; newsRequirements.value = data.requirements;
    newsSummary.textContent = `已订阅会话：${data.subscription_count}；已保存最近播报：${data.recent_count}；最近调度日期：${data.last_attempt_date || "暂无"}`;
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
  const memoryList = q(".ai-memory-list"), memorySearch = q(".ai-memory-search");
  const memoryFields = {kind:q(".ai-memory-kind"), normalized_key:q(".ai-memory-key"), subject_user_id:q(".ai-memory-subject"), scope_type:q(".ai-memory-scope"), scope_id:q(".ai-memory-scope-id"), status:q(".ai-memory-status"), confidence:q(".ai-memory-record-confidence"), importance:q(".ai-memory-importance"), expires_at:q(".ai-memory-expires"), content:q(".ai-memory-content")};
  const showMemory = memory => {
    selectedMemoryId = memory?.id || null;
    for (const [key, field] of Object.entries(memoryFields)) field.value = memory?.[key] ?? "";
    if (!memory) { memoryFields.kind.value="explicit"; memoryFields.scope_type.value="global"; memoryFields.status.value="active"; memoryFields.confidence.value="1"; memoryFields.importance.value="1"; }
    q(".ai-memory-delete").disabled = !memory;
    q(".ai-memory-sources").disabled = !memory;
  };
  const loadMemories = async selectedId => {
    const response = await fetch(`/api/summary/memories?query=${encodeURIComponent(memorySearch.value.trim())}&limit=200`);
    if (!response.ok) throw new Error("加载长期记忆失败");
    const data = await response.json(); memories = data.memories;
    q(".ai-memory-stats").textContent = `总计 ${data.stats.total}；有效 ${data.stats.active}；冲突 ${data.stats.conflicted}；遗忘 ${data.stats.forgotten}；过期 ${data.stats.expired}`;
    memoryList.replaceChildren(...memories.map(memory => { const option=document.createElement("option"); option.value=memory.id; option.textContent=`#${memory.id} [${memory.status}/${memory.kind}] ${memory.content}`; return option; }));
    const selected = memories.find(item => item.id === selectedId) || null; showMemory(selected); if (selected) memoryList.value=String(selected.id);
  };
  await Promise.all([loadConfig(), loadSearchConfig(), loadNewsConfig(), loadCharacters(), loadMemories()]);
  characterSelect.addEventListener("change", () => showCharacter(characters.find(item => item.name === characterSelect.value)));
  q(".ai-character-new").addEventListener("click", () => showCharacter(null));
  thinkingMode.addEventListener("change", () => { reasoningEffort.disabled = thinkingMode.value !== "enabled"; });
  save.addEventListener("click", async () => {
    const minimum = Number(windowMin.value), maximum = Number(windowMax.value);
    const contextLimit = Number(contextTokenLimit.value), outputLimit = Number(maxTokens.value);
    const batchSize = Number(memoryBatch.value), retrievalLimit = Number(memoryRetrieval.value), confidenceLimit = Number(memoryConfidence.value);
    const searchResultLimit = Number(searchMaxResults.value), searchTimeoutSeconds = Number(searchTimeout.value);
    const broadcastHour = Number(newsHour.value), configuredNewsCount = Number(newsCount.value);
    if (!Number.isInteger(minimum) || !Number.isInteger(maximum) || minimum < 10 || maximum > 200 || minimum > maximum) { saveStatus.textContent = "动态窗口需满足 10 ≤ 起始数 ≤ 最大数 ≤ 200"; return; }
    if (!Number.isInteger(contextLimit) || contextLimit < 1024 || contextLimit > 128000) { saveStatus.textContent = "上下文 Token 预算必须是1024至128000的整数"; return; }
    if (!Number.isInteger(outputLimit) || outputLimit < 256 || outputLimit > 65536) { saveStatus.textContent = "最大生成 Token 必须是256至65536的整数"; return; }
    if (!Number.isInteger(batchSize) || batchSize < 5 || batchSize > 100) { saveStatus.textContent = "记忆批次必须是5至100的整数"; return; }
    if (!Number.isInteger(retrievalLimit) || retrievalLimit < 1 || retrievalLimit > 20) { saveStatus.textContent = "记忆检索数必须是1至20的整数"; return; }
    if (!Number.isFinite(confidenceLimit) || confidenceLimit < 0 || confidenceLimit > 1) { saveStatus.textContent = "记忆置信度必须在0至1之间"; return; }
    if (!Number.isInteger(searchResultLimit) || searchResultLimit < 1 || searchResultLimit > 10) { saveStatus.textContent = "搜索结果数必须是1至10的整数"; return; }
    if (!Number.isInteger(searchTimeoutSeconds) || searchTimeoutSeconds < 5 || searchTimeoutSeconds > 60) { saveStatus.textContent = "搜索超时必须是5至60秒的整数"; return; }
    if (!Number.isInteger(broadcastHour) || broadcastHour < 0 || broadcastHour > 23) { saveStatus.textContent = "新闻播报小时必须是0至23的整数"; return; }
    if (!Number.isInteger(configuredNewsCount) || configuredNewsCount < 5 || configuredNewsCount > 50) { saveStatus.textContent = "新闻条数必须是5至50的整数"; return; }
    if (!newsRequirements.value.trim()) { saveStatus.textContent = "新闻内容需求不能为空"; return; }
    save.disabled = true; saveStatus.textContent = "保存中...";
    const body = {api_url: apiUrl.value.trim(), model: model.value.trim(), summary_enabled: summaryEnabled.checked, chat_enabled: chatEnabled.checked, chat_window_min: minimum, chat_window_max: maximum, thinking_mode: thinkingMode.value, reasoning_effort: reasoningEffort.value, context_token_limit: contextLimit, max_tokens: outputLimit, memory_enabled: memoryEnabled.checked, memory_auto_learn: memoryAutoLearn.checked, memory_batch_size: batchSize, memory_retrieval_limit: retrievalLimit, memory_min_confidence: confidenceLimit};
    if (apiKey.value.trim()) body.api_key = apiKey.value.trim();
    const searchBody = {enabled:searchEnabled.checked, provider:searchProvider.value, api_url:searchUrl.value.trim(), max_results:searchResultLimit, timeout_seconds:searchTimeoutSeconds};
    const newsBody = {enabled:newsEnabled.checked, broadcast_hour:broadcastHour, news_count:configuredNewsCount, requirements:newsRequirements.value.trim()};
    if (searchKey.value.trim()) searchBody.api_key = searchKey.value.trim();
    const [response, searchResponse, newsResponse] = await Promise.all([
      fetch("/api/summary/config", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)}),
      fetch("/api/web-search/config", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(searchBody)}),
      fetch("/api/news/config", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(newsBody)})
    ]);
    save.disabled = false; const data = await response.json().catch(() => ({})); const searchData = await searchResponse.json().catch(() => ({})); const newsData = await newsResponse.json().catch(() => ({}));
    saveStatus.textContent = response.ok && searchResponse.ok && newsResponse.ok ? "已保存并立即生效" : (data.detail || searchData.detail || newsData.detail || "保存失败");
    if (response.ok && searchResponse.ok && newsResponse.ok) { apiKey.value = ""; searchKey.value = ""; await Promise.all([loadConfig(), loadSearchConfig(), loadNewsConfig()]); }
  });
  clearKey.addEventListener("click", async () => {
    clearKey.disabled = true; const response = await fetch("/api/summary/key", {method: "DELETE"});
    saveStatus.textContent = response.ok ? "API Key 已清除" : "清除失败"; await loadConfig();
  });
  clearSearchKey.addEventListener("click", async () => {
    clearSearchKey.disabled = true; const response = await fetch("/api/web-search/key", {method: "DELETE"});
    searchStatus.textContent = response.ok ? "搜索 API Key 已清除" : "清除失败"; await loadSearchConfig();
  });
  searchProvider.addEventListener("change", () => {
    searchUrl.value = searchProvider.value === "tavily" ? "https://api.tavily.com/search" : "http://127.0.0.1:8080";
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
  memoryList.addEventListener("change", () => showMemory(memories.find(item => item.id === Number(memoryList.value))));
  memorySearch.addEventListener("change", () => loadMemories().catch(error => { q(".ai-memory-record-status").textContent=error.message; }));
  q(".ai-memory-new").addEventListener("click", () => showMemory(null));
  q(".ai-memory-save").addEventListener("click", async () => {
    const body = Object.fromEntries(Object.entries(memoryFields).map(([key,field]) => [key,field.value.trim()]));
    body.subject_user_id = body.subject_user_id ? Number(body.subject_user_id) : null; body.scope_id = body.scope_id || null; body.expires_at = body.expires_at || null; body.confidence=Number(body.confidence); body.importance=Number(body.importance);
    if (!body.content || !body.normalized_key || !Number.isFinite(body.confidence) || !Number.isFinite(body.importance)) { q(".ai-memory-record-status").textContent="请填写内容、语义键、置信度和重要度"; return; }
    const url = selectedMemoryId ? `/api/summary/memories/${selectedMemoryId}` : "/api/summary/memories";
    const response = await fetch(url,{method:selectedMemoryId?"PUT":"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)}); const data=await response.json().catch(()=>({}));
    q(".ai-memory-record-status").textContent=response.ok?"记忆已保存":(data.detail||"保存失败"); if(response.ok) await loadMemories(data.id);
  });
  q(".ai-memory-delete").addEventListener("click", async () => { if(!selectedMemoryId)return; const response=await fetch(`/api/summary/memories/${selectedMemoryId}`,{method:"DELETE"}); q(".ai-memory-record-status").textContent=response.ok?"已标记遗忘":"删除失败"; if(response.ok)await loadMemories(); });
  q(".ai-memory-sources").addEventListener("click", async () => { if(!selectedMemoryId)return; const response=await fetch(`/api/summary/memories/${selectedMemoryId}/sources`); const data=await response.json(); alert(data.sources.map(source=>`${source.created_at} ${source.conversation}\n${source.excerpt}`).join("\n\n")||"没有来源记录"); });
};
"""
