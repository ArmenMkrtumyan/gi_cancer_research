import { formatNumber } from '@/lib/utils'

interface MetricBarProps {
  label: string
  present: number
  total: number
  unit: string
  completeness: number | null
}

/** A single completeness meter: label, a filled track, and a present/total caption. */
export default function MetricBar({ label, present, total, unit, completeness }: MetricBarProps) {
  const pct = completeness ?? 0
  // Green when well-populated, amber mid, red when sparse — quick visual triage.
  const fill = pct >= 80 ? 'bg-green-500' : pct >= 40 ? 'bg-gold' : 'bg-red-400'
  return (
    <div>
      <div className="flex items-baseline justify-between gap-2 mb-1">
        <span className="text-sm font-medium text-foreground truncate">{label}</span>
        <span className="text-xs text-muted-foreground flex-shrink-0">
          {completeness === null ? '—' : `${pct}%`}
        </span>
      </div>
      <div className="h-2 rounded-full bg-muted overflow-hidden">
        <div className={`h-full rounded-full ${fill}`} style={{ width: `${pct}%` }} />
      </div>
      <p className="text-xs text-muted-foreground mt-1">
        {formatNumber(present)} / {formatNumber(total)} {unit}
      </p>
    </div>
  )
}
