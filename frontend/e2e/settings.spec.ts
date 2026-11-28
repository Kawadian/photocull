import { expect, test } from '@playwright/test'

test('searches advanced settings, previews, saves, and restores a previous revision', async ({ page }) => {
  await page.goto('/')
  await page.getByRole('link', { name: '設定' }).click()
  await expect(page.getByRole('heading', { name: '設定', exact: true })).toBeVisible()
  const search = page.getByRole('searchbox', { name: '設定を検索' })
  await search.fill('worker.scan_batch_size')
  await expect(page.getByRole('heading', { name: '検索結果（1 件）' })).toBeVisible()
  const batches = page.getByRole('spinbutton', { name: 'スキャンのバッチ件数' })
  await expect(batches).toHaveValue('64')
  await batches.fill('32')
  await expect(page.getByText('未保存の変更 1 件')).toBeVisible()
  await page.getByRole('button', { name: '差分を確認' }).click()
  const diff = page.getByRole('dialog', { name: '保存前の差分' })
  await expect(diff).toContainText('64')
  await expect(diff).toContainText('32')
  await diff.getByRole('button', { name: '設定を保存' }).click()
  await expect(page.getByText('設定を保存しました。')).toBeVisible()
  await expect(page.getByText('再起動待ちの設定 1 件')).toBeVisible()
  const banner = page.getByRole('region', { name: '設定の適用' })
  await expect(banner).toContainText('再起動が必要な変更があります')
  await expect(banner).toContainText('docker compose restart worker api')
  await expect(banner.getByRole('button', { name: '適用（再起動）' })).toHaveCount(0)
  await page.reload()
  await search.fill('worker.scan_batch_size')
  await expect(batches).toHaveValue('32')

  await page.getByRole('button', { name: '変更履歴' }).click()
  await page.getByRole('button', { name: '復元差分を確認' }).first().click()
  const restore = page.getByRole('dialog', { name: '復元の差分' })
  await expect(restore).toContainText('64')
  await restore.getByRole('button', { name: 'この版を復元' }).click()
  await expect(page.getByText('以前の版を復元しました。')).toBeVisible()
  await expect(batches).toHaveValue('64')
})

test('uses folder and file pickers at mobile width', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/?view=settings')
  await expect(page.getByRole('combobox', { name: '設定カテゴリ' })).toBeVisible()
  const search = page.getByRole('searchbox', { name: '設定を検索' })

  await search.fill('model_dir')
  await page.getByRole('button', { name: 'モデルキャッシュを参照' }).click()
  const folders = page.getByRole('dialog', { name: 'フォルダを選択' })
  await folders.getByRole('button', { name: /settings-pick$/ }).click()
  await folders.getByRole('button', { name: 'nested' }).click()
  await expect(folders.getByRole('navigation', { name: 'パスのパンくず' })).toContainText('nested')
  await folders.getByRole('button', { name: 'パスを検証' }).click()
  await expect(folders.getByText(/空き容量/)).toBeVisible()
  await folders.getByRole('button', { name: 'このパスを選択' }).click()
  await expect(page.getByRole('textbox', { name: 'モデルキャッシュ', exact: true })).toHaveValue(/\/settings-pick\/nested$/)

  await search.fill('story_geonames')
  await page.getByRole('button', { name: '地名データファイルを参照' }).click()
  const files = page.getByRole('dialog', { name: 'ファイルを選択' })
  await files.getByRole('button', { name: /settings-pick$/ }).click()
  await files.getByRole('button', { name: 'places.txt' }).click()
  await files.getByRole('button', { name: 'このパスを選択' }).click()
  await expect(page.getByRole('textbox', { name: '地名データファイル', exact: true })).toHaveValue(/\/settings-pick\/places.txt$/)
  await expect(page.getByText('未保存の変更 2 件')).toBeVisible()
  await page.getByRole('button', { name: '取り消し' }).click()
  await expect(page.getByRole('textbox', { name: '地名データファイル', exact: true })).toHaveValue('')
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390)
})


test('applies saved settings and waits for the API to reconnect', async ({ page }) => {
  let revision = ''
  let finished = false
  let polls = 0
  const services = ['worker', 'api']
  const result = {
    request: { id: '1'.repeat(32), revision: '', requested_at: 1, services },
    status: 'queued',
    restarted: [],
  }

  await page.route('**/api/settings/restart', async (route) => {
    if (route.request().method() === 'POST') {
      expect(route.request().postDataJSON()).toEqual({ revision })
      result.request.revision = revision
      await route.fulfill({
        status: 202,
        json: { services, pending_fields: ['worker.scan_batch_size'], automatic: true, busy: true, result },
      })
      return
    }
    polls += 1
    if (polls === 1) {
      await route.abort()
      return
    }
    finished = true
    await route.fulfill({
      json: { services: [], pending_fields: [], automatic: false, busy: false,
        result: { ...result, status: 'succeeded', restarted: services } },
    })
  })
  await page.route((url) => url.pathname === '/api/settings', async (route) => {
    const response = await route.fetch()
    const data = await response.json()
    if (route.request().method() === 'PUT') {
      revision = data.revision
      data.restart.automatic = true
    }
    if (finished) {
      data.restart = { services: [], pending_fields: [], automatic: false, busy: false,
        result: { ...result, status: 'succeeded', restarted: services } }
    }
    await route.fulfill({ response, json: data })
  })

  await page.goto('/?view=settings')
  await page.getByRole('searchbox', { name: '設定を検索' }).fill('vlm.openai.concurrency')
  await expect(page.getByText('即時反映（次の処理から）')).toBeVisible()
  await page.getByRole('searchbox', { name: '設定を検索' }).fill('worker.scan_batch_size')
  await page.getByRole('spinbutton', { name: 'スキャンのバッチ件数' }).fill('28')
  await page.getByRole('button', { name: '差分を確認' }).click()
  await page.getByRole('button', { name: '設定を保存' }).click()

  const banner = page.getByRole('region', { name: '設定の適用' })
  await expect(banner).toContainText('再起動が必要な変更があります')
  const apply = banner.getByRole('button', { name: '適用（再起動）' })
  await apply.click()
  await expect(apply).toBeDisabled()
  await expect(banner.getByText('API への再接続を待っています…')).toBeVisible()
  await expect(banner.getByText('設定の適用が完了しました。')).toBeVisible()
  await expect(banner.getByText('再起動が必要な変更があります')).toHaveCount(0)
})
