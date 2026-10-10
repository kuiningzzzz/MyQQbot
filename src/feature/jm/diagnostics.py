"""Probe one chapter/image using the production JM downloader/PDF exporter."""
from datetime import datetime
from itertools import islice

from src.feature.atom.diagnostics import DiagnosticFailure, run_worker

DEFAULT_SAMPLE_ID = '438696'
CHECKS = [('dependencies', '依赖加载'), ('metadata', '漫画与章节接口'),
          ('image', '首章首图下载与校验'), ('pdf', 'PDF 导出与校验')]


def probe(payload, report, directory):
    report.step('dependencies')
    import jmcomic
    import img2pdf  # noqa: F401
    from PIL import Image
    from pypdf import PdfReader
    from src.feature.jm.feature import JmPdfFeature, JmQueueTask
    report.step('dependencies', 'passed', 'jmcomic、img2pdf、Pillow、pypdf 已加载。')
    identifier = payload.get('sample_id') or DEFAULT_SAMPLE_ID

    class SingleImageDownloader(jmcomic.JmDownloader):
        def do_filter(self, detail):
            selected = list(islice(detail, 1))
            if not selected:
                raise DiagnosticFailure('target_unavailable', '测试漫画没有可下载的章节或图片。', '换一个已知有效的漫画号码重试。')
            return selected

        def before_photo(self, photo):
            super().before_photo(photo)
            report.step('metadata', 'passed', f'漫画 {identifier} 及首章详情已取得。')
            report.step('image')

        def after_image(self, image, path):
            with Image.open(path) as content:
                content.verify()
            super().after_image(image, path)
            report.step('image', 'passed', '首章首图已下载、还原并通过图片校验。')

        def after_album(self, album):
            # SDK image threads may collect rather than propagate their errors.
            # Keep the actual CDN/decode cause instead of reporting a PDF error.
            failures = self.download_failed_image or self.download_failed_photo
            if failures:
                raise failures[0][1]
            report.require_passed('image')
            report.step('pdf')
            super().after_album(album)  # Runs the same Feature.export_pdf hook.

    report.step('metadata')
    task = JmQueueTask(0, identifier, datetime.now(), None, directory)
    pdf = JmPdfFeature().download_pdf(task, downloader=SingleImageDownloader, request_timeout=15)
    if not pdf.is_file() or pdf.stat().st_size < 5 or pdf.read_bytes()[:5] != b'%PDF-':
        raise DiagnosticFailure('pdf_export_failed', '未生成有效的 PDF 文件。', '检查 jmcomic、img2pdf 版本和磁盘写入权限。')
    if len(PdfReader(pdf).pages) != 1:
        raise DiagnosticFailure('pdf_export_failed', '测试 PDF 页数异常，应仅包含一张样本图片。', '检查下载过滤和 PDF 导出依赖后重试。')
    report.step('pdf', 'passed', f'漫画 {identifier} 已生成并验证单页 PDF，{pdf.stat().st_size} 字节。')


if __name__ == '__main__':
    run_worker(probe)
