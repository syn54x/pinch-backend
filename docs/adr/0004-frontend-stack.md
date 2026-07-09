# Frontend: Vite SPA, not Next.js; PWA before native

The app frontend is a static Vite + React + TypeScript SPA (TanStack
Router/Query, Tailwind + shadcn/ui, AI Elements + Vercel AI SDK for Penny
chat, bklit for charts with nivo as fallback), typed against the backend's
OpenAPI schema. Mobile v0 is the same app as an installable PWA; desktop is
a Tauri wrapper when demanded; native mobile apps are explicitly deferred.
Next.js was rejected for the app because everything sits behind a login
(SSR/SEO buys nothing), it would add a second server runtime to operate and
self-host, and static export — the mode that removes that server — removes
Next's advantages too. Next.js/Astro remain candidates for a future marketing
site only.

## Consequences

- Penny chat wire protocol: AI SDK `useChat` ↔ `pydantic_ai.ui.vercel_ai.
  VercelAIAdapter` on a Litestar endpoint (mounted as an ASGI sub-app if the
  Starlette-flavored adapter needs it).
- The backend can serve the built SPA as static files — one-process
  self-hosting stays possible.
