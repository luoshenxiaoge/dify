'use client'

import type { FormEvent, ReactNode } from 'react'
import type { MigrationOptions, MigrationRunDetail, MigrationSourceScope, Strategy } from '@/service/dataset-migration'
import { RiArrowLeftLine, RiHistoryLine, RiTerminalBoxLine } from '@remixicon/react'
import Link from 'next/link'
import { usePathname, useRouter, useSearchParams } from 'next/navigation'
import { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import useSWR from 'swr'
import Button from '@/app/components/base/button'
import Input from '@/app/components/base/input'
import Loading from '@/app/components/base/loading'
import Pagination from '@/app/components/base/pagination'
import { useAppContext } from '@/context/app-context'
import {
  fetchMigrationRun,
  fetchMigrationRuns,
  fetchMigrationSources,
  retryMigration,
  startMigration,
} from '@/service/dataset-migration'
import cn from '@/utils/classnames'

const active = (status?: string) => status === 'QUEUED' || status === 'RUNNING'
const fieldClass = 'min-w-0 space-y-1.5 system-sm-medium text-text-secondary'
const panelClass = 'rounded-xl border border-divider-regular bg-background-default p-5'
const selectClass = 'h-9 w-full rounded-lg border border-divider-regular bg-components-input-bg-normal px-2 text-text-primary'
const statusColors: Record<string, string> = {
  RUNNING: 'text-text-accent',
  SUCCEEDED: 'text-text-success',
  FAILED: 'text-text-destructive',
  INTERRUPTED: 'text-text-destructive',
}

function Pager({ page, hasMore, onChange }: { page: number; hasMore?: boolean; onChange: (page: number) => void }) {
  const { t } = useTranslation()
  return (
    <div className='flex items-center justify-end gap-2'>
      <Button size='small' disabled={page <= 1} onClick={() => onChange(page - 1)}>{t('dataset.migration.previous')}</Button>
      <span className='system-xs-regular text-text-tertiary'>{page}</span>
      <Button size='small' disabled={!hasMore} onClick={() => onChange(page + 1)}>{t('dataset.migration.next')}</Button>
    </div>
  )
}

function Label({ title, children }: { title: string; children: ReactNode }) {
  return <label className={fieldClass}><span className='block'>{title}</span>{children}</label>
}

function MigrationForm({ tenantId, scope, onStarted }: {
  tenantId: string
  scope: MigrationSourceScope
  onStarted: (run: MigrationRunDetail) => void
}) {
  const { t } = useTranslation()
  const [page, setPage] = useState(1)
  const [keyword, setKeyword] = useState('')
  const [url, setUrl] = useState('')
  const [apiKey, setApiKey] = useState('')
  const [project, setProject] = useState('0')
  const [workspace, setWorkspace] = useState('')
  const [instance, setInstance] = useState('dify-1.9.1')
  const [strategy, setStrategy] = useState<Strategy>('source_file')
  const [force, setForce] = useState(false)
  const [selected, setSelected] = useState<Record<string, 'include' | 'exclude'>>({})
  const [submitting, setSubmitting] = useState(false)
  const sources = useSWR(['migration-sources', tenantId, page, keyword, scope], () => fetchMigrationSources(page, keyword, scope))
  const managerUrl = url.trim()
  const canSubmit = !submitting && !!managerUrl && !sources.error && !!sources.data?.scope_total
  const included = Object.keys(selected).filter(id => selected[id] === 'include')
  const excluded = Object.keys(selected).filter(id => selected[id] === 'exclude')

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (!canSubmit)
      return
    const options: MigrationOptions = {
      studio_manager_url: managerUrl,
      tenant_id: tenantId,
      project_id: project.trim(),
      workspace_id: workspace.trim(),
      instance_id: instance.trim(),
      strategy,
      dataset_ids: included,
      excluded_dataset_ids: excluded,
      source_scope: scope,
      force,
    }
    setSubmitting(true)
    try {
      const run = await startMigration(options, apiKey)
      setApiKey('')
      onStarted(run)
    }
    catch {
      // The console API client displays the error response.
    }
    finally {
      setSubmitting(false)
    }
  }

  return (
    <form onSubmit={submit} className='space-y-4'>
      <section className={panelClass}>
        <h2 className='system-md-semibold mb-4'>{t('dataset.migration.targetSettings')}</h2>
        <div className='grid gap-4 md:grid-cols-2'>
          <Label title={t('dataset.migration.managerUrl')}>
            <Input
              required
              type='url'
              value={url}
              onChange={e => setUrl(e.target.value)}
              placeholder='https://studio-manager.example.com'
              maxLength={2048}
            />
          </Label>
          <Label title={t('dataset.migration.apiKey')}>
            <Input required type='password' autoComplete='off' value={apiKey} onChange={e => setApiKey(e.target.value)} maxLength={4096} />
          </Label>
          <Label title={t('dataset.migration.project')}>
            <Input required value={project} onChange={e => setProject(e.target.value)} pattern='[A-Za-z0-9_\x2D]{1,64}' maxLength={64} />
          </Label>
          <Label title={t('dataset.migration.workspace')}>
            <Input required value={workspace} onChange={e => setWorkspace(e.target.value)} pattern='[A-Za-z0-9_\x2D]{1,64}' maxLength={64} />
          </Label>
          <Label title={t('dataset.migration.tenant')}>
            <Input readOnly value={tenantId} />
          </Label>
          <Label title={t('dataset.migration.instance')}>
            <Input value={instance} onChange={e => setInstance(e.target.value)} maxLength={255} />
          </Label>
        </div>
      </section>
      <section className={panelClass}>
        <h2 className='system-md-semibold mb-3'>{t('dataset.migration.strategy')}</h2>
        <div className='grid gap-3 md:grid-cols-2'>
          {(['source_file', 'segments'] as const).map(option => (
            <label key={option} className={cn(
              'flex cursor-pointer gap-2 rounded-lg border p-3',
              strategy === option ? 'border-text-accent bg-state-accent-hover' : 'border-divider-regular',
            )}>
              <input type='radio' name='strategy' value={option} checked={option === strategy} onChange={() => setStrategy(option)} />
              <span>
                <span className='system-sm-semibold block'>{t(`dataset.migration.strategies.${option}`)}</span>
                <span className='system-xs-regular text-text-tertiary'>{t(`dataset.migration.strategyHelp.${option}`)}</span>
              </span>
            </label>
          ))}
        </div>
        <label className='system-sm-medium mt-4 flex items-center gap-2'>
          <input type='checkbox' checked={force} onChange={e => setForce(e.target.checked)} />
          {t('dataset.migration.force')}
        </label>
        <p className='system-xs-regular mt-1 text-text-tertiary'>{t('dataset.migration.forceHelp')}</p>
      </section>
      <section className={panelClass}>
        <div className='mb-3 flex flex-wrap items-center justify-between gap-3'>
          <h2 className='system-md-semibold'>{t('dataset.migration.scope')}</h2>
          <Input
            showLeftIcon
            aria-label={t('dataset.migration.search')}
            placeholder={t('dataset.migration.search')}
            value={keyword}
            onChange={(e) => {
              setKeyword(e.target.value)
              setPage(1)
            }}
          />
        </div>
        <p className='system-xs-regular mb-3 text-text-tertiary'>{t('dataset.migration.scopeHelp')}</p>
        {typeof sources.data?.scope_total === 'number' && (
          <p className='system-sm-medium mb-2'>{t('dataset.migration.scopeTotal', { count: sources.data.scope_total })}</p>
        )}
        <div className='mb-2 flex items-center gap-3'>
          <span className='system-sm-medium'>{t('dataset.migration.selection', { included: included.length, excluded: excluded.length })}</span>
          {(included.length > 0 || excluded.length > 0) && <Button size='small' variant='ghost' onClick={() => setSelected({})}>{t('dataset.migration.clear')}</Button>}
        </div>
        <div className='mb-3 overflow-hidden rounded-lg border border-divider-regular'>
          {sources.error && <p className='system-sm-regular p-3 text-text-destructive'>{t('dataset.migration.loadError')}</p>}
          {sources.isLoading && <div className='p-3'><Loading /></div>}
          {sources.data?.data.length === 0 && <p className='system-sm-regular p-3 text-text-tertiary'>{t('dataset.migration.noSources')}</p>}
          {sources.data?.data.map(item => (
            <div key={item.id} className='flex items-center justify-between gap-3 border-b border-divider-subtle px-3 py-2 last:border-b-0'>
              <div className='min-w-0'>
                <div className='system-sm-medium break-words'>{item.name}</div>
                <div className='system-xs-regular break-all text-text-tertiary'>{item.id}</div>
              </div>
              <select
                aria-label={item.name}
                className={cn(selectClass, 'w-32 shrink-0')}
                value={selected[item.id] || ''}
                onChange={(e) => {
                  const value = e.target.value
                  setSelected((previous) => {
                    const next = { ...previous }
                    if (value === 'include' || value === 'exclude')
                      next[item.id] = value
                    else
                      delete next[item.id]
                    return next
                  })
                }}
              >
                <option value=''>{t('dataset.migration.defaultScope')}</option>
                <option value='include'>{t('dataset.migration.include')}</option>
                <option value='exclude'>{t('dataset.migration.exclude')}</option>
              </select>
            </div>
          ))}
        </div>
        {!!sources.data?.total && (
          <Pagination
            current={page - 1}
            onChange={value => setPage(value + 1)}
            total={sources.data.total}
            limit={20}
          />
        )}
      </section>
      <div className='flex justify-end'>
        <Button type='submit' variant='primary' size='large' loading={submitting} disabled={!canSubmit}>{t('dataset.migration.start')}</Button>
      </div>
    </form>
  )
}

function Status({ status }: { status: string }) {
  const { t } = useTranslation()
  return <span className={cn('system-xs-medium whitespace-nowrap rounded-md bg-background-section-burn px-2 py-1', statusColors[status] || 'text-text-secondary')}>{t(`dataset.migration.status.${status}`)}</span>
}

function RunDetail({ run, onRetried, onSelect }: {
  run: MigrationRunDetail
  onRetried: (run: MigrationRunDetail) => void
  onSelect: (id: string) => void
}) {
  const { t, i18n } = useTranslation()
  const [showRetry, setShowRetry] = useState(false)
  const [showLogs, setShowLogs] = useState(true)
  const [apiKey, setApiKey] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const logBox = useRef<HTMLDivElement>(null)
  const followLogs = useRef(true)
  const names = new Map(run.datasets.map(item => [item.id, item.name]))
  const { total, succeeded, failed, skipped } = run.counts
  const previousId = run.retry_of

  useEffect(() => {
    if (followLogs.current && logBox.current)
      logBox.current.scrollTop = logBox.current.scrollHeight
  }, [run.logs, showLogs])

  const retry = async (event: FormEvent) => {
    event.preventDefault()
    if (submitting)
      return
    setSubmitting(true)
    try {
      const result = await retryMigration(run.id, apiKey)
      setApiKey('')
      onRetried(result)
    }
    catch {
      // The console API client displays the error response.
    }
    finally {
      setSubmitting(false)
    }
  }

  return (
    <section className='min-w-0 flex-1 space-y-4'>
      <div className={panelClass}>
        <div className='flex flex-wrap items-center justify-between gap-2'>
          <h2 className='system-md-semibold'>{t(`dataset.migration.strategies.${run.options.strategy}`)}</h2>
          <Status status={run.status} />
        </div>
        <p className='system-xs-regular mt-2 break-all text-text-tertiary'>{run.options.studio_manager_url} · {run.options.project_id} / {run.options.workspace_id}</p>
        <p className='system-sm-medium mt-3'>{t('dataset.migration.summary', { total, succeeded, failed, skipped })}</p>
        {previousId && <Button size='small' variant='ghost' className='mt-2' onClick={() => onSelect(previousId)}>{t('dataset.migration.previousRun')}</Button>}
        {run.status === 'INTERRUPTED' && <p className='system-sm-regular mt-3 text-text-destructive'>{t('dataset.migration.interrupted')}</p>}
        {run.can_retry && !showRetry && (
          <Button className='mt-3' onClick={() => setShowRetry(true)}>{t('dataset.migration.retry')}</Button>
        )}
        {showRetry && (
          <form className='mt-3 space-y-2 rounded-lg border border-divider-regular p-3' onSubmit={retry}>
            <p className='system-sm-regular text-text-tertiary'>{t('dataset.migration.retryHelp')}</p>
            <Label title={t('dataset.migration.apiKey')}>
              <Input required autoComplete='off' type='password' value={apiKey} maxLength={4096} onChange={e => setApiKey(e.target.value)} />
            </Label>
            <div className='flex justify-end gap-2'>
              <Button onClick={() => {
                setShowRetry(false)
                setApiKey('')
              }}
              >
                {t('dataset.migration.cancel')}
              </Button>
              <Button type='submit' variant='primary' loading={submitting} disabled={submitting}>{t('dataset.migration.retry')}</Button>
            </div>
          </form>
        )}
      </div>
      <div className={panelClass}>
        <div className='mb-3 flex items-center justify-between'>
          <h2 className='system-md-semibold flex items-center gap-2'><RiTerminalBoxLine className='size-4' />{t('dataset.migration.logs')}</h2>
          <Button size='small' variant='ghost' aria-expanded={showLogs} onClick={() => setShowLogs(value => !value)}>{t(showLogs ? 'dataset.migration.hideLogs' : 'dataset.migration.showLogs')}</Button>
        </div>
        {showLogs && (
          <div
            ref={logBox}
            role='log'
            aria-label={t('dataset.migration.logs')}
            className='h-56 overflow-auto rounded-lg bg-background-section-burn p-3 font-mono text-xs leading-6 text-text-secondary'
            onScroll={(event) => {
              const box = event.currentTarget
              followLogs.current = box.scrollHeight - box.scrollTop - box.clientHeight < 32
            }}
          >
            {run.logs.map(log => (
              <div key={log.time + log.event + log.dataset_id} className={cn('break-words', log.error && 'text-text-destructive')}>
                <time className='mr-3 text-text-tertiary' dateTime={log.time}>{new Date(log.time).toLocaleTimeString(i18n.language)}</time>
                {log.dataset_id && <strong className='mr-2'>{names.get(log.dataset_id) || log.dataset_id}</strong>}
                {t(`dataset.migration.events.${log.event}`, { ...log, defaultValue: log.event })}
              </div>
            ))}
          </div>
        )}
        <p className='system-xs-regular mt-2 text-text-tertiary'>{t('dataset.migration.logLimit')}</p>
      </div>
      <div className={cn(panelClass, 'overflow-x-auto')}>
        <table className='w-full text-left text-sm'>
          <thead className='text-text-tertiary'>
            <tr>
              <th className='pb-2 pr-3'>{t('dataset.migration.sequence')}</th>
              <th className='pb-2 pr-3'>{t('dataset.migration.knowledge')}</th>
              <th className='pb-2 pr-3'>{t('dataset.migration.state')}</th>
              <th className='pb-2'>{t('dataset.migration.documents')}</th>
            </tr>
          </thead>
          <tbody>
            {run.datasets.map((item, index) => (
              <tr key={item.id} className='border-t border-divider-subtle align-top'>
                <td className='py-2 pr-3 tabular-nums'>{index + 1}</td>
                <td className='break-words py-2 pr-3'>
                  <div className='system-sm-medium'>{item.name}</div>
                  <div className='system-xs-regular break-all text-text-tertiary'>{t('dataset.migration.sourceDatasetId')} {item.id}</div>
                  {item.error && <div className='system-xs-regular mt-1 text-text-destructive'>{item.error}</div>}
                </td>
                <td className='py-2 pr-3'><Status status={item.status} /></td>
                <td className='py-2 tabular-nums'>{item.completed}/{item.total}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className='system-xs-regular mt-3 text-text-tertiary'>{t('dataset.migration.indexingHint')}</p>
      </div>
    </section>
  )
}

function MigrationPanel({ tenantId }: { tenantId: string }) {
  const { t, i18n } = useTranslation()
  const path = usePathname()
  const router = useRouter()
  const params = useSearchParams()
  const selectedId = params.get('run')
  const showResults = Boolean(selectedId) || params.get('tab') === 'results'
  const scope: MigrationSourceScope = {
    include_all: params.get('include_all') === 'true',
    keyword: params.get('source_keyword') || '',
    tag_ids: params.getAll('tag_ids'),
  }
  const [page, setPage] = useState(1)
  const runs = useSWR(showResults ? ['migration-runs', tenantId, page] : null, () => fetchMigrationRuns(page), {
    dedupingInterval: 2000,
    refreshInterval: data => data?.data.some(item => active(item.status)) ? 3000 : 0,
  })
  const runId = selectedId || runs.data?.data[0]?.id
  const detail = useSWR(showResults && runId ? ['migration-run', tenantId, runId] : null, ([, , id]) => fetchMigrationRun(id), {
    dedupingInterval: 1000,
    refreshInterval: run => active(run?.status) ? 1500 : 0,
  })

  const switchTab = (tab?: 'results', run?: string) => {
    const next = new URLSearchParams(params.toString())
    next.delete('tab')
    next.delete('run')
    if (tab)
      next.set('tab', tab)
    if (run)
      next.set('run', run)
    const query = next.toString()
    router.replace(query ? `${path}?${query}` : path, { scroll: false })
  }
  const selectRun = (id: string) => switchTab('results', id)
  const onStarted = (run: MigrationRunDetail) => {
    setPage(1)
    selectRun(run.id)
    runs.mutate()
  }

  return (
    <div className='scroll-container grow overflow-y-auto bg-background-body text-text-primary'>
      <div className='mx-auto max-w-6xl px-6 py-6 lg:px-10'>
        <Link href='/datasets' className='system-sm-medium mb-3 inline-flex items-center gap-1 text-text-secondary hover:text-text-primary'>
          <RiArrowLeftLine className='size-4' />{t('dataset.migration.back')}
        </Link>
        <h1 className='title-2xl-semi-bold'>{t('dataset.migration.title')}</h1>
        <p className='system-sm-regular mt-1 text-text-tertiary'>{t('dataset.migration.intro')}</p>
        <div className='my-5 flex gap-2 border-b border-divider-regular pb-3'>
          <Button variant={showResults ? 'ghost' : 'secondary-accent'} onClick={() => switchTab()}>{t('dataset.migration.newRun')}</Button>
          <Button variant={showResults ? 'secondary-accent' : 'ghost'} onClick={() => switchTab('results')}>
            <RiHistoryLine className='mr-1 size-4' />{t('dataset.migration.results')}
          </Button>
        </div>
        {!showResults && <MigrationForm tenantId={tenantId} scope={scope} onStarted={onStarted} />}
        {showResults && (
          <div className='flex flex-col gap-4 md:flex-row'>
            <aside className={cn(panelClass, 'h-fit shrink-0 md:w-60')}>
              <h2 className='system-md-semibold mb-3'>{t('dataset.migration.results')}</h2>
              {runs.error && <p className='system-sm-regular text-text-destructive'>{t('dataset.migration.loadError')}</p>}
              {runs.isLoading && <Loading />}
              {!runs.isLoading && runs.data?.data.length === 0 && <p className='system-sm-regular text-text-tertiary'>{t('dataset.migration.noRuns')}</p>}
              <div className='mb-3 space-y-2'>
                {runs.data?.data.map(run => (
                  <button
                    type='button'
                    key={run.id}
                    aria-current={run.id === runId}
                    className={cn('block w-full rounded-lg border p-2 text-left', run.id === runId ? 'border-text-accent bg-state-accent-hover' : 'border-divider-regular hover:bg-state-base-hover')}
                    onClick={() => selectRun(run.id)}
                  >
                    <div className='system-sm-semibold mb-1'>{t(`dataset.migration.strategies.${run.options.strategy}`)}</div>
                    <div className='system-xs-regular mb-2 text-text-tertiary'>{new Date(run.created_at).toLocaleString(i18n.language)}</div>
                    <Status status={run.status} />
                  </button>
                ))}
              </div>
              <Pager page={page} hasMore={runs.data?.has_more} onChange={setPage} />
            </aside>
            {detail.error && <p className='system-sm-regular text-text-destructive'>{t('dataset.migration.loadError')}</p>}
            {detail.isLoading && <Loading />}
            {detail.data && <RunDetail key={detail.data.id} run={detail.data} onRetried={onStarted} onSelect={selectRun} />}
          </div>
        )}
      </div>
    </div>
  )
}

export default function Migration() {
  const { currentWorkspace, isCurrentWorkspaceManager } = useAppContext()
  const { t } = useTranslation()
  if (!isCurrentWorkspaceManager)
    return <div className='p-6 text-text-secondary'>{t('dataset.migration.restricted')}</div>
  return <MigrationPanel key={currentWorkspace.id} tenantId={currentWorkspace.id} />
}
