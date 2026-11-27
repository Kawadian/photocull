import { lazy, StrictMode, Suspense } from 'react'
import { createRoot } from 'react-dom/client'
import './styles.css'

const browser = new URLSearchParams(window.location.search).get('view') === 'browser'
const Screen = browser
  ? lazy(() => import('./browser/Browser'))
  : lazy(() => import('./App').then(({ App }) => ({ default: App })))

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <Suspense>
      <Screen />
    </Suspense>
  </StrictMode>,
)
