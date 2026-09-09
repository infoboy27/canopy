# Dockerfile for Canopy + Python Plugin
# Build from repository root: docker build -f plugin/python/Dockerfile -t canopy-python .

# Stage 1: Build Canopy (Go)
FROM golang:1.26-alpine AS canopy-builder
WORKDIR /app
RUN apk add --no-cache nodejs npm
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN npm ci --prefix cmd/rpc/web/wallet && npm run build --prefix cmd/rpc/web/wallet
RUN npm ci --prefix cmd/rpc/web/explorer && npm run build --prefix cmd/rpc/web/explorer
RUN go build -trimpath -ldflags="-s -w" -o /canopy ./cmd/main/...

# Stage 2: Runtime with Python
FROM python:3.12-alpine
WORKDIR /app

# Install bash (required for pluginctl.sh)
RUN apk add --no-cache bash procps

RUN mkdir -p /tmp/plugin /root/.canopy

# Copy Canopy binary
COPY --from=canopy-builder /canopy .

# Copy Python plugin to correct path (Canopy expects plugin/python/pluginctl.sh)
COPY plugin/python/ ./plugin/python/

# Windows checkouts may preserve CRLF in the Bash entrypoint.  Normalize it in
# the image so the kernel can resolve /bin/bash when Canopy starts the plugin.
RUN sed -i 's/\r$//' /app/plugin/python/pluginctl.sh && \
    chmod 0755 /app/plugin/python/pluginctl.sh

# Remove any existing venv from copy, create fresh venv, and install dependencies
RUN rm -rf /app/plugin/python/.venv && \
    python3 -m venv /app/plugin/python/.venv && \
    /app/plugin/python/.venv/bin/pip install --no-cache-dir --upgrade pip && \
    /app/plugin/python/.venv/bin/pip install --no-cache-dir -e /app/plugin/python/

# Set plugin type for Canopy to start
RUN printf '{"plugin":"python"}\n' > /root/.canopy/config.json

# Default: run canopy (start plugin separately with: python plugin/main.py)
# Mount config at runtime: -v ~/.canopy:/root/.canopy
CMD ["./canopy", "start"]
