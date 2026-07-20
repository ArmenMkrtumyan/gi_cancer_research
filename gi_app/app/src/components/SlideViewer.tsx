import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import OpenSeadragon from 'openseadragon'
import { ZoomIn, ZoomOut, Maximize, AlertCircle, Layers, ExternalLink } from 'lucide-react'
import {
  fetchSlideInfo,
  fetchSpatialLayers,
  overlayImageUrl,
  slideTileUrl,
  type SpatialLayer,
} from '@/lib/api'

/** Deep-zoom whole-slide viewer with optional published spatial-annotation overlays. */
export default function SlideViewer({ assetId }: { assetId: number }) {
  const containerRef = useRef<HTMLDivElement>(null)
  const viewerRef = useRef<OpenSeadragon.Viewer | null>(null)
  const overlayRef = useRef<OpenSeadragon.TiledImage | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [ready, setReady] = useState(false)

  const [layers, setLayers] = useState<SpatialLayer[]>([])
  const [activeId, setActiveId] = useState<number | null>(null)
  // Off until the user asks for it. The overlay is an external algorithmic prediction drawn
  // over the tissue; showing it unprompted would put a model's output in front of a
  // pathologist who came to look at the slide itself.
  const [visible, setVisible] = useState(false)
  const [opacity, setOpacity] = useState(0.6)

  // Only the browser-renderable derivatives can be drawn; the preserved source files are
  // listed separately so their provenance stays visible.
  const renderable = useMemo(() => layers.filter((l) => l.is_renderable && l.image_url), [layers])
  const active = useMemo(
    () => renderable.find((l) => l.representation_id === activeId) ?? null,
    [renderable, activeId],
  )

  useEffect(() => {
    let cancelled = false
    let drewTile = false
    let noTileTimer: ReturnType<typeof setTimeout> | undefined
    setLoading(true)
    setError(null)
    setReady(false)

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
        viewer.addHandler('open', () => {
          if (!cancelled) setReady(true)
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
      overlayRef.current = null
      viewerRef.current?.destroy()
      viewerRef.current = null
    }
  }, [assetId])

  useEffect(() => {
    let cancelled = false
    fetchSpatialLayers(assetId)
      .then((res) => {
        if (cancelled) return
        setLayers(res.layers)
        const first = res.layers.find((l) => l.is_renderable && l.image_url)
        setActiveId(first ? first.representation_id : null)
      })
      .catch(() => {
        // An overlay is optional context — a failure here must not break slide viewing.
        if (!cancelled) setLayers([])
      })
    return () => {
      cancelled = true
    }
  }, [assetId])

  const removeOverlay = useCallback(() => {
    const viewer = viewerRef.current
    const img = overlayRef.current
    if (viewer && img && viewer.world.getIndexOfItem(img) !== -1) {
      viewer.world.removeItem(img)
    }
    overlayRef.current = null
  }, [])

  // Place the active layer using the scale/offset recorded at import time. OpenSeadragon
  // viewport coordinates are normalized by the slide's WIDTH on both axes, so the same
  // divisor applies to x, y and width — and the layer stays registered through zoom/pan
  // because it is a world item, not a screen-space decoration.
  useEffect(() => {
    const viewer = viewerRef.current
    if (!viewer || !ready) return
    removeOverlay()
    if (!active || !visible || !active.image_url) return

    const t = active.transform_metadata
    if (!t || !t.slide_width_px) return
    const w = t.slide_width_px

    viewer.addTiledImage({
      tileSource: { type: 'image', url: overlayImageUrl(active.image_url) },
      x: t.offset_x_px / w,
      y: t.offset_y_px / w,
      width: t.extent_x_px / w,
      opacity,
      // @types/openseadragon types this callback as a bare Event; the runtime passes an
      // object carrying the created TiledImage.
      success: ((event: unknown) => {
        overlayRef.current = (event as { item: OpenSeadragon.TiledImage }).item
      }) as unknown as (event: Event) => void,
    })

    return removeOverlay
  }, [active, visible, ready, removeOverlay, opacity])

  useEffect(() => {
    overlayRef.current?.setOpacity(opacity)
  }, [opacity])

  const set = active?.annotation_set

  return (
    <div className="space-y-3">
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

        {active && visible && !error && (
          <div className="absolute bottom-3 right-3 rounded-md bg-black/65 px-3 py-2 text-white">
            <p className="text-[11px] font-semibold mb-1">
              {active.source_representation_type === 'binary_mask' ? 'TIL present' : 'TIL probability'}
            </p>
            {active.source_representation_type === 'binary_mask' ? (
              <div className="flex items-center gap-1.5 text-[10px]">
                <span className="inline-block h-3 w-3 rounded-sm" style={{ background: 'rgb(255,40,255)' }} />
                <span>TIL-positive patch</span>
              </div>
            ) : (
              <>
                <div
                  className="h-2.5 w-32 rounded-sm"
                  style={{ background: 'linear-gradient(to right, rgba(0,0,89,0.1), rgb(255,40,255))' }}
                />
                <div className="flex justify-between text-[10px] mt-0.5 tabular-nums">
                  <span>0.0</span>
                  <span>1.0</span>
                </div>
              </>
            )}
          </div>
        )}
      </div>

      {renderable.length > 0 && (
        <div className="rounded-lg border p-3 space-y-2.5">
          <div className="flex items-center gap-2 flex-wrap">
            <Layers className="h-4 w-4 text-brand" />
            <span className="text-sm font-semibold">Published annotation overlay</span>
            <button
              type="button"
              onClick={() => setVisible((v) => !v)}
              aria-pressed={visible}
              className={`ml-auto text-xs font-medium px-3 py-1.5 rounded-md border transition-colors ${
                visible
                  ? 'bg-brand text-white border-brand'
                  : 'bg-card hover:bg-muted border-brand text-brand'
              }`}
            >
              {visible ? 'Hide overlay' : 'Show overlay'}
            </button>
          </div>

          {!visible && (
            <p className="text-xs text-muted-foreground">
              An external algorithmic prediction, off by default. Turn it on to draw it over the
              tissue.
            </p>
          )}

          {visible && renderable.length > 1 && (
            <div className="flex items-center gap-1.5 flex-wrap">
              {renderable.map((l) => (
                <button
                  key={l.representation_id}
                  type="button"
                  onClick={() => setActiveId(l.representation_id)}
                  title={l.annotation_label ?? undefined}
                  className={`text-xs px-2.5 py-1 rounded-md border transition-colors ${
                    l.representation_id === activeId
                      ? 'bg-brand text-white border-brand'
                      : 'bg-card hover:bg-muted'
                  }`}
                >
                  {l.source_representation_type === 'binary_mask'
                    ? 'Binary mask'
                    : 'Probability map'}
                </button>
              ))}
            </div>
          )}

          <label className="flex items-center gap-2 text-xs text-muted-foreground">
            Opacity
            <input
              type="range"
              min={0}
              max={1}
              step={0.05}
              value={opacity}
              onChange={(e) => setOpacity(Number(e.target.value))}
              className="flex-1 max-w-[200px]"
              disabled={!visible}
            />
            <span className="tabular-nums w-9">{Math.round(opacity * 100)}%</span>
          </label>

          {set && (
            <div className="rounded-md bg-amber-50 border border-amber-200 p-2.5 text-[11px] leading-relaxed">
              <p className="font-semibold text-amber-900">
                Published algorithmic result — not ground truth
              </p>
              <p className="text-amber-800 mt-0.5">
                {set.description}
              </p>
              <dl className="mt-1.5 grid grid-cols-[auto_1fr] gap-x-2 gap-y-0.5 text-amber-900">
                <dt className="font-medium">Source</dt>
                <dd>{set.name} ({set.version})</dd>
                <dt className="font-medium">Provider</dt>
                <dd>{set.provider}</dd>
                <dt className="font-medium">Method</dt>
                <dd>{set.method}</dd>
                <dt className="font-medium">Licence</dt>
                <dd>{set.license}</dd>
              </dl>
              {set.citation && (
                <details className="mt-1.5">
                  <summary className="cursor-pointer font-medium text-amber-900">Citation</summary>
                  <p className="mt-1 text-amber-800">{set.citation}</p>
                </details>
              )}
              {set.source_url && (
                <a
                  href={set.source_url}
                  target="_blank"
                  rel="noreferrer"
                  className="inline-flex items-center gap-1 mt-1.5 text-amber-900 underline"
                >
                  Source dataset <ExternalLink className="h-3 w-3" />
                </a>
              )}
            </div>
          )}

          {active?.transform_metadata && (
            <p className="text-[11px] text-muted-foreground">
              Registered in {active.coordinate_space?.replace(/_/g, ' ')} ·{' '}
              {active.transform_metadata.grid_columns}×{active.transform_metadata.grid_rows} grid ·{' '}
              {active.transform_metadata.level0_px_per_map_px_x.toFixed(1)} level-0 px per cell
            </p>
          )}
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
