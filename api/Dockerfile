FROM python:3.12-slim

# git: impact analysis (read-only plumbing); build-essential: tree-sitter / igraph wheels
RUN apt-get update \
    && apt-get install -y --no-install-recommends git build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Never run as root: the api parses untrusted repos with native code, and a
# parser exploit should land in an unprivileged account. The repos mount is
# read-only regardless.
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app
ENV PYTHONDONTWRITEBYTECODE=1
USER app

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
