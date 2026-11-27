import { decode } from 'blurhash'
import { useEffect, useRef, useState } from 'react'
import { mediaUrl } from '../api'
import type { Asset } from '../api'
import { libraryUrl } from './timeline'

type Source = Pick<Asset, 'id' | 'content_hash' | 'width' | 'height'>
export type BrowserMedia = {
  recipe: string | null
  original_download: boolean
  original_streaming: boolean
  strip_location: boolean
}

export function derivativeUrl(base: string, asset: Source, variant: string, recipe: string) {
  return base + '/assets/' + asset.id + '/media/' + variant + '/' + asset.content_hash + '-' + recipe
}

function candidates(base: string, asset: Source, size: 'grid' | 'fit', type: string, recipe: string) {
  const variants = size === 'grid' ? [256, 512] : [1280, 1920, 2560]
  const width = asset.width || 1
  const edge = Math.max(width, asset.height || 1)
  const known = asset.width && asset.height
  const sources = new Map<number, string>()
  for (const limit of variants) {
    const pixels = known ? Math.max(1, Math.round(width * Math.min(1, limit / edge))) : limit
    if (!sources.has(pixels)) {
      sources.set(pixels, derivativeUrl(base, asset, size + '-' + limit + '.' + type, recipe))
    }
  }
  return [...sources].map(([pixels, url]) => url + ' ' + pixels + 'w').join(', ')
}

export function Blurhash({ value }: { value: string }) {
  const canvas = useRef<HTMLCanvasElement>(null)
  useEffect(() => {
    const context = canvas.current?.getContext('2d')
    if (!context) return
    try {
      context.putImageData(new ImageData(new Uint8ClampedArray(decode(value, 16, 16)), 16, 16), 0, 0)
    } catch {
      context.clearRect(0, 0, 16, 16)
    }
  }, [value])
  return <canvas className="pcb-blurhash" ref={canvas} width={16} height={16} aria-hidden="true" />
}

export function ResponsiveImage({ library, asset, recipe, size, sizes, alt, priority, onError, onLoad }: {
  library: string
  asset: Source
  recipe?: string | null
  size: 'grid' | 'fit'
  sizes: string
  alt: string
  priority?: boolean
  onError: () => void
  onLoad?: () => void
}) {
  const base = libraryUrl(library)
  const legacy = mediaUrl(base, asset, size === 'grid' ? 'thumb' : 'preview')
  const [fallback, setFallback] = useState('')
  const modern = !!recipe && fallback !== legacy && /^[a-f0-9]{64}$/.test(asset.content_hash)
  const first = size === 'grid' ? 256 : 1280
  const legacyProps = {
    alt,
    loading: size === 'fit' || priority ? 'eager' as const : 'lazy' as const,
    decoding: 'async' as const,
    fetchPriority: priority ? 'high' as const : 'auto' as const,
    draggable: false,
    onLoad,
  }

  if (!modern) return <img src={legacy} {...legacyProps} onError={onError} />
  return <picture>
    <source type="image/avif" srcSet={candidates(base, asset, size, 'avif', recipe)} sizes={sizes} />
    <source type="image/webp" srcSet={candidates(base, asset, size, 'webp', recipe)} sizes={sizes} />
    <img src={derivativeUrl(base, asset, size + '-' + first + '.jpg', recipe)}
      srcSet={candidates(base, asset, size, 'jpg', recipe)} sizes={sizes}
      {...legacyProps} onError={() => setFallback(legacy)} />
  </picture>
}
