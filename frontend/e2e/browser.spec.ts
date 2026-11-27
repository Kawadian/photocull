import { expect, test } from '@playwright/test'

test('opens the cross-library timeline from Review and shares navigation state', async ({ page }, info) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto('/')
  await page.getByRole('link', { name: 'Photo browser' }).click()
  const gallery = page.getByRole('main', { name: 'Photos and videos' })
  await expect(page.getByRole('heading', { level: 1, name: 'Timeline' })).toBeVisible()
  const library = page.getByRole('combobox', { name: 'Library', exact: true })
  await expect(library).toHaveValue('')
  await page.getByRole('combobox', { name: 'Jump to month' }).selectOption('2025-10')
  const first = gallery.getByRole('button', { name: 'Frame-118.jpg · gallery-2025' })
  const second = gallery.getByRole('button', { name: 'Frame-117.jpg · gallery-2025' })
  await expect(first.locator('img')).toHaveJSProperty('complete', true)
  await expect.poll(() => first.locator('img').evaluate((image: HTMLImageElement) => image.naturalWidth)).toBeGreaterThan(0)
  await expect(second).toBeVisible()
  await expect(gallery.getByRole('heading', { name: 'October', exact: true })).toBeVisible()
  await page.screenshot({ path: info.outputPath('desktop-timeline.png') })

  await library.selectOption('gallery-2025')
  await page.getByRole('combobox', { name: 'Jump to month' }).selectOption('2025-10')
  const firstScoped = gallery.getByRole('button', { name: 'Frame-118.jpg', exact: true })
  await firstScoped.focus()
  await page.keyboard.press('ArrowRight')
  await expect(gallery.getByRole('button', { name: 'Frame-117.jpg', exact: true })).toBeFocused()
  await page.keyboard.press('Enter')
  await expect(page.getByRole('dialog', { name: 'Frame-117.jpg' })).toBeVisible()
  await expect(page).toHaveURL(/photo=gallery-2025%3A117/)
  await page.keyboard.press('ArrowLeft')
  await expect(page.getByRole('dialog', { name: 'Frame-118.jpg' })).toBeVisible()
  await page.screenshot({ path: info.outputPath('desktop-preview.png') })
  await page.keyboard.press('Escape')
  await expect(page.getByRole('dialog')).toHaveCount(0)
  await expect(page).not.toHaveURL(/photo=/)
  await expect(gallery.getByRole('button', { name: 'Frame-117.jpg', exact: true })).toBeFocused()

  await page.getByRole('combobox', { name: 'Grid density' }).selectOption('compact')
  await expect(page).toHaveURL(/density=compact/)
  await page.goBack()
  await expect(page.getByRole('combobox', { name: 'Grid density' })).toHaveValue('comfortable')
  await page.goForward()
  await page.reload()
  await expect(library).toHaveValue('gallery-2025')
  await expect(page.getByRole('combobox', { name: 'Jump to month' })).toHaveValue('2025-10')
  await expect(page.getByRole('combobox', { name: 'Grid density' })).toHaveValue('compact')
  await expect(firstScoped).toBeVisible()
  await expect(second).toHaveCount(0)
  await page.setViewportSize({ width: 3840, height: 2160 })
  await expect(firstScoped).toBeVisible()
  await expect(page.locator('body')).toHaveJSProperty('scrollWidth', 3840)
  await page.getByRole('link', { name: 'Review', exact: true }).click()
  await expect(page.getByRole('link', { name: 'Photo browser' })).toBeVisible()
})

test('loads another cursor page while keeping the number of mounted tiles bounded', async ({ page }) => {
  await page.setViewportSize({ width: 1000, height: 760 })
  await page.goto('/?view=browser&library=gallery-2025&lang=en')
  const grid = page.getByRole('main', { name: 'Photos and videos' })
  await expect(grid.getByRole('button', { name: 'Frame-118.jpg' })).toBeVisible()
  await expect(grid.getByRole('button', { name: 'Frame-047.jpg' })).toHaveCount(0)
  const nextPage = page.waitForResponse((response) => response.url().includes('/api/browse/timeline?')
    && response.url().includes('cursor=') && response.ok())
  await grid.evaluate((element) => { element.scrollTop = element.scrollHeight })
  await nextPage
  await expect(grid.getByRole('button', { name: 'Frame-045.jpg' })).toBeVisible()
  await expect(grid.getByRole('button', { name: 'Frame-118.jpg' })).toHaveCount(0)
  expect(await grid.locator('button[aria-haspopup=dialog]').count()).toBeLessThan(36)
  await grid.evaluate((element) => { element.scrollTop = 0 })
  await expect(grid.getByRole('button', { name: 'Frame-118.jpg' })).toBeVisible()
})

test('recovers from catalog and failed preview requests and opens a shared photo URL', async ({ page }) => {
  await page.route('**/api/browse/timeline?*', (route) => route.fulfill({ status: 503 }), { times: 1 })
  await page.goto('/?view=browser&library=gallery-2024&lang=en')
  await expect(page.getByRole('alert')).toContainText('Timeline unavailable')
  await page.getByRole('button', { name: 'Retry' }).click()
  await expect(page.getByRole('button', { name: 'Frame-118.jpg' })).toBeVisible()

  await page.route('**/assets/104/media/preview?*', (route) => route.abort(), { times: 1 })
  await page.goto('/?view=browser&library=gallery-2024&lang=en&photo=gallery-2024%3A104')
  const dialog = page.getByRole('dialog', { name: 'Frame-104.jpg' })
  await expect(dialog.getByText('Preview unavailable.')).toBeVisible()
  await dialog.getByRole('button', { name: 'Retry' }).click()
  await expect.poll(() => dialog.getByRole('img').evaluate((image: HTMLImageElement) => image.naturalWidth))
    .toBeGreaterThan(0)
  await dialog.getByRole('button', { name: 'Close preview' }).click()
  await expect(page).toHaveURL(/view=browser/)
  await expect(page).not.toHaveURL(/photo=/)
  await expect(dialog).toHaveCount(0)
})

test.describe('375 px touch layout', () => {
  test.use({
    viewport: { width: 375, height: 812 },
    isMobile: true,
    hasTouch: true,
    deviceScaleFactor: 2,
    locale: 'ja-JP',
    colorScheme: 'dark',
  })

  test('scrubs to another year by touch and opens a photo in Japanese', async ({ page }, info) => {
    await page.goto('/?view=browser&month=2025-10')
    await expect(page.getByRole('heading', { name: 'タイムライン' })).toBeVisible()
    const gallery = page.getByRole('main', { name: '写真と動画' })
    const first = gallery.getByRole('button', { name: 'Frame-118.jpg · gallery-2025' })
    await expect.poll(() => first.locator('img').evaluate((image: HTMLImageElement) => image.naturalWidth))
      .toBeGreaterThan(0)
    await expect(page.locator('body')).toHaveJSProperty('scrollWidth', 375)
    const slider = page.getByRole('slider', { name: 'タイムラインスクラバー' })
    await expect(slider).toBeVisible()
    await page.screenshot({ path: info.outputPath('mobile-timeline.png') })

    await first.tap()
    const dialog = page.getByRole('dialog', { name: 'Frame-118.jpg' })
    await expect.poll(() => dialog.getByRole('img').evaluate((image: HTMLImageElement) => image.naturalWidth))
      .toBeGreaterThan(0)
    await page.screenshot({ path: info.outputPath('mobile-preview.png') })
    await dialog.getByRole('button', { name: 'プレビューを閉じる' }).tap()
    await expect(dialog).toHaveCount(0)

    const dimensions = (await slider.boundingBox())!
    const client = await page.context().newCDPSession(page)
    const x = dimensions.x + dimensions.width / 2
    const y = dimensions.y + 9
    await client.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x, y }] })
    for (const progress of [.25, .55, .8, .99]) {
      await client.send('Input.dispatchTouchEvent', {
        type: 'touchMove', touchPoints: [{ x, y: y + (dimensions.height - 18) * progress }],
      })
    }
    await client.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] })
    await expect(page).toHaveURL(/month=2024-05/)
    await expect(gallery.getByRole('button', { name: 'Frame-118.jpg · gallery-2024' })).toBeVisible()
  })
})
