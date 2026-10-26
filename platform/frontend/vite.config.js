import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import AutoImport from 'unplugin-auto-import/vite'
import Components from 'unplugin-vue-components/vite'
import { ElementPlusResolver } from 'unplugin-vue-components/resolvers'

export default defineConfig({
  plugins: [
    vue(),
    AutoImport({ resolvers: [ElementPlusResolver()] }),
    Components({ resolvers: [ElementPlusResolver()] }),
  ],
  server: {
    // 默认只绑 127.0.0.1，别的机器访问不了；开发环境需要从局域网直连。
    host: '0.0.0.0',
    port: 3000,
    proxy: {
      '/api': { target: 'http://localhost:8089', changeOrigin: true, ws: true },
    },
  },
})
