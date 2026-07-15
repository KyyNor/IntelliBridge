FROM python:3.12-slim

RUN python -m pip install --no-cache-dir "pyhive[hive_pure_sasl]==0.7.0"

WORKDIR /app
COPY tests/spark_thrift/collector.py /app/collector.py

ENTRYPOINT ["python", "/app/collector.py"]
