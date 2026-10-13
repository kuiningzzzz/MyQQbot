"""Usage: python search.py MEMORY_DIRECTORY QUERY. Never edits memory."""
import argparse
import json
import re
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument('memory_directory', type=Path)
parser.add_argument('query')
args = parser.parse_args()
root = args.memory_directory.resolve()
hits = []
for area in ('user', 'group', 'common'):
    for file in (root / area).rglob('*.md'):
        if file.is_symlink() or not file.resolve().is_relative_to(root):
            continue
        relative = file.relative_to(root).as_posix()
        if not re.fullmatch(r'(?:user/[0-9]+/(?:PERSON|PREFERENCE|RECENTNESS)\.md|user/[0-9]+/memory/[^/]+\.md|group/[0-9]+/[^/\r\n]+\.md|common/GLOSSARY\.md)', relative) or any(char in relative for char in '<>:"|?*\\'):
            continue
        for number, line in enumerate(file.read_text('utf-8').splitlines(), 1):
            if args.query.casefold() in line.casefold():
                hits.append({'path': file.relative_to(root).as_posix(), 'line': number, 'text': line})
print(json.dumps(hits[:100], ensure_ascii=False))
