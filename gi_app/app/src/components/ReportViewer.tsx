import { useEffect, useState } from 'react'
import { AlertCircle, ExternalLink, FileText } from 'lucide-react'
import { fetchDownloadUrl, type CaseReport } from '@/lib/api'
import { formatBytes } from '@/lib/utils'
import { Button } from '@/components/ui/button'

/** The pathologist's report PDF for one patient, rendered inline from object storage.
 *
 * The PDF is not proxied through the API — the browser fetches it straight from object
 * storage using a short-lived signed URL, the same path the slide viewer uses. The link
 * is minted on open rather than with the case data because it expires.
 */
export default function ReportViewer({ reports }: { reports: CaseReport[] }) {
  const [urls, setUrls] = useState<Record<number, string>>({})
  const [error, setError] = useState<string | null>(null)
  const [active, setActive] = useState(0)

  const report = reports[active]

  useEffect(() => {
    if (!report || urls[report.asset_id]) return
    let cancelled = false
    fetchDownloadUrl(report.asset_id, 3600, true)
      .then((d) => {
        if (!cancelled) setUrls((prev) => ({ ...prev, [report.asset_id]: d.url }))
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : 'Could not open the report')
      })
    return () => {
      cancelled = true
    }
  }, [report, urls])

  if (reports.length === 0)
    return (
      <p className="text-sm text-muted-foreground py-4">
        No pathology report was published for this patient.
      </p>
    )

  if (error)
    return (
      <div className="text-sm text-muted-foreground py-4 flex items-center gap-2">
        <AlertCircle className="h-4 w-4 text-destructive" /> {error}
      </div>
    )

  const url = report ? urls[report.asset_id] : undefined

  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2 flex-wrap">
        <FileText className="h-4 w-4 text-brand shrink-0" />
        <span className="text-sm text-muted-foreground">
          Scanned pathologist's report as published by GDC. Layout and wording vary by
          submitting institution.
        </span>
        {url && (
          <Button size="sm" variant="outline" className="ml-auto" asChild>
            <a href={url} target="_blank" rel="noreferrer">
              <ExternalLink className="h-4 w-4 mr-1" /> Open in new tab
            </a>
          </Button>
        )}
      </div>

      {/* Some patients have more than one report file. */}
      {reports.length > 1 && (
        <div className="flex flex-wrap gap-1">
          {reports.map((r, i) => (
            <button
              key={r.asset_id}
              type="button"
              onClick={() => setActive(i)}
              className={`px-2 py-1 text-xs rounded border transition-colors ${
                i === active
                  ? 'border-brand text-brand font-medium'
                  : 'border-transparent text-muted-foreground hover:text-foreground'
              }`}
            >
              Report {i + 1}
              {r.size_bytes ? ` · ${formatBytes(r.size_bytes)}` : ''}
            </button>
          ))}
        </div>
      )}

      {url ? (
        <object data={url} type="application/pdf" className="w-full h-[32rem] rounded border">
          {/* Browsers without an inline PDF viewer (and most mobile ones) render this instead. */}
          <p className="text-sm text-muted-foreground p-4">
            This browser cannot display the PDF inline.{' '}
            <a href={url} target="_blank" rel="noreferrer" className="text-brand underline">
              Open it in a new tab
            </a>
            .
          </p>
        </object>
      ) : (
        <p className="text-sm text-muted-foreground py-4">Loading report…</p>
      )}
    </div>
  )
}
