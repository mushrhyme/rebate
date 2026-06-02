import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    host: true,
    fs: { allow: ['..'] },
    proxy: {
      '/api': { target: 'http://localhost:8800', changeOrigin: true },
    },
  },
})
