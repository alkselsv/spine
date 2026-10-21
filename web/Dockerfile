FROM node:22.20.0-bookworm-slim AS build

RUN npm install --global pnpm@12.11.2
WORKDIR /app

COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY web/package.json ./web/package.json
COPY web/api/codegen/package.json ./web/api/codegen/package.json
RUN pnpm install --frozen-lockfile

COPY web ./web
RUN pnpm --dir web build

FROM nginx:1.28.0-alpine

COPY docker/web/default.conf.template /etc/nginx/templates/default.conf.template
COPY docker/web/40-runtime-config.sh /docker-entrypoint.d/40-runtime-config.sh
COPY docker/web/runtime-config.js.template /opt/spine/runtime-config.js.template
COPY --from=build /app/web/dist /usr/share/nginx/html

RUN chmod 0555 /docker-entrypoint.d/40-runtime-config.sh

EXPOSE 8080
