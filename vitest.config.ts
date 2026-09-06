import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    // Most suites are pure logic; the hook suites opt into jsdom with a
    // `@vitest-environment jsdom` docblock.
    environment: 'node',
    include: ['src/**/*.test.ts', 'src/**/*.test.tsx'],
  },
});
