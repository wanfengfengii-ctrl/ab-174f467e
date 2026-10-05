"""批处理/HTTP 层测试：顺序保持、整批失败语义、编号与数量校验。"""

from __future__ import annotations

import unittest

from app.server import BatchError, normalize_batch


def call(events):
    return normalize_batch({"events": events})


def expect_batch_error(payload):
    with unittest.TestCase().assertRaises(BatchError) as ctx:
        normalize_batch(payload)
    return ctx.exception


class BatchHappyPathTests(unittest.TestCase):
    def test_order_preserved_mixed_inputs(self):
        events = [
            {"id": "first", "utc": "2016-12-31T23:59:60Z"},
            {"id": 2, "gps_week": 1930, "gps_sow": 17, "gps_nanos": 0},
            {"id": "third", "utc": "1972-01-01T00:00:00Z"},
            {"id": 4, "gps_week": 0, "gps_sow": 0, "gps_nanos": 0},
        ]
        out = call(events)["results"]
        self.assertEqual([r["id"] for r in out], ["first", 2, "third", 4])
        # 同一时刻两种表示必须完全一致
        self.assertEqual(out[0]["tai_nanoseconds"], out[1]["tai_nanoseconds"])
        self.assertEqual(out[0]["utc"], out[1]["utc"])
        self.assertEqual(out[0]["tai_minus_utc_offset_seconds"],
                         out[1]["tai_minus_utc_offset_seconds"])
        # UTC−TAI 为负方向整数偏移，且与正方向字段互为相反数
        self.assertEqual(out[0]["utc_minus_tai_offset_seconds"], -36)
        self.assertEqual(
            out[0]["utc_minus_tai_offset_seconds"],
            -out[0]["tai_minus_utc_offset_seconds"],
        )

    def test_tai_is_decimal_string(self):
        out = call([{"id": "x", "utc": "2016-12-31T23:59:60Z"}])["results"][0]
        text = out["tai_nanoseconds"]
        self.assertIsInstance(text, str)
        self.assertRegex(text, r"^\d+$")
        self.assertEqual(text, "1483228836000000000")
        self.assertEqual(int(text), 1483228836000000000)
        # 不得出现浮点/指数写法
        self.assertNotIn(".", text)
        self.assertNotIn("e", text)
        self.assertNotIn("E", text)

    def test_batch_of_200_ok(self):
        events = [
            {"id": i, "gps_week": 1000, "gps_sow": i, "gps_nanos": 0}
            for i in range(200)
        ]
        out = call(events)["results"]
        self.assertEqual(len(out), 200)
        self.assertEqual([r["id"] for r in out], list(range(200)))

    def test_string_integer_gps_fields_accepted(self):
        out = call(
            [{"id": "a", "gps_week": "1930", "gps_sow": "17", "gps_nanos": "0"}]
        )["results"]
        self.assertEqual(out[0]["tai_nanoseconds"], "1483228836000000000")


class BatchRejectionTests(unittest.TestCase):
    def test_invalid_date_fails_whole_batch_with_id(self):
        events = [
            {"id": "ok-1", "utc": "2016-12-31T23:59:59Z"},
            {"id": "bad-date", "utc": "2013-02-29T12:00:00Z"},
            {"id": "ok-2", "utc": "2017-01-01T00:00:00Z"},
        ]
        exc = expect_batch_error({"events": events})
        self.assertEqual(exc.status, 400)
        self.assertEqual(len(exc.errors), 1)
        err = exc.errors[0]
        self.assertEqual(err["event_id"], "bad-date")
        self.assertEqual(err["index"], 1)
        self.assertEqual(err["code"], "utc_invalid_calendar")
        self.assertIn("2013-02-29", err["message"])

    def test_gps_sow_out_of_range(self):
        events = [
            {"id": 5, "gps_week": 1930, "gps_sow": 604800, "gps_nanos": 0},
        ]
        exc = expect_batch_error({"events": events})
        err = exc.errors[0]
        self.assertEqual(err["event_id"], 5)
        self.assertEqual(err["code"], "gps_sow_range")
        self.assertIn("604800", err["message"])

    def test_false_leap_position(self):
        events = [
            {"id": "L", "utc": "2000-12-31T23:59:60Z"},
        ]
        exc = expect_batch_error({"events": events})
        self.assertEqual(exc.errors[0]["code"], "utc_leap_second_position")
        self.assertEqual(exc.errors[0]["event_id"], "L")

    def test_unsupported_era(self):
        for event in (
            {"id": "future", "utc": "2020-01-01T00:00:00Z"},
            {"id": "past", "utc": "1970-01-01T00:00:00Z"},
            {"id": "gfuture", "gps_week": 3000, "gps_sow": 0, "gps_nanos": 0},
        ):
            exc = expect_batch_error({"events": [event]})
            self.assertEqual(
                exc.errors[0]["code"], "unsupported_era", event["id"]
            )

    def test_no_partial_results_key(self):
        # 整批失败：异常承载错误明细；成功出口的结构只含 results
        events = [
            {"id": "ok", "utc": "2016-12-31T23:59:60Z"},
            {"id": "bad", "utc": "not-a-time"},
        ]
        exc = expect_batch_error({"events": events})
        self.assertEqual(len(exc.errors), 1)
        self.assertTrue(exc.message.endswith("no events were normalized"))

    def test_multiple_errors_all_reported(self):
        events = [
            {"id": "a", "utc": "2013-02-29T00:00:00Z"},
            {"id": "b", "gps_week": 1, "gps_sow": 999999, "gps_nanos": 0},
            {"id": "c", "utc": "2000-12-31T23:59:60Z"},
        ]
        exc = expect_batch_error({"events": events})
        self.assertEqual(
            [e["event_id"] for e in exc.errors], ["a", "b", "c"]
        )
        self.assertEqual(
            [e["code"] for e in exc.errors],
            [
                "utc_invalid_calendar",
                "gps_sow_range",
                "utc_leap_second_position",
            ],
        )

    def test_duplicate_ids_rejected(self):
        events = [
            {"id": "dup", "utc": "2016-12-31T23:59:59Z"},
            {"id": "dup", "utc": "2017-01-01T00:00:00Z"},
        ]
        exc = expect_batch_error({"events": events})
        self.assertEqual(exc.errors[0]["code"], "duplicate_event_id")
        # 数字 1 与字符串 "1" 视为不同编号（类型不同）
        ok = call(
            [
                {"id": 1, "utc": "2016-12-31T23:59:59Z"},
                {"id": "1", "utc": "2017-01-01T00:00:00Z"},
            ]
        )
        self.assertEqual(len(ok["results"]), 2)

    def test_batch_size_limits(self):
        exc = expect_batch_error({"events": []})
        self.assertEqual(exc.code, "batch_size")
        exc = expect_batch_error(
            {"events": [{"id": i, "utc": "2016-12-31T23:59:59Z"}
                        for i in range(201)]}
        )
        self.assertEqual(exc.code, "batch_size")

    def test_malformed_structures(self):
        self.assertEqual(
            expect_batch_error([1, 2]).code, "invalid_request"
        )
        self.assertEqual(
            expect_batch_error({"events": "x"}).code, "invalid_request"
        )
        # 事件不是对象
        exc = expect_batch_error({"events": [42]})
        self.assertEqual(exc.errors[0]["code"], "invalid_event")
        # 缺 id
        exc = expect_batch_error({"events": [{"utc": "2016-12-31T23:59:59Z"}]})
        self.assertEqual(exc.errors[0]["code"], "invalid_event_id")
        # id 类型非法
        exc = expect_batch_error(
            {"events": [{"id": [1], "utc": "2016-12-31T23:59:59Z"}]}
        )
        self.assertEqual(exc.errors[0]["code"], "invalid_event_id")
        # 同时给出两种表示 / 都不给
        exc = expect_batch_error(
            {"events": [{"id": "x", "utc": "2016-12-31T23:59:59Z",
                         "gps_week": 1, "gps_sow": 1, "gps_nanos": 0}]}
        )
        self.assertEqual(exc.errors[0]["code"], "invalid_event")
        exc = expect_batch_error({"events": [{"id": "x"}]})
        self.assertEqual(exc.errors[0]["code"], "invalid_event")
        # GPS 缺字段
        exc = expect_batch_error(
            {"events": [{"id": "x", "gps_week": 1, "gps_sow": 1}]}
        )
        self.assertEqual(exc.errors[0]["code"], "invalid_event")
        self.assertIn("gps_nanos", exc.errors[0]["message"])
        # 非整数 GPS 字段
        exc = expect_batch_error(
            {"events": [{"id": "x", "gps_week": 1.5,
                         "gps_sow": 1, "gps_nanos": 0}]}
        )
        self.assertEqual(exc.errors[0]["code"], "gps_field_type")

    def test_error_locates_index_even_without_valid_id(self):
        exc = expect_batch_error(
            {"events": [
                {"id": "ok", "utc": "2016-12-31T23:59:59Z"},
                {"utc": "2016-12-31T23:59:59Z"},  # 无 id
                "not-an-object",
            ]}
        )
        self.assertEqual([e["index"] for e in exc.errors], [1, 2])


if __name__ == "__main__":
    unittest.main(verbosity=2)
