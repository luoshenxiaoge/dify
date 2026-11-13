import { get, post } from './base'

export type Strategy = 'source_file' | 'segments'
export type RunStatus = 'QUEUED' | 'RUNNING' | 'SUCCEEDED' | 'FAILED' | 'INTERRUPTED'
export type DatasetStatus = 'PENDING' | 'RUNNING' | 'SUCCEEDED' | 'FAILED' | 'SKIPPED'
export type SourceDataset = { id: string; name: string }
export type MigrationSourceScope = { include_all: boolean; keyword: string; tag_ids: string[] }

export type MigrationOptions = {
  studio_manager_url: string
  project_id: string
  workspace_id: string
  instance_id: string
  tenant_id: string
  strategy: Strategy
  dataset_ids: string[]
  excluded_dataset_ids: string[]
  source_scope?: MigrationSourceScope
  force: boolean
}

export type MigrationRun = {
  id: string
  status: RunStatus
  created_at: string
  options: MigrationOptions
  retry_of: string | null
  can_retry: boolean
  counts: { total: number; succeeded: number; failed: number; skipped: number }
}

type DatasetResult = SourceDataset & {
  status: DatasetStatus
  total: number
  completed: number
  target?: string
  error?: string
}

type MigrationLog = {
  time: string
  event: string
  dataset_id: string
  model_type?: string
  model_provider?: string
  model_name?: string
  document?: string
  index?: number
  total?: number
  completed?: number
  target?: string
  error?: string
}

export type MigrationRunDetail = MigrationRun & { datasets: DatasetResult[]; logs: MigrationLog[] }
type Page<T> = { data: T[]; has_more: boolean }

const url = '/datasets/migrations'

export const fetchMigrationSources = (page: number, keyword: string, scope: MigrationSourceScope) => {
  const params = new URLSearchParams({
    page: String(page),
    keyword,
    include_all: String(scope.include_all),
    source_keyword: scope.keyword,
  })
  scope.tag_ids.forEach(id => params.append('tag_ids', id))
  return get<Page<SourceDataset> & { total: number; scope_total: number }>(`${url}/sources?${params}`)
}

export const fetchMigrationRuns = (page: number) =>
  get<Page<MigrationRun>>(url, { params: { page } }, { silent: true })

export const fetchMigrationRun = (id: string) =>
  get<MigrationRunDetail>(`${url}/${id}`, {}, { silent: true })

export const startMigration = (options: MigrationOptions, api_key: string) =>
  post<MigrationRunDetail>(url, { body: { ...options, api_key } })

export const retryMigration = (id: string, api_key: string) =>
  post<MigrationRunDetail>(`${url}/${id}/retry`, { body: { api_key } })
