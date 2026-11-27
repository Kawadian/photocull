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
  await expect.poll(() => first.locator('img').evaluate((image: HTMLImageElement) => image.currentSrc))
    .toMatch(/\/media\/grid-512\.avif\//)
  await expect(first.locator('canvas')).toHaveCount(1)
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

  await page.route('**/assets/104/media/fit-*/**', (route) => route.fulfill({ status: 202 }))
  await page.route('**/assets/104/media/preview?*', (route) => route.abort(), { times: 1 })
  await page.goto('/?view=browser&library=gallery-2024&lang=en&photo=gallery-2024%3A104')
  const dialog = page.getByRole('dialog', { name: 'Frame-104.jpg' })
  await expect(dialog.getByText('Preview unavailable.')).toBeVisible()
  await dialog.getByRole('button', { name: 'Retry' }).click()
  await expect.poll(() => dialog.getByRole('img').evaluate((image: HTMLImageElement) => image.naturalWidth))
    .toBeGreaterThan(0)
  await dialog.getByRole('button', { name: 'Share or download' }).click()
  const delivery = page.getByRole('dialog', { name: 'Share or download' })
  const original = delivery.getByRole('link', { name: 'Download original' })
  await expect(original).toHaveAccessibleDescription(/May contain location/)
  const downloadEvent = page.waitForEvent('download')
  await original.click()
  expect((await downloadEvent).suggestedFilename()).toBe('Frame-104.jpg')
  await page.keyboard.press('Escape')
  await expect(delivery).toHaveCount(0)
  await dialog.getByRole('button', { name: 'Close preview' }).click()
  await expect(page).toHaveURL(/view=browser/)
  await expect(page).not.toHaveURL(/photo=/)
  await expect(dialog).toHaveCount(0)
})

test('prepares a video from its poster and switches proxy quality', async ({ page, request }, info) => {
  await request.post('/api/libraries/insights/scans')
  await expect.poll(async () => {
    const response = await request.get('/api/libraries/insights/assets?kind=video')
    const { items } = await response.json()
    return items[0]?.preview_status
  }, { timeout: 15_000 }).toBe('done')
  await page.route('**/api/libraries/insights/insights/assets/*/video', (route) =>
    route.fulfill({ json: { video: { data: { highlights: [{ start: 2.5, end: 4 }] } } } }))
  await page.goto('/?view=browser&library=insights&lang=en')
  await page.getByRole('button', { name: 'walk.mov · Video', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: 'walk.mov' })
  await expect(dialog.getByRole('img')).toBeVisible()
  await dialog.getByRole('button', { name: 'Prepare playback' }).click()
  const video = dialog.locator('video')
  await expect(video).toHaveAttribute('src', /video-480\.mp4/, { timeout: 15_000 })
  await expect.poll(() => video.evaluate((element) => (element as HTMLVideoElement).readyState))
    .toBeGreaterThan(0)
  await dialog.getByRole('group', { name: 'Video highlights' }).getByRole('button', { name: '0:02' }).click()
  await expect.poll(() => video.evaluate((element) => (element as HTMLVideoElement).currentTime)).toBeCloseTo(2.5)
  await page.screenshot({ path: info.outputPath('desktop-video.png') })
  await dialog.getByRole('combobox', { name: 'Video quality' }).selectOption('720')
  await expect(video).toHaveAttribute('src', /video-720\.mp4/, { timeout: 15_000 })
  await expect.poll(() => video.evaluate((element) => (element as HTMLVideoElement).currentTime)).toBeCloseTo(2.5)
})

test('opens photo information, draws face boxes, rates, zooms and runs a slideshow', async ({ page }, info) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.route('**/gallery-2025/assets/118?details=browser', async (route) => {
    const response = await route.fetch()
    const source = await response.json()
    await route.fulfill({ json: {
      ...source,
      faces: [{ id: 2, name: 'Mika', person_id: 1, left: .33, top: .25, right: .48, bottom: .52 }],
    } })
  })
  await page.goto('/?view=browser&library=gallery-2025&lang=en')
  await page.getByRole('button', { name: 'Frame-118.jpg', exact: true }).click()
  const viewer = page.getByRole('dialog', { name: 'Frame-118.jpg' })
  await expect(viewer.getByRole('img', { name: 'Frame-118.jpg' })).toBeVisible()
  await page.keyboard.press('i')
  const panel = viewer.getByRole('complementary', { name: 'Photo information' })
  await expect(panel.getByText('35 mm Prime')).toBeVisible()
  await expect(panel.getByText('1/250 s')).toBeVisible()
  await expect(panel.getByText('35.68000, 139.76000')).toBeVisible()
  await panel.getByRole('button', { name: 'Face boxes' }).click()
  await expect(viewer.locator('.pcb-face-box')).toHaveCount(1)
  await page.keyboard.press('4')
  await expect(panel.getByRole('button', { name: 'Rate 4 stars' })).toHaveAttribute('aria-pressed', 'true')
  await page.screenshot({ path: info.outputPath('desktop-information.png') })

  await page.keyboard.press('f')
  await expect(viewer.getByRole('button', { name: 'Exit fullscreen' })).toHaveAttribute('aria-pressed', 'true')
  await page.keyboard.press('f')
  await expect(viewer.getByRole('button', { name: 'Fullscreen', exact: true })).toHaveAttribute('aria-pressed', 'false')
  await viewer.getByRole('button', { name: 'Toggle 100% zoom' }).click()
  await expect(viewer.locator('.pcb-photo-native')).toHaveAttribute('data-loaded', 'true', { timeout: 15_000 })
  await expect(viewer.locator('.pcb-photo-viewport')).toHaveAttribute('data-zoomed', 'true')
  await viewer.getByRole('button', { name: 'Toggle 100% zoom' }).click()
  await expect(viewer.locator('.pcb-photo-viewport')).not.toHaveAttribute('data-zoomed', 'true')

  await viewer.getByText('Slideshow options', { exact: true }).click()
  await viewer.getByLabel('Interval').selectOption('2')
  await viewer.getByText('Slideshow options', { exact: true }).click()
  await viewer.getByRole('button', { name: 'Play slideshow' }).click()
  const next = page.getByRole('dialog', { name: 'Frame-117.jpg' })
  await expect(next).toBeVisible()
  await next.getByRole('button', { name: 'Pause slideshow' }).click()
  await page.keyboard.press('Escape')
  await expect(next).toHaveCount(0)
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

  test('swipes, pinches and double taps with native loading held on a slow link', async ({ page }, info) => {
    await page.addInitScript(() => {
      Object.defineProperty(navigator, 'connection', { value: { effectiveType: '3g', saveData: true } })
    })
    let nativeRequests = 0
    page.on('request', (request) => { if (request.url().includes('/native.jpg/')) nativeRequests++ })
    await page.goto('/?view=browser&library=gallery-2025')
    await page.getByRole('button', { name: 'Frame-118.jpg', exact: true }).tap()
    const stage = page.locator('.pcb-photo-viewport')
    await expect(stage).toBeVisible()
    const bounds = (await stage.boundingBox())!
    const y = bounds.y + bounds.height / 2
    const middle = bounds.x + bounds.width / 2
    const client = await page.context().newCDPSession(page)
    await client.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x: middle + 95, y }] })
    await client.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [{ x: middle - 95, y }] })
    await client.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] })
    const viewer = page.getByRole('dialog', { name: 'Frame-117.jpg' })
    await expect(viewer).toBeVisible()

    await client.send('Input.dispatchTouchEvent', {
      type: 'touchStart', touchPoints: [{ x: middle - 25, y, id: 0 }, { x: middle + 25, y, id: 1 }],
    })
    await client.send('Input.dispatchTouchEvent', {
      type: 'touchMove', touchPoints: [{ x: middle - 90, y, id: 0 }, { x: middle + 90, y, id: 1 }],
    })
    await client.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] })
    await expect(stage).toHaveAttribute('data-zoomed', 'true')
    expect(nativeRequests).toBe(0)
    await page.touchscreen.tap(middle, y)
    await page.touchscreen.tap(middle, y)
    await expect(stage).not.toHaveAttribute('data-zoomed', 'true')
    await viewer.getByRole('button', { name: '写真情報' }).tap()
    await expect(viewer.getByRole('complementary', { name: '写真情報' })).toBeVisible()
    await page.screenshot({ path: info.outputPath('mobile-information.png') })
  })
})

test('pins share and download links to the selected location policy', async ({ page }, info) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto('/?view=browser&library=gallery-2025&lang=en&photo=gallery-2025%3A118')
  const viewer = page.getByRole('dialog', { name: 'Frame-118.jpg' })
  const opener = viewer.getByRole('button', { name: 'Share or download' })
  await opener.click()
  const dialog = page.getByRole('dialog', { name: 'Share or download' })
  const exclude = dialog.getByRole('radio', { name: 'Exclude location information' })
  const include = dialog.getByRole('radio', { name: 'Include location information' })
  await expect(exclude).toBeChecked()
  await include.check()
  await dialog.getByRole('button', { name: 'Prepare link' }).click()
  const link = dialog.getByRole('textbox', { name: 'Share link' })
  await expect(link).toHaveValue(/action=share&include_location=true$/, { timeout: 15_000 })
  await page.screenshot({ path: info.outputPath('desktop-share-location.png') })
  await dialog.getByRole('combobox', { name: 'Action' }).selectOption('download')
  await expect(link).toHaveCount(0)
  await exclude.check()
  await dialog.getByRole('button', { name: 'Prepare download' }).click()
  const download = dialog.getByRole('link', { name: 'Download a copy' })
  await expect(download).toHaveAttribute('href', /action=download&include_location=false$/, { timeout: 15_000 })
  await page.keyboard.press('Escape')
  await expect(viewer).toBeVisible()
  await expect(opener).toBeFocused()
})

test.describe('location choice on a 375 px touch device', () => {
  test.use({ viewport: { width: 375, height: 812 }, isMobile: true, hasTouch: true,
    deviceScaleFactor: 2, locale: 'ja-JP', colorScheme: 'dark' })

  test('uses the server default and shows the original warning in Japanese', async ({ page }, info) => {
    await page.route('**/api/browse/timeline?*', async (route) => {
      const response = await route.fetch()
      const data = await response.json()
      await route.fulfill({ json: { ...data, media: { ...data.media, strip_location: false } } })
    })
    await page.goto('/?view=browser&library=gallery-2025&photo=gallery-2025%3A118')
    await page.getByRole('button', { name: '共有・ダウンロード', exact: true }).tap()
    const dialog = page.getByRole('dialog', { name: '共有・ダウンロード', exact: true })
    await expect(dialog.getByRole('radio', { name: '位置情報を含める', exact: true })).toBeChecked()
    await expect(dialog.getByRole('link', { name: '原本をダウンロード' }))
      .toHaveAccessibleDescription(/原本には位置情報/)
    await dialog.getByRole('radio', { name: '位置情報を含めない', exact: true }).tap()
    await expect(dialog.getByRole('radio', { name: '位置情報を含めない', exact: true })).toBeChecked()
    await expect(page.locator('body')).toHaveJSProperty('scrollWidth', 375)
    await page.screenshot({ path: info.outputPath('mobile-share-location.png') })
  })
})
