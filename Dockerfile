FROM python:3.12-slim

WORKDIR /app

# Copy requirements first for better caching
COPY requirements.txt .

# Install dependencies without cache and use pip's cache mounting
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Expose port
EXPOSE 49000

# Run the application
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "49000"]
