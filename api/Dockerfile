FROM python:3.12-slim

# git: impact analysis (read-only plumbing); build-essential: tree-sitter / igraph wheels
RUN apt-get update \
    && apt-get install -y --no-install-recommends git build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt requirements-guard.txt ./
# One resolver run, so llm-guard's exact pins are checked against ours. CPU-only
# PyTorch: the default wheel bundles CUDA and adds gigabytes for nothing here.
RUN pip install --no-cache-dir --extra-index-url https://download.pytorch.org/whl/cpu \
        -r requirements.txt -r requirements-guard.txt

# Bake the guardrail model weights into the image, before the app code, so a
# code change does not re-download them. At runtime the container is OFFLINE
# for Hugging Face: no model fetches, no telemetry — customer source is scanned
# locally and nothing about it leaves.
ENV HF_HOME=/opt/hf HF_HUB_DISABLE_TELEMETRY=1
RUN python -c "from llm_guard.input_scanners import PromptInjection, Secrets; \
from llm_guard.input_scanners.prompt_injection import MatchType; \
PromptInjection(threshold=0.99, match_type=MatchType.CHUNKS); Secrets()" \
    && chmod -R a+rX /opt/hf
ENV HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 GUARD_MODE=required

COPY . .

# Never run as root: the api parses untrusted repos with native code, and a
# parser exploit should land in an unprivileged account. The repos mount is
# read-only regardless.
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app
ENV PYTHONDONTWRITEBYTECODE=1
USER app

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
