"""Bounded browser OAuth worker. Secrets only enter/leave through private pipes."""
import contextlib
import json
import os
import sys


def login(payload):
    # Keep all other proxy/environment settings out of this isolated login.
    for key in list(os.environ):
        if key.lower() in {'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy'}:
            os.environ.pop(key)
    proxy = payload.get('proxy_url', '')
    if proxy:
        os.environ['HTTPS_PROXY'] = proxy
        os.environ['HTTP_PROXY'] = proxy
    import gppt
    import gppt.browser as browser
    # Browser installation belongs to deployment, never a request handler.
    if not browser.is_chromium_installed():
        return {'ok': False, 'code': 'browser_missing', 'message':
                '未安装登录浏览器。请重新构建 Docker 镜像，或执行 python -m playwright install --with-deps chromium。'}
    browser.install = lambda *args, **kwargs: None
    # Chromium uses socks5 (remote DNS); requests accepts socks5h.
    browser.PROXIES = {'https': proxy.replace('socks5h://', 'socks5://', 1)} if proxy else {}
    try:
        token = gppt.login(username=payload['username'], password=payload['password'],
                           totp_secret=payload.get('totp_secret', ''), headless=True)
        if not token.refresh_token or not token.access_token:
            raise ValueError('empty token')
        return {'ok': True, 'refresh_token': token.refresh_token}
    except Exception as error:
        detail = str(error).lower()
        if any(word in detail for word in ('two-factor', 'totp', '2fa', 'base32')):
            return {'ok': False, 'code': 'two_factor_required', 'message':
                    '账号需要两步验证。请配置有效的 TOTP 密钥，或在本地手动登录后填写 refresh token。'}
        if any(word in detail for word in ('connection', 'proxy', 'resolve', 'form did not appear')):
            return {'ok': False, 'code': 'network_error', 'message':
                    '无法连接 Pixiv 登录页面。检查服务器网络及 PIXIV_PROXY 配置。'}
        return {'ok': False, 'code': 'login_failed', 'message':
                'Pixiv 登录未完成：请确认账号密码；验证码、风控或登录页面变化也可能导致失败。可在本地手动登录后填写 refresh token。'}


def main():
    payload = json.loads(sys.stdin.read())
    # SDK/browser logs must never mix into the result or application logs.
    with open(os.devnull, 'w') as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        try:
            result = login(payload)
        except ImportError:
            result = {'ok': False, 'code': 'dependency_missing', 'message': '请安装 requirements.txt 中的 gppt 及其浏览器依赖。'}
        except Exception:
            result = {'ok': False, 'code': 'worker_failed', 'message': '登录环境异常，请检查浏览器依赖及临时目录权限。'}
    sys.stdout.write(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
