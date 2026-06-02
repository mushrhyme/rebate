import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// 프록시 대상은 PC마다 다를 수 있어 환경변수로 뺀다 (frontend/.env, git 미추적).
//   VITE_PROXY_TARGET — 전체 URL 지정 시 우선 (예: http://localhost:8000)
//   VITE_BACKEND_PORT — 포트만 지정 (기본 8800)
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const target = env.VITE_PROXY_TARGET || `http://localhost:${env.VITE_BACKEND_PORT || '8800'}`
  return {
    plugins: [react(), tailwindcss()],
    server: {
      host: true,
      fs: { allow: ['..'] },
      proxy: {
        '/api': { target, changeOrigin: true },
      },
    },
  }
})
