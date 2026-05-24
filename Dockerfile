# syntax=docker/dockerfile:1.7

# ---- Build the SPA ----
FROM node:20-alpine AS builder

WORKDIR /app

# Better layer caching: install deps first
COPY package.json package-lock.json ./
RUN npm ci

COPY tsconfig.json vite.config.ts index.html ./
COPY src ./src

# In Docker, the frontend talks to the agent through nginx → drift-agent.
# VITE_BASE defaults to './' so the build is portable — asset URLs are
# emitted relative to index.html, and runtime code derives API URLs from
# document.baseURI (see src/lib/apiBase.ts). One image works at /, /drift/,
# /observability/drift/, etc. — whatever path you reverse-proxy onto.
ARG VITE_ENGINE=agent
ARG VITE_BASE=./
ARG VITE_API_BASE=
ENV VITE_ENGINE=${VITE_ENGINE} \
    VITE_BASE=${VITE_BASE} \
    VITE_API_BASE=${VITE_API_BASE}

RUN npm run build

# ---- Serve via nginx ----
FROM nginx:alpine AS runtime

# Drop default config; ship our SPA + /api proxy
RUN rm /etc/nginx/conf.d/default.conf
COPY nginx.conf /etc/nginx/conf.d/drift.conf

COPY --from=builder /app/dist /usr/share/nginx/html

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
  CMD wget -qO- http://localhost/ > /dev/null || exit 1
