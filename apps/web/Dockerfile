FROM node:24-alpine AS build
ARG VITE_AUTH_REQUIRED=false
ARG VITE_KEYCLOAK_URL=http://localhost:8088
ARG VITE_KEYCLOAK_REALM=semora
ARG VITE_KEYCLOAK_CLIENT_ID=semora-web
ARG VITE_OFFLINE_MODELING_ENABLED=true
ENV VITE_AUTH_REQUIRED=$VITE_AUTH_REQUIRED \
    VITE_KEYCLOAK_URL=$VITE_KEYCLOAK_URL \
    VITE_KEYCLOAK_REALM=$VITE_KEYCLOAK_REALM \
    VITE_KEYCLOAK_CLIENT_ID=$VITE_KEYCLOAK_CLIENT_ID \
    VITE_OFFLINE_MODELING_ENABLED=$VITE_OFFLINE_MODELING_ENABLED
WORKDIR /app
COPY apps/web/package*.json ./
RUN npm install
COPY apps/web/ ./
RUN npm run build

FROM nginx:1.27-alpine
ARG VITE_OFFLINE_MODELING_ENABLED=true
COPY apps/web/nginx-main.conf /etc/nginx/nginx.conf
COPY apps/web/nginx.conf /etc/nginx/conf.d/default.conf
COPY apps/web/nginx-offline-modeling.conf /etc/nginx/offline-modeling/location.conf
COPY --from=build /app/dist /usr/share/nginx/html
RUN if [ "$VITE_OFFLINE_MODELING_ENABLED" != "true" ]; then rm /etc/nginx/offline-modeling/location.conf; fi \
    && chown -R nginx:nginx /var/cache/nginx /var/run /usr/share/nginx/html
USER nginx
EXPOSE 8080

