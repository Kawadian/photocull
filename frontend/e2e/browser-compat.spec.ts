import { expect, test } from '@playwright/test'

test('preserves catalog query parameters without URLSearchParams.size', async ({ page }) => {
  await page.addInitScript(() => {
    Reflect.deleteProperty(URLSearchParams.prototype, 'size')
  })
  const scoped = page.waitForRequest((request) => request.url().includes('/api/browse/timeline')
    && request.url().includes('library=gallery-2024') && request.url().includes('month=2024-05'))
  await page.goto('/?view=browser&library=gallery-2024&month=2024-05&lang=en')
  await scoped
  const grid = page.getByRole('main', { name: 'Photos and videos' })
  await expect(grid.getByRole('button', { name: 'Frame-118.jpg', exact: true })).toBeVisible()
  const continuation = page.waitForRequest((request) => request.url().includes('/api/browse/timeline')
    && new URL(request.url()).searchParams.has('cursor'))
  await grid.evaluate((element) => { element.scrollTop = element.scrollHeight })
  expect(new URL((await continuation).url()).searchParams.get('library')).toBe('gallery-2024')
})
