import { useEffect, useRef, useState } from 'react'
import { CalendarClock, HelpCircle, Eye, ChevronLeft, ChevronRight } from 'lucide-react'
import { fetchCaseTimeline, type CaseTimeline, type TimelineEvent } from '@/lib/api'

/**
 * Longitudinal view of one patient, assembled from the source clinical records.
 *
 * Laid out left-to-right on a relative-day axis and scrolled horizontally when the course is
 * long. Every event is a row that exists in the source tables; a day is only shown when the
 * source recorded one. Records whose source carries no date are listed separately, below the
 * axis, rather than being placed on it.
 */

const COLUMN_WIDTH = 208 // px; half of this insets the axis line so it starts/ends on a dot

const EVENT_STYLES: Record<string, { label: string; dot: string }> = {
  diagnosis: { label: 'Diagnosis', dot: 'bg-brand' },
  other_diagnosis: { label: 'Other diagnosis', dot: 'bg-slate-400' },
  sample_collection: { label: 'Sample collected', dot: 'bg-emerald-500' },
  sample_received: { label: 'Received by biobank', dot: 'bg-slate-400' },
  slide_available: { label: 'Slide available', dot: 'bg-indigo-500' },
  treatment_start: { label: 'Treatment start', dot: 'bg-amber-500' },
  treatment_end: { label: 'Treatment end', dot: 'bg-amber-700' },
  molecular_test: { label: 'Molecular test', dot: 'bg-cyan-600' },
  follow_up: { label: 'Follow-up', dot: 'bg-sky-500' },
  recurrence: { label: 'Recurrence', dot: 'bg-rose-600' },
  progression: { label: 'Progression', dot: 'bg-rose-500' },
  last_follow_up: { label: 'Last known follow-up', dot: 'bg-slate-500' },
  death: { label: 'Death', dot: 'bg-slate-900' },
}

const TIMING_NOTE: Record<string, string> = {
  baseline: 'Day 0 by definition: the initial pathologic diagnosis.',
  relative_to_diagnosis: 'Recorded in the source as days from diagnosis.',
  derived_from_specimen: 'Taken from the day its specimen was procured; the source records no date for the slide itself.',
  unknown: 'The source records no clinical date for this record.',
}

// Not a clinical event: the day the biobank received the sample for processing. Archived
// tissue is often accessioned years after diagnosis, so this can fall after the patient's
// death without the record being wrong.
const ADMIN_EVENTS = new Set(['sample_received'])
const ADMIN_NOTE =
  'Administrative date: when the biobank received the sample for processing, not when it was taken from the patient.'

// GDC records prior treatment as history against the diagnosis, so a real record can sit
// years before day 0. These carry no end date and an unspecified type — they describe what
// the patient had before this cancer, not how it was treated.
const HISTORY_NOTE =
  'Recorded before this diagnosis, so it is prior medical history rather than part of this cancer’s treatment.'

function styleFor(t: string) {
  return EVENT_STYLES[t] ?? { label: t.replace(/_/g, ' '), dot: 'bg-slate-400' }
}

export default function PatientTimeline({
  caseId,
  onOpenSlide,
}: {
  caseId: string
  onOpenSlide?: (assetId: number) => void
}) {
  const [data, setData] = useState<CaseTimeline | null>(null)
  const [error, setError] = useState<string | null>(null)
  const scrollRef = useRef<HTMLDivElement>(null)
  const [overflowing, setOverflowing] = useState(false)

  useEffect(() => {
    let cancelled = false
    setData(null)
    setError(null)
    fetchCaseTimeline(caseId)
      .then((d) => !cancelled && setData(d))
      .catch((e) => !cancelled && setError(e instanceof Error ? e.message : 'Could not load timeline'))
    return () => {
      cancelled = true
    }
  }, [caseId])

  // Only offer the scroll buttons when the axis actually runs off the edge.
  useEffect(() => {
    const el = scrollRef.current
    if (!el) return
    const check = () => setOverflowing(el.scrollWidth > el.clientWidth + 4)
    check()
    const ro = new ResizeObserver(check)
    ro.observe(el)
    return () => ro.disconnect()
  }, [data])

  if (error) return <p className="text-sm text-red-600">{error}</p>
  if (!data) return <p className="text-sm text-muted-foreground">Loading timeline…</p>

  const span = (data.last_day ?? 0) - (data.first_day ?? 0)

  if (data.total_events === 0) {
    return (
      <p className="text-sm text-muted-foreground">
        No clinical records exist for this patient, so there is no timeline to show.
      </p>
    )
  }

  const scrollBy = (dir: number) => {
    scrollRef.current?.scrollBy({ left: dir * COLUMN_WIDTH * 2, behavior: 'smooth' })
  }

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
        <span className="inline-flex items-center gap-1.5">
          <CalendarClock className="h-3.5 w-3.5" />
          {data.day_unit}; day 0 = {data.baseline}
        </span>
        <span>
          {data.timed_event_count} dated event{data.timed_event_count === 1 ? '' : 's'}
          {data.untimed_event_count > 0 && ` · ${data.untimed_event_count} undated`}
        </span>
        {data.has_longitudinal_data && span > 0 && <span>· spans {span} days</span>}
        {overflowing && (
          <span className="ml-auto inline-flex items-center gap-1">
            <button
              type="button"
              aria-label="Scroll timeline left"
              onClick={() => scrollBy(-1)}
              className="p-1 rounded border hover:bg-muted transition-colors"
            >
              <ChevronLeft className="h-3.5 w-3.5" />
            </button>
            <button
              type="button"
              aria-label="Scroll timeline right"
              onClick={() => scrollBy(1)}
              className="p-1 rounded border hover:bg-muted transition-colors"
            >
              <ChevronRight className="h-3.5 w-3.5" />
            </button>
          </span>
        )}
      </div>

      {!data.has_longitudinal_data && (
        <div className="rounded-md border border-slate-300 bg-slate-50 p-2.5 text-xs text-slate-700">
          This patient has clinical records at only one point in time, so there is no course to
          plot across days. The records are shown below as recorded.
        </div>
      )}

      {data.groups.length > 0 && (
        <div ref={scrollRef} className="overflow-x-auto pb-2 rounded-lg border bg-muted/20">
          <div className="relative flex items-start min-w-min py-3">
            {/* One continuous axis behind the columns, inset so it begins and ends on a dot. */}
            <div
              className="absolute h-0.5 bg-slate-300"
              style={{
                top: 34,
                left: COLUMN_WIDTH / 2,
                right: COLUMN_WIDTH / 2,
              }}
            />
            {data.groups.map((g) => (
              <div
                key={g.day}
                className="relative shrink-0 px-2"
                style={{ width: COLUMN_WIDTH }}
              >
                <div className="h-5 text-center text-xs font-semibold text-brand tabular-nums">
                  day {g.day}
                </div>
                <div className="h-5 flex items-center justify-center">
                  <span className="relative z-10 h-3 w-3 rounded-full border-2 border-white bg-slate-500 shadow-sm" />
                </div>
                {g.events.length > 1 && (
                  <p className="text-center text-[10px] text-muted-foreground mb-1">
                    {g.events.length} records
                  </p>
                )}
                <div className="mt-1 space-y-1.5">
                  {g.events.map((e, i) => (
                    <EventCard
                      key={`${e.ref_id}-${e.event_type}-${i}`}
                      event={e}
                      onOpenSlide={onOpenSlide}
                    />
                  ))}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {data.untimed.length > 0 && (
        <div className="rounded-lg border border-dashed p-3 space-y-2">
          <p className="flex items-center gap-1.5 text-xs font-medium">
            <HelpCircle className="h-3.5 w-3.5 text-muted-foreground" />
            Undated records ({data.untimed.length})
          </p>
          <p className="text-[11px] text-muted-foreground">
            These records exist in the source but carry no clinical date, so they cannot be placed
            on the timeline above. They are listed here in full.
          </p>
          <div className="grid gap-1.5 sm:grid-cols-2 lg:grid-cols-3">
            {data.untimed.map((e, i) => (
              <EventCard key={`${e.ref_id}-${i}`} event={e} onOpenSlide={onOpenSlide} />
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

function EventCard({
  event,
  onOpenSlide,
}: {
  event: TimelineEvent
  onOpenSlide?: (assetId: number) => void
}) {
  const s = styleFor(event.event_type)
  const derived = event.timing_basis === 'derived_from_specimen'
  const unknown = event.timing_basis === 'unknown'
  const admin = ADMIN_EVENTS.has(event.event_type)
  const merged = (event.source_count ?? 1) > 1
  const history = event.day != null && event.day < 0
  return (
    <div className="rounded-md border bg-card px-2 py-1.5">
      <div className="flex items-start gap-1.5">
        <span className={`mt-1 h-2 w-2 rounded-full shrink-0 ${s.dot}`} />
        <div className="min-w-0 flex-1">
          <p className="text-[11px] font-medium leading-tight">{s.label}</p>
          {event.label && (
            <p className="text-[11px] text-muted-foreground leading-tight break-words">
              {event.label}
            </p>
          )}
          {event.detail && (
            <p className="text-[10px] text-muted-foreground/80 leading-tight break-words mt-0.5">
              {event.detail}
            </p>
          )}
          {(derived || unknown || admin || merged || history || event.asset_id != null) && (
            <div className="flex flex-wrap items-center gap-1 mt-1">
              {history && (
                <span
                  title={HISTORY_NOTE}
                  className="text-[10px] px-1.5 py-0.5 rounded border bg-slate-100 text-slate-600 border-slate-300"
                >
                  before diagnosis · prior history
                </span>
              )}
              {merged && (
                <span
                  title={`${event.source_count} source records on this day carry identical values; shown once.`}
                  className="text-[10px] px-1.5 py-0.5 rounded border bg-slate-100 text-slate-600 border-slate-300"
                >
                  {event.source_count} source records
                </span>
              )}
              {admin && (
                <span
                  title={ADMIN_NOTE}
                  className="text-[10px] px-1.5 py-0.5 rounded border bg-slate-100 text-slate-600 border-slate-300"
                >
                  administrative
                </span>
              )}
              {(derived || unknown) && (
                <span
                  title={TIMING_NOTE[event.timing_basis]}
                  className={`text-[10px] px-1.5 py-0.5 rounded border ${
                    unknown
                      ? 'bg-slate-100 text-slate-600 border-slate-300'
                      : 'bg-amber-50 text-amber-800 border-amber-300'
                  }`}
                >
                  {unknown ? 'no date in source' : 'derived timing'}
                </span>
              )}
              {event.asset_id != null && onOpenSlide && (
                <button
                  type="button"
                  onClick={() => onOpenSlide(event.asset_id as number)}
                  className="inline-flex items-center gap-1 text-[10px] text-brand underline"
                >
                  <Eye className="h-3 w-3" /> view slide
                </button>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
