import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  server: {
    fs: { allow: [fileURLToPath(new URL('..', import.meta.url))] },
    port: 5173,
    strictPort: true,
    proxy: { '/v1': { target: 'http://localhost:8000', changeOrigin: true } },
  },
});
