"""POST /api/times/normalize —— 基于标准库的零依赖 HTTP 服务。

请求体（JSON）：

    {
      "events": [
        {"id": "A001", "utc": "2016-12-31T23:59:60Z"},
        {"id": 7, "gps_week": 1929, "gps_sow": 181472, "gps_nanos": 0}
      ]
    }

成功响应 200：

    {"results": [
      {"id": "A001",
       "tai_nanoseconds": "1483228837000000000",
       "utc": "2016-12-31T23:59:60Z",
       "utc_minus_tai_offset_seconds": -36,
       "tai_minus_utc_offset_seconds": 36},
      ...
    ]}

其中 ``utc_minus_tai_offset_seconds`` 即需求指定的 UTC−TAI 偏移
（2017 年前后为 -36 / -37）；``tai_minus_utc_offset_seconds`` 是其
正方向别名，便于直接做 ``TAI = UTC + offset`` 的整数加法。两者都为
整数，绝无浮点。

任一事件非法则整个批次失败（400），绝不返回部分结果，``errors``
逐项给出事件编号、批内下标与可定位原因：

    {"error": "batch_rejected", "code": "batch_rejected",
     "message": "1 event(s) failed validation; no events were normalized",
     "errors": [{"event_id": 9, "index": 3, "code": "gps_sow_range",
                 "message": "..."}]}
"""

from __future__ import annotations

import json
import logging
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .timescales import NormalizedEvent, TimeFormatError, normalize_gps, normalize_utc

BATCH_MIN = 1
BATCH_MAX = 200
MAX_BODY_BYTES = 1_000_000
HEALTH_PATH = "/health"
NORMALIZE_PATH = "/api/times/normalize"
SERVICE_NAME = "deep-space-time-normalizer"

logger = logging.getLogger("time-normalizer")


class BatchError(Exception):
    """批次被拒绝。errors 非空时为逐事件校验失败明细。"""

    def __init__(
        self,
        code: str,
        message: str,
        status: int = 400,
        errors: list[dict[str, Any]] | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.errors = errors


def _safe_id(raw: Any) -> Any:
    """尽力取回事件编号用于错误定位；取不到返回 None。"""
    if isinstance(raw, dict):
        value = raw.get("id")
        if isinstance(value, (str, int)) and not isinstance(value, bool):
            return value
    return None


def _event_id_key(event_id: Any) -> str:
    """事件编号唯一性键：数字按数值、字符串按原样（区分类型）。"""
    if isinstance(event_id, bool):
        return "bool:" + ("1" if event_id else "0")
    if isinstance(event_id, int):
        return "int:" + str(event_id)
    if isinstance(event_id, str):
        if not event_id.strip():
            raise BatchError(
                "invalid_event_id", "event id must not be blank"
            )
        if len(event_id) > 128:
            raise BatchError(
                "invalid_event_id", "event id longer than 128 characters"
            )
        return "str:" + event_id
    raise BatchError(
        "invalid_event_id",
        f"event id must be a string or integer; "
        f"got {type(event_id).__name__}",
    )


def _coerce_int(value: Any, field: str) -> int:
    """GPS 字段接受 JSON 整数，或纯整数字符串；拒绝布尔/小数/对象。"""
    if isinstance(value, bool):
        raise BatchError(
            "gps_field_type", f"{field} must be an integer; got boolean"
        )
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text.lstrip("-").isdigit():
            return int(text)
    raise BatchError(
        "gps_field_type",
        f"{field} must be an integer; got {type(value).__name__}",
    )


def _result_to_dict(event_id: Any, result: NormalizedEvent) -> dict[str, Any]:
    # 换算内部使用 TAI−UTC（正数，TAI = UTC + 它）；需求字面要求
    # 返回 UTC−TAI，故取负。两者同时给出，均为整数。
    tai_minus_utc = result.tai_minus_utc
    return {
        "id": event_id,
        # 十进制字符串，直接来自 int，未经浮点数。
        "tai_nanoseconds": str(result.tai_nanoseconds),
        "utc": result.utc,
        "utc_minus_tai_offset_seconds": -tai_minus_utc,
        "tai_minus_utc_offset_seconds": tai_minus_utc,
    }


def normalize_batch(payload: Any) -> dict[str, Any]:
    """校验并归一化整批事件；任何非法事件都整批拒绝。"""
    if not isinstance(payload, dict):
        raise BatchError(
            "invalid_request",
            "request body must be a JSON object with an 'events' array",
        )

    events = payload.get("events")
    if not isinstance(events, list):
        raise BatchError(
            "invalid_request",
            "'events' must be an array of 1..200 event objects",
        )
    if not (BATCH_MIN <= len(events) <= BATCH_MAX):
        raise BatchError(
            "batch_size",
            f"batch must contain {BATCH_MIN}..{BATCH_MAX} events; "
            f"got {len(events)}",
        )

    normalized: list[tuple[Any, NormalizedEvent]] = []
    errors: list[dict[str, Any]] = []
    seen_ids: dict[str, int] = {}

    for index, raw in enumerate(events):
        event_id: Any = None
        try:
            if not isinstance(raw, dict):
                raise BatchError(
                    "invalid_event", f"event at index {index} must be an object"
                )
            if "id" not in raw:
                raise BatchError(
                    "invalid_event_id",
                    f"event at index {index} is missing 'id'",
                )
            event_id = raw["id"]
            id_key = _event_id_key(event_id)
            if id_key in seen_ids:
                raise BatchError(
                    "duplicate_event_id",
                    f"duplicate event id {event_id!r}; "
                    f"first used at index {seen_ids[id_key]}",
                )
            seen_ids[id_key] = index

            has_gps = any(
                f in raw for f in ("gps_week", "gps_sow", "gps_nanos")
            )
            has_utc = "utc" in raw
            if has_gps == has_utc:
                raise BatchError(
                    "invalid_event",
                    "event must provide exactly one time representation: "
                    "either 'utc' or all of gps_week/gps_sow/gps_nanos",
                )

            if has_utc:
                result = normalize_utc(raw["utc"])
            else:
                missing = [
                    f
                    for f in ("gps_week", "gps_sow", "gps_nanos")
                    if f not in raw
                ]
                if missing:
                    raise BatchError(
                        "invalid_event",
                        "GPS event is missing field(s): " + ", ".join(missing),
                    )
                week = _coerce_int(raw["gps_week"], "gps_week")
                sow = _coerce_int(raw["gps_sow"], "gps_sow")
                nanos = _coerce_int(raw["gps_nanos"], "gps_nanos")
                result = normalize_gps(week, sow, nanos)
        except (BatchError, TimeFormatError) as exc:
            errors.append(
                {
                    "event_id": _safe_id(raw),
                    "index": index,
                    "code": exc.code,
                    "message": exc.message,
                }
            )
        else:
            normalized.append((event_id, result))

    if errors:
        # 整批失败：不返回任何 results。
        raise BatchError(
            "batch_rejected",
            f"{len(errors)} event(s) failed validation; "
            "no events were normalized",
            errors=errors,
        )

    return {
        "results": [
            _result_to_dict(event_id, result) for event_id, result in normalized
        ]
    }


class TimeNormalizerHandler(BaseHTTPRequestHandler):
    server_version = "DeepSpaceTime/1.0"

    def do_GET(self) -> None:
        if self.path.split("?")[0] == HEALTH_PATH:
            self._write_json(200, {"status": "ok", "service": SERVICE_NAME})
        else:
            self._write_json(404, {"error": "not_found", "code": "not_found"})

    def do_POST(self) -> None:
        if self.path.split("?")[0] != NORMALIZE_PATH:
            self._write_json(404, {"error": "not_found", "code": "not_found"})
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            self._write_json(
                400,
                {
                    "error": "batch_rejected",
                    "code": "invalid_request",
                    "message": "empty request body",
                },
            )
            return
        if length > MAX_BODY_BYTES:
            self._write_json(
                413, {"error": "payload_too_large", "code": "payload_too_large"}
            )
            return

        raw_body = self.rfile.read(length)
        try:
            payload = json.loads(raw_body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._write_json(
                400,
                {
                    "error": "batch_rejected",
                    "code": "invalid_json",
                    "message": "request body is not valid UTF-8 JSON",
                },
            )
            return

        try:
            response = normalize_batch(payload)
        except BatchError as exc:
            body: dict[str, Any] = {
                "error": "batch_rejected",
                "code": exc.code,
                "message": exc.message,
            }
            if exc.errors:
                body["errors"] = exc.errors
            self._write_json(exc.status, body)
            return

        self._write_json(200, response)

    def _write_json(self, status: int, body: dict[str, Any]) -> None:
        data = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode(
            "utf-8"
        )
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt: str, *args: Any) -> None:
        logger.info("%s - %s", self.address_string(), fmt % args)


def main() -> int:
    port = int(os.environ.get("PORT", "8080"))
    host = os.environ.get("HOST", "0.0.0.0")
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )
    server = ThreadingHTTPServer((host, port), TimeNormalizerHandler)
    logger.info("time normalizer listening on %s:%d", host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
