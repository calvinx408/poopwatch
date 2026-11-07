import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// In dev, Vite serves the UI on :5173 and proxies data to the Python backend
// on :8420, so `npm run dev` and `python -m poopwatch serve` work side by side.
const BACKEND = process.env.POOPWATCH_API || 'http://127.0.0.1:8420'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': { target: BACKEND, changeOrigin: true },
      '/evidence': { target: BACKEND, changeOrigin: true },
    },
  },
  build: { outDir: 'dist', emptyOutDir: true },
})
