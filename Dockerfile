FROM node:20-bookworm-slim

ENV NODE_ENV=production \
    PORT=8080

WORKDIR /app

RUN groupadd --system app \
    && useradd --system --gid app --home-dir /app --shell /usr/sbin/nologin app

COPY node_execution/package.json ./package.json
RUN npm install --omit=dev --ignore-scripts --no-audit --no-fund \
    && npm cache clean --force

COPY --chown=app:app node_execution/src ./src

USER app
EXPOSE 8080
STOPSIGNAL SIGTERM

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD node -e "fetch('http://127.0.0.1:'+(process.env.PORT||8080)+'/healthz').then(r=>{if(!r.ok)process.exit(1)}).catch(()=>process.exit(1))"

CMD ["node", "src/index.js"]
