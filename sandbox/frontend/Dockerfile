# Frontend sandbox that mirrors the Vercel production deployment.
#
# Vercel's Next.js build: Node 22.x (the project default), `npm install`,
# `next build` with NEXT_PUBLIC_* inlined at BUILD time (exactly why a Vercel
# env-var change needs a redeploy), then the production server. The build
# runs from /vercel/path0 like Vercel's build container does.
FROM node:22-slim

WORKDIR /vercel/path0

COPY package.json package-lock.json ./
RUN npm install --no-audit --no-fund

COPY . .

ARG NEXT_PUBLIC_SUPABASE_URL
ARG NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY
ARG NEXT_PUBLIC_API_URL
# System env vars Vercel sets for every production build and function.
ENV CI=1 \
    VERCEL=1 \
    VERCEL_ENV=production \
    NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    NEXT_PUBLIC_SUPABASE_URL=$NEXT_PUBLIC_SUPABASE_URL \
    NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY=$NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY \
    NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL

RUN npm run build

COPY --from=sandbox-files localhost-forward.mjs /sandbox/localhost-forward.mjs

EXPOSE 3000
# localhost-forward.mjs makes http://localhost:<supabase port> inside this
# container reach the Supabase on your machine, so the one inlined
# NEXT_PUBLIC_SUPABASE_URL works for both the browser and the Next.js server
# (middleware.ts and app/chat/page.tsx call Supabase server-side). On Vercel
# the URL is public, so this shim has no production counterpart.
CMD ["sh", "-c", "node /sandbox/localhost-forward.mjs & exec npx next start -p 3000 -H 0.0.0.0"]
