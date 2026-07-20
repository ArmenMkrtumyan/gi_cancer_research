import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import {
  ArrowLeft, ExternalLink, Database, BarChart3, HeartPulse, ListChecks, Layers,
  History, AlertCircle, ShieldCheck,
} from 'lucide-react'
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer } from 'recharts'
import {
  fetchDatasetSummary, fetchSurvival, fetchMissingness,
  fetchIngestionRuns, fetchDatasetAccess,
  type DatasetSummary, type Survival, type Missingness,
  type IngestionRun, type MissingnessMetric, type AccessBreakdown,
} from '@/lib/api'
import { formatNumber, formatBytes, formatDateTime, humanizeKey, slideTypeLabel } from '@/lib/utils'
import { CHART } from '@/lib/theme'
import Section from '@/components/Section'
import StatusPill from '@/components/StatusPill'
import AccessBar from '@/components/AccessBar'
import DistributionChart from '@/components/DistributionChart'
import MetricBar from '@/components/MetricBar'
import CohortExplorer from '@/components/CohortExplorer'

const COUNT_ORDER = [
  'cases', 'diagnoses', 'treatments', 'pathology_details', 'follow_ups', 'molecular_tests',
  'samples', 'portions', 'analytes', 'aliquots', 'slides', 'annotations', 'data_assets',
]

const DISTRIBUTIONS: { key: string; title: string }[] = [
  { key: 'sex_at_birth', title: 'Sex at birth' },
  { key: 'vital_status', title: 'Alive vs. deceased' },
  { key: 'ajcc_stage', title: 'Cancer stage (AJCC)' },
  { key: 'sample_type', title: 'Sample type' },
  { key: 'slide_type', title: 'Slide type' },
]

export default function DatasetDetail() {
  const { datasetId } = useParams()
  const id = Number(datasetId)

  const [summary, setSummary] = useState<DatasetSummary | null>(null)
  const [survival, setSurvival] = useState<Survival | null>(null)
  const [missingness, setMissingness] = useState<Missingness | null>(null)
  const [runs, setRuns] = useState<IngestionRun[] | null>(null)
  const [loading, setLoading] = useState(true)
  const [coreError, setCoreError] = useState<string | null>(null)

  // Access breakdown is a live GDC scan (seconds) — load it separately so it never blocks the page.
  const [access, setAccess] = useState<AccessBreakdown | null>(null)
  const [accessLoading, setAccessLoading] = useState(true)

  useEffect(() => {
    if (!Number.isFinite(id)) {
      setCoreError('Invalid dataset id')
      setLoading(false)
      return
    }
    load(id)
    setAccessLoading(true)
    setAccess(null)
    fetchDatasetAccess(id)
      .then(setAccess)
      .catch(() => setAccess(null))
      .finally(() => setAccessLoading(false))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id])

  const load = async (datasetId: number) => {
    setLoading(true)
    setCoreError(null)
    // Load every panel independently — one failing endpoint must not blank the page.
    const [s, surv, miss, rr] = await Promise.allSettled([
      fetchDatasetSummary(datasetId),
      fetchSurvival(datasetId),
      fetchMissingness(datasetId),
      fetchIngestionRuns(datasetId),
    ])
    if (s.status === 'fulfilled') setSummary(s.value)
    else setCoreError(s.reason instanceof Error ? s.reason.message : 'Failed to load dataset')
    setSurvival(surv.status === 'fulfilled' ? surv.value : null)
    setMissingness(miss.status === 'fulfilled' ? miss.value : null)
    setRuns(rr.status === 'fulfilled' ? rr.value : null)
    setLoading(false)
  }

  if (loading) {
    return (
      <div className="bg-card rounded-lg border shadow-sm p-10 text-center text-muted-foreground">
        Loading dataset…
      </div>
    )
  }

  if (coreError || !summary) {
    return (
      <div className="bg-card rounded-lg border shadow-sm p-10 text-center">
        <AlertCircle className="h-8 w-8 text-destructive mx-auto mb-3" />
        <p className="text-foreground font-medium">Could not load this dataset</p>
        <p className="text-sm text-muted-foreground mt-1">{coreError}</p>
        <Link to="/" className="mt-4 inline-block text-sm text-primary hover:underline">
          ← Back to overview
        </Link>
      </div>
    )
  }

  const ds = summary.dataset

  return (
    <div className="space-y-6">
      {/* Hero */}
      <div>
        <Link to="/" className="inline-flex items-center gap-1 text-sm text-primary hover:underline mb-3">
          <ArrowLeft className="h-4 w-4" /> Back to overview
        </Link>
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h1 className="text-3xl font-bold text-brand">{ds.name}</h1>
            <p className="text-sm text-muted-foreground mt-1">
              {[ds.gi_cancer_types, ds.access_type, `${formatNumber(ds.n_cases)} cases`, `${formatNumber(ds.n_files)} files`]
                .filter(Boolean)
                .join('  ·  ')}
            </p>
          </div>
          <div className="flex items-center gap-3">
            {summary.latest_run && <StatusPill status={summary.latest_run.status} />}
            {ds.official_page && (
              <a
                href={ds.official_page}
                target="_blank"
                rel="noreferrer"
                className="inline-flex items-center gap-1 text-sm text-primary hover:underline"
              >
                Source <ExternalLink className="h-3.5 w-3.5" />
              </a>
            )}
          </div>
        </div>
      </div>

      {/* Table counts */}
      <Section title="Loaded records" icon={Database} description={`How many rows were loaded per table. ${formatBytes(summary.asset_bytes)} of slide images stored.`}>
        <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-6 gap-3">
          {COUNT_ORDER.filter((k) => k in summary.table_counts).map((k) => (
            <div key={k} className="border rounded-lg p-3">
              <p className="text-xs text-muted-foreground truncate">{humanizeKey(k)}</p>
              <p className="text-xl font-bold text-brand tabular-nums">{formatNumber(summary.table_counts[k])}</p>
            </div>
          ))}
        </div>
      </Section>

      {/* Access & availability */}
      <Section
        title="Access & availability"
        icon={ShieldCheck}
        description="How much of this project's data at GDC is publicly downloadable vs controlled-access (dbGaP). Clinical & biospecimen metadata is fully open — no fields are hidden; controlled access gates raw genomics (sequencing reads, germline/somatic variants)."
      >
        {accessLoading ? (
          <p className="text-sm text-muted-foreground py-4">Loading access breakdown from GDC…</p>
        ) : access && access.available ? (
          <AccessPanel access={access} />
        ) : (
          <p className="text-sm text-muted-foreground py-4">
            {access?.reason || 'Access breakdown is unavailable for this dataset.'}
          </p>
        )}
      </Section>

      {/* Distributions */}
      <Section title="Key distributions" icon={BarChart3} description="How this cohort breaks down across a few key attributes.">
        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
          {DISTRIBUTIONS.map((d) => {
            const raw = summary.distributions[d.key] ?? []
            const data =
              d.key === 'slide_type' ? raw.map((r) => ({ ...r, label: slideTypeLabel(r.label) })) : raw
            return <DistributionChart key={d.key} title={d.title} data={data} />
          })}
        </div>
      </Section>

      {/* Survival */}
      <Section
        title="Patient survival"
        icon={HeartPulse}
        description="How long patients lived after diagnosis — the key outcome for prognosis. 'Survival time' = days from diagnosis to death, or to the last time a patient was known to be alive."
      >
        {survival ? <SurvivalPanel survival={survival} /> : <Unavailable what="survival" />}
      </Section>

      {/* Missingness */}
      <Section title="Metadata completeness" icon={ListChecks} description="What share of records actually have each field filled in — higher is more complete.">
        {missingness ? <MissingnessPanel metrics={missingness.metrics} /> : <Unavailable what="completeness" />}
      </Section>

      {/* Slide explorer */}
      <Section
        title="Slide explorer"
        icon={Layers}
        description="Filter the patient cohort, then open a patient's slide in the deep-zoom viewer (pan and zoom the full image)."
      >
        <CohortExplorer datasetId={id} />
      </Section>

      {/* Ingestion runs */}
      <Section title="Ingestion runs" icon={History}>
        {runs && runs.length > 0 ? <RunsPanel runs={runs} /> : <Unavailable what="ingestion runs" />}
      </Section>
    </div>
  )
}

function Unavailable({ what }: { what: string }) {
  return <p className="text-sm text-muted-foreground py-4">No {what} data available.</p>
}

function MiniStat({ label, value, hint }: { label: string; value: string | number; hint?: string }) {
  return (
    <div
      className={`border rounded-lg p-3 text-center${hint ? ' cursor-help' : ''}`}
      title={hint}
    >
      <p className="text-2xl font-bold text-brand tabular-nums">{value}</p>
      <p className="text-xs text-muted-foreground mt-0.5">{label}</p>
    </div>
  )
}

function AccessPanel({ access }: { access: AccessBreakdown }) {
  const open = access.open ?? { files: 0, bytes: 0 }
  const controlled = access.controlled ?? { files: 0, bytes: 0 }
  const categories = access.by_category ?? []
  return (
    <div className="space-y-5">
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        <MiniStat label="Open files" value={formatNumber(open.files)} hint="Publicly downloadable — no authorization needed" />
        <MiniStat label="Open volume" value={formatBytes(open.bytes)} hint="Total size of openly downloadable files" />
        <MiniStat label="Controlled files" value={formatNumber(controlled.files)} hint="Require dbGaP / NIH authorization to download" />
        <MiniStat label="Controlled volume" value={formatBytes(controlled.bytes)} hint="Total size behind controlled access" />
      </div>

      <AccessBar open={open} controlled={controlled} height="h-3" />

      <div>
        <p className="text-sm font-semibold text-brand mb-2">By data category</p>
        <div className="overflow-x-auto rounded-lg border">
          <table className="w-full text-sm">
            <thead className="bg-brand text-white">
              <tr>
                <th className="text-left font-semibold px-4 py-2.5">Data category</th>
                <th className="text-right font-semibold px-4 py-2.5">Open files</th>
                <th className="text-right font-semibold px-4 py-2.5">Open size</th>
                <th className="text-right font-semibold px-4 py-2.5">Controlled files</th>
                <th className="text-right font-semibold px-4 py-2.5">Controlled size</th>
              </tr>
            </thead>
            <tbody>
              {categories.map((c, i) => (
                <tr key={c.category} className={i % 2 === 0 ? 'bg-card' : 'bg-muted/40'}>
                  <td className="px-4 py-2 capitalize">{c.category}</td>
                  <td className="px-4 py-2 text-right tabular-nums text-emerald-700">{c.open_files ? formatNumber(c.open_files) : '—'}</td>
                  <td className="px-4 py-2 text-right tabular-nums text-muted-foreground">{c.open_bytes ? formatBytes(c.open_bytes) : '—'}</td>
                  <td className="px-4 py-2 text-right tabular-nums text-amber-700">{c.controlled_files ? formatNumber(c.controlled_files) : '—'}</td>
                  <td className="px-4 py-2 text-right tabular-nums text-muted-foreground">{c.controlled_bytes ? formatBytes(c.controlled_bytes) : '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {access.fetched_at && (
        <p className="text-xs text-muted-foreground">Live from GDC · fetched {formatDateTime(access.fetched_at)}</p>
      )}
    </div>
  )
}

function SurvivalPanel({ survival }: { survival: Survival }) {
  const s = survival.summary
  const histdata = survival.histogram.map((b) => ({ range: `${b.start}–${b.end}`, count: b.count }))
  return (
    <div className="space-y-5">
      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-3">
        <MiniStat label="Patients" value={formatNumber(s.total_cases)} hint="Total patients in this dataset" />
        <MiniStat
          label="With survival data"
          value={formatNumber(s.with_os_time)}
          hint="Patients who have a recorded survival time"
        />
        <MiniStat
          label="Died"
          value={formatNumber(s.os_events_dead)}
          hint="Patients recorded as having died during follow-up"
        />
        <MiniStat
          label="Alive at last contact"
          value={formatNumber(s.alive_or_censored)}
          hint="Still alive, or last known alive — their full survival time is unknown (censored)"
        />
        <MiniStat
          label="Median time to death (days)"
          value={s.median_time_to_death ?? '—'}
          hint="Among patients who died, the median number of days from diagnosis to death."
        />
      </div>
      {histdata.length > 0 && (
        <div>
          <p className="text-sm font-semibold text-brand mb-1">Time from diagnosis to death (days)</p>
          <p className="text-xs text-muted-foreground mb-3">
            Only patients who died are shown — those still alive are left out, since we don't yet know their full survival time.
          </p>
          <ResponsiveContainer width="100%" height={240}>
            <BarChart data={histdata} margin={{ top: 4, right: 8, left: -12, bottom: 20 }}>
              <XAxis dataKey="range" tick={{ fontSize: 10 }} angle={-30} textAnchor="end" height={50} interval={0} />
              <YAxis allowDecimals={false} tick={{ fontSize: 11 }} width={36} />
              <Tooltip cursor={{ fill: 'rgba(13,59,79,0.06)' }} contentStyle={{ fontSize: 12, borderRadius: 8 }} />
              <Bar dataKey="count" fill={CHART.teal} radius={[4, 4, 0, 0]} />
            </BarChart>
          </ResponsiveContainer>
        </div>
      )}
    </div>
  )
}

function MissingnessPanel({ metrics }: { metrics: MissingnessMetric[] }) {
  // Group meters by their category for readable columns.
  const groups: Record<string, MissingnessMetric[]> = {}
  for (const m of metrics) {
    ;(groups[m.category] ??= []).push(m)
  }
  return (
    <div className="columns-1 md:columns-2 gap-x-8">
      {Object.entries(groups).map(([category, items]) => (
        <div key={category} className="mb-6 break-inside-avoid">
          <p className="text-xs uppercase tracking-wide text-muted-foreground mb-3">{category}</p>
          <div className="space-y-4">
            {items.map((m) => (
              <MetricBar
                key={m.label}
                label={m.label}
                present={m.present}
                total={m.total}
                unit={m.unit}
                completeness={m.completeness_pct}
              />
            ))}
          </div>
        </div>
      ))}
    </div>
  )
}

function RunsPanel({ runs }: { runs: IngestionRun[] }) {
  return (
    <div className="overflow-x-auto rounded-lg border">
      <table className="w-full text-sm">
        <thead className="bg-brand text-white">
          <tr>
            <th className="text-left font-semibold px-4 py-3">Run</th>
            <th className="text-left font-semibold px-4 py-3">Connector</th>
            <th className="text-left font-semibold px-4 py-3">Status</th>
            <th className="text-left font-semibold px-4 py-3">Started</th>
            <th className="text-left font-semibold px-4 py-3">Finished</th>
          </tr>
        </thead>
        <tbody>
          {runs.map((r, i) => (
            <tr key={r.run_id} className={i % 2 === 0 ? 'bg-card' : 'bg-muted/40'}>
              <td className="px-4 py-2.5 tabular-nums">#{r.run_id}</td>
              <td className="px-4 py-2.5">{r.connector || '—'}</td>
              <td className="px-4 py-2.5"><StatusPill status={r.status} /></td>
              <td className="px-4 py-2.5 text-muted-foreground">{formatDateTime(r.started_at)}</td>
              <td className="px-4 py-2.5 text-muted-foreground">{formatDateTime(r.finished_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
