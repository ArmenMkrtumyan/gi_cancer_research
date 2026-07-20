import { useEffect, useMemo, useState } from 'react'
import { Eye, X, AlertCircle } from 'lucide-react'
import { fetchCases, type CohortCase, type CaseSlide } from '@/lib/api'
import { formatNumber, slideTypeLabel, slideTypeDescription } from '@/lib/utils'
import { Button } from '@/components/ui/button'
import SlideViewer from '@/components/SlideViewer'
import PatientTimeline from '@/components/PatientTimeline'
import AnnotationPanel from '@/components/AnnotationPanel'

const ALL = 'All'

type PatientTab = 'slide' | 'timeline' | 'annotations'

const TAB_LABELS: Record<PatientTab, string> = {
  slide: 'Slide viewer',
  timeline: 'Clinical timeline',
  annotations: 'Metadata & annotations',
}

function uniqueSorted(values: (string | null)[]): string[] {
  return Array.from(new Set(values.filter((v): v is string => !!v))).sort()
}

/** Filter the patient cohort, then open a patient's slide in the deep-zoom viewer. */
export default function CohortExplorer({ datasetId }: { datasetId: number }) {
  const [cases, setCases] = useState<CohortCase[] | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const [sex, setSex] = useState(ALL)
  const [status, setStatus] = useState(ALL)
  const [stage, setStage] = useState(ALL)
  const [withSlidesOnly, setWithSlidesOnly] = useState(false)

  const [viewing, setViewing] = useState<
    { slide: CaseSlide; caseBarcode: string | null; caseId: string } | null
  >(null)
  const [tab, setTab] = useState<PatientTab>('slide')

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    fetchCases(datasetId)
      .then((data) => {
        if (!cancelled) {
          setCases(data)
          setLoading(false)
        }
      })
      .catch((err) => {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : 'Failed to load patients')
          setLoading(false)
        }
      })
    return () => {
      cancelled = true
    }
  }, [datasetId])

  const sexes = useMemo(() => uniqueSorted((cases ?? []).map((c) => c.sex)), [cases])
  const statuses = useMemo(() => uniqueSorted((cases ?? []).map((c) => c.vital_status)), [cases])
  const stages = useMemo(() => uniqueSorted((cases ?? []).map((c) => c.stage)), [cases])

  const filtered = useMemo(() => {
    let rows = cases ?? []
    if (sex !== ALL) rows = rows.filter((c) => c.sex === sex)
    if (status !== ALL) rows = rows.filter((c) => c.vital_status === status)
    if (stage !== ALL) rows = rows.filter((c) => c.stage === stage)
    if (withSlidesOnly) rows = rows.filter((c) => c.slides.length > 0)
    // Patients with a viewable slide float to the top.
    return [...rows].sort((a, b) => b.slides.length - a.slides.length)
  }, [cases, sex, status, stage, withSlidesOnly])

  if (loading) return <p className="text-sm text-muted-foreground py-4">Loading patients…</p>
  if (error || !cases)
    return (
      <div className="text-sm text-muted-foreground py-4 flex items-center gap-2">
        <AlertCircle className="h-4 w-4 text-destructive" /> {error}
      </div>
    )

  const withSlides = cases.filter((c) => c.slides.length > 0).length

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end gap-3">
        <FilterSelect label="Sex" value={sex} onChange={setSex} options={sexes} />
        <FilterSelect label="Status" value={status} onChange={setStatus} options={statuses} />
        <FilterSelect label="Cancer stage" value={stage} onChange={setStage} options={stages} />
        <label className="flex items-center gap-2 text-sm text-foreground cursor-pointer select-none pb-1.5">
          <input
            type="checkbox"
            checked={withSlidesOnly}
            onChange={(e) => setWithSlidesOnly(e.target.checked)}
            className="accent-brand h-4 w-4"
          />
          Only patients with a slide image
        </label>
        <span className="text-xs text-muted-foreground ml-auto pb-1.5">
          {formatNumber(filtered.length)} of {formatNumber(cases.length)} patients · {withSlides} with a viewable slide
        </span>
      </div>

      {viewing && (
        <div className="border rounded-lg p-3">
          <div className="flex items-center justify-between mb-2">
            <p className="text-sm font-semibold text-brand">
              {viewing.caseBarcode}{' '}
              <span className="font-normal text-muted-foreground font-mono text-xs">
                {viewing.slide.slide_barcode}
              </span>
            </p>
            <Button size="sm" variant="ghost" onClick={() => setViewing(null)}>
              <X className="h-4 w-4 mr-1" /> Close
            </Button>
          </div>

          <div className="flex items-center gap-1 border-b mb-3">
            {(['slide', 'timeline', 'annotations'] as PatientTab[]).map((t) => (
              <button
                key={t}
                type="button"
                onClick={() => setTab(t)}
                className={`px-3 py-1.5 text-sm border-b-2 -mb-px transition-colors ${
                  tab === t
                    ? 'border-brand text-brand font-medium'
                    : 'border-transparent text-muted-foreground hover:text-foreground'
                }`}
              >
                {TAB_LABELS[t]}
              </button>
            ))}
          </div>

          {/* The viewer stays mounted across tabs so switching away and back does not
              re-download tiles or reset the zoom the user had set. */}
          <div className={tab === 'slide' ? '' : 'hidden'}>
            <SlideViewer assetId={viewing.slide.asset_id} />
          </div>
          {tab === 'timeline' && (
            <PatientTimeline
              caseId={viewing.caseId}
              onOpenSlide={(assetId) => {
                const match = cases?.flatMap((c) => c.slides).find((s) => s.asset_id === assetId)
                if (match) setViewing({ ...viewing, slide: match })
                setTab('slide')
              }}
            />
          )}
          {tab === 'annotations' && <AnnotationPanel caseId={viewing.caseId} />}
        </div>
      )}

      <div className="overflow-auto max-h-[26rem] rounded-lg border">
        <table className="w-full text-sm">
          <thead className="bg-brand text-white sticky top-0 z-10">
            <tr>
              <th className="text-left font-semibold px-4 py-3">Patient</th>
              <th className="text-left font-semibold px-4 py-3">Sex</th>
              <th className="text-left font-semibold px-4 py-3">Status</th>
              <th className="text-left font-semibold px-4 py-3">Stage</th>
              <th className="text-right font-semibold px-4 py-3">Survival (days)</th>
              <th className="text-left font-semibold px-4 py-3">Slides</th>
            </tr>
          </thead>
          <tbody>
            {filtered.map((c, i) => (
              <tr key={c.case_id} className={i % 2 === 0 ? 'bg-card' : 'bg-muted/40'}>
                <td className="px-4 py-2.5 font-mono text-xs">{c.case_barcode}</td>
                <td className="px-4 py-2.5">{c.sex || '—'}</td>
                <td className="px-4 py-2.5">{c.vital_status || '—'}</td>
                <td className="px-4 py-2.5">{c.stage || '—'}</td>
                <td className="px-4 py-2.5 text-right tabular-nums">{c.os_time ?? '—'}</td>
                <td className="px-4 py-2.5">
                  {c.slides.length === 0 ? (
                    <span className="text-muted-foreground">—</span>
                  ) : (
                    <div className="flex flex-wrap gap-1">
                      {c.slides.map((s) => (
                        <Button
                          key={s.asset_id}
                          size="sm"
                          variant="outline"
                          title={slideTypeDescription(s.slide_type)}
                          onClick={() =>
                            setViewing({ slide: s, caseBarcode: c.case_barcode, caseId: c.case_id })
                          }
                        >
                          <Eye className="h-3.5 w-3.5 mr-1" /> {slideTypeLabel(s.slide_type)}
                        </Button>
                      ))}
                    </div>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function FilterSelect({
  label,
  value,
  onChange,
  options,
}: {
  label: string
  value: string
  onChange: (v: string) => void
  options: string[]
}) {
  return (
    <label className="flex flex-col gap-1 text-xs text-muted-foreground">
      {label}
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="border rounded-md px-2 py-1.5 text-sm text-foreground bg-card min-w-[130px]"
      >
        <option value={ALL}>All</option>
        {options.map((o) => (
          <option key={o} value={o}>
            {o}
          </option>
        ))}
      </select>
    </label>
  )
}
