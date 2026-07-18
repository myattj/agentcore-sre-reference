# Multi-stage build for the Next.js onboarding service.
# Build context: onboarding/ (not repo root).
# Requires output: "standalone" in next.config.ts.

# ---- Stage 1: install dependencies ----
FROM node:22-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

# ---- Stage 2: build ----
FROM node:22-alpine AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .

# NEXT_PUBLIC_* vars are inlined at build time into the client JS bundle.
# Pass via: docker build --build-arg NEXT_PUBLIC_BRIDGE_INSTALL_URL=https://...
ARG NEXT_PUBLIC_BRIDGE_INSTALL_URL
ENV NEXT_PUBLIC_BRIDGE_INSTALL_URL=$NEXT_PUBLIC_BRIDGE_INSTALL_URL

RUN npm run build

# ---- Stage 3: production runtime ----
FROM node:22-alpine AS runner
WORKDIR /app

ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1

# Copy the standalone server + static assets + public directory
COPY --from=builder --chown=node:node /app/.next/standalone ./
COPY --from=builder --chown=node:node /app/.next/static ./.next/static
COPY --from=builder --chown=node:node /app/public ./public

EXPOSE 3000

USER node

CMD ["node", "server.js"]
