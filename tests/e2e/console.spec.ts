// Playwright E2E test for Sentinel Incident Console (Architecture p.9, FR-IC-01..04).
// Run against live stack: BASE_URL=http://localhost:8080 npx playwright test tests/e2e/console.spec.ts
import { test, expect } from '@playwright/test';

const BASE_URL = process.env.BASE_URL ?? 'http://localhost:8080';

test('responder signs in, views incident list, and inspects incident timeline', async ({ page }) => {
  await page.goto(BASE_URL);
  await expect(page.getByText('Sentinel Console')).toBeVisible();

  await page.getByLabel('Email').fill('responder@sentinel.local');
  await page.getByLabel('Password').fill(process.env.SEED_RESPONDER_PASSWORD ?? 'responder-dev-pass');
  await page.getByRole('button', { name: 'Sign in' }).click();

  await expect(page.getByText('updates live')).toBeVisible();
  await expect(page.getByText('responder')).toBeVisible();
});
