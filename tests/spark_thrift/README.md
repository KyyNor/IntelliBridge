# Spark Thrift Server error fixture environment

This test-only environment uses the official Apache Spark `v3.1.3` image in
`local[2]` mode. It
exposes HiveServer2/Thrift on port `10000`, creates a synthetic `demo.orders`
table, and writes raw PyHive error samples to
`tests/spark_thrift/output/hive-error-samples.json`.

Start and collect samples:

```bash
docker compose -f docker-compose.spark-test.yml up --build --abort-on-container-exit --exit-code-from hive-error-collector
```

Stop and remove the test volume:

```bash
docker compose -f docker-compose.spark-test.yml down -v
```

The generated output is intentionally ignored by Git. It contains only the
synthetic SQL and table names from `collector.py`.
