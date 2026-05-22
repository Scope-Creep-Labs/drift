# Drift Deploy edge agent — runs the reconcile loop inside a container.
# Mounts the host's Docker socket so `docker compose` calls operate on
# the host's daemon (not a nested DinD). Same script as before, no native
# host deps required beyond Docker itself.
FROM alpine:3.20

RUN apk add --no-cache \
        bash \
        curl \
        jq \
        tar \
        coreutils \
        util-linux \
        docker-cli \
        docker-cli-compose \
        python3 \
        py3-websockets

COPY drift-deploy-agent.sh /usr/local/bin/drift-deploy-agent.sh
COPY terminal-bridge.py /opt/drift/terminal-bridge.py
RUN chmod +x /usr/local/bin/drift-deploy-agent.sh /opt/drift/terminal-bridge.py

WORKDIR /var/lib/drift-deploy
ENTRYPOINT ["/usr/local/bin/drift-deploy-agent.sh"]
