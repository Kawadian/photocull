import { useEffect, useState } from 'react'
import type { Asset, StageStatus } from '../api'

export type FaceBox = {
  id: number
  name: string | null
  person_id: number
  left: number
  top: number
  right: number
  bottom: number
}

export type PhotoLabels = {
  favorite: boolean
  user_tags: string[]
}

export type LabelUpdate = {
  favorite?: boolean
  add_tags?: string[]
  remove_tags?: string[]
}

export type ViewerAsset = Asset & PhotoLabels & {
  latitude: number | null
  longitude: number | null
  size: number
  lens: string | null
  shutter_seconds: number | null
  aperture: number | null
  focal_length_mm: number | null
  iso: number | null
  exif_status: StageStatus | null
  faces: FaceBox[]
}

export function constrainedConnection() {
  const connection = (navigator as Navigator & {
    connection?: { saveData?: boolean; effectiveType?: string; downlink?: number }
  }).connection
  if (!connection) return false
  return !!connection.saveData || /2g|3g/.test(connection.effectiveType ?? '')
    || (connection.downlink != null && connection.downlink < 1.25)
}

export function useDerivativeAvailability(url: string, attempt = 0) {
  const key = url + ':' + attempt
  const [response, setResponse] = useState<{ key: string; status: 'ready' | 'error' }>()
  const status = response?.key === key ? response.status : 'pending'

  useEffect(() => {
    if (!url) return
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout>
    async function check() {
      try {
        const pending = await fetch(url, { method: 'HEAD', signal: controller.signal })
        if (controller.signal.aborted) return
        if (pending.status === 202 || pending.status === 503) {
          const seconds = Number(pending.headers.get('Retry-After')) || 2
          timer = setTimeout(check, Math.max(2, Math.min(10, seconds)) * 1_000)
        } else {
          setResponse({ key, status: pending.ok ? 'ready' : 'error' })
        }
      } catch {
        if (!controller.signal.aborted) setResponse({ key, status: 'error' })
      }
    }
    void check()
    return () => { controller.abort(); clearTimeout(timer) }
  }, [key, url])

  return status
}
