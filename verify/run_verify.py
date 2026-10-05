"""一次性 verify 服务：

1. **等待 API 健康**：轮询 ``$API_BASE/health``，直至 200 或超时；
2. **代码测试**：运行 ``tests/`` 下全部 unittest；
3. **镜像构建检查**：校验构建哨兵 ``IMAGE_BUILD_SHA`` 存在、非空，
   且与当前运行环境中的 app 源码摘要一致 —— 由此确认响应健康检查的
   API 与 verify 同属一个成功构建的镜像；
4. **跨闰秒 HTTP 冒烟**：对 ``POST /api/times/normalize`` 发起多个
   覆盖闰秒边界（重点 2016-12-31，兼及其它历史闰秒）的批次，并断言：
   * GPS 与带 :60 的 UTC 对同一时刻给出**完全一致**的时间值；
   * 23:59:59 → 23:59:60 → 00:00:00 在 TAI 轴上严格每秒一格；
   * 非法日期 / 越界周内秒 / 非真实闰秒 / 超出支持年代 整批失败，
     不返回 partial results，并携带事件编号与原因码。

全部通过则返回退出码 0，任一失败返回 1 并打印分项结论。
"""

from __future__ import annotations

import json
import os
import time
import unittest
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
APP_ROOT = os.path.dirname(HERE)
SENTINEL = os.path.join(APP_ROOT, "IMAGE_BUILD_SHA")
DEFAULT_BASE = "http://api:8080"
WAIT_TIMEOUT_SECONDS = 60
WAIT_INTERVAL_SECONDS = 1

GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
RESET = "\033[0m"


def _stamp(msg: str, color: str = "") -> None:
    print(f"{color}{msg}{RESET}", flush=True)


def _section(title: str) -> None:
    print(f"\n===== {title} =====", flush=True)


# ---------------------------------------------------------------------------
# 1) 等待 API 健康
# ---------------------------------------------------------------------------
def wait_for_health(base_url: str) -> bool:
    deadline = time.monotonic() + WAIT_TIMEOUT_SECONDS
    attempt = 0
    last_error = ""
    while time.monotonic() < deadline:
        attempt += 1
        try:
            with urllib.request.urlopen(
                f"{base_url}/health", timeout=3
            ) as resp:
                if resp.status == 200:
                    body = json.loads(resp.read().decode("utf-8"))
                    if body.get("status") == "ok":
                        _stamp(
                            f"[PASS] API healthy after {attempt} attempt(s): "
                            f"{body}",
                            GREEN,
                        )
                        return True
                    last_error = f"unexpected health body: {body}"
                else:
                    last_error = f"status {resp.status}"
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            last_error = str(exc)
        print(f"  ...waiting for API ({attempt}) {last_error}", flush=True)
        time.sleep(WAIT_INTERVAL_SECONDS)
    _stamp(f"[FAIL] API did not become healthy within {WAIT_TIMEOUT_SECONDS}s", RED)
    return False


# ---------------------------------------------------------------------------
# 2) 代码测试
# ---------------------------------------------------------------------------
def run_unit_tests() -> bool:
    _section("2) CODE TESTS (unittest)")
    loader = unittest.TestLoader()
    suite = loader.discover(
        start_dir=os.path.join(APP_ROOT, "tests"),
        pattern="test_*.py",
        top_level_dir=APP_ROOT,
        # discover 需要能 import app 与 verify
    )
    runner = unittest.TextTestRunner(verbosity=1)
    result = runner.run(suite)
    if result.wasSuccessful():
        _stamp("[PASS] all unit tests passed", GREEN)
        return True
    _stamp("[FAIL] unit tests failed", RED)
    return False


# ---------------------------------------------------------------------------
# 3) 镜像构建检查
# ---------------------------------------------------------------------------
def _source_sha() -> str:
    """与 Dockerfile 中相同规则：app/**/*.py 路径排序后串联 sha256。"""
    import hashlib as _hl

    paths = []
    app_dir = os.path.join(APP_ROOT, "app")
    for dirpath, _dirs, files in os.walk(app_dir):
        for name in files:
            if name.endswith(".py"):
                paths.append(os.path.join(dirpath, name))
    paths.sort()
    outer = _hl.sha256()
    for path in paths:
        rel = os.path.relpath(path, APP_ROOT)
        h = _hl.sha256()
        with open(path, "rb") as fh:
            h.update(fh.read())
        outer.update(f"{h.hexdigest()}  {rel}\n".encode("utf-8"))
    return outer.hexdigest()


def image_build_check() -> bool:
    _section("3) IMAGE BUILD CHECK")
    if not os.path.exists(SENTINEL):
        _stamp(
            "[FAIL] IMAGE_BUILD_SHA sentinel missing; container was not "
            "produced by the expected multi-stage Docker build",
            RED,
        )
        return False
    with open(SENTINEL, encoding="ascii") as fh:
        recorded = fh.read().strip()
    if not recorded:
        _stamp("[FAIL] IMAGE_BUILD_SHA is empty", RED)
        return False
    actual = _source_sha()
    print(f"  recorded sha256(app/**.py) = {recorded}")
    print(f"  actual   sha256(app/**.py) = {actual}")
    if recorded != actual:
        _stamp(
            "[FAIL] source fingerprint mismatch: the healthy API is not "
            "running from this image build",
            RED,
        )
        return False
    _stamp("[PASS] API image build verified (sentinel matches source)", GREEN)
    return True


# ---------------------------------------------------------------------------
# 4) 跨闰秒 HTTP 冒烟
# ---------------------------------------------------------------------------
def _post(base_url: str, events):
    data = json.dumps({"events": events}).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/api/times/normalize",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def smoke_across_leap_seconds(base_url: str) -> bool:
    _section("4) CROSS-LEAP-SECOND HTTP SMOKE")
    ok = True

    # --- 4.1 2016 闰秒：GPS 与 UTC 混合批次 -------------------------------
    events = [
        {"id": "before-leap-gps", "gps_week": 1930, "gps_sow": 16,
         "gps_nanos": 0},
        {"id": "before-leap-utc", "utc": "2016-12-31T23:59:59Z"},
        {"id": "leap-gps", "gps_week": 1930, "gps_sow": 17,
         "gps_nanos": 0},
        {"id": "leap-utc", "utc": "2016-12-31T23:59:60Z"},
        {"id": "after-leap-gps", "gps_week": 1930, "gps_sow": 18,
         "gps_nanos": 0},
        {"id": "after-leap-utc", "utc": "2017-01-01T00:00:00Z"},
        # 亚秒一致性
        {"id": "leap-half-gps", "gps_week": 1930, "gps_sow": 17,
         "gps_nanos": 500_000_000},
        {"id": "leap-half-utc", "utc": "2016-12-31T23:59:60.5Z"},
    ]
    status, body = _post(base_url, events)
    if status != 200:
        _stamp(f"[FAIL] valid cross-leap batch got HTTP {status}: {body}", RED)
        return False
    by_id = {r["id"]: r for r in body["results"]}

    def value_tuple(item_id):
        r = by_id[item_id]
        return (
            r["tai_nanoseconds"],
            r["utc"],
            r["utc_minus_tai_offset_seconds"],
            r["tai_minus_utc_offset_seconds"],
        )

    def tai(item_id):
        return by_id[item_id]["tai_nanoseconds"]

    # 输入顺序保持
    expected_order = [e["id"] for e in events]
    if [r["id"] for r in body["results"]] != expected_order:
        _stamp("[FAIL] result order does not match input order", RED)
        ok = False
    # 同一时刻两种表示的时间值必须完全一致（id 允许不同）
    for pair in (
        ("before-leap-gps", "before-leap-utc"),
        ("leap-gps", "leap-utc"),
        ("after-leap-gps", "after-leap-utc"),
        ("leap-half-gps", "leap-half-utc"),
    ):
        a, b = pair
        if value_tuple(a) != value_tuple(b):
            _stamp(f"[FAIL] mismatched time values for {a} vs {b}: "
                   f"{value_tuple(a)} vs {value_tuple(b)}", RED)
            ok = False
    # TAI 轴严格每秒一格（59 -> 60 -> 次日 00:00）
    t59, t60, t00 = tai("before-leap-utc"), tai("leap-utc"), tai("after-leap-utc")
    if int(t60) - int(t59) != 1_000_000_000:
        _stamp("[FAIL] :59->:60 not exactly one TAI second", RED)
        ok = False
    if int(t00) - int(t60) != 1_000_000_000:
        _stamp("[FAIL] :60->00:00 not exactly one TAI second", RED)
        ok = False
    # 偏移：闰秒整秒 TAI−UTC=36（UTC−TAI=-36），午夜后 37 / -37
    if by_id["leap-utc"]["tai_minus_utc_offset_seconds"] != 36:
        ok = False
    if by_id["leap-utc"]["utc_minus_tai_offset_seconds"] != -36:
        ok = False
    if by_id["after-leap-utc"]["tai_minus_utc_offset_seconds"] != 37:
        ok = False
    if by_id["after-leap-utc"]["utc_minus_tai_offset_seconds"] != -37:
        ok = False
    if not ok:
        _stamp("[FAIL] cross-leap timing assertions failed", RED)
        return False
    _stamp("[PASS] 2016 leap boundary: GPS/UTC identical, TAI strictly "
           "1s-spaced, offsets 36->37", GREEN)

    # --- 4.2 历史闰秒抽样 -------------------------------------------------
    historical = ["1972-06-30", "2008-12-31", "2015-06-30"]
    hist_events = []
    for day in historical:
        hist_events.extend([
            {"id": f"{day}-59", "utc": f"{day}T23:59:59Z"},
            {"id": f"{day}-60", "utc": f"{day}T23:59:60Z"},
        ])
    status, body = _post(base_url, hist_events)
    if status != 200:
        _stamp(f"[FAIL] historical leap batch got HTTP {status}: {body}", RED)
        return False
    hb = {r["id"]: r for r in body["results"]}
    for day in historical:
        a = int(hb[f"{day}-59"]["tai_nanoseconds"])
        b = int(hb[f"{day}-60"]["tai_nanoseconds"])
        if b - a != 1_000_000_000:
            _stamp(f"[FAIL] {day} :59->:60 gap != 1s", RED)
            ok = False
        if not hb[f"{day}-60"]["utc"].endswith("T23:59:60Z"):
            _stamp(f"[FAIL] {day} :60 label not canonical", RED)
            ok = False
    if ok:
        _stamp("[PASS] historical leap days (1972/2008/2015) continuous", GREEN)

    # --- 4.3 整批失败，无部分结果 -----------------------------------------
    bad_events = [
        {"id": "good", "utc": "2016-12-31T23:59:60Z"},
        {"id": "bad-calendar", "utc": "2013-02-29T00:00:00Z"},
        {"id": "bad-sow", "gps_week": 1930, "gps_sow": 604800,
         "gps_nanos": 0},
        {"id": "fake-leap", "utc": "2016-12-30T23:59:60Z"},
        {"id": "too-new", "utc": "2020-01-01T00:00:00Z"},
    ]
    status, body = _post(base_url, bad_events)
    if status != 400:
        _stamp(f"[FAIL] invalid batch expected 400 got {status}", RED)
        ok = False
    elif "results" in body:
        _stamp("[FAIL] partial results returned for invalid batch", RED)
        ok = False
    else:
        codes = {e["event_id"]: e["code"] for e in body.get("errors", [])}
        expected_codes = {
            "bad-calendar": "utc_invalid_calendar",
            "bad-sow": "gps_sow_range",
            "fake-leap": "utc_leap_second_position",
            "too-new": "unsupported_era",
        }
        for event_id, code in expected_codes.items():
            if codes.get(event_id) != code:
                _stamp(f"[FAIL] {event_id}: expected {code}, got {codes.get(event_id)}", RED)
                ok = False
        good_still_not_returned = all(
            e["event_id"] != "good" for e in body.get("errors", [])
        )
        if not good_still_not_returned:
            _stamp("[FAIL] valid event mis-reported among errors", RED)
            ok = False
    if ok:
        _stamp("[PASS] invalid events reject whole batch, no partial results", GREEN)

    # --- 4.4 边界：批次大小与编号唯一 -------------------------------------
    status_big, body_big = _post(
        base_url,
        [{"id": i, "gps_week": 1000, "gps_sow": 0, "gps_nanos": 0}
         for i in range(201)],
    )
    if status_big != 400 or body_big.get("code") != "batch_size":
        _stamp(f"[FAIL] 201-event batch not rejected: {status_big} {body_big}", RED)
        ok = False
    status_dup, body_dup = _post(
        base_url,
        [
            {"id": "x", "utc": "2016-12-31T23:59:59Z"},
            {"id": "x", "utc": "2016-12-31T23:59:60Z"},
        ],
    )
    if status_dup != 400:
        _stamp(f"[FAIL] duplicate id batch not rejected: {status_dup}", RED)
        ok = False
    if ok:
        _stamp("[PASS] batch-size and duplicate-id boundaries enforced", GREEN)

    return ok


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> int:
    base_url = os.environ.get("API_BASE", DEFAULT_BASE).rstrip("/")
    _stamp(f"{YELLOW}Deep-space time normalizer — one-shot verification{RESET}")
    print(f"target API: {base_url}", flush=True)

    results: list[tuple[str, bool]] = []

    healthy = wait_for_health(base_url)
    results.append(("wait-for-health", healthy))
    if not healthy:
        return _summary(results)

    # 注意：健康检查通过后才执行其余检查。
    results.append(("code-tests", run_unit_tests()))
    results.append(("image-build-check", image_build_check()))
    results.append(("cross-leap-smoke", smoke_across_leap_seconds(base_url)))

    return _summary(results)


def _summary(results: list[tuple[str, bool]]) -> int:
    _section("SUMMARY")
    all_passed = all(passed for _name, passed in results)
    for name, passed in results:
        _stamp(f"  [{'PASS' if passed else 'FAIL'}] {name}",
               GREEN if passed else RED)
    if all_passed:
        _stamp("\nVERIFY RESULT: PASS (exit 0)", GREEN)
        return 0
    _stamp("\nVERIFY RESULT: FAIL (exit 1)", RED)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
