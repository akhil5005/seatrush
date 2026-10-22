import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// In production the React build is served BY NGINX from the same origin as
// /api, so there is no CORS and no API base URL to configure -- every fetch is
// a relative path like `/api/events`.
//
// `npm run dev` breaks that same-origin property (Vite serves on :5173), so the
// dev server proxies the same relative paths to the running compose stack over
// HTTPS. secure:false accepts the self-signed certificate.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': { target: 'https://localhost:8443', changeOrigin: true, secure: false },
      '/nginx_status': { target: 'https://localhost:8443', changeOrigin: true, secure: false },
    },
  },
  build: {
    // Hashed filenames in /assets/ are what let nginx.conf cache them for a
    // year with `immutable`: a new build produces new names, so there is
    // nothing to invalidate.
    outDir: 'dist',
    assetsDir: 'assets',
    sourcemap: false,
  },
})
