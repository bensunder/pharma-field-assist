# syntax=docker/dockerfile:1.7
FROM node:22-alpine AS build
WORKDIR /web
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY . .
ARG VITE_AUTH_MODE=demo
ARG VITE_ENTRA_CLIENT_ID=
ARG VITE_ENTRA_TENANT_ID=
ARG VITE_API_SCOPE=
ENV VITE_AUTH_MODE=$VITE_AUTH_MODE VITE_ENTRA_CLIENT_ID=$VITE_ENTRA_CLIENT_ID VITE_ENTRA_TENANT_ID=$VITE_ENTRA_TENANT_ID VITE_API_SCOPE=$VITE_API_SCOPE
RUN npm run typecheck && npm test && npm run build

FROM nginxinc/nginx-unprivileged:1.27-alpine AS runtime
COPY nginx.conf /etc/nginx/templates/default.conf.template
COPY --from=build /web/dist /usr/share/nginx/html
ENV API_UPSTREAM=http://api:8000
EXPOSE 8080
