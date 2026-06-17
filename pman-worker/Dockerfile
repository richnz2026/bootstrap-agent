# pman-worker — Pearl Mining Image for Vast.ai
# Uses pre-built binaries extracted from previous build
FROM nvidia/cuda:13.0.0-devel-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive

# System dependencies
RUN apt-get update && apt-get install -y \
    curl wget ca-certificates python3 \
    && rm -rf /var/lib/apt/lists/*

# Install uv and exact Python 3.12 used to build the venv
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:$PATH"
RUN uv python install cpython-3.12-linux-x86_64-gnu

# Copy pre-built binaries and venv
COPY pearl-bin/ /opt/pearl/bin/
COPY pearl-venv/ /opt/pearl/.venv/
COPY pearl-miner-src/ /opt/pearl/miner/

# Make binaries executable
RUN chmod +x /opt/pearl/bin/*

# Fix broken venv Python symlinks
RUN ln -sf /root/.local/share/uv/python/cpython-3.12-linux-x86_64-gnu/bin/python3.12 /opt/pearl/.venv/bin/python && \
    ln -sf /root/.local/share/uv/python/cpython-3.12-linux-x86_64-gnu/bin/python3.12 /opt/pearl/.venv/bin/python3 && \
    ln -sf /root/.local/share/uv/python/cpython-3.12-linux-x86_64-gnu/bin/python3.12 /opt/pearl/.venv/bin/python3.12

# Fix .pth files missing newlines
RUN for f in /opt/pearl/.venv/lib/python3.12/site-packages/*.pth; do \
    echo "" >> "$f"; \
    done

# Disable SSL verification for self-signed pearld cert
RUN sed -i 's/return aiohttp.ClientSession(auth=self.auth)/return aiohttp.ClientSession(auth=self.auth, connector=aiohttp.TCPConnector(ssl=False))/' \
    /opt/pearl/miner/pearl-gateway/src/pearl_gateway/pearl_client.py

ENV PATH="/opt/pearl/bin:/opt/pearl/.venv/bin:$PATH"

# Copy entrypoint
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 44107 44108 44207

ENTRYPOINT ["/entrypoint.sh"]
