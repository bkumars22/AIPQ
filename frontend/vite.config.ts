import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// https://vite.dev/config/
// GitHub Pages serves this app from a /AIPQ/ subpath and has no live backend
// (hence demo mode); the Docker/local build serves from root against a real
// backend. Only the explicit `--mode ghpages` build (used by the Pages
// workflow) should get the subpath base — the plain `vite build` Docker runs
// must stay at '/' or its JS/CSS 404 and the page renders blank.
export default defineConfig(({ mode }) => ({
  plugins: [react(), tailwindcss()],
  base: mode === 'ghpages' ? '/AIPQ/' : '/',
  server: {
    port: 3001,
  },
}))
