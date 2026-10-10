"""Live Pixiv auth/API/CDN probe, run in a short-lived child process."""
from src.feature.atom.diagnostics import DiagnosticFailure, run_worker
from src.feature.pixiv.client import PixivClient, PixivDownloadError

CHECKS = [('dependencies', '依赖加载'), ('auth', 'refresh token 认证'),
          ('metadata', '作品接口'), ('image', '首张原图下载与校验')]


def probe(payload, report, directory):
    report.step('dependencies')
    import pixivpy3  # noqa: F401
    from PIL import Image
    report.step('dependencies', 'passed', 'pixivpy3、Pillow 已加载。')
    report.step('auth')
    if not payload.get('refresh_token'):
        raise DiagnosticFailure('not_configured', '尚未配置 Pixiv refresh token。', '在本页面保存 refresh token 后重新测试。')
    # A fresh adapter forces refresh-token authentication, not a cached token.
    client = PixivClient(payload['refresh_token'], payload.get('proxy_url', ''), request_timeout=15)
    api = client._get_api()
    if not api.access_token:
        raise DiagnosticFailure('auth_failed', '认证未取得 access token。', '重新获取并保存 refresh token 后重试。')
    report.step('auth', 'passed', '已用当前保存的 refresh token 重新认证。')
    report.step('metadata')
    identifier = payload.get('sample_id')
    if not identifier:
        ranking = api.illust_ranking(mode='day')
        if getattr(ranking, 'error', None):
            raise PixivDownloadError(client._error_message(ranking.error))
        candidates = [item for item in (getattr(ranking, 'illusts', None) or [])
                      if getattr(item, 'x_restrict', 0) == 0 and getattr(item, 'type', '') != 'ugoira']
        if not candidates:
            raise DiagnosticFailure('target_unavailable', '日榜没有可用于测试的普通图片作品。', '手动填写一个已知可访问的作品 ID 重试。')
        identifier = str(candidates[0].id)
    # Reuse the exact metadata validation and download adapter used by /pixiv.
    illust = client._get_illust(api, identifier)
    report.step('metadata', 'passed', f'已取得作品 {identifier} 的详情及原图地址。')
    report.step('image')
    work = client.download_work(identifier, directory, max_images=1, illust=illust)
    image = work.image_paths[0]
    if not image.stat().st_size:
        raise DiagnosticFailure('invalid_image', '下载结果为空文件。', '检查图片 CDN、代理和作品访问权限。')
    with Image.open(image) as content:
        content.verify()
    report.step('image', 'passed', f'作品 {identifier} 首张原图已落盘并通过图片校验，{image.stat().st_size} 字节。')


if __name__ == '__main__':
    run_worker(probe)
