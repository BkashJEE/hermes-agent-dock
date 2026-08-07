/**
 * Hermes Agent Dock — native direct-profile chat for Hermes Desktop.
 *
 * Plain ESM loaded by the disk-plugin host. UI is jsx() calls rather than JSX
 * syntax so installation needs no build step or package lifecycle scripts.
 */
import {
  atom,
  Badge,
  Button,
  cn,
  Codicon,
  haptic,
  host,
  PALETTE_AREA,
  PANES_AREA,
  STATUSBAR_AREAS,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  Textarea,
  useQuery,
  useValue
} from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'
import { useEffect, useMemo, useRef, useState } from 'react'

const ID = 'hermes-agent-dock'
const MAX_LOCAL_MESSAGES = 30
let rest
let storage
let dockPaneDisposer = null
const $dockOpen = atom(false)

// STATE_HELPERS_START — dependency-free logic exercised by tests/test_dock_state.mjs.
const REASONING_EFFORTS = Object.freeze([
  { value: 'minimal', label: 'Minimal' },
  { value: 'low', label: 'Low' },
  { value: 'medium', label: 'Medium' },
  { value: 'high', label: 'High' },
  { value: 'xhigh', label: 'Extra High' },
  { value: 'max', label: 'Max' },
  { value: 'ultra', label: 'Ultra' }
])
const VALID_REASONING_EFFORTS = new Set(REASONING_EFFORTS.map(option => option.value))
const ACTIVE_JOB_STATUSES = new Set(['starting', 'queued', 'running', 'finalizing', 'cancelling'])
const STARTING_JOB_TTL_MS = 60_000

function profileDisplayLabel(rawProfile) {
  const raw = String(rawProfile ?? '')
  return raw
    .split(/[-_]+/)
    .filter(Boolean)
    .map(part => `${part.charAt(0).toUpperCase()}${part.slice(1).toLowerCase()}`)
    .join(' ')
}

function modelOptionKey(provider, model) {
  return `${encodeURIComponent(provider)}::${encodeURIComponent(model)}`
}

function modelFromEntry(entry) {
  if (typeof entry === 'string') return entry.trim()
  if (!entry || typeof entry !== 'object') return ''
  return String(entry.model ?? entry.slug ?? entry.id ?? '').trim()
}

function flattenModelOptions(payload) {
  const providers = Array.isArray(payload?.providers) ? payload.providers : []
  const seen = new Set()
  const options = []
  for (const provider of providers) {
    const slug = String(provider?.slug ?? '').trim()
    const providerName = String(provider?.name ?? slug).trim() || slug
    const models = Array.isArray(provider?.models) ? provider.models : []
    const capabilities = provider?.capabilities && typeof provider.capabilities === 'object' ? provider.capabilities : {}
    if (!slug) continue
    for (const entry of models) {
      const model = modelFromEntry(entry)
      const key = modelOptionKey(slug, model)
      if (!model || seen.has(key)) continue
      seen.add(key)
      const capability = capabilities[model] && typeof capabilities[model] === 'object' ? capabilities[model] : {}
      options.push({
        key,
        model,
        provider: slug,
        providerName,
        reasoning: capability.reasoning === true,
        fast: capability.fast === true
      })
    }
  }
  return options
}

function groupModelOptions(options) {
  const groups = new Map()
  for (const option of options || []) {
    const key = option.provider
    const group = groups.get(key) || { provider: key, providerName: option.providerName || key, options: [] }
    group.options.push(option)
    groups.set(key, group)
  }
  return [...groups.values()]
}

function selectedModelCapabilities(payload, provider, model) {
  const providerRow = Array.isArray(payload?.providers)
    ? payload.providers.find(row => String(row?.slug ?? '').trim() === String(provider ?? '').trim())
    : null
  const capability = providerRow?.capabilities?.[model]
  return { reasoning: capability?.reasoning === true, fast: capability?.fast === true }
}

function normalizeReasoningEffort(effort) {
  return VALID_REASONING_EFFORTS.has(effort) ? effort : 'medium'
}

function resolveModelSettings({ modelPayload, provider, model, thinking, effort, fast }) {
  const capabilities = selectedModelCapabilities(modelPayload, provider, model)
  const thinkingEnabled = thinking === true && capabilities.reasoning
  return {
    reasoning: capabilities.reasoning,
    fast: capabilities.fast,
    thinking: thinkingEnabled,
    reasoning_effort: thinkingEnabled ? normalizeReasoningEffort(effort) : 'none',
    fast_enabled: fast === true && capabilities.fast
  }
}

function buildJobPayload({ profile, provider, model, thinking, effort, fast, message, session_id, request_id, assign_task, modelPayload }) {
  const settings = resolveModelSettings({ modelPayload, provider, model, thinking, effort, fast })
  return {
    profile: String(profile ?? '').trim(),
    provider: String(provider ?? '').trim() || null,
    model: String(model ?? '').trim() || null,
    reasoning_effort: settings.reasoning_effort,
    fast: settings.fast_enabled,
    message,
    session_id: session_id ?? null,
    request_id: request_id ?? null,
    assign_task: assign_task === true
  }
}

function appendUniqueMessage(histories, profile, message, limit = 30) {
  const currentMessages = histories[profile] || []
  if (currentMessages.some(item => item.id === message.id)) return histories
  return { ...histories, [profile]: [...currentMessages, message].slice(-limit) }
}

function stampMessage(message, now = Date.now()) {
  const createdAt = Number(message?.created_at)
  if (Number.isFinite(createdAt) && createdAt > 0) return message
  return { ...message, created_at: now }
}

function formatMessageTimestamp(value, locale, timeZone) {
  const timestamp = Number(value)
  if (!Number.isFinite(timestamp) || timestamp <= 0) return 'Date unavailable'
  const date = new Date(timestamp)
  if (Number.isNaN(date.getTime())) return 'Date unavailable'
  const options = {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
    second: '2-digit',
    timeZoneName: 'short'
  }
  if (timeZone) options.timeZone = timeZone
  return new Intl.DateTimeFormat(locale || undefined, options).format(date)
}

function upsertProfileJob(jobs, profile, job) {
  return { ...jobs, [profile]: job }
}

function removeProfileJob(jobs, profile, jobId) {
  if (jobs[profile]?.id !== jobId) return jobs
  const next = { ...jobs }
  delete next[profile]
  return next
}

function reserveProfileJob(jobs, profile, job) {
  if (jobs[profile]) return { jobs, reserved: false }
  return { jobs: upsertProfileJob(jobs, profile, job), reserved: true }
}

function activeJobActivities(jobs, now = Date.now()) {
  return Object.entries(jobs || {})
    .filter(([, job]) => {
      if (!job) return false
      const status = job.status || (job.id ? 'running' : 'starting')
      if (!ACTIVE_JOB_STATUSES.has(status)) return false
      if (job.id || status !== 'starting') return true
      const startedAt = Number(job.started_at)
      return Number.isFinite(startedAt) && now - startedAt <= STARTING_JOB_TTL_MS
    })
    .map(([profile, job]) => ({ profile, status: job.status || (job.id ? 'running' : 'starting'), job }))
    .sort((left, right) => left.profile.localeCompare(right.profile))
}

function workingProfileNames(jobs, now = Date.now()) {
  return activeJobActivities(jobs, now).map(activity => activity.profile)
}

function profileActivityLabel(job) {
  const status = String(job?.status || '').toLowerCase()
  if (!ACTIVE_JOB_STATUSES.has(status)) return 'Idle'
  return status === 'cancelling' ? 'Cancelling' : 'Working'
}

function pruneExpiredStartingJobs(jobs, now = Date.now()) {
  const current = jobs || {}
  const activeProfiles = new Set(activeJobActivities(current, now).map(activity => activity.profile))
  let next = current
  for (const [profile, job] of Object.entries(current)) {
    if (job && !job.id && (job.status || 'starting') === 'starting' && !activeProfiles.has(profile)) {
      if (next === current) next = { ...current }
      delete next[profile]
    }
  }
  return next
}

function isNotFoundError(error) {
  const statuses = [error?.status, error?.statusCode, error?.response?.status, error?.cause?.status]
  if (statuses.some(status => Number(status) === 404)) return true
  return /\b404\b|job not found/i.test(String(error?.message || error))
}

async function resolveActiveJobActivities(jobs, readJob, now = Date.now()) {
  const candidates = activeJobActivities(jobs, now)
  const resolved = await Promise.all(candidates.map(async activity => {
    if (!activity.job.id) return activity
    try {
      const current = await readJob(activity.job.id)
      return ACTIVE_JOB_STATUSES.has(current?.status)
        ? { profile: activity.profile, status: current.status, job: current }
        : null
    } catch (error) {
      return isNotFoundError(error) ? null : activity
    }
  }))
  return resolved.filter(Boolean).sort((left, right) => left.profile.localeCompare(right.profile))
}

function activitySummary(activities) {
  if (!activities.length) return 'No agents have active sessions'
  const labels = activities.map(activity => profileDisplayLabel(activity.profile))
  if (activities.length === 1) {
    return activities[0].status === 'cancelling'
      ? `${labels[0]} is cancelling`
      : `${labels[0]} has an active session`
  }
  return `${activities.length} agents have active sessions: ${labels.join(', ')}`
}

function replaceStartingJob(jobs, profile, requestId, job) {
  if (jobs?.[profile]?.request_id !== requestId) return jobs
  return upsertProfileJob(jobs, profile, { ...job, profile })
}

async function submitWithIdempotentRetry(submit, allowRetry) {
  try {
    return await submit()
  } catch (firstError) {
    if (!allowRetry) throw firstError
    return submit()
  }
}

async function reconcileIdempotentSubmission(submit, options) {
  const {
    allowRetry,
    isReserved,
    onAccepted,
    onPending,
    schedule,
    retryDelay = 10_000
  } = options
  try {
    const job = await submitWithIdempotentRetry(submit, allowRetry)
    if (isReserved()) onAccepted(job)
    return job
  } catch (error) {
    if (!isReserved()) return null
    onPending(error, allowRetry)
    if (allowRetry) {
      schedule(
        () => reconcileIdempotentSubmission(submit, options),
        retryDelay
      )
    }
    return null
  }
}
// STATE_HELPERS_END

function makeRequestId() {
  return globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(36).slice(2)}`
}

function HermesMark({ compact = false }) {
  return jsx('span', {
    'aria-hidden': true,
    className: cn(
      'inline-flex shrink-0 items-center justify-center overflow-hidden rounded-md border border-(--ui-stroke-secondary) bg-white',
      compact ? 'size-7' : 'size-9'
    ),
    children: jsx('img', { alt: '', className: 'size-full object-contain', src: 'nous-girl.jpg' })
  })
}

function playAchievementChime() {
  const Audio = window.AudioContext || window.webkitAudioContext
  if (!Audio) return
  const ctx = new Audio()
  const gain = ctx.createGain()
  gain.gain.setValueAtTime(0.0001, ctx.currentTime)
  gain.gain.exponentialRampToValueAtTime(0.12, ctx.currentTime + 0.02)
  gain.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + 0.55)
  gain.connect(ctx.destination)
  ;[523.25, 659.25, 783.99].forEach((frequency, index) => {
    const oscillator = ctx.createOscillator()
    oscillator.type = 'sine'
    oscillator.frequency.value = frequency
    oscillator.connect(gain)
    const start = ctx.currentTime + index * 0.09
    oscillator.start(start)
    oscillator.stop(start + 0.3)
  })
  window.setTimeout(() => void ctx.close(), 900)
}

const TIER_TONES = {
  Copper: 'var(--ui-warm,var(--ui-accent))',
  Silver: 'var(--ui-text-secondary)',
  Gold: 'var(--ui-accent)',
  Diamond: 'var(--ui-accent-secondary)',
  Olympian: 'var(--ui-text-primary)'
}

function tierStyle(tier) {
  const tone = TIER_TONES[tier] || 'var(--ui-accent)'
  return {
    borderColor: `color-mix(in srgb, ${tone} 58%, var(--ui-stroke-secondary))`,
    background: `linear-gradient(135deg, color-mix(in srgb, ${tone} 15%, var(--ui-bg-elevated)), var(--ui-bg-elevated))`,
    boxShadow: `inset 0 1px 0 color-mix(in srgb, ${tone} 18%, transparent)`
  }
}

function relativeUnlock(seconds) {
  if (!seconds) return ''
  const delta = Math.max(0, Math.round(Date.now() / 1000 - seconds))
  if (delta < 60) return 'just now'
  if (delta < 3600) return `${Math.floor(delta / 60)}m ago`
  if (delta < 86400) return `${Math.floor(delta / 3600)}h ago`
  return `${Math.floor(delta / 86400)}d ago`
}

function AchievementCard({ item, compact = false }) {
  const pct = Number.isFinite(item.progress_pct) ? Math.max(0, Math.min(100, item.progress_pct)) : null
  return jsxs('article', {
    className: cn('rounded-xl border', compact ? 'p-3' : 'p-3.5'),
    style: tierStyle(item.tier),
    children: [
      jsxs('div', {
        className: 'flex items-start justify-between gap-2',
        children: [
          jsxs('div', {
            className: 'min-w-0',
            children: [
              jsx('p', { className: 'truncate text-xs font-semibold text-(--ui-text-primary)', children: item.name }),
              jsx('p', {
                className: 'mt-0.5 truncate text-[0.64rem] uppercase tracking-[0.12em] text-(--ui-text-tertiary)',
                children: item.category
              })
            ]
          }),
          jsx(Badge, {
            variant: 'outline',
            className: 'shrink-0 text-[0.62rem]',
            style: { color: TIER_TONES[item.tier] || 'var(--ui-accent)' },
            children: item.tier || 'Earned'
          })
        ]
      }),
      !compact
        ? jsx('p', {
            className: 'mt-2 line-clamp-2 text-[0.7rem] leading-relaxed text-(--ui-text-secondary)',
            children: item.description
          })
        : null,
      item.next_tier
        ? jsxs('div', {
            className: 'mt-2',
            children: [
              jsxs('div', {
                className: 'flex items-center justify-between text-[0.62rem] text-(--ui-text-tertiary)',
                children: [
                  jsx('span', { children: `Next · ${item.next_tier}` }),
                  jsx('span', {
                    className: 'tabular-nums',
                    children: item.next_threshold == null ? '' : `${item.progress ?? 0}/${item.next_threshold}`
                  })
                ]
              }),
              jsx('div', {
                className: 'mt-1 h-1 overflow-hidden rounded-full bg-(--ui-bg-quaternary)',
                children: jsx('div', {
                  className: 'h-full rounded-full bg-(--ui-accent) transition-all',
                  style: { width: `${pct ?? 0}%` }
                })
              })
            ]
          })
        : jsx('p', {
            className: 'mt-2 text-[0.62rem] text-(--ui-text-tertiary)',
            children: `Unlocked ${relativeUnlock(item.unlocked_at)}`
          })
    ]
  })
}

function MessageBubble({ message }) {
  const user = message.role === 'user'
  const timestamp = formatMessageTimestamp(message.created_at)
  return jsx('div', {
    className: cn('flex', user ? 'justify-end' : 'justify-start'),
    children: jsxs('div', {
      className: cn(
        'max-w-[88%] rounded-xl px-3 py-2 text-[0.75rem] leading-relaxed whitespace-pre-wrap wrap-anywhere',
        user
          ? 'bg-[color-mix(in_srgb,var(--ui-accent)_18%,var(--ui-bg-elevated))] text-(--ui-text-primary)'
          : message.error
            ? 'border border-(--ui-danger,var(--ui-stroke-secondary)) bg-(--ui-bg-elevated) text-(--ui-text-secondary)'
            : 'border border-(--ui-stroke-secondary) bg-(--ui-bg-secondary) text-(--ui-text-primary)'
      ),
      children: [
        jsx('p', { children: message.text }),
        jsx('p', {
          className: 'mt-1 text-[0.56rem] tabular-nums tracking-[0.04em] text-(--ui-text-quaternary)',
          children: `${user
            ? message.assignment ? 'You · Task assignment' : 'You'
            : profileDisplayLabel(message.profile)} · ${timestamp}`
        })
      ]
    })
  })
}

function AgentDock() {
  const profilesQuery = useQuery({ queryKey: [ID, 'profiles'], queryFn: () => rest('/profiles'), refetchInterval: 20_000 })
  const achievementsQuery = useQuery({
    queryKey: [ID, 'achievements'],
    queryFn: () => rest('/achievements'),
    refetchInterval: 30_000
  })
  const [selected, setSelected] = useState(() => storage.get('selected-profile', ''))
  const [histories, setHistories] = useState(() => storage.get('histories', {}))
  const [sessions, setSessions] = useState(() => storage.get('sessions', {}))
  const [selectedModels, setSelectedModels] = useState(() => storage.get('selected-models', {}))
  const [selectedProviders, setSelectedProviders] = useState(() => storage.get('selected-providers', {}))
  const [selectedEfforts, setSelectedEfforts] = useState(() => storage.get('selected-efforts', {}))
  const [thinkingByProfile, setThinkingByProfile] = useState(() => storage.get('thinking', {}))
  const [fastByProfile, setFastByProfile] = useState(() => storage.get('fast', {}))
  const [modelFilter, setModelFilter] = useState('')
  const [assignTask, setAssignTask] = useState(false)
  const [drafts, setDrafts] = useState(() => storage.get('drafts', {}))
  const [activeJobs, setActiveJobs] = useState(() => {
    const stored = storage.get('active-jobs', {})
    const reconciled = pruneExpiredStartingJobs(stored)
    if (reconciled !== stored) storage.set('active-jobs', reconciled)
    return reconciled
  })
  const [muted, setMuted] = useState(() => storage.get('muted', false))
  const [achievementToast, setAchievementToast] = useState(null)
  const scrollRef = useRef(null)
  const notifiedJobIds = useRef(new Set())
  const activeJobsRef = useRef(activeJobs)

  const profiles = profilesQuery.data?.profiles || []
  const supportsIdempotentSubmit = profilesQuery.data?.capabilities?.idempotent_submit !== false
  const supportsModelOverride = profilesQuery.data?.capabilities?.model_override === true
  const supportsModelCatalog = profilesQuery.data?.capabilities?.model_catalog === true
  const supportsReasoning = profilesQuery.data?.capabilities?.reasoning === true
  const supportsFast = profilesQuery.data?.capabilities?.fast === true
  const supportsKanbanAssignment = profilesQuery.data?.capabilities?.kanban_assignment === true
  const currentProfile = profiles.find(profile => profile.name === selected) || profiles[0] || null
  const currentName = currentProfile?.name || selected
  const modelsQuery = useQuery({
    queryKey: [ID, 'models', currentName],
    queryFn: () => rest(`/models/${currentName}`),
    enabled: Boolean(currentName),
    refetchInterval: 60_000
  })
  const modelPayload = modelsQuery.data || null
  const modelOptions = useMemo(() => flattenModelOptions(modelPayload), [modelPayload])
  const filteredModelOptions = useMemo(() => {
    const needle = modelFilter.trim().toLowerCase()
    if (!needle) return modelOptions
    return modelOptions.filter(option =>
      `${option.providerName} ${option.provider} ${option.model}`.toLowerCase().includes(needle)
    )
  }, [modelFilter, modelOptions])
  const modelGroups = useMemo(() => groupModelOptions(filteredModelOptions), [filteredModelOptions])
  const selectedModel = selectedModels[currentName] || ''
  const selectedProvider = selectedProviders[currentName] || ''
  const selectedOption = useMemo(() => {
    if (!selectedModel) return null
    const matches = modelOptions.filter(option => option.model === selectedModel)
    return matches.find(option => !selectedProvider || option.provider === selectedProvider) || (matches.length === 1 ? matches[0] : null)
  }, [modelOptions, selectedModel, selectedProvider])
  const effectiveModel = selectedOption?.model || modelPayload?.model || currentProfile?.model || ''
  const effectiveProvider = selectedOption?.provider || modelPayload?.provider || currentProfile?.provider || ''
  const capabilities = selectedModelCapabilities(modelPayload, effectiveProvider, effectiveModel)
  const thinkingPreference = thinkingByProfile[currentName] !== false
  const effectiveThinking = thinkingPreference && capabilities.reasoning
  const effort = normalizeReasoningEffort(selectedEfforts[currentName] || 'medium')
  const fastPreference = fastByProfile[currentName] === true
  const effectiveFast = fastPreference && capabilities.fast
  const selectedOptionValue = selectedOption?.key || '__profile_default__'
  const modelControlsReady = supportsModelOverride && supportsModelCatalog && !modelsQuery.isPending && !modelsQuery.isError
  const messages = histories[currentName] || []
  const draft = drafts[currentName] || ''
  const activeJob = activeJobs[currentName] || null
  const selectedActivityLabel = profileActivityLabel(activeJob)
  const activeJobIds = useMemo(
    () => Object.values(activeJobs).map(job => job?.id).filter(Boolean).sort().join('|'),
    [activeJobs]
  )
  const startingRequestIds = useMemo(
    () => Object.values(activeJobs).filter(job => job && !job.id).map(job => job.request_id).filter(Boolean).sort().join('|'),
    [activeJobs]
  )
  const workingCount = Object.keys(activeJobs).length

  useEffect(() => {
    if (!selected && profiles.length) {
      setSelected(profiles[0].name)
      storage.set('selected-profile', profiles[0].name)
    } else if (selected && profiles.length && !profiles.some(profile => profile.name === selected)) {
      setSelected(profiles[0].name)
      storage.set('selected-profile', profiles[0].name)
    }
  }, [profiles.length, selected])

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' })
  }, [currentName, messages.length, activeJob?.status])

  useEffect(() => {
    const data = achievementsQuery.data
    if (!data?.available || !data.items?.length) return
    const latest = Math.max(...data.items.map(item => Number(item.unlocked_at) || 0))
    const baseline = storage.get('achievement-baseline', null)
    if (baseline == null) {
      storage.set('achievement-baseline', latest)
      return
    }
    if (latest <= baseline) return
    const newest = data.items.find(item => Number(item.unlocked_at) === latest)
    storage.set('achievement-baseline', latest)
    if (!newest) return
    setAchievementToast(newest)
    host.notify({ kind: 'success', message: `${newest.name} · ${newest.tier || 'Achievement unlocked'}` })
    if (!muted) playAchievementChime()
  }, [achievementsQuery.data?.generated_at, achievementsQuery.data?.unlocked_count])

  useEffect(() => {
    if (!achievementToast) return
    const timer = window.setTimeout(() => setAchievementToast(null), 6500)
    return () => window.clearTimeout(timer)
  }, [achievementToast?.id, achievementToast?.unlocked_at])

  const persistHistory = (profile, nextMessages) => {
    setHistories(current => {
      const next = { ...current, [profile]: nextMessages.slice(-MAX_LOCAL_MESSAGES) }
      storage.set('histories', next)
      return next
    })
  }

  const append = (profile, message) => {
    const stamped = stampMessage(message)
    setHistories(current => {
      const next = appendUniqueMessage(current, profile, stamped, MAX_LOCAL_MESSAGES)
      if (next === current) return current
      storage.set('histories', next)
      return next
    })
  }

  const setDraft = (profile, value) => {
    setDrafts(current => {
      const next = { ...current }
      if (value) next[profile] = value
      else delete next[profile]
      storage.set('drafts', next)
      return next
    })
  }

  const updateActiveJobs = updater => {
    setActiveJobs(current => {
      const next = typeof updater === 'function' ? updater(current) : updater
      activeJobsRef.current = next
      storage.set('active-jobs', next)
      return next
    })
  }

  const removeActiveJob = (profile, jobId) => {
    updateActiveJobs(current => {
      return removeProfileJob(current, profile, jobId)
    })
  }

  const reserveActiveJob = (profile, job) => {
    const result = reserveProfileJob(activeJobsRef.current, profile, job)
    if (!result.reserved) return false
    activeJobsRef.current = result.jobs
    setActiveJobs(result.jobs)
    storage.set('active-jobs', result.jobs)
    return true
  }

  useEffect(() => {
    if (!startingRequestIds) return
    const reconcileStartingJobs = () => {
      const stored = storage.get('active-jobs', {})
      const reconciled = pruneExpiredStartingJobs(stored)
      if (reconciled !== stored) storage.set('active-jobs', reconciled)
      const shouldSync = Object.entries(activeJobsRef.current).some(([profile, job]) => {
        if (!job || job.id) return false
        const persisted = reconciled[profile]
        return !persisted || persisted.id || persisted.request_id !== job.request_id
      })
      if (shouldSync) updateActiveJobs(reconciled)
    }
    reconcileStartingJobs()
    const timer = window.setInterval(reconcileStartingJobs, 250)
    return () => window.clearInterval(timer)
  }, [startingRequestIds])

  const notifyJobOnce = (jobId, kind, message) => {
    if (notifiedJobIds.current.has(jobId)) return
    notifiedJobIds.current.add(jobId)
    host.notify({ kind, message })
  }

  useEffect(() => {
    if (!activeJobIds) return
    let disposed = false
    const timers = new Map()

    const poll = async (profile, jobId, failures = 0) => {
      try {
        const job = await rest(`/jobs/${jobId}`)
        if (disposed || job.id !== jobId || activeJobsRef.current[profile]?.id !== jobId) return
        updateActiveJobs(current =>
          current[profile]?.id === job.id
            ? { ...current, [profile]: { ...current[profile], ...job, profile } }
            : current
        )
        if (job.status === 'done') {
          append(profile, { id: `${job.id}:assistant`, role: 'assistant', profile, text: job.response || '(No response text)' })
          if (job.kanban_error) {
            append(profile, { id: `${job.id}:kanban-error`, role: 'assistant', profile, error: true, text: job.kanban_error })
          }
          if (job.session_id) {
            setSessions(current => {
              const next = { ...current, [profile]: job.session_id }
              storage.set('sessions', next)
              return next
            })
          }
          removeActiveJob(profile, job.id)
          notifyJobOnce(job.id, 'success', `${profileDisplayLabel(profile)} finished. The result is ready in Agent Dock.`)
          haptic('success')
          return
        }
        if (job.status === 'error' || job.status === 'cancelled') {
          append(profile, {
            id: `${job.id}:error`,
            role: 'assistant',
            profile,
            error: true,
            text: job.status === 'cancelled'
              ? `Direct session cancelled.${job.kanban_error ? `\n${job.kanban_error}` : ''}`
              : `${job.error || 'The specialist session failed.'}${job.kanban_error ? `\n${job.kanban_error}` : ''}`
          })
          removeActiveJob(profile, job.id)
          if (job.status === 'error') {
            notifyJobOnce(job.id, 'error', `${profileDisplayLabel(profile)} could not finish. Open Agent Dock for details.`)
          }
          return
        }
        const timerKey = `${profile}:${job.id}`
        timers.set(timerKey, window.setTimeout(() => void poll(profile, job.id), 900))
      } catch (error) {
        if (disposed || activeJobsRef.current[profile]?.id !== jobId) return
        if (isNotFoundError(error)) {
          removeActiveJob(profile, jobId)
          return
        }
        if (failures < 2) {
          const timerKey = `${profile}:${jobId}`
          const delay = 900 * 2 ** failures
          timers.set(timerKey, window.setTimeout(() => void poll(profile, jobId, failures + 1), delay))
          return
        }
        append(profile, {
          id: `${jobId}:poll-error`,
          role: 'assistant',
          profile,
          error: true,
          text: `Session status is temporarily unavailable: ${error?.message || error}. Monitoring continues.`
        })
        const timerKey = `${profile}:${jobId}`
        timers.set(timerKey, window.setTimeout(() => void poll(profile, jobId, 0), 10_000))
      }
    }

    for (const [profile, job] of Object.entries(activeJobs)) {
      if (job?.id) void poll(profile, job.id)
    }
    return () => {
      disposed = true
      for (const timer of timers.values()) window.clearTimeout(timer)
    }
  }, [activeJobIds])

  const selectProfile = name => {
    setSelected(name)
    storage.set('selected-profile', name)
    haptic('tap')
  }

  const selectModel = value => {
    if (!currentName) return
    const option = value === '__profile_default__' ? null : modelOptions.find(item => item.key === value)
    if (value !== '__profile_default__' && !option) return
    setSelectedModels(current => {
      const next = { ...current }
      if (option) next[currentName] = option.model
      else delete next[currentName]
      storage.set('selected-models', next)
      return next
    })
    setSelectedProviders(current => {
      const next = { ...current }
      if (option) next[currentName] = option.provider
      else delete next[currentName]
      storage.set('selected-providers', next)
      return next
    })
    setModelFilter('')
    haptic('tap')
  }

  const setThinking = checked => {
    if (!currentName || !capabilities.reasoning) return
    setThinkingByProfile(current => {
      const next = { ...current, [currentName]: checked === true }
      storage.set('thinking', next)
      return next
    })
  }

  const setEffort = value => {
    if (!currentName || !VALID_REASONING_EFFORTS.has(value)) return
    setSelectedEfforts(current => {
      const next = { ...current, [currentName]: value }
      storage.set('selected-efforts', next)
      return next
    })
  }

  const setFast = checked => {
    if (!currentName || !capabilities.fast) return
    setFastByProfile(current => {
      const next = { ...current, [currentName]: checked === true }
      storage.set('fast', next)
      return next
    })
  }

  const send = async () => {
    const message = draft.trim()
    if (!message || !currentName || activeJob) return
    const profile = currentName
    const requestId = makeRequestId()
    const sessionId = sessions[profile] || null
    const assignment = assignTask === true && supportsKanbanAssignment
    const optimistic = {
      id: `local:${requestId}`,
      role: 'user',
      profile,
      text: message,
      assignment,
      created_at: Date.now()
    }
    if (!reserveActiveJob(profile, {
      id: null,
      profile,
      request_id: requestId,
      session_id: sessionId,
      status: 'starting',
      started_at: Date.now()
    })) return
    append(profile, optimistic)
    setDraft(profile, '')
    const body = buildJobPayload({
      profile,
      provider: modelControlsReady ? effectiveProvider : '',
      model: modelControlsReady ? effectiveModel : '',
      thinking: supportsReasoning && effectiveThinking,
      effort,
      fast: supportsFast && effectiveFast,
      message,
      session_id: sessionId,
      request_id: requestId,
      assign_task: assignment,
      modelPayload
    })
    const submit = () => rest('/jobs', { method: 'POST', body, timeoutMs: 15_000 })
    const acceptJob = job => {
      const stored = storage.get('active-jobs', {})
      const next = replaceStartingJob(stored, profile, requestId, job)
      activeJobsRef.current = next
      storage.set('active-jobs', next)
      setActiveJobs(next)
      if (job.kanban_task_id) {
        append(profile, {
          id: `${job.id}:kanban`,
          role: 'assistant',
          profile,
          text: `Task ${job.kanban_task_id} was added to ${job.kanban_board || 'Kanban'} and assigned to ${profileDisplayLabel(profile)}.`
        })
      }
      if (assignment) setAssignTask(false)
      haptic('tap')
    }
    await reconcileIdempotentSubmission(submit, {
      allowRetry: supportsIdempotentSubmit,
      isReserved: () => activeJobsRef.current[profile]?.request_id === requestId,
      onAccepted: acceptJob,
      onPending: (error, retrying) => append(profile, {
        id: `error:${requestId}`,
        role: 'assistant',
        profile,
        error: true,
        text: retrying
          ? `Submission status is temporarily unavailable: ${error?.message || error}. Reconciliation continues.`
          : `Submission could not be confirmed safely: ${error?.message || error}`
      }),
      schedule: (callback, delay) => window.setTimeout(callback, delay)
    })
  }

  const cancel = async () => {
    if (!activeJob?.id) return
    const profile = currentName
    const jobId = activeJob.id
    const previousStatus = activeJob.status
    updateActiveJobs(current =>
      current[profile]?.id === jobId
        ? { ...current, [profile]: { ...current[profile], status: 'cancelling' } }
        : current
    )
    try {
      const job = await rest(`/jobs/${jobId}`, { method: 'DELETE' })
      updateActiveJobs(current =>
        current[profile]?.id === jobId
          ? { ...current, [profile]: { ...current[profile], ...job, profile } }
          : current
      )
    } catch (error) {
      updateActiveJobs(current =>
        current[profile]?.id === jobId
          ? { ...current, [profile]: { ...current[profile], status: previousStatus } }
          : current
      )
      append(profile, {
        id: `${jobId}:cancel-error`,
        role: 'assistant',
        profile,
        error: true,
        text: `Cancellation could not be confirmed: ${error?.message || error}`
      })
    }
  }

  const clearConversation = () => {
    if (!currentName || activeJob) return
    persistHistory(currentName, [])
    setSessions(current => {
      const next = { ...current }
      delete next[currentName]
      storage.set('sessions', next)
      return next
    })
  }

  const agentHeading = currentProfile
    ? `${profileDisplayLabel(currentProfile.name)}${effectiveModel ? ` · ${effectiveModel}` : ''}`
    : 'Choose an agent'

  return jsxs('section', {
    className: 'relative flex h-full min-h-0 flex-col overflow-hidden bg-(--ui-surface-background)',
    children: [
      achievementToast
        ? jsx('div', {
            className: 'absolute left-3 right-3 top-3 z-50 animate-in slide-in-from-top-2 fade-in',
            children: jsx(AchievementCard, { item: achievementToast })
          })
        : null,
      jsxs('header', {
        className: 'shrink-0 border-b border-(--ui-stroke-secondary) px-3 py-2',
        children: [
          jsxs('div', {
            className: 'flex items-center gap-2',
            children: [
              jsx(HermesMark, { compact: true }),
              jsxs('div', {
                className: 'min-w-0 flex-1',
                children: [
                  jsx('h2', { className: 'text-sm font-semibold', children: 'Agent Dock' }),
                  jsx('p', {
                    className: 'truncate text-[0.65rem] text-(--ui-text-tertiary)',
                    children: workingCount
                      ? `${workingCount} specialist${workingCount === 1 ? '' : 's'} working · switch freely`
                      : 'Direct specialist sessions · local profile routing'
                  })
                ]
              }),
              jsx('button', {
                'aria-label': muted ? 'Enable achievement sound' : 'Mute achievement sound',
                className: 'grid size-7 place-items-center rounded-md text-(--ui-text-tertiary) hover:bg-(--ui-control-hover-background) hover:text-foreground',
                onClick: () => {
                  const next = !muted
                  setMuted(next)
                  storage.set('muted', next)
                },
                type: 'button',
                children: jsx(Codicon, { name: muted ? 'mute' : 'unmute', size: '0.82rem' })
              })
            ]
          }),
          profilesQuery.isError
            ? jsx('p', { className: 'mt-2 text-[0.68rem] text-(--ui-danger)', children: 'Profile discovery failed. Restart Hermes after enabling the backend.' })
            : profiles.length
              ? jsxs('div', {
                  className: 'mt-2 space-y-1.5',
                  children: [
                    jsxs('div', {
                      className: 'grid grid-cols-[minmax(0,0.78fr)_minmax(0,1.22fr)] gap-2',
                      children: [
                        jsxs('label', {
                          className: 'min-w-0',
                          children: [
                            jsx('span', { className: 'mb-1 block text-[0.58rem] font-medium uppercase tracking-[0.1em] text-(--ui-text-quaternary)', children: 'Agent' }),
                            jsxs(Select, {
                              onValueChange: selectProfile,
                              value: currentName,
                              children: [
                                jsx(SelectTrigger, {
                                  'aria-label': `Select agent. ${profileDisplayLabel(currentName)} is ${selectedActivityLabel.toLowerCase()}`,
                                  className: cn('h-7 w-full min-w-0 text-[0.7rem]', activeJob && 'font-medium text-(--ui-accent)'),
                                  style: activeJob
                                    ? {
                                        borderColor: 'var(--ui-accent)',
                                        background: 'color-mix(in srgb, var(--ui-accent) 13%, var(--ui-control-hover-background))'
                                      }
                                    : undefined,
                                  title: `${profileDisplayLabel(currentName)} · ${selectedActivityLabel}`,
                                  children: jsxs('span', {
                                    className: 'flex min-w-0 flex-1 items-center gap-1.5',
                                    children: [
                                      jsx('span', {
                                        'aria-hidden': true,
                                        className: cn(
                                          'inline-block size-1.5 shrink-0 rounded-full',
                                          activeJob ? 'animate-pulse bg-(--ui-accent)' : 'bg-(--ui-text-quaternary)'
                                        )
                                      }),
                                      jsx(SelectValue, { placeholder: 'Choose agent' }),
                                      jsx('span', {
                                        className: cn(
                                          'ml-auto shrink-0 text-[0.56rem] uppercase tracking-[0.08em]',
                                          activeJob ? 'text-(--ui-accent)' : 'text-(--ui-text-quaternary)'
                                        ),
                                        children: selectedActivityLabel
                                      })
                                    ]
                                  })
                                }),
                                jsx(SelectContent, {
                                  children: profiles.map(profile => {
                                    const profileJob = activeJobs[profile.name]
                                    const profileWorking = Boolean(profileJob)
                                    const profileStatus = profileActivityLabel(profileJob)
                                    return jsx(SelectItem, {
                                      className: profileWorking ? 'font-medium text-(--ui-accent)' : undefined,
                                      value: profile.name,
                                      children: jsxs('span', {
                                        className: 'flex w-full items-center gap-1.5',
                                        children: [
                                          jsx('span', {
                                            'aria-hidden': true,
                                            className: cn(
                                              'inline-block size-1.5 shrink-0 rounded-full',
                                              profileWorking ? 'animate-pulse bg-(--ui-accent)' : 'bg-(--ui-text-quaternary)'
                                            )
                                          }),
                                          jsx('span', { children: profileDisplayLabel(profile.name) }),
                                          jsx('span', {
                                            className: cn(
                                              'ml-auto pl-3 text-[0.56rem] uppercase tracking-[0.08em]',
                                              profileWorking ? 'text-(--ui-accent)' : 'text-(--ui-text-quaternary)'
                                            ),
                                            children: profileStatus
                                          })
                                        ]
                                      })
                                    }, profile.name)
                                  })
                                })
                              ]
                            })
                          ]
                        }),
                        jsxs('label', {
                          className: 'min-w-0',
                          children: [
                            jsx('span', { className: 'mb-1 block text-[0.58rem] font-medium uppercase tracking-[0.1em] text-(--ui-text-quaternary)', children: 'Model' }),
                            jsxs(Select, {
                              disabled: !modelControlsReady,
                              onValueChange: selectModel,
                              value: selectedOptionValue,
                              children: [
                                jsx(SelectTrigger, {
                                  'aria-label': 'Select model',
                                  className: 'h-7 w-full min-w-0 text-[0.7rem]',
                                  title: modelControlsReady ? 'Select any model configured for this agent' : 'Loading the native Hermes model catalog',
                                  children: jsx(SelectValue, { placeholder: modelsQuery.isPending ? 'Loading models…' : 'Profile default' })
                                }),
                                jsxs(SelectContent, {
                                  children: [
                                    jsx(SelectItem, {
                                      value: '__profile_default__',
                                      children: `Profile default${currentProfile?.model ? ` · ${currentProfile.model}` : ''}`
                                    }),
                                    ...modelGroups.flatMap(group => [
                                      jsx(SelectItem, {
                                        className: 'pointer-events-none text-[0.58rem] font-semibold uppercase tracking-[0.1em] text-(--ui-text-quaternary)',
                                        disabled: true,
                                        value: `__provider__${group.provider}`,
                                        children: group.providerName
                                      }, `provider:${group.provider}`),
                                      ...group.options.map(option =>
                                        jsx(SelectItem, { value: option.key, children: option.model }, option.key)
                                      )
                                    ])
                                  ]
                                })
                              ]
                            })
                          ]
                        })
                      ]
                    }),
                    jsx('input', {
                      'aria-label': 'Filter models',
                      className: 'h-7 w-full rounded-md border border-(--ui-stroke-secondary) bg-(--ui-bg-secondary) px-2 text-[0.67rem] text-foreground outline-none placeholder:text-(--ui-text-quaternary) focus:border-(--ui-accent)',
                      disabled: !modelControlsReady,
                      onChange: event => setModelFilter(event.target.value),
                      placeholder: modelOptions.length ? `Filter ${modelOptions.length} configured models…` : 'No configured models',
                      type: 'search',
                      value: modelFilter
                    }),
                    modelsQuery.isError
                      ? jsx('p', { className: 'text-[0.62rem] text-(--ui-danger)', children: 'The native model catalog could not be loaded for this agent.' })
                      : null
                  ]
                })
              : jsx('p', { className: 'mt-2 text-[0.68rem] text-(--ui-text-tertiary)', children: 'No Hermes profiles found.' })
        ]
      }),
      jsxs('div', {
        className: 'flex shrink-0 items-center gap-1 border-b border-(--ui-stroke-secondary) px-3 py-1',
        children: [
          jsx('span', {
            className: 'rounded-md bg-(--ui-control-active-background) px-2.5 py-1 text-[0.68rem] text-foreground',
            children: 'Chat'
          }),
          jsx('span', { className: 'ml-auto truncate text-[0.61rem] text-(--ui-text-quaternary)', children: agentHeading })
        ]
      }),
      jsxs('div', {
            className: 'flex min-h-0 flex-1 flex-col',
            children: [
              jsx('div', {
                ref: scrollRef,
                className: 'min-h-0 flex-1 space-y-2 overflow-y-auto px-3 py-2',
                children: messages.length
                  ? messages.map(message => jsx(MessageBubble, { key: message.id, message }))
                  : jsxs('div', {
                      className: 'grid h-full place-content-center px-7 text-center',
                      children: [
                        jsx(HermesMark, {}),
                        jsx('p', { className: 'mt-3 text-xs font-medium', children: 'Talk directly to a specialist' }),
                        jsx('p', {
                          className: 'mt-1 text-[0.68rem] leading-relaxed text-(--ui-text-tertiary)',
                          children: 'Choose a configured profile above. This starts its own Hermes session even when your orchestrator is busy.'
                        })
                      ]
                    })
              }),
              activeJob
                ? jsxs('div', {
                    className: 'mx-3 mb-1.5 flex items-center gap-2 rounded-lg border border-(--ui-stroke-secondary) bg-(--ui-bg-secondary) px-2.5 py-1.5',
                    children: [
                      jsx(Codicon, { name: 'loading', className: 'animate-spin text-(--ui-accent)', size: '0.76rem' }),
                      jsx('span', {
                        className: 'min-w-0 flex-1 truncate text-[0.68rem] text-(--ui-text-secondary)',
                        children: activeJob.id
                          ? `${profileDisplayLabel(activeJob.profile)} is working in a direct session…`
                          : `${profileDisplayLabel(activeJob.profile)} is starting a direct session…`
                      }),
                      activeJob.id
                        ? jsx('button', {
                            className: 'text-[0.64rem] text-(--ui-text-tertiary) hover:text-foreground',
                            onClick: cancel,
                            type: 'button',
                            children: 'Cancel'
                          })
                        : null
                    ]
                  })
                : null,
              jsxs('div', {
                className: 'shrink-0 border-t border-(--ui-stroke-secondary) p-2.5',
                children: [
                  jsx(Textarea, {
                    'aria-label': `Message ${profileDisplayLabel(currentName) || 'selected agent'}`,
                    className: 'min-h-[3.25rem] max-h-32 resize-none text-xs',
                    disabled: !currentName || Boolean(activeJob),
                    onChange: event => setDraft(currentName, event.target.value),
                    onKeyDown: event => {
                      if (event.key === 'Enter' && !event.shiftKey) {
                        event.preventDefault()
                        void send()
                      }
                    },
                    placeholder: currentName ? `Message ${profileDisplayLabel(currentName)}…` : 'No profiles found',
                    value: draft
                  }),
                  jsxs('div', {
                    className: 'mt-1.5 flex items-center gap-2',
                    children: [
                      jsx('button', {
                        className: 'text-[0.62rem] text-(--ui-text-quaternary) hover:text-(--ui-text-secondary) disabled:opacity-40',
                        disabled: Boolean(activeJob) || !messages.length,
                        onClick: clearConversation,
                        type: 'button',
                        children: sessions[currentName] ? 'New conversation' : 'Clear'
                      }),
                      jsx('button', {
                        'aria-pressed': assignTask,
                        className: cn(
                          'rounded px-1.5 py-0.5 text-[0.62rem] disabled:opacity-40',
                          assignTask
                            ? 'bg-[color-mix(in_srgb,var(--ui-accent)_18%,transparent)] text-(--ui-accent)'
                            : 'text-(--ui-text-quaternary) hover:text-(--ui-text-secondary)'
                        ),
                        disabled: Boolean(activeJob) || !supportsKanbanAssignment,
                        onClick: () => setAssignTask(current => !current),
                        title: supportsKanbanAssignment
                          ? 'Create and track this message on the executive-organization Kanban board'
                          : 'Kanban assignment is unavailable',
                        type: 'button',
                        children: assignTask ? 'Task ✓' : 'Assign task'
                      }),
                      jsx('span', {
                        className: 'ml-auto text-[0.58rem] text-(--ui-text-quaternary)',
                        children: 'Enter send · Shift+Enter newline'
                      }),
                      jsx(Button, {
                        disabled: !draft.trim() || !currentName || Boolean(activeJob),
                        onClick: send,
                        size: 'sm',
                        children: assignTask ? 'Assign' : 'Send'
                      })
                    ]
                  })
                ]
              })
            ]
          })
    ]
  })
}

function DockStatusButton({ onToggle }) {
  const open = useValue($dockOpen)
  const [activities, setActivities] = useState(() => activeJobActivities(storage.get('active-jobs', {})))
  const working = activities.length > 0

  useEffect(() => {
    let disposed = false
    let timer = null

    const refreshActivity = async () => {
      const savedJobs = storage.get('active-jobs', {})
      const reconciled = pruneExpiredStartingJobs(savedJobs)
      if (reconciled !== savedJobs) storage.set('active-jobs', reconciled)
      const active = await resolveActiveJobActivities(reconciled, jobId => rest(`/jobs/${jobId}`))

      if (!disposed) {
        setActivities(active)
        timer = window.setTimeout(refreshActivity, 900)
      }
    }

    void refreshActivity()
    return () => {
      disposed = true
      if (timer) window.clearTimeout(timer)
    }
  }, [])

  const toggleLabel = open ? 'Hide Agent Dock' : 'Open Agent Dock'
  const activityLabel = activitySummary(activities)

  return jsxs('button', {
    'aria-label': `${toggleLabel}. ${activityLabel}`,
    'aria-pressed': open,
    className: cn(
      'inline-flex h-full items-center gap-1.5 rounded-none px-2 text-[0.6875rem] transition-colors',
      working
        ? 'font-medium text-(--ui-accent)'
        : open
          ? 'bg-(--chrome-action-hover) text-foreground'
          : 'text-(--ui-text-tertiary) hover:bg-(--chrome-action-hover) hover:text-foreground'
    ),
    onClick: () => {
      haptic('tap')
      onToggle()
    },
    style: working
      ? {
          background: 'color-mix(in srgb, var(--ui-accent) 16%, var(--ui-control-hover-background))',
          boxShadow: 'inset 0 1px 0 color-mix(in srgb, var(--ui-accent) 55%, transparent)'
        }
      : undefined,
    title: `${toggleLabel} · ${activityLabel}`,
    type: 'button',
    children: [
      jsx(Codicon, { name: working ? 'loading' : 'hubot', className: working ? 'animate-spin' : undefined, size: '0.72rem' }),
      jsx('span', { children: 'Agent Dock' }),
      working && activities.length > 1
        ? jsx('span', { className: 'tabular-nums text-[0.6rem]', children: activities.length })
        : null
    ]
  })
}

export default {
  id: ID,
  name: 'Hermes Agent Dock',
  description: 'A docked direct-chat pane for specialist Hermes profiles with native model controls.',
  defaultEnabled: true,
  register(ctx) {
    rest = ctx.rest
    storage = ctx.storage
    dockPaneDisposer = null
    $dockOpen.set(false)

    const openDock = () => {
      if (dockPaneDisposer) return
      dockPaneDisposer = ctx.register({
        id: 'dock-v3',
        area: PANES_AREA,
        order: 80,
        title: 'Agent Dock',
        data: {
          placement: 'right',
          dock: { pane: 'files', pos: 'top' },
          height: '50%',
          width: 'clamp(19rem, 24vw, 22rem)',
          minWidth: '18rem',
          maxWidth: '26rem',
          uncloseable: true
        },
        render: () => jsx(AgentDock, {})
      })
      storage.set('dock-open', true)
      $dockOpen.set(true)
    }

    const closeDock = () => {
      const dispose = dockPaneDisposer
      dockPaneDisposer = null
      dispose?.()
      storage.set('dock-open', false)
      $dockOpen.set(false)
    }

    const toggleDock = () => ($dockOpen.get() ? closeDock() : openDock())

    ctx.registerMany([
      {
        id: 'launcher',
        area: STATUSBAR_AREAS.right,
        order: 95,
        render: () => jsx(DockStatusButton, { onToggle: toggleDock })
      },
      {
        id: 'focus',
        area: PALETTE_AREA,
        data: {
          id: 'hermes-agent-dock.focus',
          label: 'Agent Dock: Toggle specialist pane',
          keywords: ['agent', 'profile', 'chat', 'dock', 'specialist'],
          run: toggleDock
        }
      }
    ])

    if (storage.get('dock-open', false)) openDock()
    ctx.onDispose(() => {
      dockPaneDisposer = null
      $dockOpen.set(false)
    })
  }
}
