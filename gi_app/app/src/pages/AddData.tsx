import { useEffect, useMemo, useRef, useState } from 'react'
import { toast } from 'sonner'
import {
  DownloadCloud, Eye, Trash2, Plus, AlertCircle, ExternalLink, X, HardDrive,
} from 'lucide-react'
import {
  fetchCatalog, addCatalog, deleteCatalog, fetchManifest, startDownload, fetchStorageTargets,
  purgeDataset, fetchDatasetSummary,
  type CatalogEntry, type Manifest, type StorageTargets, type DownloadJob,
} from '@/lib/api'
import { formatNumber, formatBytes, cn } from '@/lib/utils'
import Section from '@/components/Section'
import StatusPill from '@/components/StatusPill'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'

const ACTIVE = ['pending', 'downloading', 'ingesting']

// GDC's own `experimental_strategy` values are "Diagnostic Slide" and "Tissue Slide". The
// word describes how the slide was prepared and what it is for, not a diagnosis record —
// a reading people reliably get wrong, so the preparation is spelled out alongside it.
const SLIDE_TYPE_NOTE: Record<string, string> = {
  diagnostic:
    'Diagnostic slide (DX): an FFPE section, the archival slide a pathologist reads to make or confirm the diagnosis.',
  tissue:
    'Tissue slide (TS/BS): a frozen section cut alongside the tissue sent for sequencing, used to check its tumour content.',
}

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

  const [purgeTarget, setPurgeTarget] = useState<CatalogEntry | null>(null)
  const [purging, setPurging] = useState(false)

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

  // Permanently delete an ingested dataset's downloaded slide files + Postgres records.
  const onPurge = async () => {
    if (purgeTarget?.dataset_id == null) return
    setPurging(true)
    try {
      const res = await purgeDataset(purgeTarget.dataset_id)
      if (manifest?.entryId === purgeTarget.id) setManifest(null)
      const freed = res.bytes_freed ? ` · freed ${formatBytes(res.bytes_freed)}` : ''
      if (res.storage_errors.length) {
        toast.warning(`Removed ${res.dataset_name} records, but ${res.storage_errors.length} file(s) could not be deleted from storage${freed}`)
      } else {
        toast.success(`Deleted ${res.dataset_name}: ${res.objects_deleted} slide file(s)${freed}`)
      }
      setPurgeTarget(null)
      await load()
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Could not delete the data')
    } finally {
      setPurging(false)
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
      onPreview(entry.id) // auto-open the manifest so the user sees exactly which files are being pulled
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
        <h1 className="text-3xl font-bold text-brand">Data</h1>
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

      {/* Manifest preview — carries live per-file download progress for its dataset */}
      {manifest && (
        <ManifestPanel
          manifest={manifest.data}
          job={catalog.find((c) => c.id === manifest.entryId)?.latest_job ?? null}
          onClose={() => setManifest(null)}
        />
      )}

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
                        <div className="flex flex-col gap-1 min-w-[150px]">
                          <StatusPill status={job!.status} />
                          {job!.message && (
                            <span className="text-xs text-muted-foreground">
                              {/* Compact here; per-file detail lives in the preview panel. */}
                              {job!.status === 'downloading' ? job!.message.split(':')[0] : job!.message}
                            </span>
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
                        {c.ingested && c.dataset_id != null && (
                          <Button
                            size="sm"
                            variant="outline"
                            disabled={running}
                            onClick={() => setPurgeTarget(c)}
                            title="Delete the downloaded slide files and records from local storage"
                            className="text-destructive hover:text-destructive"
                          >
                            <HardDrive className="h-3.5 w-3.5 mr-1" /> Delete data
                          </Button>
                        )}
                        <button
                          title="Remove from registry (does not delete downloaded data)"
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

      {purgeTarget && (
        <PurgeDialog
          entry={purgeTarget}
          busy={purging}
          onCancel={() => (purging ? null : setPurgeTarget(null))}
          onConfirm={onPurge}
        />
      )}
    </div>
  )
}

function PurgeDialog({
  entry,
  busy,
  onCancel,
  onConfirm,
}: {
  entry: CatalogEntry
  busy: boolean
  onCancel: () => void
  onConfirm: () => void
}) {
  const [stats, setStats] = useState<{ files: number; bytes: number } | null>(null)

  useEffect(() => {
    let active = true
    if (entry.dataset_id == null) return
    fetchDatasetSummary(entry.dataset_id)
      .then((s) => {
        if (active) setStats({ files: s.table_counts.data_assets ?? 0, bytes: s.asset_bytes })
      })
      .catch(() => {})
    return () => {
      active = false
    }
  }, [entry.dataset_id])

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" onClick={onCancel}>
      <div
        className="bg-card rounded-lg border shadow-xl max-w-md w-full p-6"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-start gap-3">
          <div className="mt-0.5 rounded-full bg-destructive/10 p-2">
            <HardDrive className="h-5 w-5 text-destructive" />
          </div>
          <div className="flex-1">
            <h2 className="text-lg font-semibold text-foreground">Delete downloaded data?</h2>
            <p className="text-sm text-muted-foreground mt-1">
              This permanently removes <span className="font-medium text-foreground">{entry.name}</span> from
              local storage: {stats ? `${formatNumber(stats.files)} slide file(s) (${formatBytes(stats.bytes)})` : 'its slide files'} plus
              all of its clinical &amp; biospecimen records. It disappears from the dashboard.
            </p>
            <p className="text-xs text-muted-foreground mt-2">
              The registry entry stays, so you can re-download it anytime.
            </p>
          </div>
        </div>
        <div className="mt-5 flex justify-end gap-2">
          <Button variant="outline" size="sm" onClick={onCancel} disabled={busy}>
            Cancel
          </Button>
          <Button
            size="sm"
            onClick={onConfirm}
            disabled={busy}
            className="bg-destructive text-white hover:bg-destructive/90"
          >
            {busy ? 'Deleting…' : 'Delete data'}
          </Button>
        </div>
      </div>
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

function ManifestPanel({ manifest, job, onClose }: { manifest: Manifest; job: DownloadJob | null; onClose: () => void }) {
  // Overlay live per-file download progress onto the manifest rows. The orchestrator reports
  // "Slide i of N: <file_name>" + that file's own bytes, so we match the active row by file name
  // and derive done/queued from its position (preview rows share plan()'s order with the download).
  const showProgress = !!job && ['downloading', 'ingesting', 'done'].includes(job.status)
  const activeIdx =
    job?.status === 'downloading'
      ? manifest.slides.findIndex((s) => (job.message ?? '').includes(s.file_name))
      : -1
  const rowState = (i: number): 'done' | 'active' | 'pending' | 'none' => {
    if (!job) return 'none'
    if (job.status === 'ingesting' || job.status === 'done') return 'done'
    if (job.status === 'downloading' && activeIdx >= 0) {
      if (i < activeIdx) return 'done'
      if (i === activeIdx) return 'active'
      return 'pending'
    }
    return 'none'
  }

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
      {job?.status === 'failed' && (
        <div className="mb-3 text-xs text-red-600">Download failed: {job.message}</div>
      )}
      <div className="overflow-x-auto rounded-lg border">
        <table className="w-full text-sm">
          <thead className="bg-brand text-white">
            <tr>
              <th className="text-left font-semibold px-4 py-2.5">Type</th>
              <th className="text-left font-semibold px-4 py-2.5">Case</th>
              <th className="text-left font-semibold px-4 py-2.5">File</th>
              <th className="text-right font-semibold px-4 py-2.5">Size (MB)</th>
              {showProgress && <th className="text-right font-semibold px-4 py-2.5">Progress</th>}
            </tr>
          </thead>
          <tbody>
            {manifest.slides.map((s, i) => {
              const st = rowState(i)
              return (
                <tr
                  key={s.file_name}
                  className={cn(i % 2 === 0 ? 'bg-card' : 'bg-muted/40', st === 'active' && 'bg-brand/5')}
                >
                  <td className="px-4 py-2">
                    <Badge
                      variant={s.slide_type === 'diagnostic' ? 'gold' : 'secondary'}
                      title={SLIDE_TYPE_NOTE[s.slide_type] ?? undefined}
                    >
                      {s.slide_type === 'diagnostic' ? 'diagnostic (FFPE)' : 'tissue (frozen)'}
                    </Badge>
                  </td>
                  <td className="px-4 py-2 font-mono text-xs">{s.case}</td>
                  <td className={cn('px-4 py-2 truncate max-w-[360px]', st === 'active' ? 'text-foreground font-medium' : 'text-muted-foreground')}>
                    {s.file_name}
                  </td>
                  <td className="px-4 py-2 text-right tabular-nums">{formatNumber(s.size_mb)}</td>
                  {showProgress && (
                    <td className="px-4 py-2 text-right align-middle">
                      {st === 'done' && <span className="text-xs text-green-600">✓ done</span>}
                      {st === 'pending' && <span className="text-xs text-muted-foreground">queued</span>}
                      {st === 'active' && (
                        <div className="flex flex-col items-end gap-1 min-w-[120px] ml-auto">
                          <div className="h-1.5 w-full bg-muted rounded-full overflow-hidden">
                            <div
                              className="h-full bg-brand transition-all"
                              style={{ width: `${job!.bytes_total ? Math.min(100, ((job!.bytes_done ?? 0) / job!.bytes_total) * 100) : 0}%` }}
                            />
                          </div>
                          <span className="text-[11px] text-muted-foreground tabular-nums">
                            {formatBytes(job!.bytes_done)} / {formatBytes(job!.bytes_total)}
                          </span>
                        </div>
                      )}
                      {st === 'none' && <span className="text-xs text-muted-foreground">—</span>}
                    </td>
                  )}
                </tr>
              )
            })}
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
