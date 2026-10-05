# 深空测控时间归一化服务 (Deep-Space Time Normalizer)

把归档事件中的两类时间表示——**GPS 周/周内秒/纳秒** 与**允许秒值 60 的
UTC（闰秒标签）**——归一到同一条精确的 **TAI 整数纳秒**时间轴，避免在
闰秒边界错排遥测与指令。

* 零第三方依赖：仅 Python 3.11 标准库（`http.server`）；
* **全程整数运算，不经过浮点数**（纳秒为最小单位，输出为十进制字符串）；
* 闰秒表冻结于 IERS 公布的 **截至 2016-12-31** 的全部 27 次闰秒；
* 支持年代闭区间：`1972-01-01T00:00:00Z` … `2017-01-01T00:00:00Z`
  （含 2016-12-31 的正闰秒整秒）；
* 任一事件非法即**整批失败**，绝不返回部分结果，错误逐项给出
  事件编号、批内下标、错误码与可定位原因。

## 目录结构

```
app/timescales.py      闰秒表 / GPS·UTC·TAI 纯整数换算核心
app/server.py          POST /api/times/normalize 与 /health
tests/                 unittest 代码测试（43 项）
verify/run_verify.py   一次性 verify 服务（等健康→测试→构建检查→冒烟）
Dockerfile             多阶段：api 运行镜像 + verify 校验镜像
docker-compose.yml     api（可配置宿主端口、健康检查）+ verify
```

## 运行（Docker Compose）

```bash
# 默认宿主机端口 8080；可用 HOST_PORT 覆盖
HOST_PORT=9090 docker compose up --build

# 一次性校验：等待 API 健康后执行 代码测试 + 镜像构建检查 + 跨闰秒冒烟，
# 以退出码报告结论（0 成功）后自行退出
docker compose --profile verify up --build --abort-on-container-exit verify
# 或
docker compose run --rm verify
```

健康检查：`GET /health` → `200 {"status":"ok",...}`（镜像内置
`HEALTHCHECK`，compose 的 `verify` 通过 `depends_on: service_healthy`
等待其通过后才启动）。

## API

`POST /api/times/normalize`，请求体：

```json
{
  "events": [
    {"id": "A001", "utc": "2016-12-31T23:59:60Z"},
    {"id": 7, "gps_week": 1930, "gps_sow": 17, "gps_nanos": 0}
  ]
}
```

* `events` 长度 **1..200**；`id` 必须唯一（字符串或整数）；
* 每个事件**恰好**给出一种表示：
  * UTC：`utc`，形如 `YYYY-MM-DDTHH:MM:SS[.fffffffff]Z`，闰秒日
    （仅表内日期）的 `23:59:60` 合法，小数最多 9 位；
  * GPS：同时给出 `gps_week`（≥0）、`gps_sow`（0..604799）、
    `gps_nanos`（0..999999999）。

成功 `200`（结果保持输入顺序）：

```json
{"results": [
  {"id": "A001",
   "tai_nanoseconds": "1483228836000000000",
   "utc": "2016-12-31T23:59:60Z",
   "utc_minus_tai_offset_seconds": -36,
   "tai_minus_utc_offset_seconds": 36},
  {"id": 7,
   "tai_nanoseconds": "1483228836000000000",
   "utc": "2016-12-31T23:59:60Z",
   "utc_minus_tai_offset_seconds": -36,
   "tai_minus_utc_offset_seconds": 36}
]}
```

* `tai_nanoseconds`：自 `1970-01-01T00:00:00 TAI` 起算的整数纳秒，
  以**十进制字符串**返回；同一时刻的 GPS 与 UTC 输入逐值一致；
* `utc`：规范 UTC（整秒不带小数，闰秒保留 `:60` 标签）；
* `utc_minus_tai_offset_seconds`：需求指定的 **UTC−TAI** 偏移
  （闰秒整秒为 -36，次日 00:00 起为 -37）；
  `tai_minus_utc_offset_seconds` 为其正方向别名，满足
  `TAI = UTC + tai_minus_utc`。

失败 `400`（整批拒绝，无 `results`）：

```json
{"error":"batch_rejected","code":"batch_rejected",
 "message":"2 event(s) failed validation; no events were normalized",
 "errors":[
   {"event_id":"bad","index":1,"code":"utc_leap_second_position",
    "message":"2016-12-30 is not a date carrying a leap second ..."},
   {"event_id":9,"index":2,"code":"gps_sow_range",
    "message":"gps_sow must be within 0..604799; got 700000"}
 ]}
```

### 错误码

| code | 含义 |
|---|---|
| `invalid_request` / `invalid_json` | 请求体不是合法 JSON 对象/`events` 数组 |
| `batch_size` | 事件数不在 1..200 |
| `invalid_event` / `invalid_event_id` / `duplicate_event_id` | 事件结构、编号缺失/类型错/重复 |
| `gps_field_type` / `gps_week_range` / `gps_sow_range` / `gps_nanos_range` | GPS 字段非整数或越界（含周内秒 604800） |
| `utc_syntax` / `utc_fraction_precision` | UTC 形态或小数位数非法 |
| `utc_invalid_calendar` | 非法日期（如 2013-02-29、月份 13） |
| `utc_out_of_range` | 时/分/秒越界 |
| `utc_leap_second_position` | 秒值 60 不在表内闰秒日的 23:59:60 |
| `unsupported_era` | 超出 1972-01-01 .. 2017-01-01 支持年代 |

## 闰秒语义

正闰秒标签 `23:59:60` 物理上占用跳变前那一秒，因此该整秒仍使用
跳变前偏移；下一个 UTC 午夜启用新偏移。以 2016-12-31 为例（Unix 日）：

| UTC 标签 | TAI−UTC | TAI(Unix 秒) |
|---|---|---|
| `2016-12-31T23:59:59Z` | 36 | 1483228835 |
| `2016-12-31T23:59:60Z` | 36 | 1483228836 |
| `2017-01-01T00:00:00Z` | 37 | 1483228837 |

三个标签在 TAI 轴上严格相隔一秒、不重叠不断档；GPS 与 UTC 在这三点
映射到完全相同的 TAI 值（该时刻 GPS 周内秒为 week 1930 的 16/17/18）。

## 本地开发（无需 Docker）

```bash
python3 -m unittest discover -s tests -v     # 代码测试
PORT=8080 python3 -m app.server              # 启动服务
```
