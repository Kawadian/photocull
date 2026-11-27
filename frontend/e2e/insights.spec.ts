import { expect, test } from '@playwright/test'

test('people and stories lead back to keyboard review on a 375px screen', async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 })
  await page.goto('/')
  await page.getByLabel('Library', { exact: true }).selectOption('insights')
  await page.getByRole('button', { name: 'Scan library' }).click()
  const nav = page.getByRole('navigation', { name: 'Library views' })
  await page.keyboard.press('i')
  await expect(page.getByRole('heading', { name: 'People', exact: true })).toBeFocused()
  await page.getByRole('button', { name: /Person \d+ 5 photos/ }).click()
  await page.getByLabel('Person name').fill('Mika')
  await page.getByRole('button', { name: 'Save name' }).click()
  await page.getByRole('button', { name: 'Review photos of Mika' }).click()
  await expect(page.getByRole('button', { name: 'Clear insight filter' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'a.jpg', exact: true })).toBeVisible()
  await page.keyboard.press('ArrowRight')
  await expect(page.getByRole('button', { name: 'b.jpg', exact: true })).toHaveAttribute('aria-current', 'true')
  await nav.getByRole('button', { name: 'Stories' }).click()
  await page.getByRole('button', { name: /2025-02-03/ }).click()
  await expect(page.getByRole('heading', { name: '2025-02-03' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Review this day' })).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(375)
  await page.keyboard.press('Escape')
  await expect(nav.getByRole('button', { name: 'Review' })).toHaveAttribute('aria-current', 'page')
})

test('video review, recommendations and portable downloads use the indexed library', async ({ page }, info) => {
  await page.goto('/')
  await page.getByLabel('Library', { exact: true }).selectOption('insights')
  await page.getByRole('button', { name: 'Scan library' }).click()
  const nav = page.getByRole('navigation', { name: 'Library views' })
  await nav.getByRole('button', { name: 'Videos' }).click()
  await expect(page.getByRole('heading', { name: 'walk.mov' })).toBeVisible()
  const video = page.locator('video')
  await expect(video).toHaveAttribute('src', /\/video\/preview/)
  await page.getByRole('button', { name: /Play 0:00/ }).click()
  await expect.poll(() => video.evaluate((player) => (player as HTMLVideoElement).currentTime))
    .toBeGreaterThan(0.05)
  await video.evaluate((player) => (player as HTMLVideoElement).pause())
  await page.getByRole('button', { name: 'Flag as dead clip' }).click()
  await expect(page.getByText('Flagged as a dead clip')).toBeVisible()
  await page.getByRole('button', { name: 'Restore automatic flag' }).click()
  await expect(page.getByText('No dead-clip flag')).toBeVisible()
  await expect(page.getByText('Local transcription is disabled.')).toBeVisible()

  await nav.getByRole('button', { name: 'Recommendations' }).click()
  await expect(page.getByText(/More rated photos are needed/)).toBeVisible()
  await page.getByRole('button', { name: 'Review by aesthetic score' }).click()
  await expect(page.getByText(/Aesthetic score \d+%/)).toBeVisible()

  await nav.getByRole('button', { name: 'Exports' }).click()
  await page.getByLabel('Minimum rating').selectOption('0')
  await expect(page.getByRole('combobox', { name: 'Location information' })).toHaveValue('false')
  await page.getByRole('button', { name: 'Preview export' }).click()
  const preview = page.getByRole('region', { name: 'Export preview' })
  await expect(preview).toContainText('Export removes source location information.')
  await page.screenshot({ path: info.outputPath('desktop-keeper-preview.png') })
  await preview.getByRole('button', { name: 'Start export' }).click()
  const zip = page.getByRole('link', { name: 'Download ZIP' }).first()
  await expect(zip).toBeVisible({ timeout: 20_000 })
  const zipEvent = page.waitForEvent('download')
  await zip.click()
  expect((await zipEvent).suggestedFilename()).toBe('photocull-keepers.zip')

  await nav.getByRole('button', { name: 'Trip reports' }).click()
  await expect(page.getByRole('combobox', { name: 'Location information' })).toHaveValue('false')
  await page.getByLabel('From date').fill('2025-02-03')
  await page.getByLabel('To date').fill('2025-02-03')
  await page.getByRole('button', { name: 'Generate HTML report' }).click()
  const html = page.getByRole('link', { name: 'Download HTML' })
  await expect(html).toBeVisible()
  const htmlEvent = page.waitForEvent('download')
  await html.click()
  expect((await htmlEvent).suggestedFilename()).toBe('photocull-trip.html')
})

test('insight views expose disabled, loading, empty and recoverable errors on mobile', async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 })
  const capabilities = {
    faces: false, story: false, video: false, transcript: false, aesthetic: false, watch: false,
    export: false, report: false, destinations: [],
    taste: { status: 'disabled', positives: 0, negatives: 0, error: null }, stages: {},
  }
  const viewData = [
    ['People', 'faces', '**/insights/people?*', 'No faces found in this library.'],
    ['Stories', 'story', '**/insights/stories?*', 'No day stories yet. Scan a library to create them.'],
    ['Videos', 'video', '**/assets?kind=video&*', 'No videos in this library.'],
    ['Recommendations', 'taste', '**/assets?sort=taste&*', 'No recommendations available.'],
    ['Exports', 'export', '**/insights/transfers?kind=export', 'No exports yet.'],
    ['Trip reports', 'report', '**/insights/transfers?kind=report', 'No trip reports generated yet.'],
  ] as const
  await page.route('**/insights', (route) => route.fulfill({ json: capabilities }))
  await page.goto('/')
  const nav = page.getByRole('navigation', { name: 'Library views' })
  const main = page.getByRole('main', { name: /People|Stories|Videos|Recommendations|Exports|Trip reports/ })
  for (const [name] of viewData) {
    await nav.getByRole('button', { name, exact: true }).click()
    await expect(main.getByRole('heading', { name, exact: true })).toBeFocused()
    await expect(main.getByText(`${name} are disabled in the library configuration.`)).toBeVisible()
    await page.keyboard.press('Escape')
    await expect(nav.getByRole('button', { name: 'Review' })).toHaveAttribute('aria-current', 'page')
  }

  for (const [name, setting, url, empty] of viewData) {
    if (setting === 'taste') capabilities.taste.status = 'ready'
    else capabilities[setting] = true
    let failRequest = true
    let release: () => void = () => {}
    const delayedError = new Promise<void>((resolve) => { release = resolve })
    await page.route(url, async (route) => {
      if (failRequest) {
        await delayedError
        await route.fulfill({ status: 503, json: { detail: 'Library results temporarily unavailable' } })
      } else {
        await route.fulfill({ json: { items: [], next_cursor: null } })
      }
    })
    await page.reload()
    await nav.getByRole('button', { name, exact: true }).click()
    await expect(main.getByText('Loading…', { exact: true }).first()).toBeVisible()
    release()
    await expect(main.getByRole('alert')).toHaveText(/Library results temporarily unavailable/)
    failRequest = false
    await main.getByRole('button', { name: 'Retry', exact: true }).focus()
    await page.keyboard.press('Enter')
    await expect(main.getByText(empty, { exact: true })).toBeVisible()
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(375)
    await page.unroute(url)
  }
})

test('mobile export reports validation errors and allows a queued transfer to be cancelled', async ({ page }, info) => {
  await page.setViewportSize({ width: 375, height: 812 })
  await page.goto('/')
  const nav = page.getByRole('navigation', { name: 'Library views' })
  await nav.getByRole('button', { name: 'Exports' }).click()
  await page.getByLabel('From date').fill('2025-02-04')
  await page.getByLabel('To date').fill('2025-02-03')
  await page.getByRole('button', { name: 'Preview export' }).click()
  await expect(page.getByRole('alert')).toContainText('End date must be on or after the start date')

  const id = 'a'.repeat(32)
  let status = 'pending'
  await page.route('**/insights/transfers?kind=export', (route) => route.fulfill({ json: { items: [{
    id, kind: 'zip', status, destination: null, error: null, processed: 0, total: 0,
    total_bytes: 0, written_bytes: 0, skipped: 1, cancellable: status === 'pending',
  }] } }))
  await page.route(`**/insights/transfers/${id}/skips?*`, (route) => route.fulfill({ json: {
    items: [{ path: 'sample.png', reason: 'Metadata-only location removal is unavailable for .png' }], next_cursor: null,
  } }))
  await page.route(`**/insights/transfers/${id}`, (route) => {
    status = 'cancelled'
    return route.fulfill({ json: { id, status } })
  })
  await page.getByRole('button', { name: 'Retry', exact: true }).focus()
  await page.keyboard.press('Enter')
  await page.getByText('1 skipped', { exact: true }).click()
  await expect(page.getByText('sample.png')).toBeVisible()
  await expect(page.getByText(/location removal is unavailable for .png/)).toBeVisible()
  await page.screenshot({ path: info.outputPath('mobile-keeper-skips.png') })
  const cancel = page.getByRole('button', { name: 'Cancel', exact: true })
  await cancel.focus()
  await page.keyboard.press('Enter')
  await expect(page.getByText('cancelled', { exact: true })).toBeVisible()
})

test('video copies stay together and a review hint can be overridden', async ({ page }) => {
  const paths = ['event/walk.mov', 'event/other.mov', 'event/walk_stabilized.mp4',
    'event/walk_2025-02-03.mp4', 'other/walk_stabilized.mp4']
  await page.route('**/assets?kind=video&*', (route) => route.fulfill({
    json: { items: paths.map((path, i) => ({ id: i + 1, path, content_hash: 'fixture' })), next_cursor: null },
  }))
  let override: boolean | null = null
  await page.route('**/insights/assets/*/video', (route) => {
    if (route.request().method() === 'PATCH') {
      override = route.request().postDataJSON().dead
      return route.fulfill({ json: { override } })
    }
    return route.fulfill({ json: {
      override, video: { status: 'done', data: {
        dead: false, reasons: ['mostly_dark'], duration: 10, highlights: [], preview: false,
      } },
    } })
  })
  await page.goto('/')
  await page.getByLabel('Library', { exact: true }).selectOption('insights')
  await page.getByRole('button', { name: 'Videos', exact: true }).click()
  const copies = page.getByRole('group', { name: 'Versions of walk.mov' })
  await expect(copies.getByRole('button')).toHaveCount(3)
  await expect(copies.getByRole('button', { name: 'walk.mov' })).toHaveAttribute('aria-current', 'true')
  await page.keyboard.press('ArrowRight')
  await expect(copies.getByRole('button', { name: 'walk_stabilized.mp4' })).toHaveAttribute('aria-current', 'true')
  await expect(page.getByText('Review suggested · Mostly dark')).toBeVisible()
  await page.getByRole('button', { name: 'Flag as dead clip' }).click()
  await expect(page.getByText('Flagged as a dead clip · Mostly dark')).toBeVisible()
  await page.getByRole('button', { name: 'Restore automatic flag' }).click()
  await expect(page.getByText('Review suggested · Mostly dark')).toBeVisible()
})
