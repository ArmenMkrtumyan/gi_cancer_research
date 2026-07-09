import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  Database, Users, FlaskConical, Layers, Image, HardDrive, BarChart3, AlertCircle,
} from 'lucide-react'
import {
  BarChart, Bar, Cell, XAxis, YAxis, Tooltip, ResponsiveContainer,
} from 'recharts'
import { fetchOverview, type Overview as OverviewData, type DatasetRow } from '@/lib/api'
import { formatNumber, formatBytes } from '@/lib/utils'
import { SERIES } from '@/lib/theme'
import StatCard from '@/components/StatCard'
import Section from '@/components/Section'
import StatusPill from '@/components/StatusPill'
import { Badge } from '@/components/ui/badge'

// A run is "clean" when it finished successfully — no need to surface it in that case.
function isSuccessfulRun(status: string | null): boolean {
  return ['success', 'completed', 'done'].includes((status || '').toLowerCase())
}

function AccessBadge({ access }: { access: string | null }) {
  if (!access) return <Badge variant="secondary">unknown</Badge>
  return access.toLowerCase() === 'open'
    ? <Badge variant="success">open</Badge>
    : <Badge variant="gold">{access}</Badge>
}

export default function Overview() {
  const [data, setData] = useState<OverviewData | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    load()
  }, [])

  const load = async () => {
    setLoading(true)
    setError(null)
    try {
      setData(await fetchOverview())
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load overview')
    } finally {
      setLoading(false)
    }
  }

  if (loading) {
    return (
      <div className="bg-card rounded-lg border shadow-sm p-10 text-center text-muted-foreground">
        Loading overview…
      </div>
    )
  }

  if (error || !data) {
    return (
      <div className="bg-card rounded-lg border shadow-sm p-10 text-center">
        <AlertCircle className="h-8 w-8 text-destructive mx-auto mb-3" />
        <p className="text-foreground font-medium">Could not reach the API</p>
        <p className="text-sm text-muted-foreground mt-1">{error}</p>
        <button onClick={load} className="mt-4 text-sm text-primary hover:underline">
          Retry
        </button>
      </div>
    )
  }

  const tiles = [
    { title: 'Datasets', value: formatNumber(data.datasets), icon: Database, color: 'bg-brand' },
    { title: 'Cases', value: formatNumber(data.cases), icon: Users, color: 'bg-teal-600' },
    { title: 'Samples', value: formatNumber(data.samples), icon: FlaskConical, color: 'bg-cyan-600' },
    { title: 'Slides', value: formatNumber(data.slides), icon: Layers, color: 'bg-violet-600' },
    { title: 'WSI assets', value: formatNumber(data.wsi_assets), icon: Image, color: 'bg-amber-500' },
    { title: 'Stored size', value: formatBytes(data.total_asset_bytes), icon: HardDrive, color: 'bg-slate-600' },
  ]

  const casesByDataset = [...data.datasets_table]
    .sort((a, b) => b.n_cases - a.n_cases)
    .slice(0, 15)
    .map((d) => ({ name: d.name, cases: d.n_cases }))

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-3xl font-bold text-brand">Overview</h1>
        {data.latest_run && !isSuccessfulRun(data.latest_run.status) && (
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            <span>Latest run:</span>
            <StatusPill status={data.latest_run.status} />
            <span className="hidden sm:inline">{data.latest_run.dataset_name}</span>
          </div>
        )}
      </header>

      <div className="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-6 gap-4">
        {tiles.map((t) => (
          <StatCard key={t.title} {...t} />
        ))}
      </div>

      {casesByDataset.length > 0 && (
        <Section title="Cases by dataset" icon={BarChart3}>
          <ResponsiveContainer width="100%" height={Math.max(120, casesByDataset.length * 40)}>
            <BarChart
              layout="vertical"
              data={casesByDataset}
              margin={{ top: 4, right: 24, left: 8, bottom: 4 }}
            >
              <XAxis type="number" allowDecimals={false} tick={{ fontSize: 11 }} />
              <YAxis type="category" dataKey="name" width={150} tick={{ fontSize: 11 }} />
              <Tooltip
                cursor={{ fill: 'rgba(13,59,79,0.06)' }}
                contentStyle={{ fontSize: 12, borderRadius: 8 }}
              />
              <Bar dataKey="cases" radius={[0, 4, 4, 0]}>
                {casesByDataset.map((_, i) => (
                  <Cell key={i} fill={SERIES[i % SERIES.length]} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </Section>
      )}

      <Section title="Datasets" icon={Database} description="Select a dataset to inspect its clinical, biospecimen, and slide data.">
        <DatasetTable rows={data.datasets_table} />
      </Section>
    </div>
  )
}

function DatasetTable({ rows }: { rows: DatasetRow[] }) {
  if (rows.length === 0) {
    return <p className="text-sm text-muted-foreground py-6 text-center">No datasets ingested yet.</p>
  }
  return (
    <div className="overflow-x-auto rounded-lg border">
      <table className="w-full text-sm">
        <thead className="bg-brand text-white">
          <tr>
            <th className="text-left font-semibold px-4 py-3">Dataset</th>
            <th className="text-left font-semibold px-4 py-3">Cancer type(s)</th>
            <th className="text-left font-semibold px-4 py-3">Access</th>
            <th className="text-right font-semibold px-4 py-3">Cases</th>
            <th className="text-right font-semibold px-4 py-3">Files</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((d, i) => (
            <tr key={d.dataset_id} className={i % 2 === 0 ? 'bg-card' : 'bg-muted/40'}>
              <td className="px-4 py-3">
                <Link to={`/datasets/${d.dataset_id}`} className="font-medium text-primary hover:underline">
                  {d.name}
                </Link>
              </td>
              <td className="px-4 py-3 text-muted-foreground">{d.gi_cancer_types || '—'}</td>
              <td className="px-4 py-3"><AccessBadge access={d.access_type} /></td>
              <td className="px-4 py-3 text-right tabular-nums">{formatNumber(d.n_cases)}</td>
              <td className="px-4 py-3 text-right tabular-nums">{formatNumber(d.n_files)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
