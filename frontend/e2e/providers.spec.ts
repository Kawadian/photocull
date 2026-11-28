import { expect, test } from '@playwright/test'

test('choose independent providers, test the connection, and scope reanalysis', async ({ page }) => {
  let selection = { tags: 'local', flags: 'local' }
  await page.route('**/api/libraries/*/providers', async (route) => {
    if (route.request().method() === 'PUT') {
      selection = route.request().postDataJSON() as typeof selection
    }
    await route.fulfill({ json: { selection, pending: 0 } })
  })
  await page.route('**/providers/test', (route) =>
    route.fulfill({ json: { ok: true, endpoint: 'https://***' } }))
  await page.route('**/providers/reanalyze', async (route) => {
    expect(route.request().postDataJSON()).toEqual({
      kind: 'tags', scope: 'local', start: '2025-01-01', end: null,
    })
    await route.fulfill({ status: 202, json: { queued: 3 } })
  })

  await page.setViewportSize({ width: 375, height: 812 })
  await page.goto('/')
  await page.getByRole('button', { name: 'Decision providers', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: /^Decision providers/ })
  await dialog.getByRole('combobox', { name: 'Automatic tags', exact: true }).selectOption('openai')
  await expect(dialog.getByRole('combobox', { name: 'Scores and auto-reject', exact: true })).toHaveValue('local')
  await expect(dialog.getByRole('button', { name: 'Queue reanalysis' })).toBeDisabled()
  await dialog.getByRole('button', { name: 'Save provider choices' }).click()
  await expect(dialog.getByRole('status')).toHaveText('Provider choices saved for this library.')
  await dialog.getByRole('button', { name: 'Test connection' }).click()
  await expect(dialog.getByRole('status')).toHaveText('Connection succeeded · https://***')

  await dialog.getByRole('combobox', { name: 'Decisions', exact: true }).selectOption('tags')
  await dialog.getByRole('combobox', { name: 'Include photos with', exact: true }).selectOption('local')
  await dialog.getByLabel('From', { exact: true }).fill('2025-01-01')
  await dialog.getByRole('button', { name: 'Queue reanalysis' }).click()
  await expect(dialog.getByRole('status')).toHaveText('3 automatic decisions queued.')
  await page.keyboard.press('Escape')
  await expect(dialog).toBeHidden()
})
