"""Usage: python search.py MEMORY_DIRECTORY QUERY. Never edits memory."""
import argparse
import json
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument('memory_directory', type=Path)
parser.add_argument('query')
args = parser.parse_args()
root = args.memory_directory.resolve()
hits = []
for area in ('user', 'common'):
    for file in (root / area).rglob('*.md'):
        if file.is_symlink() or not file.resolve().is_relative_to(root):
            continue
        for number, line in enumerate(file.read_text('utf-8').splitlines(), 1):
            if args.query.casefold() in line.casefold():
                hits.append({'path': file.relative_to(root).as_posix(), 'line': number, 'text': line})
print(json.dumps(hits[:100], ensure_ascii=False))
