import { lazy, StrictMode, Suspense } from 'react'
import { createRoot } from 'react-dom/client'
import './styles.css'

const view = new URLSearchParams(window.location.search).get('view')
const screens = {
  browser: lazy(() => import('./browser/Browser')),
  settings: lazy(() => import('./Settings')),
  review: lazy(() => import('./App').then(({ App }) => ({ default: App }))),
}
const Screen = view === 'browser' || view === 'settings' ? screens[view] : screens.review

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <Suspense>
      <Screen />
    </Suspense>
  </StrictMode>,
)
