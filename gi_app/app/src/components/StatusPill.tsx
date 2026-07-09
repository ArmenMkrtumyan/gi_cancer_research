import { Badge } from '@/components/ui/badge'

/** Map an ingestion-run status string to a colored badge. */
export default function StatusPill({ status }: { status: string | null | undefined }) {
  if (!status) return <Badge variant="secondary">unknown</Badge>
  const s = status.toLowerCase()
  if (s === 'success' || s === 'completed' || s === 'done') return <Badge variant="success">{status}</Badge>
  if (s === 'failed' || s === 'error') return <Badge variant="destructive">{status}</Badge>
  if (['running', 'started', 'in_progress', 'pending', 'downloading', 'ingesting'].includes(s))
    return <Badge variant="warning">{status}</Badge>
  return <Badge variant="secondary">{status}</Badge>
}
