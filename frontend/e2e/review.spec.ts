import { expect, test } from '@playwright/test'
import type { Page } from '@playwright/test'

function burstAt(page: Page, seconds: string) {
  return page.getByRole('button', { name: new RegExp(`Burst at .*04:05:${seconds}`) })
}

test('filter and review bursts with keyboard shortcuts and reversible flags', async ({ page }) => {
  const review = page.getByRole('main', { name: 'Photo review' })
  await page.goto('/')
  await page.getByRole('button', { name: 'Bursts', exact: true }).click()
  await expect(page.getByText('No bursts yet.')).toBeVisible()
  await page.getByRole('button', { name: 'Scan library', exact: true }).click()
  await expect(burstAt(page, '05')).toBeVisible()
  await expect(burstAt(page, '15')).toBeVisible()

  await page.keyboard.press(']')
  await expect(burstAt(page, '05')).toHaveAttribute('aria-current', 'true')
  await expect(review.getByRole('img', { name: 'a.jpg' })).toBeVisible()
  await page.keyboard.press('ArrowRight')
  await expect(review.getByRole('img', { name: 'b.jpg' })).toBeVisible()

  await page.keyboard.press('?')
  const help = page.getByRole('dialog', { name: 'Keyboard shortcuts' })
  await expect(help).toBeVisible()
  await expect(help.getByRole('button', { name: 'Close' })).toBeFocused()
  await page.keyboard.press('ArrowRight')
  await page.keyboard.press('Escape')
  await expect(help).toBeHidden()
  await expect(review.getByRole('img', { name: 'b.jpg' })).toBeVisible()

  await review.getByRole('button', { name: 'Compare with best' }).click()
  await expect(review.getByText('Current photo', { exact: true })).toBeVisible()
  await expect(review.getByText('Best · c.jpg', { exact: true })).toBeVisible()
  await page.keyboard.press('b')
  await expect(review.getByText('Selected best', { exact: true })).toBeVisible()
  await page.keyboard.press('4')
  await expect(page.getByRole('button', { name: 'Rate 4 stars' })).toHaveAttribute('aria-pressed', 'true')

  await page.keyboard.press('r')
  const confirm = page.getByRole('dialog', { name: 'Reject the other photos in this burst?' })
  await expect(confirm.getByRole('button', { name: 'Cancel' })).toBeFocused()
  await page.keyboard.press('Escape')
  await expect(confirm).toBeHidden()
  await page.keyboard.press('r')
  await confirm.getByRole('button', { name: 'Flag remaining photos' }).click()
  await expect(page.getByText('Flagged 2 photos in the burst.')).toBeVisible()
  await page.keyboard.press('Shift+U')
  await expect(page.getByText('Removed 2 burst flags.')).toBeVisible()
  await expect(page.getByRole('button', { name: 'Rate 4 stars' })).toHaveAttribute('aria-pressed', 'true')
  await page.keyboard.press('u')
  await expect(page.getByRole('button', { name: 'Rate 4 stars' })).toHaveAttribute('aria-pressed', 'false')

  await page.keyboard.press(']')
  await expect(burstAt(page, '15')).toHaveAttribute('aria-current', 'true')
  await expect(review.getByRole('img', { name: 'd.jpg' })).toBeVisible()
  await page.keyboard.press('[')
  await expect(burstAt(page, '05')).toHaveAttribute('aria-current', 'true')

  await page.getByRole('button', { name: 'Folders' }).click()
  await page.getByText('Score filters', { exact: true }).click()
  await page.getByLabel('Auto-reject', { exact: true }).selectOption('true')
  await page.getByRole('button', { name: 'Apply' }).click()
  await expect(review.getByRole('img', { name: 'white.jpg' })).toBeVisible()
  await expect(page.getByText('Clipped highlights', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: 'Clear auto-reject', exact: true }).click()
  await expect(review.getByText('No matching assets')).toBeVisible()
})

test('375px layout, focus visibility and burst list error recovery', async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 })
  await page.goto('/')
  await page.getByLabel('Library', { exact: true }).selectOption('mobile')
  const shortcuts = page.getByRole('button', { name: 'Keyboard shortcuts' })
  await shortcuts.focus()
  const focus = await shortcuts.evaluate((element) => getComputedStyle(element).outlineStyle)
  expect(focus).toBe('solid')

  await page.route(/\/api\/libraries\/mobile\/bursts(?:\?.*)?$/, (route) =>
    route.fulfill({ status: 503, json: { detail: 'Burst list temporarily unavailable' } }),
  { times: 1 })
  await page.getByRole('button', { name: 'Bursts', exact: true }).click()
  await expect(page.getByRole('alert')).toHaveText('Burst list temporarily unavailable')
  await page.getByRole('button', { name: 'Retry groups' }).click()
  await expect(page.getByText('No bursts yet.')).toBeVisible()
  await page.getByRole('button', { name: 'Scan library', exact: true }).click()
  await burstAt(page, '05').click()
  const review = page.getByRole('main', { name: 'Photo review' })
  await expect(review.getByRole('img', { name: 'a.jpg' })).toBeVisible()
  await review.getByRole('button', { name: 'Compare with best' }).click()
  await expect(review.getByRole('img', { name: 'c.jpg' })).toBeVisible()
  const current = await review.getByText('Current photo', { exact: true }).boundingBox()
  const best = await review.getByText('Best · c.jpg', { exact: true }).boundingBox()
  expect(best?.y).toBeGreaterThan(current?.y ?? 0)
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(375)
})

test('search Japanese and English, browse similar groups, and filter tags on mobile', async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 })
  await page.goto('/')
  await page.getByLabel('Library', { exact: true }).selectOption('semantic')
  const review = page.getByRole('main', { name: 'Photo review' })
  const search = page.getByRole('searchbox', { name: 'Search photos' })
  await expect(search).toBeEnabled()
  const scan = page.getByRole('button', { name: 'Scan library', exact: true })
  await scan.focus()
  await page.keyboard.press('/')
  await expect(search).toBeFocused()
  await search.press('Escape')
  await expect(scan).toBeFocused()
  await page.keyboard.press('/')
  await search.fill('猫')
  await search.press('Enter')
  await expect(review.getByText('No matching photos')).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(search).toBeFocused()

  await scan.click()
  await expect(page.getByLabel('Embedding progress')).toHaveAttribute('value', '6')
  await page.route('**/api/libraries/semantic/search', (route) =>
    route.fulfill({ status: 503, json: { detail: 'Search worker is busy' } }), { times: 1 })
  await search.fill('猫')
  await search.press('Enter')
  await expect(review.getByRole('alert')).toContainText('Search worker is busy')
  await review.getByRole('button', { name: 'Retry search' }).click()
  await expect(review.getByRole('img', { name: 'a.jpg' })).toBeVisible()
  await page.keyboard.press('ArrowRight')
  await expect(review.getByRole('img', { name: 'b.jpg' })).toBeVisible()

  await search.fill('a dog ')
  await search.press('4')
  await expect(search).toHaveValue('a dog 4')
  await expect(page.getByRole('button', { name: 'Rate 4 stars' })).toHaveAttribute('aria-pressed', 'false')
  await search.press('Enter')
  await expect(review.getByRole('img', { name: 'white.jpg' })).toBeVisible()
  await page.getByRole('button', { name: 'Dog / 犬', exact: true }).click()
  await expect(page.getByLabel('Photo tag')).toHaveValue('dog')
  await expect(review.getByRole('img', { name: 'white.jpg' })).toBeVisible()
  await page.getByLabel('Photo tag').selectOption('')
  await expect(review.getByRole('img', { name: 'a.jpg' })).toBeVisible()

  await review.getByRole('button', { name: 'a.jpg', exact: true }).focus()
  await page.keyboard.press('ArrowRight')
  await expect(review.getByRole('img', { name: 'b.jpg' })).toBeVisible()
  await page.getByRole('button', { name: 'Find similar photos' }).click()
  await expect(review.getByRole('heading', { name: 'Similar to b.jpg' })).toBeVisible()
  await expect(review.getByRole('region', { name: 'Source photo' })).toContainText('b.jpg')
  await expect(review.getByRole('img', { name: 'a.jpg' })).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(page.getByRole('button', { name: 'Find similar photos' })).toBeFocused()
  await expect(review.getByRole('img', { name: 'b.jpg' })).toBeVisible()
  await page.getByRole('button', { name: 'Similar', exact: true }).click()
  await page.getByRole('button', { name: 'Similar group a.jpg, 5 photos' }).click()
  await expect(review.getByRole('heading', { name: 'Similar group', exact: true })).toBeVisible()
  await expect(review.getByRole('img', { name: 'a.jpg' })).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(375)
})

test('ML unavailable state leaves the review available', async ({ page }) => {
  const progress = {
    scan: null, worker: null, worker_online: true,
    catalog_revision: 0, stages: { exif: {}, preview: {}, score: {} },
    ml: { enabled: true, search_enabled: true, tags_enabled: true, waiting: false, grouping: false,
      image_error: 'Offline cache is missing. Run photocull fetch-model.',
      text_error: null, tag_error: null, group_error: null },
  }
  await page.route('**/api/libraries/desktop/events', (route) => route.fulfill({
    status: 200, contentType: 'text/event-stream', body: `data: ${JSON.stringify(progress)}\n\n`,
  }))
  await page.goto('/')
  await expect(page.getByRole('searchbox')).toBeDisabled()
  await expect(page.getByText('Offline cache is missing.', { exact: false })).toBeVisible()
  await expect(page.getByRole('main', { name: 'Photo review' })).toBeVisible()
})

test('pending search is cancellable and an offline worker error is announced', async ({ page }) => {
  await page.goto('/')
  await page.getByLabel('Library', { exact: true }).selectOption('semantic')
  const search = page.getByRole('searchbox', { name: 'Search photos' })
  const review = page.getByRole('main', { name: 'Photo review' })
  await expect(search).toBeEnabled()

  let online = true
  const result = /\/api\/libraries\/semantic\/search\/[a-f0-9]{32}$/
  await page.route(result, async (route) => {
    if (route.request().method() === 'DELETE') {
      await route.fulfill({ status: 204 })
    } else if (online) {
      await route.fulfill({ json: { status: 'running', items: [], error: null } })
    } else {
      await route.fulfill({ status: 503, json: { detail: 'The indexing worker is offline. Start it and retry the search.' } })
    }
  })
  await search.fill('猫')
  const queried = page.waitForResponse((response) => result.test(response.url()) && response.request().method() === 'GET')
  await search.press('Enter')
  await queried
  await expect(review.getByText('Searching photos…')).toBeVisible()
  const cancelled = page.waitForRequest((request) => result.test(request.url()) && request.method() === 'DELETE')
  await page.keyboard.press('Escape')
  await cancelled
  await expect(search).toBeFocused()

  await search.press('Enter')
  await expect(review.getByText('Searching photos…')).toBeVisible()
  online = false
  await expect(review.getByRole('alert')).toContainText('The indexing worker is offline.')
  await expect(review.getByRole('button', { name: 'Retry search' })).toBeVisible()
})

test('browse and retry a paginated scan error report on mobile', async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 })
  await page.goto('/')
  await page.getByLabel('Library', { exact: true }).selectOption('scan-errors')
  await page.route('**/api/libraries/scan-errors/scans/1/errors?cursor=0', (route) =>
    route.fulfill({ status: 503, json: { detail: 'Scan report temporarily unavailable' } }),
  { times: 1 })
  const report = page.getByRole('button', { name: '50 unreadable files, 1 unreadable folder' })
  await report.focus()
  await page.keyboard.press('Enter')
  const dialog = page.getByRole('dialog', { name: 'Scan errors' })
  await expect(dialog.getByRole('button', { name: 'Close' })).toBeFocused()
  await expect(dialog.getByRole('alert')).toHaveText('Scan report temporarily unavailable')
  await dialog.getByRole('button', { name: 'Retry' }).click()
  await expect(dialog.getByText('2025/写真-00.NEF')).toBeVisible()
  await expect(dialog.getByText('Permission denied').first()).toBeVisible()
  const previous = dialog.getByRole('button', { name: 'Previous page of scan errors' })
  const next = dialog.getByRole('button', { name: 'Next page of scan errors' })
  await expect(previous).toBeDisabled()
  await next.click()
  await expect(dialog.getByText('closed-folder')).toBeVisible()
  await expect(dialog.getByText('Listing folder · File system error (errno 5)')).toBeVisible()
  await expect(next).toBeDisabled()
  await previous.click()
  await expect(dialog.getByText('2025/写真-00.NEF')).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(375)
  await page.keyboard.press('Escape')
  await expect(dialog).toBeHidden()
  await expect(report).toBeFocused()
})
