import { expect, test } from '@playwright/test';

test('test_built_spa_loads_control_plane_boundary', async ({ page }) => {
  await page.goto('/');

  await expect(page).toHaveTitle('Spine Control Plane');
  await expect(
    page.getByRole('heading', { name: 'Spine Control Plane' }),
  ).toBeVisible();
  await expect(page.getByRole('status')).toHaveText('Workspace connected');
});
