import re
import sqlite3
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

MAX_CENTS = 99_999_999_999_999


def name_value(value):
    if not isinstance(value, str) or not (value := value.strip()) or len(value) > 80 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError('人名需要1至80个字符，不能包含换行或控制字符。')
    return value


def cents_value(value):
    if not isinstance(value, str) or len(value) > 20 or not re.fullmatch(r'[+-]?\d+(?:\.\d{1,2})?', value.strip(), re.ASCII):
        raise ValueError('金额请使用普通数字，最多两位小数，允许负数。')
    cents = int(Decimal(value.strip()) * 100)
    if abs(cents) > MAX_CENTS:
        raise ValueError('金额超出支持范围。')
    return cents


def amount_text(cents):
    return format(Decimal(cents) / 100, '.2f')


class Store:
    def __init__(self, root: Path):
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / 'loveyou.sqlite3'
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS sponsors (name TEXT PRIMARY KEY, cents INTEGER NOT NULL, version INTEGER NOT NULL DEFAULT 1)')
            db.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value BLOB NOT NULL)')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def snapshot(self):
        with self.connect() as db:
            db.execute('BEGIN')
            entries = [{'name': row['name'], 'amount': amount_text(row['cents']), 'version': row['version'], 'cents': row['cents']}
                       for row in db.execute('SELECT * FROM sponsors ORDER BY cents DESC, name ASC')]
            row = db.execute("SELECT value FROM settings WHERE key='payment_image'").fetchone()
        return entries, bytes(row[0]) if row else None

    def add(self, name, amount):
        name, delta = name_value(name), cents_value(amount)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT cents FROM sponsors WHERE name=?', (name,)).fetchone()
            cents = (row[0] if row else 0) + delta
            if abs(cents) > MAX_CENTS:
                raise ValueError('累计金额超出支持范围。')
            db.execute('INSERT INTO sponsors(name,cents) VALUES (?,?) ON CONFLICT(name) DO UPDATE SET cents=excluded.cents,version=sponsors.version+1', (name, cents))
        return amount_text(cents)

    def edit(self, name, amount, previous_name=None, version=None):
        name, cents = name_value(name), cents_value(amount)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if previous_name is None:
                if db.execute('SELECT 1 FROM sponsors WHERE name=?', (name,)).fetchone():
                    raise ValueError('该人名已存在，请编辑原记录。')
                db.execute('INSERT INTO sponsors(name,cents) VALUES (?,?)', (name, cents))
            else:
                previous_name = name_value(previous_name)
                if name != previous_name and db.execute('SELECT 1 FROM sponsors WHERE name=?', (name,)).fetchone():
                    raise ValueError('新的人名已存在，未合并或覆盖。')
                changed = db.execute('UPDATE sponsors SET name=?,cents=?,version=version+1 WHERE name=? AND version=?', (name, cents, previous_name, version))
                if changed.rowcount != 1:
                    raise ValueError('记录已发生变化，请刷新榜单后重试。')

    def delete(self, name, version):
        with self.connect() as db:
            changed = db.execute('DELETE FROM sponsors WHERE name=? AND version=?', (name_value(name), version))
            if changed.rowcount != 1:
                raise ValueError('记录已发生变化，请刷新榜单后重试。')

    def set_image(self, image):
        with self.connect() as db:
            if image is None:
                db.execute("DELETE FROM settings WHERE key='payment_image'")
            else:
                db.execute("INSERT INTO settings VALUES ('payment_image',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (image,))
