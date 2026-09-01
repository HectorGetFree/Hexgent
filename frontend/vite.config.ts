import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

/** 为 Hexgent React 客户端提供开发服务器和生产构建配置。 */
export default defineConfig({
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    port: 5173,
  },
})
