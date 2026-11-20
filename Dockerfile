FROM debian:trixie-slim@sha256:a72b4433de1cf7d6bae0c4cee74d0547f58601a643b3375f070c51600d63b6b2

ENV LANG=C.UTF-8 PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

COPY --chmod=0755 docker/install-expressvpn.sh /usr/local/bin/install-expressvpn
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates curl iproute2 iptables nftables iso-codes libatomic1 libbrotli1 \
        libcap2-bin libdbus-1-3 libglib2.0-0t64 libnl-3-200 libnl-genl-3-200 libnl-route-3-200 \
        procps psmisc python3 python3-aiohttp python3-dnspython \
    && /usr/local/bin/install-expressvpn \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY app/ /app/
RUN chmod -R a+rX /app

EXPOSE 8080/tcp 1080/tcp
HEALTHCHECK --interval=15s --timeout=3s --start-period=90s \
    CMD curl -fsS --max-time 2 http://127.0.0.1:8080/healthz || exit 1
ENTRYPOINT ["/usr/bin/python3", "/app/main.py"]
