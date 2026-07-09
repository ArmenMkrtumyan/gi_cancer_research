import { type ClassValue, clsx } from 'clsx'
import { twMerge } from 'tailwind-merge'

/** Merge Tailwind class names (shadcn convention). */
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

/** Format an integer with thousands separators, e.g. 1490 -> "1,490". */
export function formatNumber(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  return value.toLocaleString('en-US')
}

/** Human-readable byte size, e.g. 1610612736 -> "1.5 GB". */
export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined || bytes <= 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB']
  const i = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1)
  const value = bytes / Math.pow(1024, i)
  return `${value.toFixed(i === 0 ? 0 : 1)} ${units[i]}`
}

/** Rounded percentage from a nullable API value, e.g. 87.5 -> "87.5%". */
export function formatPct(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  return `${value}%`
}

/** Format an ISO datetime as a short local string, or "—" when absent. */
export function formatDateTime(value: string | null | undefined): string {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString('en-US', {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
}

/** Turn a snake_case table/field key into a display label, e.g. "molecular_tests" -> "Molecular tests". */
export function humanizeKey(key: string): string {
  const spaced = key.replace(/_/g, ' ').trim()
  return spaced.charAt(0).toUpperCase() + spaced.slice(1)
}

// TCGA slide-type codes (from the slide barcode): DX = FFPE diagnostic section (the
// H&E slides pathologists read, the ones used for pathology AI); TS/BS/MS = fresh-frozen
// section slides (top/bottom/middle of the frozen block, used for molecular assays).
const SLIDE_TYPE_LABELS: Record<string, string> = {
  DX: 'Diagnostic',
  TS: 'Frozen (top)',
  BS: 'Frozen (bottom)',
  MS: 'Frozen (middle)',
}
const SLIDE_TYPE_DESCRIPTIONS: Record<string, string> = {
  DX: 'Diagnostic slide — an FFPE (formalin-fixed) H&E section that pathologists read. These are the slides used for pathology AI.',
  TS: 'Frozen top slide — a section from the top of the fresh-frozen tissue block (used for molecular assays).',
  BS: 'Frozen bottom slide — a section from the bottom of the fresh-frozen tissue block.',
  MS: 'Frozen middle slide — a section from the middle of the fresh-frozen tissue block.',
}

/** Human-readable name for a TCGA slide-type code (DX/TS/BS/MS). */
export function slideTypeLabel(code: string | null | undefined): string {
  if (!code) return 'slide'
  return SLIDE_TYPE_LABELS[code.toUpperCase()] ?? code
}

/** Tooltip description for a TCGA slide-type code. */
export function slideTypeDescription(code: string | null | undefined): string {
  if (!code) return ''
  return SLIDE_TYPE_DESCRIPTIONS[code.toUpperCase()] ?? code
}
