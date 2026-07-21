import { useEffect, useMemo, useRef, useState } from 'react'
import { toast } from 'sonner'
import {
  DownloadCloud, Eye, Trash2, Plus, AlertCircle, ExternalLink, X, HardDrive,
  ClipboardCheck, CheckCircle2, MinusCircle, CircleSlash,
} from 'lucide-react'
import {
  fetchCatalog, addCatalog, deleteCatalog, fetchManifest, startDownload, fetchStorageTargets,
  purgeDataset, fetchDatasetSummary, checkSource, fetchCompatibility,
  type CatalogEntry, type Manifest, type StorageTargets, type DownloadJob,
  type CompatReport, type CompatVerdict, type CompatFill,
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

function SourceBadge({ type, label }: { type: string; label?: string }) {
  const text = label || type
  if (type === 'gdc') return <Badge variant="gold">{text}</Badge>
  return <Badge variant="secondary">{text}</Badge>
}

// One verdict -> one badge. The wording answers "what do I do now?", not "how good is it?".
const VERDICT_META: Record<CompatVerdict, { label: string; variant: 'success' | 'warning' | 'destructive' | 'secondary' }> = {
  supported: { label: 'Ready to download', variant: 'success' },
  partial: { label: 'Good fit — connector needed', variant: 'warning' },
  needs_review: { label: 'Needs investigation', variant: 'warning' },
  unsupported: { label: 'Cannot be automated', variant: 'destructive' },
  unknown: { label: 'Unrecognised link', variant: 'secondary' },
}

function VerdictBadge({ verdict }: { verdict: CompatVerdict }) {
  const meta = VERDICT_META[verdict] ?? VERDICT_META.unknown
  return <Badge variant={meta.variant}>{meta.label}</Badge>
}

const FILL_META: Record<CompatFill, { icon: typeof CheckCircle2; label: string; className: string }> = {
  full: { icon: CheckCircle2, label: 'Filled', className: 'text-green-700' },
  partial: { icon: MinusCircle, label: 'Partly filled', className: 'text-amber-600' },
  none: { icon: CircleSlash, label: 'Stays empty', className: 'text-muted-foreground' },
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

  const [compat, setCompat] = useState<CompatReport | null>(null)
  const [compatLoading, setCompatLoading] = useState(false)

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

  // Check a link before it is registered, so the user sees what they would get.
  const onCheck = async (url: string) => {
    if (!url.trim()) return
    setCompatLoading(true)
    try {
      setCompat(await checkSource(url.trim()))
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Could not check that link')
    } finally {
      setCompatLoading(false)
    }
  }

  // The same report for a row already in the registry.
  const onCheckEntry = async (entry: CatalogEntry) => {
    setCompatLoading(true)
    try {
      setCompat(await fetchCompatibility(entry.id))
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Could not check that entry')
    } finally {
      setCompatLoading(false)
    }
  }

  const onAdd = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!newName.trim() || !newUrl.trim()) return
    setAdding(true)
    const url = newUrl.trim()
    try {
      await addCatalog({ name: newName.trim(), source_url: url })
      setNewName('')
      setNewUrl('')
      toast.success('Added to the registry')
      await load()
      // Always show the report after adding — an unsupported source should say so
      // immediately rather than leaving the user to wonder why Download is missing.
      await onCheck(url)
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
          <Button
            type="button"
            variant="outline"
            disabled={compatLoading || !newUrl.trim()}
            onClick={() => onCheck(newUrl)}
          >
            <ClipboardCheck className="h-4 w-4 mr-1" /> {compatLoading ? 'Checking…' : 'Check link'}
          </Button>
          <Button type="submit" disabled={adding || !newName.trim() || !newUrl.trim()}>
            <Plus className="h-4 w-4 mr-1" /> {adding ? 'Adding…' : 'Add'}
          </Button>
        </form>

        <p className="mt-2 text-xs text-muted-foreground">
          "Check link" reads the source and reports which tables it would fill before you commit.
          Adding runs the same check automatically.
        </p>

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

      {/* Compatibility report — what this source would and would not fill */}
      {compat && <CompatPanel report={compat} onClose={() => setCompat(null)} />}

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
                    <td className="px-4 py-3">
                      <div className="flex flex-col items-start gap-1">
                        <SourceBadge type={c.source_type} label={c.source_label} />
                        {!c.downloadable && <VerdictBadge verdict={c.verdict} />}
                      </div>
                    </td>
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
                        <Button
                          size="sm"
                          variant="outline"
                          disabled={compatLoading}
                          onClick={() => onCheckEntry(c)}
                          title="What would this fill, and what is missing?"
                        >
                          <ClipboardCheck className="h-3.5 w-3.5 mr-1" /> Check
                        </Button>
                        {c.downloadable && (
                          <>
                            <Button size="sm" variant="outline" disabled={manifestLoading === c.id} onClick={() => onPreview(c.id)}>
                              <Eye className="h-3.5 w-3.5 mr-1" /> {manifestLoading === c.id ? 'Loading…' : 'Preview'}
                            </Button>
                            <Button size="sm" disabled={running} onClick={() => onDownload(c)}>
                              <DownloadCloud className="h-3.5 w-3.5 mr-1" /> {c.ingested ? 'Re-download' : 'Download'}
                            </Button>
                          </>
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

/** Live facts read from the source, rendered as label/value pairs. */
function ProbeFacts({ report }: { report: CompatReport }) {
  const p = report.probe
  if (!p) return null
  const facts: Array<[string, string]> = []
  if (p.n_cases != null) facts.push(['Patients', formatNumber(p.n_cases)])
  if (p.n_samples != null) facts.push(['Samples', formatNumber(p.n_samples)])
  if (p.n_slides != null) facts.push(['Slides available', formatNumber(p.n_slides)])
  if (p.n_diagnostic_slides != null) facts.push(['— diagnostic (FFPE)', formatNumber(p.n_diagnostic_slides)])
  if (p.n_tissue_slides != null) facts.push(['— tissue (frozen)', formatNumber(p.n_tissue_slides)])
  if (p.total_mb != null) facts.push(['Total image size', formatBytes(p.total_mb * 1e6)])
  if (p.assay_type) facts.push(['Assay', p.assay_type])
  if (p.platform) facts.push(['Platform', p.platform])
  if (p.organism) facts.push(['Organism', p.organism])
  if (p.file_formats?.length) facts.push(['File formats', p.file_formats.join(', ')])
  if (!facts.length) return null

  return (
    <div className="rounded-lg border bg-muted/30 p-4">
      <div className="text-xs font-semibold uppercase tracking-wide text-muted-foreground mb-3">
        Read from the source
      </div>
      {p.title && <p className="text-sm text-foreground mb-3">{p.title}</p>}
      <dl className="grid grid-cols-2 gap-x-6 gap-y-1.5 sm:grid-cols-3">
        {facts.map(([label, value]) => (
          <div key={label} className="flex flex-col">
            <dt className="text-xs text-muted-foreground">{label}</dt>
            <dd className="text-sm font-medium text-foreground tabular-nums">{value}</dd>
          </div>
        ))}
      </dl>
    </div>
  )
}

/**
 * The compatibility report: whether the source can be loaded, which schema tables it
 * would fill, and what to look into. Shown after "Check link" and after adding.
 */
function CompatPanel({ report, onClose }: { report: CompatReport; onClose: () => void }) {
  const filled = report.tables.filter((t) => t.fill !== 'none')

  return (
    <Section
      title={`Compatibility — ${report.name || report.accession || report.source_label}`}
      icon={ClipboardCheck}
      description={report.headline}
      action={
        <Button size="sm" variant="ghost" onClick={onClose}>
          <X className="h-4 w-4" />
        </Button>
      }
    >
      <div className="space-y-4">
        <div className="flex flex-wrap items-center gap-2">
          <VerdictBadge verdict={report.verdict} />
          <SourceBadge type={report.source_type} label={report.source_label} />
          <span className="text-sm text-muted-foreground">
            Would fill {filled.length} of {report.n_tables_total} tables
            {report.connector ? ` · connector: ${report.connector}` : ' · no connector yet'}
          </span>
        </div>

        {report.probe_error && (
          <div className="flex items-start gap-2 rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900">
            <AlertCircle className="h-4 w-4 mt-0.5 shrink-0" />
            <span>
              Live details could not be read, so this report is based on the source type alone.
              {' '}{report.probe_error}
            </span>
          </div>
        )}

        <ProbeFacts report={report} />

        <div className="overflow-x-auto rounded-lg border">
          <table className="w-full text-sm">
            <thead className="bg-muted">
              <tr>
                <th className="text-left font-semibold px-4 py-2">Table</th>
                <th className="text-left font-semibold px-4 py-2">Holds</th>
                <th className="text-left font-semibold px-4 py-2">Result</th>
              </tr>
            </thead>
            <tbody>
              {report.tables.map((t, i) => {
                const meta = FILL_META[t.fill]
                const Icon = meta.icon
                return (
                  <tr key={t.table} className={i % 2 === 0 ? 'bg-card' : 'bg-muted/40'}>
                    <td className="px-4 py-2 font-medium text-foreground whitespace-nowrap">{t.label}</td>
                    <td className="px-4 py-2 text-muted-foreground">{t.description}</td>
                    <td className="px-4 py-2">
                      <div className={cn('flex items-start gap-1.5', meta.className)}>
                        <Icon className="h-4 w-4 mt-0.5 shrink-0" />
                        <div>
                          <div className="font-medium">{meta.label}</div>
                          {t.note && <div className="text-xs text-muted-foreground mt-0.5">{t.note}</div>}
                        </div>
                      </div>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>

        {report.warnings.length > 0 && (
          <div>
            <h4 className="text-sm font-semibold text-foreground mb-2">Worth knowing</h4>
            <ul className="space-y-1.5">
              {report.warnings.map((w) => (
                <li key={w} className="flex items-start gap-2 text-sm text-muted-foreground">
                  <AlertCircle className="h-4 w-4 mt-0.5 shrink-0 text-amber-600" />
                  <span>{w}</span>
                </li>
              ))}
            </ul>
          </div>
        )}

        {report.next_steps.length > 0 && (
          <div>
            <h4 className="text-sm font-semibold text-foreground mb-2">What to do next</h4>
            <ol className="space-y-1.5 list-decimal list-inside">
              {report.next_steps.map((s) => (
                <li key={s} className="text-sm text-muted-foreground">{s}</li>
              ))}
            </ol>
          </div>
        )}
      </div>
    </Section>
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
