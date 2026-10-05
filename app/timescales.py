"""深空测控时间轴归一化核心。

支持两种事件输入：

1. GPS 时间：GPS 周 (week)、周内秒 (seconds of week)、周内纳秒偏移；
2. UTC 时间：``YYYY-MM-DDTHH:MM:SS[.fraction]Z``，闰秒日允许秒值 60。

二者都归一到同一条 TAI 时间轴，结果为自 TAI 历元
``1970-01-01T00:00:00 TAI`` 起算的整数纳秒。

设计约束（对应需求）：

* 闰秒表冻结在 IERS 公布的截至 2016-12-31 的全部闰秒；
* 支持年代闭区间为
  ``1972-01-01T00:00:00Z`` … ``2017-01-01T00:00:00Z``
  （含 2016-12-31 的正闰秒 :60 整秒），区间外一律拒绝；
* **全程整数运算，不出现浮点数**：纳秒是最小单位，除法只使用
  精确整除 / ``divmod``，输出十进制字符串直接来自 ``int``。

闰秒约定（与 SOFA/USNO 等经典换算一致）：正闰秒标签 ``23:59:60``
物理上占用跳变前那一秒，因此该整秒内仍使用跳变前的 UTC−TAI
偏移；下一个 UTC 午夜才启用新偏移。这样 :59 → :60 → 次日 00:00:00
三个标签在 TAI 轴上严格相隔一秒、互不重叠。
"""

from __future__ import annotations

from typing import NamedTuple

NANOS_PER_SECOND = 1_000_000_000
SECONDS_PER_DAY = 86_400
SECONDS_PER_WEEK = 604_800

GPS_SECONDS_PER_WEEK_MAX = 604_799
NANOS_MAX = 999_999_999


class TimeFormatError(ValueError):
    """携带可定位错误码的时间换算异常。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# 闰秒表（IERS，冻结于 2016-12-31）
#
# 每个三元组是“以 23:59:60 结束的那个 UTC 日期”（闰秒实际发生日）。
# 1972-01-01 是整数闰秒体制的起点（TAI−UTC=10s），本身不是闰秒日；
# 其余 27 个日期各发生一次 +1 正闰秒。最后一个为 2016-12-31，
# 此后 TAI−UTC=37s。
# ---------------------------------------------------------------------------
_SUPPORT_START = (1972, 1, 1)
_SUPPORT_END = (2017, 1, 1)  # 闭区间上界（2016 闰秒之后的午夜）

_LEAP_DAYS: tuple[tuple[int, int, int], ...] = (
    (1972, 6, 30),
    (1972, 12, 31),
    (1973, 12, 31),
    (1974, 12, 31),
    (1975, 12, 31),
    (1976, 12, 31),
    (1977, 12, 31),
    (1978, 12, 31),
    (1979, 12, 31),
    (1981, 6, 30),
    (1982, 6, 30),
    (1983, 6, 30),
    (1985, 6, 30),
    (1987, 12, 31),
    (1989, 12, 31),
    (1990, 12, 31),
    (1992, 6, 30),
    (1993, 6, 30),
    (1994, 6, 30),
    (1995, 12, 31),
    (1997, 6, 30),
    (1998, 12, 31),
    (2005, 12, 31),
    (2008, 12, 31),
    (2012, 6, 30),
    (2015, 6, 30),
    (2016, 12, 31),
)

_INITIAL_TAI_MINUS_UTC = 10
_TAI_MINUS_GPS = 19  # GPS 历元 1980-01-06 起恒定 19s


# ---------------------------------------------------------------------------
# 公历日期 <-> 自 1970-01-01 起的天数（Howard Hinnant 位值算法，纯整数）
# ---------------------------------------------------------------------------
def days_from_civil(year: int, month: int, day: int) -> int:
    """公历 (year, month, day) 到 1970-01-01 的天数（前推为负）。"""
    y = year - (1 if month <= 2 else 0)
    era = y // 400
    yoe = y - era * 400
    doy = (153 * (month + (-3 if month > 2 else 9)) + 2) // 5 + day - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146097 + doe - 719468


def civil_from_days(days: int) -> tuple[int, int, int]:
    """days_from_civil 的逆运算。"""
    z = days + 719468
    era = z // 146097
    doe = z - era * 146097
    yoe = (doe - doe // 1460 + doe // 36524 - doe // 146096) // 365
    y = yoe + era * 400
    doy = doe - (365 * yoe + yoe // 4 - yoe // 100)
    mp = (5 * doy + 2) // 153
    d = doy - (153 * mp + 2) // 5 + 1
    m = mp + (3 if mp < 10 else -9)
    y += 1 if m <= 2 else 0
    return y, m, d


def _days_in_month(year: int, month: int) -> int:
    if month == 2:
        leap = year % 400 == 0 or (year % 4 == 0 and year % 100 != 0)
        return 29 if leap else 28
    return 31 if month in (1, 3, 5, 7, 8, 10, 12) else 30


# ---------------------------------------------------------------------------
# 预计算结构
# ---------------------------------------------------------------------------
_SUPPORT_START_ORD = days_from_civil(*_SUPPORT_START)
_SUPPORT_END_ORD = days_from_civil(*_SUPPORT_END)

# TAI 纳秒支持区间（闭区间）
TAI_MIN_NANOSECONDS = (
    _SUPPORT_START_ORD * SECONDS_PER_DAY + _INITIAL_TAI_MINUS_UTC
) * NANOS_PER_SECOND
TAI_MAX_NANOSECONDS = (
    _SUPPORT_END_ORD * SECONDS_PER_DAY + 37
) * NANOS_PER_SECOND

_LEAP_DAY_ORDS = tuple(days_from_civil(*d) for d in _LEAP_DAYS)
_LEAP_DAY_SET = frozenset(_LEAP_DAY_ORDS)

# 分段：每段内 TAI−UTC 偏移恒定；闰秒日并入“跳变前”那一段，
# 该段因此多出整整一秒（86401 个标签秒）。
# 字段：段起始 TAI 秒（含）、段结束 TAI 秒（不含，末段为 None）、
#       段起始 UTC 日序数、本段闰秒日序数（无则 None）、TAI−UTC 偏移。
class _Segment(NamedTuple):
    start_second: int
    end_second: int | None
    start_day: int
    leap_day: int | None
    tai_minus_utc: int


def _build_segments() -> tuple[_Segment, ...]:
    segs: list[_Segment] = []
    n = len(_LEAP_DAY_ORDS)
    for r in range(n + 1):
        start_day = (
            _SUPPORT_START_ORD if r == 0 else _LEAP_DAY_ORDS[r - 1] + 1
        )
        offset = _INITIAL_TAI_MINUS_UTC + r
        leap_day = _LEAP_DAY_ORDS[r] if r < n else None
        start_second = start_day * SECONDS_PER_DAY + offset
        end_second = (
            None
            if leap_day is None
            else (leap_day + 1) * SECONDS_PER_DAY + offset + 1
        )
        segs.append(
            _Segment(start_second, end_second, start_day, leap_day, offset)
        )
    return tuple(segs)


_SEGMENTS = _build_segments()

# GPS 历元 1980-01-06T00:00:00Z 的 TAI 纳秒（当时 TAI−UTC=19）。
GPS_EPOCH_TAI_NANOSECONDS = (
    days_from_civil(1980, 1, 6) * SECONDS_PER_DAY + _TAI_MINUS_GPS
) * NANOS_PER_SECOND

_SUPPORTED_ERA_TEXT = (
    "1972-01-01T00:00:00Z .. 2017-01-01T00:00:00Z "
    "(leap-second table frozen at 2016-12-31)"
)


def _check_era(tai_nanoseconds: int) -> None:
    if (
        tai_nanoseconds < TAI_MIN_NANOSECONDS
        or tai_nanoseconds > TAI_MAX_NANOSECONDS
    ):
        raise TimeFormatError(
            "unsupported_era",
            f"instant is outside the supported era {_SUPPORTED_ERA_TEXT}",
        )


# ---------------------------------------------------------------------------
# TAI -> UTC
# ---------------------------------------------------------------------------
class UtcInstant(NamedTuple):
    year: int
    month: int
    day: int
    hour: int
    minute: int
    second: int  # 闰秒标签时为 60
    nanosecond: int
    tai_minus_utc: int


def tai_to_utc(tai_nanoseconds: int) -> UtcInstant:
    """把 TAI 整数纳秒换算为带闰秒标签的 UTC 分量。"""
    if not isinstance(tai_nanoseconds, int) or isinstance(
        tai_nanoseconds, bool
    ):
        raise TypeError("tai_nanoseconds must be an integer")
    _check_era(tai_nanoseconds)

    total_seconds, nanosecond = divmod(tai_nanoseconds, NANOS_PER_SECOND)

    segment: _Segment | None = None
    for seg in _SEGMENTS:
        if total_seconds >= seg.start_second and (
            seg.end_second is None or total_seconds < seg.end_second
        ):
            segment = seg
            break
    if segment is None:  # 理论不可达：已做年代检查
        raise TimeFormatError("unsupported_era", "no UTC segment found")

    delta = total_seconds - segment.start_second
    leap_day = segment.leap_day
    if leap_day is not None:
        day_count = leap_day - segment.start_day + 1
        if delta >= day_count * SECONDS_PER_DAY:
            # 只能恰好落在多出的那一秒：23:59:60
            if delta != day_count * SECONDS_PER_DAY:
                raise TimeFormatError("unsupported_era", "leap second gap")
            day_ord = leap_day
            second_of_day = SECONDS_PER_DAY
        else:
            day_offset, second_of_day = divmod(delta, SECONDS_PER_DAY)
            day_ord = segment.start_day + day_offset
    else:
        day_offset, second_of_day = divmod(delta, SECONDS_PER_DAY)
        day_ord = segment.start_day + day_offset

    if second_of_day == SECONDS_PER_DAY:
        hour, minute, second = 23, 59, 60
    else:
        hour, rest = divmod(second_of_day, 3600)
        minute, second = divmod(rest, 60)

    year, month, day = civil_from_days(day_ord)
    return UtcInstant(
        year,
        month,
        day,
        hour,
        minute,
        second,
        nanosecond,
        segment.tai_minus_utc,
    )


def format_utc(instant: UtcInstant) -> str:
    """规范 UTC 表示；非零纳秒固定 9 位小数，结尾 Z。"""
    text = (
        f"{instant.year:04d}-{instant.month:02d}-{instant.day:02d}"
        f"T{instant.hour:02d}:{instant.minute:02d}:{instant.second:02d}"
    )
    if instant.nanosecond:
        text += f".{instant.nanosecond:09d}"
    return text + "Z"


# ---------------------------------------------------------------------------
# UTC -> TAI
# ---------------------------------------------------------------------------
def utc_parts_to_tai(
    year: int,
    month: int,
    day: int,
    hour: int,
    minute: int,
    second: int,
    nanosecond: int,
) -> tuple[int, int]:
    """校验过的 UTC 分量 -> (TAI 纳秒, TAI−UTC 偏移)。"""
    day_ord = days_from_civil(year, month, day)

    # 闰秒标签 60 只能出现在表内闰秒日的 23:59:60。
    if second == 60:
        if not (hour == 23 and minute == 59):
            raise TimeFormatError(
                "utc_leap_second_position",
                f"second 60 is only valid at 23:59:60 on a tabulated "
                f"leap day; got {hour:02d}:{minute:02d}:60",
            )
        if day_ord not in _LEAP_DAY_SET:
            raise TimeFormatError(
                "utc_leap_second_position",
                f"{year:04d}-{month:02d}-{day:02d} is not a date carrying "
                f"a leap second in the table (frozen at 2016-12-31)",
            )
        second_of_day = SECONDS_PER_DAY
    else:
        second_of_day = hour * 3600 + minute * 60 + second

    # 闰秒日整秒内仍使用跳变前的旧偏移。
    offset = _INITIAL_TAI_MINUS_UTC + sum(
        1 for ordinal in _LEAP_DAY_ORDS if ordinal < day_ord
    )
    tai_seconds = day_ord * SECONDS_PER_DAY + second_of_day + offset
    tai_nanoseconds = tai_seconds * NANOS_PER_SECOND + nanosecond
    _check_era(tai_nanoseconds)
    return tai_nanoseconds, offset


def parse_utc(text: str) -> tuple[int, str, int]:
    """解析 UTC 字符串。

    返回 ``(TAI 纳秒, 规范 UTC 字符串, TAI−UTC 偏移)``。
    """
    if not isinstance(text, str):
        raise TimeFormatError("utc_type", "utc must be a string")

    # 严格形态：YYYY-MM-DDTHH:MM:SS[.fraction]Z，仅允许大写 T/Z。
    if (
        len(text) < 20
        or text[4] != "-"
        or text[7] != "-"
        or text[10] != "T"
        or text[13] != ":"
        or text[16] != ":"
        or text[-1] != "Z"
    ):
        raise TimeFormatError(
            "utc_syntax",
            "UTC must look like YYYY-MM-DDTHH:MM:SS[.fraction]Z",
        )

    date_part = text[0:10]
    time_part = text[11:-1]
    if "." in time_part:
        hms, frac = time_part.split(".", 1)
        if not frac or not frac.isdigit():
            raise TimeFormatError(
                "utc_syntax", "fractional seconds must be digits"
            )
        if len(frac) > 9:
            raise TimeFormatError(
                "utc_fraction_precision",
                f"fractional seconds may have at most 9 digits; "
                f"got {len(frac)}",
            )
        nanosecond = int(frac.ljust(9, "0"))
    else:
        hms = time_part
        nanosecond = 0

    digits = date_part.split("-") + hms.split(":")
    if len(digits) != 6 or not all(
        p.isdigit() and len(p) == w
        for p, w in zip(digits, (4, 2, 2, 2, 2, 2))
    ):
        raise TimeFormatError(
            "utc_syntax",
            "UTC must look like YYYY-MM-DDTHH:MM:SS[.fraction]Z",
        )

    year = int(digits[0])
    month = int(digits[1])
    day = int(digits[2])
    hour = int(digits[3])
    minute = int(digits[4])
    second = int(digits[5])

    if not (1 <= month <= 12):
        raise TimeFormatError(
            "utc_invalid_calendar", f"invalid month: {month}"
        )
    if not (1 <= day <= _days_in_month(year, month)):
        raise TimeFormatError(
            "utc_invalid_calendar",
            f"invalid calendar date {year:04d}-{month:02d}-{day:02d}",
        )
    if not (0 <= hour <= 23):
        raise TimeFormatError("utc_out_of_range", f"invalid hour: {hour}")
    if not (0 <= minute <= 59):
        raise TimeFormatError(
            "utc_out_of_range", f"invalid minute: {minute}"
        )
    if not (0 <= second <= 60):
        raise TimeFormatError(
            "utc_out_of_range", f"invalid second: {second}"
        )
    if not (0 <= nanosecond <= NANOS_MAX):
        raise TimeFormatError(
            "utc_out_of_range", "nanosecond out of range"
        )

    tai_nanoseconds, _offset = utc_parts_to_tai(
        year, month, day, hour, minute, second, nanosecond
    )
    canonical = format_utc(tai_to_utc(tai_nanoseconds))
    return tai_nanoseconds, canonical, _offset


# ---------------------------------------------------------------------------
# GPS -> TAI
# ---------------------------------------------------------------------------
def gps_to_tai(
    week: int, seconds_of_week: int, nanosecond: int
) -> int:
    """GPS (周, 周内秒, 纳秒) -> TAI 整数纳秒。入参必须已是 int。"""
    if not isinstance(week, int) or isinstance(week, bool):
        raise TimeFormatError("gps_field_type", "gps_week must be an integer")
    if (
        not isinstance(seconds_of_week, int)
        or isinstance(seconds_of_week, bool)
    ):
        raise TimeFormatError("gps_field_type", "gps_sow must be an integer")
    if not isinstance(nanosecond, int) or isinstance(nanosecond, bool):
        raise TimeFormatError(
            "gps_field_type", "gps_nanos must be an integer"
        )

    if week < 0:
        raise TimeFormatError("gps_week_range", f"gps_week must be >= 0; got {week}")
    if not (0 <= seconds_of_week <= GPS_SECONDS_PER_WEEK_MAX):
        raise TimeFormatError(
            "gps_sow_range",
            f"gps_sow must be within 0..{GPS_SECONDS_PER_WEEK_MAX}; "
            f"got {seconds_of_week}",
        )
    if not (0 <= nanosecond <= NANOS_MAX):
        raise TimeFormatError(
            "gps_nanos_range",
            f"gps_nanos must be within 0..{NANOS_MAX}; got {nanosecond}",
        )

    tai_nanoseconds = (
        GPS_EPOCH_TAI_NANOSECONDS
        + (week * SECONDS_PER_WEEK + seconds_of_week) * NANOS_PER_SECOND
        + nanosecond
    )
    _check_era(tai_nanoseconds)
    return tai_nanoseconds


# ---------------------------------------------------------------------------
# 对外归一化：两种输入 -> 完全一致的结果三元组
# ---------------------------------------------------------------------------
class NormalizedEvent(NamedTuple):
    tai_nanoseconds: int
    utc: str
    tai_minus_utc: int


def normalize_utc(text: str) -> NormalizedEvent:
    tai_nanoseconds, canonical, offset = parse_utc(text)
    return NormalizedEvent(tai_nanoseconds, canonical, offset)


def normalize_gps(
    week: int, seconds_of_week: int, nanosecond: int
) -> NormalizedEvent:
    tai_nanoseconds = gps_to_tai(week, seconds_of_week, nanosecond)
    instant = tai_to_utc(tai_nanoseconds)
    return NormalizedEvent(
        tai_nanoseconds, format_utc(instant), instant.tai_minus_utc
    )
