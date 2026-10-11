from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import re

BJT = ZoneInfo('Asia/Shanghai')
PAST = '我只是个小破机器人，不可以回溯时空哦！'
FORMATS = {'-y': 'MM-DD-HH-MM-SS', '-m': 'DD-HH-MM-SS', '-w': '01至07-HH-MM-SS（周一至周日）', '-d': 'HH-MM-SS', '-o': 'YY-MM-DD-HH-MM-SS（也支持四位年份）'}


def parse_time(flag, value):
    if flag not in FORMATS or not isinstance(value, str):
        raise ValueError('循环选项须为 -y / -m / -w / -d / -o')
    counts = {'-y': 5, '-m': 4, '-w': 4, '-d': 3, '-o': 6}
    if not re.fullmatch(r'(?:\d{2}|\d{4})(?:-\d{2})+', value) or len(value.split('-')) != counts[flag]:
        raise ValueError('时间格式：' + FORMATS[flag])
    pieces = value.split('-')
    if any(len(p) != 2 for p in pieces[1:]) or (len(pieces[0]) != 2 and flag != '-o'):
        raise ValueError('时间格式：' + FORMATS[flag])
    numbers = list(map(int, pieces))
    hour, minute, second = numbers[-3:]
    if not (0 <= hour < 24 and 0 <= minute < 60 and 0 <= second < 60):
        raise ValueError('时分秒超出范围')
    if flag == '-o':
        if len(pieces[0]) == 2:
            numbers[0] += 2000
        datetime(*numbers, tzinfo=BJT)  # validate real calendar date
    elif flag == '-y':
        datetime(2000, *numbers, tzinfo=BJT)  # allow February 29
    elif flag == '-m' and not 1 <= numbers[0] <= 31:
        raise ValueError('日期须为01至31')
    elif flag == '-w' and not 1 <= numbers[0] <= 7:
        raise ValueError('星期须为01至07（周一至周日）')
    return numbers


def next_run(flag, value, now=None):
    now = datetime.now(BJT) if now is None else now.astimezone(BJT)
    parts = parse_time(flag, value)
    clock = dict(zip(('hour', 'minute', 'second'), parts[-3:]), microsecond=0)
    if flag == '-o':
        candidate = datetime(*parts, tzinfo=BJT)
        if candidate <= now:
            raise ValueError(PAST)
        return candidate.timestamp()
    if flag == '-d':
        candidate = now.replace(**clock)
        return (candidate if candidate > now else candidate + timedelta(days=1)).timestamp()
    if flag == '-w':
        candidate = now.replace(**clock) + timedelta(days=(parts[0] - now.isoweekday()) % 7)
        return (candidate if candidate > now else candidate + timedelta(days=7)).timestamp()
    # Nonexistent dates are skipped, never silently moved to a different day.
    for offset in range(0, 97):
        if flag == '-y':
            year, month, day = now.year + offset, parts[0], parts[1]
        else:
            index = now.year * 12 + now.month - 1 + offset
            year, month, day = index // 12, index % 12 + 1, parts[0]
        try:
            candidate = datetime(year, month, day, tzinfo=BJT, **clock)
        except ValueError:
            continue
        if candidate > now:
            return candidate.timestamp()
    raise ValueError('找不到有效的下一次日期')


def display_time(timestamp):
    return datetime.fromtimestamp(timestamp, BJT).strftime('%Y-%m-%d %H:%M:%S') if timestamp else '—'
