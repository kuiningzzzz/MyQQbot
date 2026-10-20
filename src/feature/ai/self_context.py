"""Small mandatory rules and current authority; detailed manuals load on demand."""
import json
from pathlib import Path


CORE_PROMPT = Path(__file__).with_name('core_prompt.md')


def self_context(feature, kind, *, cwd=None, scene=None, requester=None,
                 owner=False, explicit=False, files_directory=None, role=None):
    if kind not in {'chat', 'memory'}:
        raise ValueError('未知对话类型')
    settings = feature.config.load()
    chat = kind == 'chat'
    role = role or ('main' if chat else 'memory')
    state = {
        'conversation': kind,
        'agent_role': role,
        'background_spawn_available': role == 'main',
        'native_execution_allowed': role in {'worker', 'memory', 'scheduled'},
        'workspace': str(feature.workspace.root),
        'cwd': str(cwd or feature.workspace.root),
        'formal_memory_directory': str(feature.workspace.memory_root),
        'editable_memory_directory': str((cwd or feature.workspace.root) / 'memory'),
        'current_scene': scene if chat else None,
        'requester_qq': str(requester) if chat and requester is not None else None,
        'owner': bool(chat and owner),
        'explicit_request': bool(chat and explicit),
        'qq_reply_requires_tool_call': chat,
        'files_directory': str(files_directory) if chat and files_directory else None,
        'shell_allowed': bool(chat and owner and role in {'worker', 'scheduled'}),
        'qq_tools_allowed': chat,
        'web_tools_allowed': bool(chat and role in {'worker', 'scheduled'}),
        'sticker_send_allowed': bool(chat and role in {'main', 'scheduled'} and settings.stickers_enabled),
        'file_transfer_allowed': bool(chat and settings.workspace_enabled and explicit),
        'chat_enabled': settings.chat_enabled,
        'summary_enabled': settings.summary_enabled,
        'memory_enabled': settings.memory_enabled,
        'memory_auto_learn': settings.memory_auto_learn,
        'file_transfer_enabled': settings.workspace_enabled,
        'ocr_enabled': settings.ocr_enabled,
        'vector_index_enabled': settings.vector_index_enabled,
        'timezone': settings.timezone,
        'bot_guide': str(feature.workspace.root / '.agents/skills/qqbot-guide/SKILL.md'),
    }
    provider = feature.application_context
    application = provider(False) if provider else {'features': [], 'notice': '模块状态未知'}
    state['application'] = {'features': [
        {key: item[key] for key in ('name', 'title', 'enabled') if key in item}
        for item in application.get('features', [])
    ]}
    if application.get('notice'):
        state['application']['notice'] = application['notice']
    return CORE_PROMPT.read_text('utf-8') + '\n【当轮宿主状态】\n' + json.dumps(state, ensure_ascii=False, separators=(',', ':'))
