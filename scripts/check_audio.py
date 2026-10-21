"""Check cloud audio without starting QQ, reading chat data, or printing secrets."""
import argparse
import asyncio
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


async def check(live):
    from src.feature.ai.feature import AiFeature
    from src.feature.ai.audio import AudioError
    with tempfile.TemporaryDirectory() as directory:
        feature = AiFeature(Path(directory))
        report = {'deepseek_key_configured': bool(feature.config.load().api_key), 'audio': feature.audio.status(), 'live': live}
        try:
            if live:
                ctx = SimpleNamespace(message_type='private', user_id=0)
                result = await feature.audio.synthesize(ctx, '你好，这是语音服务连通性测试。', '自然轻松地说话。', lambda: True)
                _, audio = feature.audio.voices.get('private:0', identifier=result['voice_id'])
                report['synthesis'] = {'success': True, 'bytes': len(audio)}
                description = await feature.audio.analyze_bytes(audio)
                report['hearing'] = {'success': True, 'description': str(description)[:800]}
        except AudioError as exc:
            report['failure'] = {'code': exc.code, 'message': str(exc)}
        except Exception as exc:
            report['failure'] = {'code': type(exc).__name__, 'message': '检查失败，请检查环境和依赖；未发送QQ消息。'}
        finally:
            report['audio'] = feature.audio.status()
            await feature.stop()
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return int('failure' in report)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='默认仅检查配置；--live发起一次合成和一次声音理解，产生少量云端费用，不发送QQ消息。')
    parser.add_argument('--live', action='store_true')
    args = parser.parse_args()
    raise SystemExit(asyncio.run(check(args.live)))
