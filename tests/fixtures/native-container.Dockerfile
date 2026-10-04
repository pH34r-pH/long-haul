# Qualification-only image. BASE_IMAGE is resolved to an immutable digest first.
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-venv libgomp1 && rm -rf /var/lib/apt/lists/*
COPY wheels /wheels
RUN python3 -m venv /venv && /venv/bin/pip install --no-index --find-links=/wheels \
      long-haul pytest tokenizers Jinja2 && rm -rf /wheels
COPY bin /native
COPY tests /checks/tests
COPY capture.py /checks/capture.py
ENV PYTHONDONTWRITEBYTECODE=1 HOME=/tmp \
    LONG_HAUL_NATIVE_COMPLETION=/native/llama-completion \
    LONG_HAUL_NATIVE_TOKENIZE=/native/llama-tokenize \
    LONG_HAUL_NATIVE_SERVER=/native/llama-server \
    LONG_HAUL_NATIVE_GGUF=/inputs/model.gguf \
    LONG_HAUL_REFERENCE_TOKENIZER=/inputs/tokenizer \
    LONG_HAUL_NATIVE_EVIDENCE=/evidence LONG_HAUL_REQUIRE_NATIVE_SMOKE=1 \
    LONG_HAUL_REQUIRE_CONTAINER_BOUNDARY=1
WORKDIR /checks
USER 65534:65534
ENTRYPOINT ["/venv/bin/python", "/checks/capture.py"]
