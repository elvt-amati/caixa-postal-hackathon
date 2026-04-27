FROM public.ecr.aws/docker/library/python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    NODE_VERSION=20.11.1

# Install Node.js for MCP stdio servers (e.g., @modelcontextprotocol/server-filesystem)
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates xz-utils \
 && curl -fsSL "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.xz" -o /tmp/node.tar.xz \
 && tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1 \
 && rm /tmp/node.tar.xz \
 && apt-get purge -y xz-utils \
 && apt-get autoremove -y && apt-get clean && rm -rf /var/lib/apt/lists/* \
 && node --version && npm --version

# Prime the MCP filesystem server so it's cached offline
RUN mkdir -p /tmp/caixa-mcp-root \
 && echo "bem-vindo ao filesystem MCP do agente Pesquisa" > /tmp/caixa-mcp-root/README.txt \
 && echo '{"demo":"this JSON can be read via the mcp filesystem tool read_file"}' > /tmp/caixa-mcp-root/sample.json \
 && npx -y @modelcontextprotocol/server-filesystem --help > /dev/null 2>&1 || true

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY backend/ ./backend/
COPY frontend/ ./frontend/

EXPOSE 8080
WORKDIR /app/backend
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
