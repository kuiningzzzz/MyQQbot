from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from src.feature.news.feature import NewsFeature


class NewsConfigRequest(BaseModel):
    broadcast_hour: int
    news_count: int
    requirements: str


class NewsAdmin:
    def __init__(self, feature: NewsFeature):
        self.feature = feature

    def register_routes(self, app: FastAPI) -> None:
        feature = self.feature

        @app.get("/api/news/config")
        async def get_news_config():
            value = feature.config.load()
            return {
                "broadcast_hour": value.broadcast_hour,
                "news_count": value.news_count,
                "requirements": value.requirements,
                "subscription_count": len(feature.store.targets()),
                "recent_count": len(feature.store.recent_news(5)),
                "last_attempt_date": feature.store.last_attempt_date(),
            }

        @app.post("/api/news/config")
        async def save_news_config(body: NewsConfigRequest):
            try:
                feature.config.save(
                    body.broadcast_hour, body.news_count, body.requirements
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            return {"ok": True}

    @property
    def panel_html(self) -> str:
        return """
<template data-feature-panel="news">
  <div class="feature-extra">
    <p class="news-summary">正在读取新闻播报设置...</p>
    <label>每天北京时间几点整播报（0-23）<input class="news-hour" type="text" inputmode="numeric" placeholder="8"></label>
    <label>每次播报新闻条数（5-50）<input class="news-count" type="text" inputmode="numeric" placeholder="10"></label>
    <label>希望获取的新闻内容需求<textarea class="news-requirements" spellcheck="false"></textarea></label>
    <button class="news-save" type="button">保存新闻设置</button>
    <span class="news-status status"></span>
    <div class="token-guide">
      <h3>会话订阅方式</h3>
      <p>在需要接收播报的群聊或私聊发送 <code>/open-news</code>；发送 <code>/close-news</code> 关闭。所有订阅会话每天收到同一份内容。</p>
      <p>没有任何订阅时，当天会直接跳过搜索和模型生成，不消耗 Tavily 搜索额度。</p>
    </div>
  </div>
</template>
"""

    @property
    def javascript(self) -> str:
        return r"""
featureAdminControls.news = async row => {
  const hour = row.querySelector(".news-hour"), count = row.querySelector(".news-count");
  const requirements = row.querySelector(".news-requirements"), summary = row.querySelector(".news-summary");
  const save = row.querySelector(".news-save"), status = row.querySelector(".news-status");
  const load = async () => {
    const response = await fetch("/api/news/config");
    if (!response.ok) throw new Error("加载新闻设置失败");
    const data = await response.json();
    hour.value = data.broadcast_hour; count.value = data.news_count; requirements.value = data.requirements;
    summary.textContent = `已订阅会话：${data.subscription_count}；已保存最近播报：${data.recent_count}；最近调度日期：${data.last_attempt_date || "暂无"}`;
  };
  await load();
  save.addEventListener("click", async () => {
    const broadcast_hour = Number(hour.value), news_count = Number(count.value);
    if (!Number.isInteger(broadcast_hour) || broadcast_hour < 0 || broadcast_hour > 23) { status.textContent = "播报小时必须是0至23的整数"; return; }
    if (!Number.isInteger(news_count) || news_count < 5 || news_count > 50) { status.textContent = "新闻条数必须是5至50的整数"; return; }
    if (!requirements.value.trim()) { status.textContent = "新闻内容需求不能为空"; return; }
    save.disabled = true;
    const response = await fetch("/api/news/config", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({broadcast_hour, news_count, requirements:requirements.value.trim()})});
    save.disabled = false; const data = await response.json().catch(() => ({}));
    status.textContent = response.ok ? "已保存并立即生效" : (data.detail || "保存失败");
    if (response.ok) await load();
  });
};
"""
