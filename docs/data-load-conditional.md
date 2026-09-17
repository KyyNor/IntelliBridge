# Load API：conditional load 与 source_version（issue #3）

Load API（`tools/load_export.py`）在原有声明式导出之上增加 **条件加载**：调用方带上
已知版本，IB 在**执行数据扫描之前**比较当前版本，未变化的逻辑日期不执行 SELECT、
不写 Parquet、不产生 spool 文件。

source_version 只做 freshness 判断：

- 不作为物理分区身份（分区身份永远是 `logical_date`）；
- 不参与授权判断；
- 对调用方是 opaque string（Taosha 只做相等比较，不解释格式）。

## 请求

```json
POST /api/load/hive
{
  "database": "db8",
  "table": "balance",
  "dates": ["2026-09-16", "2026-09-17"],
  "known_versions": {
    "2026-09-16": "1789529400",
    "2026-09-17": "1789615800"
  }
}
```

- `known_versions` 的 key 是 **logical date**，绝不出现 `etl_date` / `cdate`：
  物理分区字段仍完全封装在 IB 内部。
- full_table（无时间分区表）用表级 `known_version`（不是 `known_versions`）；
  两者用错会明确失败，而不是被静默忽略。
- 所有 key 必须是本次 `dates` 里的日期，值不能为空——形状错误在提交时即拒绝。
- 不传 `known_versions` / `known_version`：行为与 issue #1 完全一致（正常导出）。

## 返回

ready 结果里每个逻辑日期都有明确的判定：

```json
{
  "status": "ready",
  "load_mode": "time_partitioned",
  "requested_dates": ["2026-09-16", "2026-09-17"],
  "actual_dates": ["2026-09-17"],
  "partitions": [
    {"logical_date": "2026-09-16", "status": "not_modified", "source_version": "1789529400", "file_id": null},
    {"logical_date": "2026-09-17", "status": "modified", "source_version": "1789619400", "file_id": "file_1"}
  ],
  "version_check": {"requested": true, "not_modified": 1, "modified": 1, "unknown": 0},
  "files": [
    {"id": "file_1", "name": "balance__2026-09-17.parquet", "logical_date": "2026-09-17",
     "format": "parquet", "size": 123, "sha256": "…", "download_url": "/api/load/…/files/file_1"}
  ]
}
```

| 字段 | 含义 |
| --- | --- |
| `partitions[].status` | `not_modified` / `modified` / `unknown` |
| `partitions[].source_version` | 当前版本（读不到为 `null`）——调用方据此建立/刷新缓存 |
| `partitions[].file_id` | 只有 `modified` 的分区有文件，`not_modified` 一定是 `null` |
| `actual_dates` | 真正产出了文件（= 执行了导出）的逻辑日期 |
| `version_check` | 判定汇总；`requested=false` 表示本次没做比较 |
| `source_version` / `version_source` / `version_status` | full_table 的表级版本、来源与判定 |

**判定方向是刻意的**（`resolve_version_status`）：

```
known 缺失            → modified（未要求比较，照常导出）
current 读不到        → unknown（照常导出，绝不伪 not_modified）
known == current      → not_modified（不扫描、不产文件）
known != current      → modified
```

假 `not_modified` 会让调用方一直使用陈旧数据；假 `modified` 只是多导出一次。
因此只有「双方都有版本且相等」才允许 `not_modified`。

## 版本从哪里读

只读元数据，不扫描数据、不做文件 hash：

| 场景 | SQL | 版本位置 |
| --- | --- | --- |
| time_partitioned | `DESCRIBE FORMATTED db.t PARTITION (phys='date')` | `Partition Parameters.transient_lastDdlTime` |
| full_table | `DESCRIBE FORMATTED db.t` | `Table Parameters` / `Table Properties.transient_lastDdlTime` |

解析（`utils/source_version.py`）同时兼容两种服务端输出形状：

```
Spark: ('Partition Parameters', '{transient_lastDdlTime=…, totalSize=…, numFiles=…}', '')
Hive:  ('Table Parameters:', '', '') 后跟 ('transient_lastDdlTime', '1789615800', '')
```

分区级版本**只认分区参数**：表级属性对所有分区相同，拿它当分区版本会让所有分区
一起「看起来没变」，是危险方向。读不到 → `unknown`。

执行顺序（`_run_hive`）：resolve profile → 解析物理分区字段 → 校验分区存在 →
读当前版本 → 比较 → 只对需要导出的日期执行 SELECT + ParquetWriter。

版本探测失败（会话不可用、元数据缺失）**不会让 load 失败**：记 warning 后退化为
普通导出，但状态是 `unknown` 而不是 `not_modified`。

## 实测限制（重要）

在真实 Spark 3.1.3 ThriftServer + Hive-serde 分区表上实测（`INSERT OVERWRITE` 前后
读取分区元数据）：

| 操作 | 分区 `transient_lastDdlTime` | 数据 |
| --- | --- | --- |
| 新建分区 | 写入时刻 | 已写入 |
| `INSERT OVERWRITE` 覆盖已存在分区 | **不变** | **已替换** |
| 同一分区连续 overwrite（1 行 → 20 万行） | **不变**（`totalSize`/`numFiles` 也不变） | 已替换 |
| `DROP PARTITION` 后再写入 | 变化 | 已替换 |

也就是说：**Spark 3.1.3 不会在 overwrite 时刷新分区元数据**，元数据里的
`totalSize` / `numFiles` 同样是分区创建时算的、之后不再更新。因此

- ETL 采用「先 DROP 分区再写」的模式时，conditional load 判断是准确的；
- ETL 采用「原地 INSERT OVERWRITE」时，IB 会返回 `not_modified`，而数据其实已经变了。

这是**数据新鲜度机制的上限，不是契约缺陷**。使用方（Taosha #17 的 Shared Dataset
Cache）必须把 source_version 当作「TTL 到期后的一次额外确认」而不是永久保证：

- TTL 仍是新鲜度的兜底上界；
- 引擎不刷新该元数据时，可关掉条件校验（每次 TTL 到期硬 reload），
  或把 TTL 调到与 ETL 节奏一致。

后续若要更强的信号，需要的是「可插拔的版本来源」（例如按分区目录列文件 mtime，
或 metastore 侧的事件/版本字段），而不是在本层加文件 hash——那会引入全量 IO，
与本 issue「不扫描数据」的目标冲突。
