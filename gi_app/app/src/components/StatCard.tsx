import { type LucideIcon } from 'lucide-react'
import { cn } from '@/lib/utils'

interface StatCardProps {
  title: string
  value: string | number
  icon: LucideIcon
  /** Tailwind background class for the icon chip, e.g. "bg-teal-600". */
  color?: string
  sub?: string
}

/** KPI tile: colored icon chip + label + big value (mirrors university-app stat cards). */
export default function StatCard({ title, value, icon: Icon, color = 'bg-brand', sub }: StatCardProps) {
  return (
    <div className="bg-card rounded-lg border shadow-sm p-5 flex items-center gap-4">
      <div className={cn('p-3 rounded-lg text-white flex-shrink-0', color)}>
        <Icon className="h-6 w-6" />
      </div>
      <div className="min-w-0">
        <p className="text-sm text-muted-foreground truncate">{title}</p>
        <p className="text-2xl font-bold text-brand">{value}</p>
        {sub && <p className="text-xs text-muted-foreground mt-0.5 truncate">{sub}</p>}
      </div>
    </div>
  )
}
