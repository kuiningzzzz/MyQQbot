"""Conversation evidence, not a second model or a mandatory reply decision."""
import re


def segments(event):
    value = event.get('message')
    return [s for s in value if isinstance(s, dict) and isinstance(s.get('data'), dict)] if isinstance(value, list) else []


def message_text(event):
    return ''.join(str(s['data'].get('text') or '') for s in segments(event) if s.get('type') == 'text')


class ConversationEvidence:
    def __init__(self, feature):
        self.feature = feature
        self.records = {}

    def remember(self, record):
        self.records[(record['scene_type'] + ':' + record['scene_id'], str(record['id']))] = record

    def lookup(self, scene, identifier):
        key = (scene, str(identifier))
        if key not in self.records:
            record = next((r for r in reversed(self.feature.context.scenes.get(scene, {}).get('recent_messages', []))
                           if str(r['id']) == key[1]), None)
            if record is None:
                # QQ IDs are scoped to the scene. Never use a quoted sender
                # supplied by the quoting user or search another conversation.
                with self.feature.journal.connect() as db:
                    row = db.execute('SELECT * FROM messages WHERE identity=?', (scene + ':' + key[1],)).fetchone()
                if row:
                    record = self.feature.journal_record(dict(row))['record']
            self.records[key] = record
        return self.records[key]

    def facts(self, event, scene):
        parts = segments(event)
        mentions = list(dict.fromkeys(str(s['data']['qq']) for s in parts
                                    if s.get('type') == 'at' and s['data'].get('qq') is not None))[:16]
        replies = []
        for identifier in list(dict.fromkeys(str(s['data']['id']) for s in parts
                                           if s.get('type') == 'reply' and s['data'].get('id') is not None))[:8]:
            record = self.lookup(scene, identifier)
            replies.append({'message_id': identifier, 'resolved': record is not None, **({
                'sender_qq': record['qq'], 'is_self': bool(record.get('is_self')),
                'content': record.get('rendered_content', record['content'])[:1000],
            } if record else {})})
        return {'mention_qq': mentions, 'replies': replies}


def address_hint(event, text, nickname, identity_ids, facts):
    """Only explicit routing is strong evidence; a bare 'you' is not."""
    if event.get('message_type') == 'private':
        return 'private', True
    if set(facts['mention_qq']) & identity_ids:
        return 'mention_self', True
    name_address = nickname and re.match(
        r'^\s*' + re.escape(nickname) + r'(?:\s*[,，:：!！?？~～]|\s+|$|(?=帮|请|能|在吗|你|给我|来))', text, re.I)
    if name_address:
        return 'nickname_address', True
    # Without a direct address to us, an actual @ to someone else takes
    # precedence over a quoted bot message. "小鱼，提醒@某人" still addresses us.
    if facts['mention_qq']:
        return 'addressed_to_others', False
    if any(r.get('is_self') for r in facts['replies']):
        return 'reply_self', True
    if facts['replies']:
        return ('reply_others' if all(r['resolved'] for r in facts['replies']) else 'reply_unknown'), False
    if nickname and nickname.casefold() in text.casefold():
        return 'nickname_mentioned', False
    return 'group_observation', False
