from fastapi import FastAPI, HTTPException

from src.feature.atom import FeatureBusyError
from src.feature.jm.feature import JmPdfFeature
from src.feature.jm.diagnostics import DEFAULT_SAMPLE_ID
from src.feature.atom.diagnostics import DiagnosticRequest
from src.feature.atom.diagnostic_panel import diagnostic_panel, diagnostic_javascript


class JmAdmin:
    def __init__(self, feature: JmPdfFeature):
        self.feature = feature

    def register_routes(self, app: FastAPI) -> None:
        feature = self.feature

        @app.get('/api/jm/test')
        async def test_status():
            return feature.diagnostics.snapshot()

        @app.post('/api/jm/test', status_code=202)
        async def test_availability(body: DiagnosticRequest | None = None):
            try:
                return feature.diagnostics.start({'sample_id': body.sample_id if body else ''})
            except FeatureBusyError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc

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
""" + diagnostic_panel(f'检查漫画/章节接口、首章首图下载与还原，以及真实 PDF 导出。留空使用样本 {DEFAULT_SAMPLE_ID}；若样本失效，可换一个已知有效的漫画号码。', f'留空使用 {DEFAULT_SAMPLE_ID}') + """
    <p>删除空闲任务目录；正在处理或等待处理的任务不会被清理。</p>
    <button class="danger cleanup" type="button">手动清理存储垃圾</button>
    <span class="cleanup-status status"></span>
  </div>
</template>
"""

    @property
    def javascript(self) -> str:
        return r"""
featureAdminControls.jm_pdf = async row => {
  const button = row.querySelector(".cleanup");
  const status = row.querySelector(".cleanup-status");
  button.addEventListener("click", async () => {
    status.textContent = "清理中...";
    button.disabled = true;
    const response = await fetch("/api/jm/cleanup", {method: "POST"});
    button.disabled = false;
    const data = await response.json().catch(() => ({}));
    status.textContent = response.ok ? "已清理" : (data.detail || "清理失败");
  });
""" + diagnostic_javascript('jm') + r"""
};
"""
