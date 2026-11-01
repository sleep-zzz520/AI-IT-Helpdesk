import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// 后端地址可覆盖：本机 8000 被占用时用 VITE_API_TARGET 指向别的端口，
// 不设置时保持原有 127.0.0.1:8000 行为。
const apiTarget = process.env.VITE_API_TARGET || 'http://127.0.0.1:8000'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    // 本地开发代理：前端用相对路径 /api，这里转发到后端（与生产 nginx 行为一致）
    proxy: {
      '/api': {
        target: apiTarget,
        changeOrigin: true,
        configure: (proxy) => {
          // SSE 实时推送：关闭 Nagle 算法，避免小数据包被合并延迟（实测 proxy 下 SSE 晚 2s+）
          proxy.on('proxyRes', (proxyRes, req, res) => {
            res.socket?.setNoDelay(true)
          })
        },
      },
    },
  },
})
