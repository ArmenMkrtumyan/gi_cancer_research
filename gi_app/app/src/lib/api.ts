// Single API layer for the dashboard. Base URL is baked at build time from
// VITE_API_URL (see Dockerfile / docker-compose); defaults to the local API port.
export const API_BASE_URL = import.meta.env.VITE_API_URL || 'http://localhost:8008'

// ---- Shared shapes ---------------------------------------------------------

export interface LabelCount {
  label: string
  count: number
}

export interface DatasetRow {
  dataset_id: number
  name: string
  access_type: string | null
  gi_cancer_types: string | null
  official_page: string | null
  n_cases: number
  n_files: number
}

export interface IngestionRun {
  run_id: number
  dataset_id: number
  dataset_name: string
  connector: string | null
  started_at: string | null
  finished_at: string | null
  status: string | null
  log_uri: string | null
}

// ---- /stats/overview -------------------------------------------------------

export interface Overview {
  datasets: number
  cases: number
  samples: number
  slides: number
  assets: number
  wsi_assets: number
  annotations: number
  total_asset_bytes: number
  latest_run: IngestionRun | null
  by_access_type: LabelCount[]
  datasets_table: DatasetRow[]
}

// ---- /datasets/{id}/summary ------------------------------------------------

export interface DatasetSummary {
  dataset: DatasetRow
  table_counts: Record<string, number>
  distributions: Record<string, LabelCount[]>
  asset_bytes: number
  latest_run: IngestionRun | null
}

// ---- /datasets/{id}/survival -----------------------------------------------

export interface SurvivalSummary {
  total_cases: number
  with_os_time: number
  missing_os_time: number
  os_events_dead: number
  alive_or_censored: number
  median_time_to_death: number | null
}

export interface SurvivalBin {
  start: number
  end: number
  count: number
}

export interface SurvivalRecord {
  case_id: number
  case_barcode: string
  vital_status: string | null
  os_time: number | null
  os_event: number | null
}

export interface Survival {
  summary: SurvivalSummary
  histogram: SurvivalBin[]
  records: SurvivalRecord[]
}

// ---- /datasets/{id}/missingness --------------------------------------------

export interface MissingnessMetric {
  label: string
  category: string
  unit: string
  total: number
  present: number
  missing: number
  completeness_pct: number | null
}

export interface Missingness {
  dataset_id: number
  metrics: MissingnessMetric[]
}

// ---- /datasets/{id}/linkage ------------------------------------------------

export interface Linkage {
  dataset_id: number
  chain: LabelCount[]
  checks: Record<string, number>
}

// ---- assets & slides -------------------------------------------------------

export interface DataAsset {
  asset_id: number
  dataset_id: number
  asset_type: string | null
  layer: string | null
  uri: string
  file_name: string | null
  format: string | null
  md5: string | null
  size_bytes: number
  source_file_id: string | null
  created_at: string | null
  slide_id: number | null
  slide_barcode: string | null
  slide_type: string | null
  sample_id: number | null
  sample_barcode: string | null
  case_id: number | null
  case_barcode: string | null
}

export interface SlideRow {
  slide_id: number
  slide_barcode: string | null
  slide_type: string | null
  section_location: string | null
  percent_tumor_cells: number | null
  percent_tumor_nuclei: number | null
  percent_necrosis: number | null
  case_id: number
  case_barcode: string | null
  sample_id: number
  sample_barcode: string | null
  sample_type: string | null
  tissue_type: string | null
  asset: DataAsset | null
}

export interface SlidesResponse {
  dataset_id: number
  total: number
  limit: number
  offset: number
  slides: SlideRow[]
}

export interface DownloadUrl {
  asset_id: number
  uri: string
  expires: number
  url: string
}

// ---- fetch helpers ---------------------------------------------------------

/** GET JSON, throwing a readable Error (FastAPI `{detail}` when present) on non-2xx. */
async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`)
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`
    try {
      const body = await response.json()
      if (body?.detail) detail = body.detail
    } catch {
      // non-JSON error body — keep the status text
    }
    throw new Error(detail)
  }
  return (await response.json()) as T
}

export function fetchOverview(): Promise<Overview> {
  return getJson<Overview>('/stats/overview')
}

export function fetchDataset(datasetId: number): Promise<DatasetRow & { latest_run: IngestionRun | null }> {
  return getJson(`/datasets/${datasetId}`)
}

export function fetchDatasetSummary(datasetId: number): Promise<DatasetSummary> {
  return getJson<DatasetSummary>(`/datasets/${datasetId}/summary`)
}

export function fetchSurvival(datasetId: number, limit = 500): Promise<Survival> {
  return getJson<Survival>(`/datasets/${datasetId}/survival?limit=${limit}`)
}

export function fetchMissingness(datasetId: number): Promise<Missingness> {
  return getJson<Missingness>(`/datasets/${datasetId}/missingness`)
}

export function fetchLinkage(datasetId: number): Promise<Linkage> {
  return getJson<Linkage>(`/datasets/${datasetId}/linkage`)
}

export function fetchSlides(
  datasetId: number,
  opts: { downloadedOnly?: boolean; limit?: number; offset?: number } = {},
): Promise<SlidesResponse> {
  const params = new URLSearchParams()
  if (opts.downloadedOnly) params.append('downloaded_only', 'true')
  params.append('limit', String(opts.limit ?? 200))
  params.append('offset', String(opts.offset ?? 0))
  return getJson<SlidesResponse>(`/datasets/${datasetId}/slides?${params}`)
}

export function fetchIngestionRuns(datasetId?: number): Promise<IngestionRun[]> {
  const params = new URLSearchParams()
  if (datasetId !== undefined) params.append('dataset_id', String(datasetId))
  return getJson<IngestionRun[]>(`/ingestion-runs?${params}`)
}

export function fetchDownloadUrl(assetId: number, expires = 3600): Promise<DownloadUrl> {
  return getJson<DownloadUrl>(`/assets/${assetId}/download-url?expires=${expires}`)
}

// ---- cohort explorer + slide viewer -----------------------------------------

export interface CaseSlide {
  asset_id: number
  slide_barcode: string | null
  slide_type: string | null
}

export interface CohortCase {
  case_id: string
  case_barcode: string | null
  sex: string | null
  vital_status: string | null
  stage: string | null
  os_time: number | null
  os_event: number | null
  slides: CaseSlide[]
}

export function fetchCases(datasetId: number): Promise<CohortCase[]> {
  return getJson<CohortCase[]>(`/datasets/${datasetId}/cases`)
}

export interface SlideInfo {
  asset_id: number
  width: number
  height: number
  tile_size: number
  overlap: number
  levels: number
  mpp_x: number | null
  mpp_y: number | null
  objective_power: number | null
}

export function fetchSlideInfo(assetId: number): Promise<SlideInfo> {
  return getJson<SlideInfo>(`/slides/${assetId}/info`)
}

/** DeepZoom tile URL for the OpenSeadragon viewer. */
export function slideTileUrl(assetId: number, level: number, x: number, y: number): string {
  return `${API_BASE_URL}/slides/${assetId}/tile/${level}/${x}/${y}`
}

// ---- "Add data" — download registry + jobs ---------------------------------

async function sendJson<T>(method: string, path: string, body?: unknown): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    method,
    headers: body !== undefined ? { 'Content-Type': 'application/json' } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  })
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`
    try {
      const b = await response.json()
      if (b?.detail) detail = b.detail
    } catch {
      // keep status text
    }
    throw new Error(detail)
  }
  return (await response.json()) as T
}

export interface DownloadJob {
  id: number
  catalog_id: number | null
  project: string
  dataset_name: string | null
  target: string
  status: string // pending | downloading | ingesting | done | failed
  message: string | null
  n_slides: number | null
  bytes_done: number | null
  bytes_total: number | null
  started_at: string | null
  finished_at: string | null
}

export interface CatalogEntry {
  id: number
  name: string
  source_url: string
  source_type: string // gdc | geo | other
  gi_cancer_types: string | null
  notes: string | null
  downloadable: boolean
  ingested: boolean
  dataset_id: number | null // the ingested dataset (present when `ingested`); target for purge
  latest_job: DownloadJob | null
}

export interface PurgeResult {
  deleted: number
  dataset_name: string
  objects_deleted: number
  bytes_freed: number
  storage_errors: string[]
}

export interface ManifestSlide {
  slide_type: string
  size_mb: number
  case: string
  barcode: string
  file_name: string
  md5: string | null
}

export interface Manifest {
  project: string
  n_cases: number
  n_slides: number
  total_mb: number
  full: boolean
  slides: ManifestSlide[]
}

export interface StorageTargets {
  local: boolean
  aws: boolean
}

export interface AccessCategory {
  category: string
  open_files: number
  open_bytes: number
  controlled_files: number
  controlled_bytes: number
}

export interface AccessBreakdown {
  available: boolean
  reason?: string
  project?: string
  total_files?: number
  open?: { files: number; bytes: number }
  controlled?: { files: number; bytes: number }
  by_category?: AccessCategory[]
  fetched_at?: string
}

export function fetchCatalog(): Promise<CatalogEntry[]> {
  return getJson<CatalogEntry[]>('/catalog')
}

export function addCatalog(body: { name: string; source_url: string; gi_cancer_types?: string }): Promise<{ id: number }> {
  return sendJson('POST', '/catalog', body)
}

export function deleteCatalog(id: number): Promise<{ deleted: number }> {
  return sendJson('DELETE', `/catalog/${id}`)
}

/** Permanently delete an ingested dataset's slide files + Postgres records (frees disk). */
export function purgeDataset(datasetId: number): Promise<PurgeResult> {
  return sendJson<PurgeResult>('DELETE', `/datasets/${datasetId}`)
}

export function fetchManifest(id: number, limit: number | null): Promise<Manifest> {
  return sendJson<Manifest>('POST', `/catalog/${id}/manifest`, { limit })
}

export function startDownload(id: number, target: string, limit: number | null): Promise<DownloadJob> {
  return sendJson<DownloadJob>('POST', `/catalog/${id}/download`, { target, limit })
}

export function fetchStorageTargets(): Promise<StorageTargets> {
  return getJson<StorageTargets>('/storage-targets')
}

export function fetchDatasetAccess(id: number): Promise<AccessBreakdown> {
  return getJson<AccessBreakdown>(`/datasets/${id}/access`)
}

// ---- Annotations -----------------------------------------------------------
// Every annotation carries the provenance of the collection it came from, so the UI can
// always show where a label originated and whether a person or an algorithm produced it.

export interface AnnotationSet {
  annotation_set_id: number
  dataset_id: number
  name: string
  provider: string | null
  source_url: string | null
  citation: string | null
  license: string | null
  version: string | null
  method: string | null
  origin: string
  description: string | null
  retrieved_at: string | null
  is_algorithmic: boolean
  is_published_derived: boolean
  annotation_count?: number
}

export interface Annotation {
  annotation_id: number
  source_annotation_id: string
  case_id: string | null
  target_asset_id: number | null
  scope: string
  is_spatial: boolean
  annotation_type: string | null
  label: string | null
  category: string | null
  classification: string | null
  value_text: string | null
  value_number: number | null
  units: string | null
  confidence: number | null
  review_status: string | null
  source_entity_type: string | null
  source_entity_submitter_id: string | null
  notes: string | null
  source_created_datetime: string | null
  /** UI grouping only — never a source judgement. See _FLAG_GROUPS in the API. */
  flag_group: string | null
  representation_count: number
  annotation_set: AnnotationSet
}

/** Scale/offset placing a representation on slide level 0, recorded at import time. */
export interface TransformMetadata {
  coordinate_space: string
  level: number
  slide_width_px: number
  slide_height_px: number
  grid_columns: number
  grid_rows: number
  level0_px_per_map_px_x: number
  level0_px_per_map_px_y: number
  offset_x_px: number
  offset_y_px: number
  extent_x_px: number
  extent_y_px: number
}

export interface SpatialLayer {
  representation_id: number
  annotation_id: number
  annotation_label: string | null
  annotation_type: string | null
  representation_type: string
  /**
   * What the ORIGINAL file is (`probability_map` | `binary_mask` | …). A rendering derivative
   * is always typed `rendering_derivative`, so this is the only field that says what a
   * drawable layer actually depicts.
   */
  source_representation_type: string
  coordinate_space: string | null
  width: number | null
  height: number | null
  level: number | null
  minimum_value: number | null
  maximum_value: number | null
  transform_metadata: TransformMetadata | null
  asset_id: number
  asset_format: string | null
  asset_type: string | null
  is_renderable: boolean
  is_source_original: boolean
  image_url: string | null
  annotation_set: AnnotationSet
}

export interface TimelineEvent {
  event_type: string
  day: number | null
  timing_basis: string
  label: string | null
  detail: string | null
  ref_table: string | null
  ref_id: string | null
  asset_id: number | null
  /** How many identical source records this card stands for (1 unless merged). */
  source_count?: number
  /** Every source id behind the card, present when source_count > 1. */
  ref_ids?: string[]
}

export interface TimelineGroup {
  day: number
  events: TimelineEvent[]
}

export interface CaseTimeline {
  case_id: string
  case_barcode: string
  baseline: string
  day_unit: string
  total_events: number
  timed_event_count: number
  untimed_event_count: number
  first_day: number | null
  last_day: number | null
  has_longitudinal_data: boolean
  groups: TimelineGroup[]
  untimed: TimelineEvent[]
}

export function fetchAnnotationSets(datasetId?: number): Promise<{ total: number; annotation_sets: AnnotationSet[] }> {
  const q = datasetId === undefined ? '' : `?dataset_id=${datasetId}`
  return getJson(`/annotation-sets${q}`)
}

export function fetchCaseAnnotations(caseId: string): Promise<{ case_id: string; total: number; annotations: Annotation[] }> {
  return getJson(`/cases/${caseId}/annotations`)
}

export function fetchAssetAnnotations(assetId: number): Promise<{ asset_id: number; total: number; annotations: Annotation[] }> {
  return getJson(`/assets/${assetId}/annotations`)
}

export function fetchSpatialLayers(assetId: number): Promise<{ asset_id: number; total: number; layers: SpatialLayer[] }> {
  return getJson(`/assets/${assetId}/spatial-representations`)
}

export function fetchCaseTimeline(caseId: string): Promise<CaseTimeline> {
  return getJson(`/cases/${caseId}/timeline`)
}

/** Absolute URL for an overlay image (OpenSeadragon needs a full URL, not a path). */
export function overlayImageUrl(path: string): string {
  return `${API_BASE_URL}${path}`
}
