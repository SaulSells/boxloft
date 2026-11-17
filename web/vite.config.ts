import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';
import path from 'path';

export default defineConfig({
  plugins: [react(), tailwindcss()],
  base: '/',
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    rollupOptions: { output: { manualChunks: { 'vendor-react': ['react', 'react-dom', '@tanstack/react-query', 'framer-motion'], 'vendor-radix': ['radix-ui'] } } },
  },
  resolve: { alias: { '@': path.resolve(__dirname, './src') } },
  server: {
    host: true,
    proxy: { '/api': { target: 'http://127.0.0.1:5101', changeOrigin: true } },
  },
});
