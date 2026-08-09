# Multi-stage build for the Next.js analytical frontend (development-plan step 10).

FROM node:22-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm ci

FROM node:22-alpine AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
ENV NEXT_TELEMETRY_DISABLED=1
# next.config.mjs evaluates rewrites() at build time and bakes the /api/backend/*
# proxy destination into the routes manifest, so BACKEND_URL must be set here (not
# only in the runner stage) — otherwise it falls back to localhost and browser
# calls to /api/backend/* 500. Defaults to the Compose service; override with
# --build-arg BACKEND_URL=... if the API lives elsewhere.
ARG BACKEND_URL=http://api:8000
ENV BACKEND_URL=${BACKEND_URL}
RUN npm run build

FROM node:22-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1
ENV PORT=3000
# The Next.js standalone server binds to $HOSTNAME, and Docker sets that variable
# to the container id, which resolves to only one of the container's networks. The
# frontend is attached to both `fantasy-internal` and `dokploy-network`, so without
# this the server may listen only on the private network and Traefik gets a
# connection refused (502 Bad Gateway).
ENV HOSTNAME=0.0.0.0
# BACKEND_URL is read at server startup by next.config.mjs rewrites.
ENV BACKEND_URL=http://api:8000

RUN addgroup --system --gid 1001 nodejs \
  && adduser --system --uid 1001 nextjs

COPY --from=builder /app/public ./public
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static

USER nextjs
EXPOSE 3000
CMD ["node", "server.js"]
