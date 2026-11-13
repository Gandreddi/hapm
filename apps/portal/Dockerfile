FROM docker.io/library/node:22.12.0-alpine AS build
RUN npm install -g pnpm@9.15.0
WORKDIR /app
COPY package.json pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY . .
ENV NEXT_TELEMETRY_DISABLED=1
RUN pnpm build

FROM docker.io/library/node:22.12.0-alpine
WORKDIR /app
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 HOSTNAME=0.0.0.0 PORT=3000
RUN addgroup -S hapm && adduser -S hapm -G hapm
COPY --from=build --chown=hapm:hapm /app/.next/standalone ./
COPY --from=build --chown=hapm:hapm /app/.next/static ./.next/static
USER hapm
EXPOSE 3000
CMD ["node", "server.js"]
