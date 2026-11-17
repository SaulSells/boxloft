# Builds the web app, then runs the Python server. Not yet run by the maintainers: please report what breaks.
FROM node:22-slim AS web
WORKDIR /src/web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM python:3.12-slim
WORKDIR /app
COPY requirements-ai.txt ./
RUN pip install --no-cache-dir -r requirements-ai.txt
COPY backend backend
COPY scripts scripts
COPY extension extension
COPY --from=web /src/web/dist web/dist
ENV BOXLOFT_DATA=/data
VOLUME /data
# The server listens on 127.0.0.1:5101 only. Caddy shares this container's network (docker-compose.yml),
# so requests arrive from 127.0.0.1 and the server trusts Caddy's X-Forwarded-For for its per-device sign-in lockout.
CMD ["python3", "backend/server.py"]
