import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    // 本地开发代理：前端用相对路径 /api，这里转发到后端（与生产 nginx 行为一致）
    proxy: {
      '/api': 'http://127.0.0.1:8000',
    },
  },
})
