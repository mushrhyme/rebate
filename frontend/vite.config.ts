import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { createRequire } from 'node:module'
import { cpSync, existsSync, createReadStream } from 'node:fs'
import { join, dirname } from 'node:path'

// pdf.js가 일본어(CID 키) 폰트를 렌더링하려면 cmaps·standard_fonts 에셋이 필요하다.
// node_modules의 에셋을 dev에서는 미들웨어로, 빌드에서는 dist로 복사해 /pdfjs/ 경로로 서빙한다.
// (의존성 추가·바이너리 커밋 없이 ColdStart PDF 미리보기의 "틀만 보이고 글자 안 보임" 해결)
function pdfjsAssets() {
  const require = createRequire(import.meta.url)
  const pdfjsDir = dirname(require.resolve('pdfjs-dist/package.json'))
  const dirs = ['cmaps', 'standard_fonts']
  return {
    name: 'pdfjs-assets',
    configureServer(server: any) {
      server.middlewares.use((req: any, res: any, next: any) => {
        const url: string = req.url || ''
        if (!url.startsWith('/pdfjs/')) return next()
        const rel = url.slice('/pdfjs/'.length).split('?')[0]
        const fp = join(pdfjsDir, rel)
        if (existsSync(fp)) {
          res.setHeader('Content-Type', 'application/octet-stream')
          createReadStream(fp).pipe(res)
          return
        }
        next()
      })
    },
    writeBundle(options: any) {
      const out = options.dir || 'dist'
      for (const d of dirs) cpSync(join(pdfjsDir, d), join(out, 'pdfjs', d), { recursive: true })
    },
  }
}

// 프록시 대상은 PC마다 다를 수 있어 환경변수로 뺀다 (frontend/.env, git 미추적).
//   VITE_PROXY_TARGET — 전체 URL 지정 시 우선 (예: http://localhost:8000)
//   VITE_BACKEND_PORT — 포트만 지정 (기본 8800)
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const target = env.VITE_PROXY_TARGET || `http://localhost:${env.VITE_BACKEND_PORT || '8800'}`
  return {
    plugins: [react(), tailwindcss(), pdfjsAssets()],
    server: {
      host: true,
      fs: { allow: ['..'] },
      proxy: {
        '/api': { target, changeOrigin: true },
      },
    },
  }
})
