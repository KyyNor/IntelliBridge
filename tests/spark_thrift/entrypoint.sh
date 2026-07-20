#!/usr/bin/env bash
set -euo pipefail

mkdir -p /tmp/spark-warehouse

/opt/spark/sbin/start-thriftserver.sh \
  --master local[2] \
  --conf spark.sql.warehouse.dir=/tmp/spark-warehouse \
  --conf spark.ui.enabled=true \
  --conf spark.ui.port=4040 \
  --conf spark.driver.bindAddress=0.0.0.0 \
  --hiveconf hive.server2.thrift.port=10000 \
  --hiveconf hive.server2.thrift.bind.host=0.0.0.0 \
  --hiveconf hive.server2.enable.doAs=false

shutdown() {
  /opt/spark/sbin/stop-thriftserver.sh || true
}

trap shutdown EXIT
trap 'exit 0' INT TERM

echo "Waiting for Spark Thrift Server on port 10000..."
for _ in $(seq 1 60); do
  if bash -c 'cat < /dev/null > /dev/tcp/127.0.0.1/10000' 2>/dev/null; then
    echo "Spark Thrift Server is ready."
    while bash -c 'cat < /dev/null > /dev/tcp/127.0.0.1/10000' 2>/dev/null; do
      sleep 2
    done
    exit 1
  fi
  sleep 2
done

echo "Spark Thrift Server did not become ready in time." >&2
exit 1
