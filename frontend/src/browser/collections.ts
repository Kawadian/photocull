import { json, request } from '../api'
import type { FilterValues } from './filterValues'
import type { Language } from './location'

export type Album = {
  id: string
  name: string
  kind: 'manual' | 'smart'
  library: string | null
  filters: Record<string, string | number | boolean> | null
  count?: number
  revision: number
}
export type AssetRef = { library: string; id: number; expected_revision?: number }
export type BatchResult = { library: string; count: number; status: 'done' | 'stale' | 'conflict' | 'busy' | 'tag_limit' }
export const maxSelection = 200
export const albumUrl = '/api/browse/albums'

export const albumFilters = (album: Album | undefined): FilterValues => Object.fromEntries(
  Object.entries(album?.filters ?? {}).map(([key, value]) => [key, String(value)]),
)

export function changeMembers(album: string, assets: AssetRef[], operation: 'add' | 'remove') {
  return request<{ changed: number }>(albumUrl + '/' + album + '/members', json('POST', { assets, operation }))
}

export function archiveUrl(library: string, ids: number[]) {
  return '/?' + new URLSearchParams({ view: 'archive', library, ids: ids.join(',') })
}

export function saveAlbum(name: string, kind: Album['kind'], library?: string, filters?: FilterValues, album?: Album) {
  return request<Album>(album ? albumUrl + '/' + album.id : albumUrl, json(album ? 'PATCH' : 'POST', {
    name, kind, library: library || null, filters: kind === 'smart' ? filters ?? {} : null,
    ...(album && { expected_revision: album.revision }),
  }))
}

const en = {
  albums: 'Albums', timeline: 'Back to timeline', empty: 'No albums yet.', new: 'New album',
  edit: 'Edit album', editFilters: 'Edit saved filters', delete: 'Delete album',
  confirmDelete: (name: string) => 'Delete “' + name + '”? The photos will stay in the timeline.',
  name: 'Album name', type: 'Album type', manual: 'Manual album', smart: 'Smart album',
  smartHelp: (count: number) => 'Photos update as they match the saved filters (' + count + ').',
  count: (count: number) => count + (count === 1 ? ' item' : ' items'), save: 'Save', cancel: 'Cancel',
  close: 'Close', error: 'The operation could not be completed.',
  select: 'Select photos', selection: (count: number) => count + ' selected', done: 'Done selecting',
  limit: 'You can select up to 200 items at a time.', actions: 'Selection actions', action: 'Action',
  add: 'Add to album', remove: 'Remove from this album', apply: 'Apply', rating: 'Set rating',
  favorite: 'Change favorite status', favoriteOn: 'Mark as favorite', favoriteOff: 'Remove favorite',
  tag: 'User tag', addTag: 'Add user tag', removeTag: 'Remove user tag',
  noManual: 'Create a manual album before adding photos.', chooseAlbum: 'Choose an album', export: 'Export selection',
  perLibrary: 'Open the export planner for each library.', finished: 'Selection updated.',
  failedLibraries: 'The selection could not be updated in these libraries.',
  stale: 'Photos changed. Refresh the timeline and try again.',
  conflict: 'An item was rated elsewhere. Close this dialog and select the photos again.', busy: 'Catalog is busy. Try again.',
  tag_limit: 'At least one photo has reached its user tag limit.',
  selectHelp: 'Tap or press Enter to select. Hold Shift to select a range.',
}
export const collectionText: Record<Language, typeof en> = {
  en,
  ja: {
    albums: 'アルバム', timeline: 'タイムラインに戻る', empty: 'アルバムはまだありません。', new: '新規アルバム',
    edit: 'アルバムを編集', editFilters: '保存した条件を編集', delete: 'アルバムを削除',
    confirmDelete: (name) => '「' + name + '」を削除しますか？ 写真はタイムラインに残ります。',
    name: 'アルバム名', type: 'アルバムの種類', manual: '手動アルバム', smart: 'スマートアルバム',
    smartHelp: (count) => '保存した ' + count + ' 件の条件に一致する写真を自動で表示します。',
    count: (count) => count + ' 件', save: '保存', cancel: 'キャンセル', close: '閉じる',
    error: '操作を完了できませんでした。', select: '写真を選択', selection: (count) => count + ' 件を選択中',
    done: '選択を終了', limit: '一度に選択できるのは 200 件までです。', actions: '一括操作', action: '操作',
    add: 'アルバムに追加', remove: 'このアルバムから除外', apply: '実行', rating: '評価を設定',
    favorite: 'お気に入りを変更', favoriteOn: 'お気に入りに追加', favoriteOff: 'お気に入りから削除',
    tag: 'ユーザータグ', addTag: 'ユーザータグを追加', removeTag: 'ユーザータグを削除',
    noManual: '先に手動アルバムを作成してください。', chooseAlbum: 'アルバムを選択', export: '選択項目を書き出す',
    perLibrary: 'ライブラリごとに書き出し画面を開きます。', finished: '選択項目を更新しました。',
    failedLibraries: '一部のライブラリで更新できませんでした。', stale: '写真が変更されました。一覧を更新してください。',
    conflict: '評価が他の画面で変更されました。この画面を閉じ、写真を選び直してください。',
    busy: 'カタログが処理中です。再度お試しください。', tag_limit: 'ユーザータグが上限に達している写真があります。',
    selectHelp: 'タップまたは Enter で選択し、Shift を押しながら操作すると範囲選択できます。',
  },
}
export type CollectionText = typeof en
