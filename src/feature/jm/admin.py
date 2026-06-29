from fastapi import FastAPI, HTTPException

from src.feature.atom import FeatureBusyError
from src.feature.jm.feature import JmPdfFeature


class JmAdmin:
    def __init__(self, feature: JmPdfFeature):
        self.feature = feature

    def register_routes(self, app: FastAPI) -> None:
        feature = self.feature

        @app.post("/api/jm/cleanup")
        async def cleanup():
            try:
                await feature.cleanup_storage()
            except FeatureBusyError as exc:
                raise HTTPException(
                    status_code=409,
                    detail="JM 功能仍有任务，请稍后再清理",
                ) from exc
            return {"ok": True}

    @property
    def panel_html(self) -> str:
        return """
<template data-feature-panel="jm_pdf">
  <div class="feature-extra">
    <p>删除空闲任务目录；正在处理或等待处理的任务不会被清理。</p>
    <button class="danger" type="button">手动清理存储垃圾</button>
    <span class="status"></span>
  </div>
</template>
"""

    @property
    def javascript(self) -> str:
        return r"""
featureAdminControls.jm_pdf = row => {
  const button = row.querySelector("button");
  const status = row.querySelector(".status");
  button.addEventListener("click", async () => {
    status.textContent = "清理中...";
    button.disabled = true;
    const response = await fetch("/api/jm/cleanup", {method: "POST"});
    button.disabled = false;
    const data = await response.json().catch(() => ({}));
    status.textContent = response.ok ? "已清理" : (data.detail || "清理失败");
  });
};
"""
