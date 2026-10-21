"""Standard-library CLI for the currently authorized dsh task, not admin login."""
import argparse
import json
import os
from pathlib import Path
import re
import sys
import urllib.request
from urllib.parse import urlparse


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description='QQbot 内置表情管理；仅当前主人执行任务可用')
    parser.add_argument('--task-id', required=True)
    parser.add_argument('action', choices=['sync', 'list', 'get', 'add', 'update', 'delete', 'retry'])
    parser.add_argument('--input', type=Path)
    args = parser.parse_args()
    try:
        body = {}
        if args.input:
            if args.input.stat().st_size > 64 * 1024:
                raise ValueError('请求JSON最多64KiB；图片请提供path、url或attachment_id')
            body = json.loads(args.input.read_text('utf-8-sig'))
        if not isinstance(body, dict) or 'base64' in body:
            raise ValueError('请求必须为对象；图片使用path、url或attachment_id')
        url = os.environ.get('DSH_QQBOT_CALLBACK', '')
        token = os.environ.get('DSH_QQBOT_STICKER_TOKEN', '')
        if not url:
            raise ValueError('缺少表情管理桥接地址；请更新并重启服务，在当前主人执行任务中运行')
        if not token:
            raise ValueError('缺少当前任务的表情管理凭据；请更新并重启服务，通过主人发起的执行任务运行，勿手工填入全局令牌')
        parsed = urlparse(url)
        if parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or parsed.path != '/tool' or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('表情管理桥接地址无效，必须使用宿主注入的本机回环地址')
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,100}', args.task_id):
            raise ValueError('执行任务编号格式无效，必须使用本次 origin.task_id 或 execution_task_id')
        payload = {'task_id': args.task_id, 'name': '__stickers', 'arguments': {'action': args.action, 'body': body}}
        request = urllib.request.Request(url, data=json.dumps(payload).encode(),
            headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}, method='POST')
        client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with client.open(request, timeout=140) as response:
            result = json.load(response)
        if not result.get('ok'):
            raise ValueError(result.get('error', '宿主拒绝表情操作'))
        output = result['result']
        print(json.dumps(output, ensure_ascii=False))
        return 0 if output.get('success') else 1
    except Exception as error:
        message = str(error) if isinstance(error, ValueError) else '桥接失败或超时；QQ写操作结果可能未知，请同步核对，不要自动重试'
        print(json.dumps({'success': False, 'error': message}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    sys.exit(main())
