import { formatNumber } from '@/lib/utils'

/**
 * A stacked bar showing a GDC project's open (publicly downloadable) vs controlled
 * (dbGaP-gated) file split, with a "% open / % controlled" caption. Sizing is by file count.
 */
export default function AccessBar({
  open,
  controlled,
  height = 'h-2',
}: {
  open: { files: number; bytes: number }
  controlled: { files: number; bytes: number }
  height?: string
}) {
  const total = open.files + controlled.files
  const openPct = total ? (open.files / total) * 100 : 0
  return (
    <div className="min-w-[150px]">
      <div className={`flex ${height} rounded-full overflow-hidden bg-muted`}>
        <div
          className="bg-emerald-500"
          style={{ width: `${openPct}%` }}
          title={`Open: ${formatNumber(open.files)} files`}
        />
        <div
          className="bg-amber-500"
          style={{ width: `${100 - openPct}%` }}
          title={`Controlled: ${formatNumber(controlled.files)} files`}
        />
      </div>
      <div className="flex justify-between text-[11px] text-muted-foreground mt-1">
        <span>{openPct.toFixed(0)}% open</span>
        <span>{(100 - openPct).toFixed(0)}% controlled</span>
      </div>
    </div>
  )
}
