import { useEffect, useRef, useState } from 'react'
import OpenSeadragon from 'openseadragon'
import { ZoomIn, ZoomOut, Maximize, AlertCircle } from 'lucide-react'
import { fetchSlideInfo, slideTileUrl } from '@/lib/api'

/** Deep-zoom whole-slide viewer backed by the API's OpenSlide tile endpoints. */
export default function SlideViewer({ assetId }: { assetId: number }) {
  const containerRef = useRef<HTMLDivElement>(null)
  const viewerRef = useRef<OpenSeadragon.Viewer | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let cancelled = false
    let drewTile = false
    let noTileTimer: ReturnType<typeof setTimeout> | undefined
    setLoading(true)
    setError(null)

    fetchSlideInfo(assetId)
      .then((info) => {
        if (cancelled || !containerRef.current) return
        const viewer = OpenSeadragon({
          element: containerRef.current,
          // OSD 4.1 defaults to the WebGL drawer; the canvas drawer is reliable here.
          drawer: 'canvas',
          crossOriginPolicy: 'Anonymous',
          showNavigationControl: false, // we render our own buttons (no external images)
          showNavigator: true,
          navigatorPosition: 'TOP_RIGHT',
          visibilityRatio: 1,
          minZoomImageRatio: 0.8,
          maxZoomPixelRatio: 2,
          gestureSettingsMouse: { clickToZoom: false, dblClickToZoom: true },
          tileSources: {
            width: info.width,
            height: info.height,
            tileSize: info.tile_size,
            tileOverlap: info.overlap,
            minLevel: 0,
            maxLevel: info.levels - 1,
            getTileUrl: (level: number, x: number, y: number) => slideTileUrl(assetId, level, x, y),
          },
        } as OpenSeadragon.Options)

        viewer.addHandler('tile-drawn', () => {
          drewTile = true
        })
        viewer.addHandler('open-failed', (e) => {
          if (!cancelled) setError((e as { message?: string }).message || 'The viewer could not open this slide.')
        })
        viewer.addHandler('tile-load-failed', (e) => {
          if (!cancelled && !drewTile) {
            setError(`A slide tile failed to load: ${(e as { message?: string }).message || 'unknown error'}`)
          }
        })

        viewerRef.current = viewer
        setLoading(false)
        // Catch the silent-failure case (viewer opens but paints nothing).
        noTileTimer = setTimeout(() => {
          if (!cancelled && !drewTile) {
            setError('The viewer opened but no tiles rendered. See the browser console for details.')
          }
        }, 6000)
      })
      .catch((err) => {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : 'Could not open slide')
          setLoading(false)
        }
      })

    return () => {
      cancelled = true
      if (noTileTimer) clearTimeout(noTileTimer)
      viewerRef.current?.destroy()
      viewerRef.current = null
    }
  }, [assetId])

  return (
    <div className="relative w-full rounded-lg overflow-hidden border bg-slate-900">
      {/* Explicit height — OpenSeadragon measures this element and needs a real size. */}
      <div ref={containerRef} className="w-full h-[520px]" />

      {loading && !error && (
        <div className="absolute inset-0 flex items-center justify-center text-white/70 text-sm">
          Loading slide…
        </div>
      )}
      {error && (
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 bg-slate-900/95 px-6 text-center">
          <AlertCircle className="h-6 w-6 text-red-400" />
          <p className="text-white/90 text-sm font-medium">Slide could not be displayed</p>
          <p className="text-white/60 text-xs max-w-md">{error}</p>
        </div>
      )}

      {!loading && !error && (
        <div className="absolute bottom-3 left-3 flex gap-1">
          <ViewerButton label="Zoom in" onClick={() => viewerRef.current?.viewport.zoomBy(1.4).applyConstraints()}>
            <ZoomIn className="h-4 w-4" />
          </ViewerButton>
          <ViewerButton label="Zoom out" onClick={() => viewerRef.current?.viewport.zoomBy(0.7).applyConstraints()}>
            <ZoomOut className="h-4 w-4" />
          </ViewerButton>
          <ViewerButton label="Reset view" onClick={() => viewerRef.current?.viewport.goHome()}>
            <Maximize className="h-4 w-4" />
          </ViewerButton>
        </div>
      )}
    </div>
  )
}

function ViewerButton({
  label,
  onClick,
  children,
}: {
  label: string
  onClick: () => void
  children: React.ReactNode
}) {
  return (
    <button
      type="button"
      title={label}
      aria-label={label}
      onClick={onClick}
      className="p-2 rounded-md bg-black/50 text-white hover:bg-black/70 transition-colors"
    >
      {children}
    </button>
  )
}
