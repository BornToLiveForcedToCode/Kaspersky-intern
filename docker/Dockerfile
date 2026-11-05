
FROM vllm/vllm-openai-cpu:v0.33.0

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    HOME=/tmp/home \
    HF_HOME=/models/.cache/huggingface \
    HF_HUB_DISABLE_TELEMETRY=1 \
    VLLM_LOGGING_LEVEL=INFO \
    VLLM_REQUEST_BODY_LOG_LEVEL=CRITICAL

WORKDIR /app

COPY docker/requirements.txt /tmp/requirements.txt
COPY docker/verify_lock.py /tmp/verify_lock.py
RUN python /tmp/verify_lock.py /tmp/requirements.txt

COPY src/logging_config.py /app/logging_config.py
COPY src/download_model.py /app/download_model.py

USER 10001:10001

ENTRYPOINT ["vllm", "serve"]
