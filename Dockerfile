FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    XDG_CONFIG_HOME=/data

WORKDIR /app
COPY pyproject.toml constraints.txt ./
COPY src/copilot_proxy ./src/copilot_proxy
RUN python -m pip install --no-cache-dir -c constraints.txt . \
    && groupadd --gid 10001 copilot \
    && useradd --uid 10001 --gid 10001 --no-create-home copilot \
    && mkdir -p /config /data \
    && chown -R 10001:10001 /config /data
USER 10001:10001
EXPOSE 4140
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import json,urllib.request; c=json.load(open('/config/adapter-config.json')); h=c.get('host','0.0.0.0'); h='127.0.0.1' if h=='0.0.0.0' else h; r=json.load(urllib.request.urlopen('http://'+h+':'+str(c.get('port',4140))+'/v1/models',timeout=3)); assert r['data']"
ENTRYPOINT ["python", "-m", "copilot_proxy.cli"]
CMD ["serve", "--root", "/config"]
