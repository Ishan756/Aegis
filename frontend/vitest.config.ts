import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  test: {
    // happy-dom rather than jsdom: it starts an order of magnitude faster,
    // which matters when node_modules lives on a slow mounted filesystem
    // (for example a Windows drive inside WSL).
    environment: 'happy-dom',
    globals: true,
    include: ['src/**/*.test.{ts,tsx}'],
  },
})