"""时间尺度核心换算测试。

全部使用标准库 unittest，可离线执行：

    python -m unittest discover -s tests -v

校验重点：
* IERS 截至 2016-12-31 的闰秒表与各历史偏移；
* 闰秒边界 23:59:59 -> 23:59:60 -> 00:00:00 的 TAI 严格连续；
* GPS 与 UTC 对同一时刻给出完全一致的 TAI 纳秒；
* 非法日期 / 越界周内秒 / 虚假闰秒位置 / 超年代 全部拒绝；
* 源码层面不存在浮点数（AST 静态检查 + 运行时类型检查）。
"""

from __future__ import annotations

import ast
import os
import unittest
from itertools import pairwise

from app.timescales import (
    NANOS_MAX,
    NANOS_PER_SECOND,
    SECONDS_PER_DAY,
    TAI_MAX_NANOSECONDS,
    TAI_MIN_NANOSECONDS,
    TimeFormatError,
    civil_from_days,
    days_from_civil,
    gps_to_tai,
    normalize_gps,
    normalize_utc,
    tai_to_utc,
    utc_parts_to_tai,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# IERS 官方表：每次闰秒“次日午夜起”的 TAI−UTC 偏移。
# 27 次闰秒，偏移从 11 一路到 37。
OFFICIAL_POST_LEAP_OFFSETS = [
    ("1972-07-01", 11),
    ("1973-01-01", 12),
    ("1974-01-01", 13),
    ("1975-01-01", 14),
    ("1976-01-01", 15),
    ("1977-01-01", 16),
    ("1978-01-01", 17),
    ("1979-01-01", 18),
    ("1980-01-01", 19),
    ("1981-07-01", 20),
    ("1982-07-01", 21),
    ("1983-07-01", 22),
    ("1985-07-01", 23),
    ("1988-01-01", 24),
    ("1990-01-01", 25),
    ("1991-01-01", 26),
    ("1992-07-01", 27),
    ("1993-07-01", 28),
    ("1994-07-01", 29),
    ("1996-01-01", 30),
    ("1997-07-01", 31),
    ("1999-01-01", 32),
    ("2006-01-01", 33),
    ("2009-01-01", 34),
    ("2012-07-01", 35),
    ("2015-07-01", 36),
    ("2017-01-01", 37),
]

# 闰秒日（与模块内表一致，独立写一份用于交叉核对）
OFFICIAL_LEAP_DAYS = [
    "1972-06-30",
    "1972-12-31",
    "1973-12-31",
    "1974-12-31",
    "1975-12-31",
    "1976-12-31",
    "1977-12-31",
    "1978-12-31",
    "1979-12-31",
    "1981-06-30",
    "1982-06-30",
    "1983-06-30",
    "1985-06-30",
    "1987-12-31",
    "1989-12-31",
    "1990-12-31",
    "1992-06-30",
    "1993-06-30",
    "1994-06-30",
    "1995-12-31",
    "1997-06-30",
    "1998-12-31",
    "2005-12-31",
    "2008-12-31",
    "2012-06-30",
    "2015-06-30",
    "2016-12-31",
]


class CalendarTests(unittest.TestCase):
    def test_known_days_from_epoch(self):
        # 1970-01-01 为第 0 天
        self.assertEqual(days_from_civil(1970, 1, 1), 0)
        # 1970、1971 各 365 天
        self.assertEqual(days_from_civil(1972, 1, 1), 730)
        # 到 GPS 历元 1980-01-06 为 3657 天（10 年 + 1972/1976 两个闰日 +5）
        self.assertEqual(days_from_civil(1980, 1, 6), 3657)
        # 2000-01-01：10957 天（独立已知值）
        self.assertEqual(days_from_civil(2000, 1, 1), 10957)
        # 2017-01-01：17167 天
        self.assertEqual(days_from_civil(2017, 1, 1), 17167)

    def test_round_trip_many_years(self):
        ordinal = days_from_civil(1960, 1, 1)
        end = days_from_civil(2030, 1, 1)
        while ordinal <= end:
            y, m, d = civil_from_days(ordinal)
            self.assertEqual(days_from_civil(y, m, d), ordinal)
            ordinal += 1

    def test_leap_years_february(self):
        for leap_year in (2000, 1976, 2016, 1992):
            self.assertEqual(days_from_civil(leap_year, 3, 1) -
                             days_from_civil(leap_year, 2, 1), 29)
        for common_year in (1900, 2100, 2013, 1970):
            self.assertEqual(days_from_civil(common_year, 3, 1) -
                             days_from_civil(common_year, 2, 1), 28)


class AnchorTests(unittest.TestCase):
    def test_known_anchors(self):
        # 支持年代起点：1972-01-01T00:00:00Z，TAI = UTC + 10
        r = normalize_utc("1972-01-01T00:00:00Z")
        self.assertEqual(r.tai_nanoseconds, 730 * SECONDS_PER_DAY * NANOS_PER_SECOND
                         + 10 * NANOS_PER_SECOND)
        self.assertEqual(r.tai_minus_utc, 10)
        self.assertEqual(r.utc, "1972-01-01T00:00:00Z")

        # GPS 历元 1980-01-06T00:00:00Z，TAI = 3657*86400+19 = 315964819
        r = normalize_utc("1980-01-06T00:00:00Z")
        self.assertEqual(r.tai_nanoseconds, 315964819 * NANOS_PER_SECOND)
        self.assertEqual(r.tai_minus_utc, 19)

        # 2016 闰秒三点（Unix 秒 1483228799/8800/8801 已知）
        r59 = normalize_utc("2016-12-31T23:59:59Z")
        r60 = normalize_utc("2016-12-31T23:59:60Z")
        r00 = normalize_utc("2017-01-01T00:00:00Z")
        self.assertEqual(r59.tai_nanoseconds, 1483228835 * NANOS_PER_SECOND)
        self.assertEqual(r60.tai_nanoseconds, 1483228836 * NANOS_PER_SECOND)
        self.assertEqual(r00.tai_nanoseconds, 1483228837 * NANOS_PER_SECOND)
        # :59 与 :60 使用跳变前偏移 36；午夜起 37
        self.assertEqual(r59.tai_minus_utc, 36)
        self.assertEqual(r60.tai_minus_utc, 36)
        self.assertEqual(r00.tai_minus_utc, 37)

    def test_era_bounds(self):
        self.assertEqual(
            TAI_MIN_NANOSECONDS,
            (730 * SECONDS_PER_DAY + 10) * NANOS_PER_SECOND,
        )
        self.assertEqual(
            TAI_MAX_NANOSECONDS,
            (17167 * SECONDS_PER_DAY + 37) * NANOS_PER_SECOND,
        )


class LeapSecondContinuityTests(unittest.TestCase):
    def test_every_leap_second_three_points(self):
        for leap_day in OFFICIAL_LEAP_DAYS:
            at58 = f"{leap_day}T23:59:58Z"
            at59 = f"{leap_day}T23:59:59Z"
            at60 = f"{leap_day}T23:59:60Z"
            y, m, d = (int(x) for x in leap_day.split("-"))
            ny, nm, nd = civil_from_days(days_from_civil(y, m, d) + 1)
            after = f"{ny:04d}-{nm:02d}-{nd:02d}T00:00:00Z"

            rb = normalize_utc(at58)
            r59 = normalize_utc(at59)
            r60 = normalize_utc(at60)
            ra = normalize_utc(after)

            # TAI 轴上严格每秒一格、不重叠不断档
            self.assertEqual(
                r59.tai_nanoseconds - rb.tai_nanoseconds, NANOS_PER_SECOND,
                leap_day,
            )
            self.assertEqual(
                r60.tai_nanoseconds - r59.tai_nanoseconds, NANOS_PER_SECOND,
                leap_day,
            )
            self.assertEqual(
                ra.tai_nanoseconds - r60.tai_nanoseconds, NANOS_PER_SECOND,
                leap_day,
            )
            # :60 保留闰秒标签；其余标签原样
            self.assertTrue(r60.utc.endswith("T23:59:60Z"), r60.utc)
            self.assertEqual(r59.tai_minus_utc + 1, ra.tai_minus_utc)
            self.assertEqual(r60.tai_minus_utc, r59.tai_minus_utc)

    def test_official_offset_table(self):
        for date_text, expected_offset in OFFICIAL_POST_LEAP_OFFSETS:
            r = normalize_utc(f"{date_text}T00:00:00Z")
            self.assertEqual(r.tai_minus_utc, expected_offset, date_text)

    def test_leap_day_has_86401_distinct_labels(self):
        # 每个闰秒日 00:00:00..23:59:60 必须映射到 86401 个连续 TAI 秒
        for leap_day in OFFICIAL_LEAP_DAYS:
            first = normalize_utc(f"{leap_day}T00:00:00Z").tai_nanoseconds
            last = normalize_utc(f"{leap_day}T23:59:60Z").tai_nanoseconds
            span_seconds = (last - first) // NANOS_PER_SECOND
            self.assertEqual(span_seconds, 86400, leap_day)

    def test_full_second_grid_round_trips(self):
        # 以 997 秒为步长扫遍整个支持年代，并在每个闰秒附近加密，
        # TAI->UTC->TAI 必须精确还原，纳秒分量与 TAI 模 1e9 一致。
        probes: list[int] = []
        start = TAI_MIN_NANOSECONDS
        end = TAI_MAX_NANOSECONDS
        step = 997 * NANOS_PER_SECOND
        t = start
        while t <= end:
            probes.append(t)
            t += step
        for leap_day in OFFICIAL_LEAP_DAYS:
            base = normalize_utc(f"{leap_day}T23:59:00Z").tai_nanoseconds
            # 23:59:00 .. 次日 00:00:00（含闰秒 :60）共 61 个整秒；
            # 2016-12-31 的次日午夜正是支持上界，不能再多取。
            for k in range(61):
                probes.append(base + k * NANOS_PER_SECOND)
        probes = sorted(set(probes))

        for tai in probes:
            instant = tai_to_utc(tai)
            back, offset = utc_parts_to_tai(
                instant.year, instant.month, instant.day,
                instant.hour, instant.minute, instant.second,
                instant.nanosecond,
            )
            self.assertIs(type(back), int)
            self.assertEqual(back, tai)
            self.assertEqual(offset, instant.tai_minus_utc)
            self.assertEqual(instant.nanosecond, tai % NANOS_PER_SECOND)
            # 规范字符串再解析一遍也必须落回同一 TAI
            from app.timescales import format_utc, parse_utc
            text = format_utc(instant)
            tai2, _canon, _off = parse_utc(text)
            self.assertEqual(tai2, tai, text)
            self.assertEqual(text, _canon)

    def test_monotonic_labels_near_every_leap(self):
        # 标签顺序（含 :60）映射到严格单调 TAI
        for leap_day in OFFICIAL_LEAP_DAYS:
            labels = [
                f"{leap_day}T23:59:58Z",
                f"{leap_day}T23:59:59Z",
                f"{leap_day}T23:59:60Z",
            ]
            y, m, d = (int(x) for x in leap_day.split("-"))
            ny, nm, nd = civil_from_days(days_from_civil(y, m, d) + 1)
            labels.append(f"{ny:04d}-{nm:02d}-{nd:02d}T00:00:00Z")
            # 2016-12-31 的次日 00:00:01 已越过支持上界，不再取
            if leap_day != "2016-12-31":
                labels.append(f"{ny:04d}-{nm:02d}-{nd:02d}T00:00:01Z")
            tais = [normalize_utc(x).tai_nanoseconds for x in labels]
            for a, b in pairwise(tais):
                self.assertEqual(b - a, NANOS_PER_SECOND)


class GpsUtcConsistencyTests(unittest.TestCase):
    def test_gps_epoch(self):
        # GPS week 0, sow 0 == 1980-01-06T00:00:00Z == TAI 315964819
        tai = gps_to_tai(0, 0, 0)
        self.assertEqual(tai, 315964819 * NANOS_PER_SECOND)
        u = normalize_utc("1980-01-06T00:00:00Z")
        self.assertEqual(tai, u.tai_nanoseconds)

    def test_2016_leap_second_gps_equivalent(self):
        u59 = normalize_utc("2016-12-31T23:59:59Z")
        u60 = normalize_utc("2016-12-31T23:59:60Z")
        u00 = normalize_utc("2017-01-01T00:00:00Z")
        # TAI 315964819 起算的 GPS 周内秒：
        # GPS−UTC=18，故 :59:59->sow16, :59:60->sow17, 00:00:00->sow18
        g59 = gps_to_tai(1930, 16, 0)
        g60 = gps_to_tai(1930, 17, 0)
        g00 = gps_to_tai(1930, 18, 0)
        self.assertEqual(g59, u59.tai_nanoseconds)
        self.assertEqual(g60, u60.tai_nanoseconds)
        self.assertEqual(g00, u00.tai_nanoseconds)
        # GPS 结果的规范 UTC 也保留 :60 标签
        self.assertTrue(
            normalize_gps(1930, 17, 0).utc.endswith("T23:59:60Z")
        )

    def test_mixed_representations_identical_with_nanos(self):
        cases = [
            # (utc, gps week, sow, nanos)
            ("2016-12-31T23:59:60.5Z", 1930, 17, 500_000_000),
            ("2016-12-31T23:59:59.123456789Z", 1930, 16, 123_456_789),
            ("2017-01-01T00:00:00Z", 1930, 18, 0),
            # 1999-01-01T00:00:00Z: 历元起 6935 天 = 990 周 + 5 天（周五），
            # 周内秒 = 5*86400 + 历元以来 13 次闰秒 = 432013
            ("1999-01-01T00:00:00Z", 990, 432_013, 0),
            ("1972-01-01T00:00:00Z", None, None, None),  # GPS 尚未诞生
        ]
        for utc_text, week, sow, nanos in cases:
            u = normalize_utc(utc_text)
            self.assertIsInstance(u.tai_nanoseconds, int)
            if week is not None:
                g = normalize_gps(week, sow, nanos)
                self.assertEqual(g.tai_nanoseconds, u.tai_nanoseconds, utc_text)
                self.assertEqual(g.utc, u.utc, utc_text)
                self.assertEqual(
                    g.tai_minus_utc, u.tai_minus_utc, utc_text
                )

    def test_gps_week_rollover_boundary(self):
        # week 1929 最后一秒 与 week 1930 第一秒相邻
        a = gps_to_tai(1929, 604_799, 0)
        b = gps_to_tai(1930, 0, 0)
        self.assertEqual(b - a, NANOS_PER_SECOND)


class FractionAndCanonicalTests(unittest.TestCase):
    def test_fractional_parsing_exact(self):
        r = normalize_utc("2016-12-31T23:59:60.5Z")
        base = normalize_utc("2016-12-31T23:59:60Z").tai_nanoseconds
        self.assertEqual(r.tai_nanoseconds, base + 500_000_000)
        self.assertEqual(r.utc, "2016-12-31T23:59:60.500000000Z")

        r = normalize_utc("2016-12-31T23:59:60.123456789Z")
        self.assertEqual(r.tai_nanoseconds, base + 123_456_789)
        self.assertEqual(r.utc, "2016-12-31T23:59:60.123456789Z")

        r = normalize_utc("2016-12-31T23:59:60.000000001Z")
        self.assertEqual(r.tai_nanoseconds, base + 1)

    def test_whole_seconds_have_no_fraction(self):
        r = normalize_utc("2016-12-31T23:59:59.000000000Z")
        self.assertEqual(r.utc, "2016-12-31T23:59:59Z")

    def test_nanoseconds_via_gps(self):
        base = gps_to_tai(1929, 0, 0)
        r = normalize_gps(1929, 0, NANOS_MAX)
        self.assertEqual(r.tai_nanoseconds - base, NANOS_MAX)
        self.assertTrue(r.utc.endswith(".999999999Z"))

    def test_output_is_integer_decimal_string_upstream(self):
        r = normalize_utc("2015-06-30T23:59:60Z")
        # 调用方拿到的是 int；HTTP 层负责 str()，二者都不得含小数点/指数
        self.assertEqual(int(str(r.tai_nanoseconds)), r.tai_nanoseconds)


class RejectionTests(unittest.TestCase):
    def assert_rejected(self, text, code=None):
        with self.assertRaises(TimeFormatError) as ctx:
            normalize_utc(text)
        if code is not None:
            self.assertEqual(ctx.exception.code, code, text)

    def test_invalid_calendar_dates(self):
        self.assert_rejected("2013-02-29T00:00:00Z", "utc_invalid_calendar")
        self.assert_rejected("2013-02-29T00:00:60Z", "utc_invalid_calendar")
        self.assert_rejected("2000-13-01T00:00:00Z", "utc_invalid_calendar")
        self.assert_rejected("2000-00-01T00:00:00Z", "utc_invalid_calendar")
        self.assert_rejected("2000-04-31T00:00:00Z", "utc_invalid_calendar")
        self.assert_rejected("1900-02-29T12:00:00Z", "utc_invalid_calendar")

    def test_bad_clock_fields(self):
        self.assert_rejected("2016-12-31T24:00:00Z", "utc_out_of_range")
        self.assert_rejected("2016-12-31T23:60:00Z", "utc_out_of_range")
        self.assert_rejected("2016-12-31T23:59:61Z", "utc_out_of_range")

    def test_syntax_errors(self):
        for text in (
            "2016-12-31T23:59:60",       # 缺 Z
            "2016-12-31 23:59:60Z",      # 缺 T
            "2016-12-31T23:59Z",         # 缺秒
            "20161231T235960Z",          # 缺分隔符
            "2016-12-31T23:59:60+00:00", # 非 Z 结尾
            "2016-12-31T23:59:60.Z",     # 空小数
            "2016-12-31T23:59:60.1234567890Z",  # 超过 9 位
            "",
            "not-a-time",
        ):
            self.assert_rejected(text)

    def test_false_leap_second_positions(self):
        # 闰秒日但错误时刻
        self.assert_rejected(
            "2016-12-31T12:59:60Z", "utc_leap_second_position"
        )
        self.assert_rejected(
            "2016-12-31T23:58:60Z", "utc_leap_second_position"
        )
        # 真实闰秒日的邻日不允许 :60
        self.assert_rejected(
            "2016-12-30T23:59:60Z", "utc_leap_second_position"
        )
        self.assert_rejected(
            "2017-01-01T23:59:60Z", "utc_leap_second_position"
        )
        # 普通年份的 12-31 / 06-30
        self.assert_rejected(
            "2000-12-31T23:59:60Z", "utc_leap_second_position"
        )
        self.assert_rejected(
            "2014-06-30T23:59:60Z", "utc_leap_second_position"
        )
        # 半闰秒（带小数的 :60 标签合法，但日期仍必须真实）
        self.assert_rejected(
            "2011-06-30T23:59:60Z", "utc_leap_second_position"
        )

    def test_unsupported_era_utc(self):
        self.assert_rejected("1971-12-31T23:59:59Z", "unsupported_era")
        self.assert_rejected("1972-01-00T23:59:59Z", "utc_invalid_calendar")
        self.assert_rejected("2017-01-01T00:00:01Z", "unsupported_era")
        self.assert_rejected("2020-01-01T00:00:00Z", "unsupported_era")
        # 边界本身合法
        normalize_utc("1972-01-01T00:00:00Z")
        normalize_utc("2017-01-01T00:00:00Z")

    def test_gps_rejections(self):
        with self.assertRaises(TimeFormatError) as ctx:
            gps_to_tai(1929, 604_800, 0)
        self.assertEqual(ctx.exception.code, "gps_sow_range")
        with self.assertRaises(TimeFormatError) as ctx:
            gps_to_tai(1929, -1, 0)
        self.assertEqual(ctx.exception.code, "gps_sow_range")
        with self.assertRaises(TimeFormatError) as ctx:
            gps_to_tai(-1, 0, 0)
        self.assertEqual(ctx.exception.code, "gps_week_range")
        with self.assertRaises(TimeFormatError) as ctx:
            gps_to_tai(1929, 0, 1_000_000_000)
        self.assertEqual(ctx.exception.code, "gps_nanos_range")
        with self.assertRaises(TimeFormatError) as ctx:
            gps_to_tai(1929, 0, -1)
        self.assertEqual(ctx.exception.code, "gps_nanos_range")

    def test_gps_unsupported_era(self):
        # 远超上界
        with self.assertRaises(TimeFormatError) as ctx:
            gps_to_tai(3000, 0, 0)
        self.assertEqual(ctx.exception.code, "unsupported_era")
        # week 1930 sow 18 恰为 2017-01-01T00:00:00Z（闭区间上界，允许），
        # 1 纳秒后即超出支持年代
        gps_to_tai(1930, 18, 0)
        with self.assertRaises(TimeFormatError) as ctx:
            gps_to_tai(1930, 18, 1)
        self.assertEqual(ctx.exception.code, "unsupported_era")
        # week 1929（2017 闰秒前一周）的起点远在区间内
        gps_to_tai(1929, 0, 0)

    def test_gps_boolean_fields_rejected(self):
        # bool 是 int 子类，必须显式拒绝而不是被当成 0/1
        with self.assertRaises(TimeFormatError) as ctx:
            gps_to_tai(True, 0, 0)  # type: ignore[arg-type]
        self.assertEqual(ctx.exception.code, "gps_field_type")


class NoFloatingPointTests(unittest.TestCase):
    """换算代码不得出现浮点：AST 中禁止 float 字面量、真除与 float 调用。"""

    def scan(self, relative_path: str):
        path = os.path.join(REPO_ROOT, relative_path)
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, float):
                self.fail(f"{relative_path}:{node.lineno} float literal")
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
                self.fail(f"{relative_path}:{node.lineno} true division '/'")
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name) and func.id == "float":
                    self.fail(f"{relative_path}:{node.lineno} float() call")
            if isinstance(node, ast.Name) and node.id in ("inf", "nan"):
                self.fail(f"{relative_path}:{node.lineno} float name")

    def test_timescales_module_has_no_float(self):
        self.scan(os.path.join("app", "timescales.py"))

    def test_server_module_has_no_float(self):
        self.scan(os.path.join("app", "server.py"))

    def test_runtime_values_are_ints(self):
        probes = [
            normalize_utc("2016-12-31T23:59:60.123456789Z"),
            normalize_utc("1972-01-01T00:00:00Z"),
            normalize_gps(1930, 17, 1),
        ]
        for r in probes:
            self.assertIsInstance(r.tai_nanoseconds, int)
            self.assertIsInstance(r.tai_minus_utc, int)
            digits = str(r.tai_nanoseconds)
            self.assertRegex(digits, r"^\d+$")  # 本年代内必为非负


if __name__ == "__main__":
    unittest.main(verbosity=2)
