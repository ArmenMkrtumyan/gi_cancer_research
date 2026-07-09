import { type LucideIcon } from 'lucide-react'
import { cn } from '@/lib/utils'

interface SectionProps {
  title: string
  icon?: LucideIcon
  description?: string
  action?: React.ReactNode
  className?: string
  children: React.ReactNode
}

/** A white dashboard card with an icon + title header (university-app design language). */
export default function Section({ title, icon: Icon, description, action, className, children }: SectionProps) {
  return (
    <section className={cn('bg-card rounded-lg border shadow-sm p-6', className)}>
      <div className="flex items-start justify-between gap-4 mb-5">
        <div className="flex items-center gap-2">
          {Icon && <Icon className="h-5 w-5 text-brand" />}
          <div>
            <h2 className="text-xl font-bold text-brand">{title}</h2>
            {description && <p className="text-sm text-muted-foreground mt-0.5">{description}</p>}
          </div>
        </div>
        {action && <div className="flex-shrink-0">{action}</div>}
      </div>
      {children}
    </section>
  )
}
