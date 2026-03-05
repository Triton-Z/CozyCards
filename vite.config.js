import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  base: '/', // absolute paths for Vercel deployment
  optimizeDeps: {
    // sql.js ships a complex CommonJS bundle that must not be pre-bundled by Vite
    exclude: ['sql.js'],
  },
  build: {
    rollupOptions: {
      output: {
        manualChunks: {
          // Separate heavy vendor libs so they cache independently
          'vendor-react': ['react', 'react-dom', 'react-router-dom'],
          'vendor-tiptap': [
            '@tiptap/react', '@tiptap/starter-kit',
            '@tiptap/extension-image', '@tiptap/extension-underline',
            '@tiptap/extension-text-style', '@tiptap/extension-color',
            '@tiptap/extension-highlight', '@tiptap/extension-code-block',
            '@tiptap/extension-text-align', '@tiptap/extension-placeholder',
          ],
          'vendor-dnd': ['@dnd-kit/core', '@dnd-kit/sortable', '@dnd-kit/utilities'],
          'vendor-katex': ['katex'],
        },
      },
    },
  },
  server: {
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:5328',
        changeOrigin: true,
      },
    },
  },
})