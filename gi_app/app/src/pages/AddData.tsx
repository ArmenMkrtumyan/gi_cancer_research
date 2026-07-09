import { useEffect, useMemo, useRef, useState } from 'react'
import { toast } from 'sonner'
import {
  DownloadCloud, Eye, Trash2, Plus, AlertCircle, ExternalLink, X,
} from 'lucide-react'
import {
  fetchCatalog, addCatalog, deleteCatalog, fetchManifest, startDownload, fetchStorageTargets,
  type CatalogEntry, type Manifest, type StorageTargets,
} from '@/lib/api'
import { formatNumber, cn } from '@/lib/utils'
import Section from '@/components/Section'
import StatusPill from '@/components/StatusPill'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'

const ACTIVE = ['pending', 'downloading', 'ingesting']

function SourceBadge({ type }: { type: string }) {
  if (type === 'gdc') return <Badge variant="gold">TCGA / GDC</Badge>
  if (type === 'geo') return <Badge variant="secondary">GEO</Badge>
  return <Badge variant="secondary">{type}</Badge>
}

export default function AddData() {
  const [catalog, setCatalog] = useState<CatalogEntry[] | null>(null)
  const [targets, setTargets] = useState<StorageTargets | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const [destination, setDestination] = useState<'local' | 'aws'>('local')
  const [scope, setScope] = useState<'sample' | 'all'>('sample')
  const [sampleCount, setSampleCount] = useState(6)
  const [newName, setNewName] = useState('')
  const [newUrl, setNewUrl] = useState('')
  const [adding, setAdding] = useState(false)

  const [manifest, setManifest] = useState<{ entryId: number; data: Manifest } | null>(null)
  const [manifestLoading, setManifestLoading] = useState<number | null>(null)

  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const load = async () => {
    try {
      setCatalog(await fetchCatalog())
      setError(null)
      // Storage targets are optional — degrade to "aws not configured" if unavailable.
      try {
        setTargets(await fetchStorageTargets())
      } catch {
        setTargets({ local: true, aws: false })
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load the registry')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
  }, [])

  // Poll the registry while any job is running so status pills update live.
  const hasActive = useMemo(
    () => (catalog ?? []).some((c) => c.latest_job && ACTIVE.includes(c.latest_job.status)),
    [catalog],
  )
  useEffect(() => {
    if (hasActive && !pollRef.current) {
      pollRef.current = setInterval(() => {
        fetchCatalog().then(setCatalog).catch(() => {})
      }, 4000)
    } else if (!hasActive && pollRef.current) {
      clearInterval(pollRef.current)
      pollRef.current = null
    }
    return () => {
      if (pollRef.current) {
        clearInterval(pollRef.current)
        pollRef.current = null
      }
    }
  }, [hasActive])

  const onAdd = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!newName.trim() || !newUrl.trim()) return
    setAdding(true)
    try {
      await addCatalog({ name: newName.trim(), source_url: newUrl.trim() })
      setNewName('')
      setNewUrl('')
      toast.success('Added to the registry')
      await load()
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Could not add')
    } finally {
      setAdding(false)
    }
  }

  const onDelete = async (id: number) => {
    try {
      await deleteCatalog(id)
      if (manifest?.entryId === id) setManifest(null)
      await load()
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Could not remove')
    }
  }

  const onPreview = async (id: number) => {
    setManifestLoading(id)
    setManifest(null)
    try {
      const data = await fetchManifest(id, scope === 'all' ? null : sampleCount)
      setManifest({ entryId: id, data })
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Preview failed')
    } finally {
      setManifestLoading(null)
    }
  }

  const onDownload = async (entry: CatalogEntry) => {
    try {
      await startDownload(entry.id, destination, scope === 'all' ? null : sampleCount)
      const scopeLabel = scope === 'all' ? 'all slides' : `${sampleCount} slides`
      toast.success(`Download started (${scopeLabel}) → ${destination === 'local' ? 'local MinIO' : 'AWS S3'}`)
      await load()
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Could not start the download')
    }
  }

  if (loading) {
    return <div className="bg-card rounded-lg border shadow-sm p-10 text-center text-muted-foreground">Loading…</div>
  }
  if (error || !catalog) {
    return (
      <div className="bg-card rounded-lg border shadow-sm p-10 text-center">
        <AlertCircle className="h-8 w-8 text-destructive mx-auto mb-3" />
        <p className="text-sm text-muted-foreground">{error}</p>
      </div>
    )
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-3xl font-bold text-brand">Add data</h1>
        <p className="text-sm text-muted-foreground mt-1">
          Register datasets to acquire, preview what will be pulled, then download → ingest into the platform.
        </p>
      </div>

      {/* Add form + destination */}
      <Section title="Register a dataset" icon={Plus} description="Anyone can add a name and a source link. We detect the source and only enable download for connectors we support.">
        <form onSubmit={onAdd} className="flex flex-wrap items-end gap-3">
          <label className="flex flex-col gap-1 text-xs text-muted-foreground">
            Name
            <input
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
              placeholder="TCGA-BRCA"
              className="border rounded-md px-3 py-2 text-sm text-foreground bg-card min-w-[180px]"
            />
          </label>
          <label className="flex flex-col gap-1 text-xs text-muted-foreground flex-1 min-w-[280px]">
            Source link
            <input
              value={newUrl}
              onChange={(e) => setNewUrl(e.target.value)}
              placeholder="https://portal.gdc.cancer.gov/projects/…"
              className="border rounded-md px-3 py-2 text-sm text-foreground bg-card w-full"
            />
          </label>
          <Button type="submit" disabled={adding || !newName.trim() || !newUrl.trim()}>
            <Plus className="h-4 w-4 mr-1" /> {adding ? 'Adding…' : 'Add'}
          </Button>
        </form>

        <div className="mt-5 pt-4 border-t flex flex-wrap items-center gap-3">
          <span className="text-sm text-muted-foreground">Download destination:</span>
          <div className="flex gap-2">
            <DestButton active={destination === 'local'} onClick={() => setDestination('local')}>
              Local MinIO (bronze)
            </DestButton>
            <DestButton
              active={destination === 'aws'}
              disabled={!targets?.aws}
              onClick={() => setDestination('aws')}
            >
              AWS S3{targets?.aws ? '' : ' — not configured'}
            </DestButton>
          </div>
        </div>

        <div className="mt-3 flex flex-wrap items-center gap-x-3 gap-y-2">
          <span className="text-sm text-muted-foreground">Slides:</span>
          <div className="flex items-center gap-2">
            <DestButton active={scope === 'sample'} onClick={() => setScope('sample')}>Sample</DestButton>
            {scope === 'sample' && (
              <input
                type="number"
                min={1}
                max={5000}
                value={sampleCount}
                onChange={(e) => setSampleCount(Math.max(1, Number(e.target.value) || 1))}
                className="border rounded-md px-2 py-1.5 text-sm w-20 bg-card text-foreground"
              />
            )}
            <DestButton active={scope === 'all'} onClick={() => setScope('all')}>All slides</DestButton>
          </div>
          {scope === 'all' && (
            <span className="text-xs text-amber-600">
              ⚠ Full downloads can be hundreds of GB — best sent to AWS S3.
            </span>
          )}
        </div>
      </Section>

      {/* Manifest preview */}
      {manifest && <ManifestPanel manifest={manifest.data} onClose={() => setManifest(null)} />}

      {/* Registry */}
      <Section title="Datasets to download" icon={DownloadCloud}>
        <div className="overflow-x-auto rounded-lg border">
          <table className="w-full text-sm">
            <thead className="bg-brand text-white">
              <tr>
                <th className="text-left font-semibold px-4 py-3">Dataset</th>
                <th className="text-left font-semibold px-4 py-3">Source</th>
                <th className="text-left font-semibold px-4 py-3">Cancer type</th>
                <th className="text-left font-semibold px-4 py-3">Status</th>
                <th className="text-right font-semibold px-4 py-3">Actions</th>
              </tr>
            </thead>
            <tbody>
              {catalog.map((c, i) => {
                const job = c.latest_job
                const running = !!job && ACTIVE.includes(job.status)
                return (
                  <tr key={c.id} className={i % 2 === 0 ? 'bg-card' : 'bg-muted/40'}>
                    <td className="px-4 py-3">
                      <div className="font-medium text-foreground">{c.name}</div>
                      <a href={c.source_url} target="_blank" rel="noreferrer" className="text-xs text-primary hover:underline inline-flex items-center gap-1">
                        source <ExternalLink className="h-3 w-3" />
                      </a>
                    </td>
                    <td className="px-4 py-3"><SourceBadge type={c.source_type} /></td>
                    <td className="px-4 py-3 text-muted-foreground">{c.gi_cancer_types || '—'}</td>
                    <td className="px-4 py-3">
                      {running ? (
                        <div className="flex flex-col gap-1 min-w-[210px]">
                          <StatusPill status={job!.status} />
                          {job!.status === 'downloading' && job!.bytes_total ? (
                            <>
                              <div className="h-1.5 bg-muted rounded-full overflow-hidden mt-0.5">
                                <div
                                  className="h-full bg-brand transition-all"
                                  style={{ width: `${Math.min(100, ((job!.bytes_done ?? 0) / job!.bytes_total) * 100)}%` }}
                                />
                              </div>
                              <span className="text-xs text-muted-foreground">
                                {((job!.bytes_done ?? 0) / 1e9).toFixed(1)} / {(job!.bytes_total / 1e9).toFixed(1)} GB — {job!.message}
                              </span>
                            </>
                          ) : (
                            job!.message && <span className="text-xs text-muted-foreground">{job!.message}</span>
                          )}
                        </div>
                      ) : job?.status === 'failed' ? (
                        <div className="flex flex-col gap-1">
                          <StatusPill status="failed" />
                          {job.message && <span className="text-xs text-red-600 max-w-[240px]">{job.message}</span>}
                        </div>
                      ) : c.ingested ? (
                        <Badge variant="success">in dashboard</Badge>
                      ) : (
                        <span className="text-muted-foreground text-xs">not downloaded</span>
                      )}
                    </td>
                    <td className="px-4 py-3">
                      <div className="flex items-center justify-end gap-2">
                        {c.downloadable ? (
                          <>
                            <Button size="sm" variant="outline" disabled={manifestLoading === c.id} onClick={() => onPreview(c.id)}>
                              <Eye className="h-3.5 w-3.5 mr-1" /> {manifestLoading === c.id ? 'Loading…' : 'Preview'}
                            </Button>
                            <Button size="sm" disabled={running} onClick={() => onDownload(c)}>
                              <DownloadCloud className="h-3.5 w-3.5 mr-1" /> {c.ingested ? 'Re-download' : 'Download'}
                            </Button>
                          </>
                        ) : (
                          <span className="text-xs text-muted-foreground italic">connector needed</span>
                        )}
                        <button
                          title="Remove from registry"
                          onClick={() => onDelete(c.id)}
                          className="p-1.5 text-muted-foreground hover:text-destructive"
                        >
                          <Trash2 className="h-4 w-4" />
                        </button>
                      </div>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      </Section>
    </div>
  )
}

function DestButton({
  active,
  disabled,
  onClick,
  children,
}: {
  active: boolean
  disabled?: boolean
  onClick: () => void
  children: React.ReactNode
}) {
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={onClick}
      className={cn(
        'px-3 py-1.5 rounded-md text-sm border transition-colors',
        active ? 'bg-brand text-white border-brand' : 'bg-card text-foreground hover:bg-muted',
        disabled && 'opacity-50 cursor-not-allowed',
      )}
    >
      {children}
    </button>
  )
}

function ManifestPanel({ manifest, onClose }: { manifest: Manifest; onClose: () => void }) {
  return (
    <Section
      title={`Manifest preview — ${manifest.project}`}
      icon={Eye}
      description={`${manifest.full ? 'Full download' : 'Sample'}: ${formatNumber(manifest.n_slides)} slides · ${formatNumber(manifest.total_mb)} MB · plus full clinical & biospecimen for ${formatNumber(manifest.n_cases)} patients.`}
      action={
        <Button size="sm" variant="ghost" onClick={onClose}>
          <X className="h-4 w-4 mr-1" /> Close
        </Button>
      }
    >
      <div className="overflow-x-auto rounded-lg border">
        <table className="w-full text-sm">
          <thead className="bg-brand text-white">
            <tr>
              <th className="text-left font-semibold px-4 py-2.5">Type</th>
              <th className="text-left font-semibold px-4 py-2.5">Case</th>
              <th className="text-left font-semibold px-4 py-2.5">File</th>
              <th className="text-right font-semibold px-4 py-2.5">Size (MB)</th>
            </tr>
          </thead>
          <tbody>
            {manifest.slides.map((s, i) => (
              <tr key={s.file_name} className={i % 2 === 0 ? 'bg-card' : 'bg-muted/40'}>
                <td className="px-4 py-2">
                  <Badge variant={s.slide_type === 'diagnostic' ? 'gold' : 'secondary'}>{s.slide_type}</Badge>
                </td>
                <td className="px-4 py-2 font-mono text-xs">{s.case}</td>
                <td className="px-4 py-2 text-muted-foreground truncate max-w-[360px]">{s.file_name}</td>
                <td className="px-4 py-2 text-right tabular-nums">{formatNumber(s.size_mb)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {manifest.slides.length < manifest.n_slides && (
        <p className="text-xs text-muted-foreground mt-2">
          Showing the first {manifest.slides.length} of {formatNumber(manifest.n_slides)} slides.
        </p>
      )}
    </Section>
  )
}
