from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import re

from lunar_python import LunarYear, Solar

BJT = ZoneInfo('Asia/Shanghai')
PAST = '我只是个小破机器人，不可以回溯时空哦！'
FORMATS = {'-y': 'MM-DD-HH-MM-SS', '-m': 'DD-HH-MM-SS', '-w': '01至07-HH-MM-SS（周一至周日）', '-d': 'HH-MM-SS', '-o': 'YY-MM-DD-HH-MM-SS（也支持四位年份）'}
LUNAR_MIN_YEAR, LUNAR_MAX_YEAR = 1900, 2100


def base_flag(flag):
    return flag.removesuffix('-cn') if isinstance(flag, str) else flag


def lunar_datetime(year, month, day, **clock):
    if not LUNAR_MIN_YEAR <= year <= LUNAR_MAX_YEAR:
        raise ValueError('农历年份须为1900至2100')
    lunar_month = LunarYear.fromYear(year).getMonth(month)
    if lunar_month is None or not 1 <= day <= lunar_month.getDayCount():
        raise ValueError('该农历日期不存在（月份没有这一天）')
    solar = Solar.fromJulianDay(lunar_month.getFirstJulianDay() + day - 1)
    return datetime(solar.getYear(), solar.getMonth(), solar.getDay(), tzinfo=BJT, **clock)


def parse_time(flag, value):
    lunar = isinstance(flag, str) and flag.endswith('-cn')
    flag = base_flag(flag)
    if flag not in FORMATS or not isinstance(value, str):
        raise ValueError('循环选项须为 -y / -m / -w / -d / -o；农历加-cn后缀，如-y-cn')
    counts = {'-y': 5, '-m': 4, '-w': 4, '-d': 3, '-o': 6}
    pieces = value.split('-')
    if len(pieces) != counts[flag] or any(
        not re.fullmatch(r'(?:[0-9]{1,2}|[0-9]{4})' if flag == '-o' and index == 0 else r'[0-9]{1,2}', part)
        for index, part in enumerate(pieces)
    ):
        raise ValueError('时间格式：' + FORMATS[flag] + '；用-分隔，月、日、星期、时分秒无需补零')
    numbers = list(map(int, pieces))
    hour, minute, second = numbers[-3:]
    if not (0 <= hour < 24 and 0 <= minute < 60 and 0 <= second < 60):
        raise ValueError('时分秒超出范围')
    if flag == '-o':
        if len(pieces[0]) <= 2:
            numbers[0] += 2000
        if lunar:
            lunar_datetime(*numbers[:3], hour=hour, minute=minute, second=second)
        else:
            datetime(*numbers, tzinfo=BJT)  # validate real calendar date
    elif flag == '-y':
        if lunar:
            if not (1 <= numbers[0] <= 12 and 1 <= numbers[1] <= 30):
                raise ValueError('农历月份须为01至12，日期须为01至30')
        else:
            datetime(2000, *numbers, tzinfo=BJT)  # allow February 29
    elif flag == '-m' and not 1 <= numbers[0] <= (30 if lunar else 31):
        raise ValueError('农历日期须为01至30' if lunar else '日期须为01至31')
    elif flag == '-w' and not 1 <= numbers[0] <= 7:
        raise ValueError('星期须为01至07（周一至周日）')
    return numbers


def next_run(flag, value, now=None):
    now = datetime.now(BJT) if now is None else now.astimezone(BJT)
    parts = parse_time(flag, value)
    lunar = flag.endswith('-cn')
    flag = base_flag(flag)
    clock = dict(zip(('hour', 'minute', 'second'), parts[-3:]), microsecond=0)
    if flag == '-o':
        candidate = lunar_datetime(*parts[:3], **clock) if lunar else datetime(*parts, tzinfo=BJT)
        if candidate <= now:
            raise ValueError(PAST)
        return candidate.timestamp()
    if flag == '-d':
        candidate = now.replace(**clock)
        return (candidate if candidate > now else candidate + timedelta(days=1)).timestamp()
    if flag == '-w':
        candidate = now.replace(**clock) + timedelta(days=(parts[0] - now.isoweekday()) % 7)
        return (candidate if candidate > now else candidate + timedelta(days=7)).timestamp()
    if lunar:
        current = Solar.fromYmd(now.year, now.month, now.day).getLunar()
        if not LUNAR_MIN_YEAR <= current.getYear() <= LUNAR_MAX_YEAR:
            raise ValueError('农历调度支持1900至2100年')
        for year in range(current.getYear(), LUNAR_MAX_YEAR + 1):
            # Annual birthdays use the ordinary month once, never its leap twin.
            # Monthly recurrences follow every lunar month, including leap months.
            months = [LunarYear.fromYear(year).getMonth(parts[0])] if flag == '-y' else LunarYear.fromYear(year).getMonthsInYear()
            day = parts[1] if flag == '-y' else parts[0]
            for month in months:
                if day > month.getDayCount():
                    continue
                candidate = lunar_datetime(year, month.getMonth(), day, **clock)
                if candidate > now:
                    return candidate.timestamp()
        raise ValueError('2100年内找不到有效的下一次农历日期')
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


def display_lunar_time(timestamp):
    if not timestamp:
        return '—'
    local = datetime.fromtimestamp(timestamp, BJT)
    lunar = Solar.fromYmd(local.year, local.month, local.day).getLunar()
    return f'{lunar.getYear()}年{lunar.getMonthInChinese()}月{lunar.getDayInChinese()} {local:%H:%M:%S}'
