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
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
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
// Kept as a string contract so this plugin still loads on older Hermes builds
// that predate the public SDK export. Supporting hosts resolve this area; older
// hosts safely retain the status-bar and command-palette launchers.
const PET_ACTIONS_AREA = 'pet.actions'
const MAX_LOCAL_MESSAGES = 30
let rest
let storage
let dockPaneDisposer = null
const $dockOpen = atom(false)
const $dockMode = atom('floating')

// STATE_HELPERS_START — dependency-free logic exercised by tests/test_dock_state.mjs.
const DEFAULT_DOCK_MODE = 'floating'
const DOCK_MODES = Object.freeze(['floating', 'docked'])
const MAX_IMAGE_ATTACHMENTS = 4
const MAX_IMAGE_BYTES = 10 * 1024 * 1024
const IMAGE_MIME_TYPES = new Set(['image/png', 'image/jpeg', 'image/gif', 'image/webp', 'image/bmp'])
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
const REASONING_SLIDER_VALUES = Object.freeze(['low', 'medium', 'high'])
const WORKLOAD_TIER_LABELS = Object.freeze({ low: 'Low', medium: 'Medium', high: 'High' })
const MODEL_WORKLOAD_TIERS = Object.freeze({
  'gpt-5.6-sol': 'high',
  'gpt-5.6-sol-pro': 'high',
  'gpt-5.6-terra-pro': 'high',
  'gpt-5.6-terra': 'medium',
  'gpt-5.6-luna-pro': 'medium',
  'gpt-5.5': 'medium',
  'gpt-5.4': 'medium',
  'gpt-5.6-luna': 'low',
  'gpt-5.4-mini': 'low',
  'gpt-5.3-codex-spark': 'low'
})
const MODEL_DISPLAY_LABELS = Object.freeze({
  'gpt-5.6-sol': 'GPT 5.6 Sol',
  'gpt-5.6-sol-pro': 'GPT 5.6 Sol Pro',
  'gpt-5.6-terra-pro': 'GPT 5.6 Terra Pro',
  'gpt-5.6-terra': 'GPT 5.6 Terra',
  'gpt-5.6-luna-pro': 'GPT 5.6 Luna Pro',
  'gpt-5.5': 'GPT 5.5',
  'gpt-5.4': 'GPT 5.4',
  'gpt-5.6-luna': 'GPT 5.6 Luna',
  'gpt-5.4-mini': 'GPT 5.4 Mini',
  'gpt-5.3-codex-spark': 'GPT 5.3 Codex Spark'
})
const ACTIVE_JOB_STATUSES = new Set(['starting', 'queued', 'running', 'finalizing', 'cancelling'])
const STARTING_JOB_TTL_MS = 60_000

function normalizeDockMode(mode) {
  return DOCK_MODES.includes(mode) ? mode : DEFAULT_DOCK_MODE
}

function nextDockMode(mode) {
  return normalizeDockMode(mode) === 'floating' ? 'docked' : 'floating'
}

function dockModeAction(mode) {
  return normalizeDockMode(mode) === 'floating' ? 'Dock' : 'Undock'
}

function dockPaneData(mode) {
  return normalizeDockMode(mode) === 'floating'
    ? {
        placement: 'floating',
        anchor: 'top-right',
        width: '380px',
        height: '540px',
        uncloseable: true
      }
    : {
        placement: 'bottom',
        dock: { pane: 'workspace', pos: 'bottom' },
        height: '42vh',
        minHeight: '18rem',
        maxHeight: '70vh',
        uncloseable: true
      }
}

function profileDisplayLabel(rawProfile) {
  const raw = String(rawProfile ?? '')
  return raw
    .split(/[-_]+/)
    .filter(Boolean)
    .map(part => `${part.charAt(0).toUpperCase()}${part.slice(1).toLowerCase()}`)
    .join(' ')
}

function profileAvatarInitials(rawProfile) {
  const parts = String(rawProfile ?? '').split(/[-_]+/).filter(Boolean)
  if (!parts.length) return 'AI'
  return parts.slice(0, 2).map(part => part.charAt(0).toUpperCase()).join('') || 'AI'
}

function modelOptionKey(provider, model) {
  return `${encodeURIComponent(provider)}::${encodeURIComponent(model)}`
}

function modelPresentation(model, capabilities = {}) {
  const raw = String(model ?? '').trim()
  const normalized = raw.toLowerCase()
  const tier = MODEL_WORKLOAD_TIERS[normalized] || (capabilities.reasoning === true ? 'medium' : 'low')
  return {
    label: MODEL_DISPLAY_LABELS[normalized] || raw || 'Unknown model',
    tier,
    tierLabel: WORKLOAD_TIER_LABELS[tier]
  }
}

function compactModelLabel(label) {
  const normalized = String(label ?? '').trim()
  return normalized.replace(/^GPT\s+/i, '') || 'No model'
}

function migrateSavedModelSelections(savedModels, savedProviders = {}) {
  if (!savedModels || typeof savedModels !== 'object' || Array.isArray(savedModels)) return {}
  const providers = savedProviders && typeof savedProviders === 'object' && !Array.isArray(savedProviders)
    ? savedProviders
    : {}
  return Object.fromEntries(Object.entries(savedModels).flatMap(([profile, selection]) => {
    if (typeof selection === 'string') {
      const model = selection.trim()
      if (!model) return []
      return [[profile, { provider: String(providers[profile] ?? '').trim(), model }]]
    }
    if (!selection || typeof selection !== 'object' || Array.isArray(selection)) return []
    const model = String(selection.model ?? '').trim()
    if (!model) return []
    return [[profile, { provider: String(selection.provider ?? '').trim(), model }]]
  }))
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

function reasoningEffortSliderPosition(effort) {
  const normalized = normalizeReasoningEffort(effort)
  if (normalized === 'minimal' || normalized === 'low') return 0
  if (normalized === 'medium') return 1
  return 2
}

function reasoningEffortForSliderPosition(position) {
  return REASONING_SLIDER_VALUES[Number(position)] || 'medium'
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

function validateImageFileMetadata(file, currentCount = 0) {
  if (currentCount >= MAX_IMAGE_ATTACHMENTS) return `Attach at most ${MAX_IMAGE_ATTACHMENTS} images.`
  if (!IMAGE_MIME_TYPES.has(String(file?.type || '').toLowerCase())) return 'Use PNG, JPEG, GIF, WebP, or BMP images.'
  const size = Number(file?.size)
  if (!Number.isFinite(size) || size <= 0) return 'The selected image is empty.'
  if (size > MAX_IMAGE_BYTES) return `Each image must be ${MAX_IMAGE_BYTES / (1024 * 1024)} MB or smaller.`
  return null
}

function extractClipboardImageFiles(clipboard) {
  const images = []
  const seen = new Set()
  const push = file => {
    if (!file || !String(file.type || '').toLowerCase().startsWith('image/')) return
    const key = [file.name || '', file.size || 0, file.type || '', file.lastModified || 0].join(':')
    if (seen.has(key)) return
    seen.add(key)
    images.push(file)
  }

  for (const item of Array.from(clipboard?.items || [])) {
    if (item?.kind === 'file' && String(item.type || '').toLowerCase().startsWith('image/')) {
      push(item.getAsFile?.())
    }
  }

  // Chromium commonly mirrors one clipboard image in both collections.
  if (!images.length) {
    for (const file of Array.from(clipboard?.files || [])) push(file)
  }
  return images
}

function shouldConsumeClipboardPaste(clipboard, images, currentCount = 0) {
  if (String(clipboard?.getData?.('text/plain') || '')) return false
  return images.some(image => !validateImageFileMetadata(image, currentCount))
}

function buildJobPayload({ profile, provider, model, thinking, effort, fast, message, images, session_id, request_id, assign_task, modelPayload }) {
  const settings = resolveModelSettings({ modelPayload, provider, model, thinking, effort, fast })
  return {
    profile: String(profile ?? '').trim(),
    provider: String(provider ?? '').trim() || null,
    model: String(model ?? '').trim() || null,
    reasoning_effort: settings.reasoning_effort,
    fast: settings.fast_enabled,
    message,
    images: Array.isArray(images)
      ? images.map(image => ({ name: image.name, mime_type: image.mime_type, data_url: image.data_url }))
      : [],
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

function messageAttachmentMetadata(images) {
  return Array.isArray(images)
    ? images.map(image => ({
        name: attachmentDisplayName(image.name),
        mime_type: image.mime_type,
        size: image.size
      }))
    : []
}

function attachmentDisplayName(name) {
  const basename = String(name ?? '').split(/[\\/]/).pop()?.trim()
  return basename || 'image'
}

async function copyTextToClipboard(text, clipboard = globalThis.navigator?.clipboard) {
  if (!clipboard || typeof clipboard.writeText !== 'function') return 'unavailable'
  try {
    await clipboard.writeText(String(text ?? ''))
    return 'copied'
  } catch {
    return 'failed'
  }
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

function normalizeSubagents(rows) {
  if (!Array.isArray(rows)) return []
  const allowed = new Set(['running', 'completed', 'failed', 'interrupted'])
  return rows
    .filter(row => (
      row &&
      typeof row.subagent_id === 'string' &&
      Number.isInteger(row.task_index) &&
      allowed.has(String(row.status || '').toLowerCase())
    ))
    .map(row => ({
      subagent_id: row.subagent_id.slice(0, 128),
      task_index: row.task_index,
      status: String(row.status).toLowerCase(),
      current_tool: typeof row.current_tool === 'string' ? row.current_tool.slice(0, 32) : null,
      started_at: Number(row.started_at) || null,
      updated_at: Number(row.updated_at) || null,
      finished_at: Number(row.finished_at) || null,
      duration_seconds: Number(row.duration_seconds) || 0,
      model: typeof row.model === 'string' ? row.model.slice(0, 128) : null,
      api_calls: Number.isInteger(row.api_calls) && row.api_calls >= 0 ? row.api_calls : null,
      input_tokens: Number.isInteger(row.input_tokens) && row.input_tokens >= 0 ? row.input_tokens : null,
      output_tokens: Number.isInteger(row.output_tokens) && row.output_tokens >= 0 ? row.output_tokens : null,
      total_tokens: Number.isInteger(row.total_tokens) && row.total_tokens >= 0 ? row.total_tokens : null,
      usage_state: row.usage_state === 'reported' ? 'reported' : 'unavailable',
      direct_chat_available: false
    }))
    .sort((left, right) => left.task_index - right.task_index)
}

function subagentStatusLabel(status) {
  return ({
    running: 'Running',
    completed: 'Completed',
    failed: 'Failed',
    interrupted: 'Interrupted'
  })[String(status || '').toLowerCase()] || 'Unknown'
}

function updateProfileSubagents(snapshots, profile, job) {
  if (!job?.id || !Array.isArray(job.subagents)) return snapshots
  return {
    ...(snapshots || {}),
    [profile]: { job_id: job.id, subagents: normalizeSubagents(job.subagents) }
  }
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

const INTERVENTION_KINDS = Object.freeze(['ask', 'nudge', 'redirect'])

function normalizeInterventionKind(value) {
  return INTERVENTION_KINDS.includes(value) ? value : 'ask'
}

function interventionMethod(kind) {
  const normalized = normalizeInterventionKind(kind)
  if (normalized === 'nudge') return 'session.steer'
  if (normalized === 'redirect') return 'session.redirect'
  return 'prompt.submit'
}

function interventionNeedsConfirmation(kind) {
  return normalizeInterventionKind(kind) === 'redirect'
}

function normalizeRuntimeProfile(value) {
  if (typeof value !== 'string') return null
  const normalized = value.trim().toLowerCase()
  return /^[a-z0-9][a-z0-9_-]{0,63}$/.test(normalized) ? normalized : null
}

function knownProfileNames(profiles) {
  if (!Array.isArray(profiles)) return []
  return profiles
    .map(profile => typeof profile === 'string' ? profile : profile?.name)
    .map(normalizeRuntimeProfile)
    .filter(Boolean)
}

function exactRuntimeProfile(selectedProfile, runtimeProfile, profiles) {
  const selected = normalizeRuntimeProfile(selectedProfile)
  const runtime = normalizeRuntimeProfile(runtimeProfile)
  if (!selected || !runtime || selected !== runtime) return null
  return knownProfileNames(profiles).includes(selected) ? selected : null
}

function liveSessionsForProfile(rows, selectedProfile, runtimeProfile, profiles) {
  const exactProfile = exactRuntimeProfile(selectedProfile, runtimeProfile, profiles)
  if (!exactProfile || !Array.isArray(rows)) return []
  return rows
    .filter(row => (
      row &&
      typeof row.id === 'string' &&
      typeof row.session_key === 'string' &&
      (!row.profile || normalizeRuntimeProfile(row.profile) === exactProfile)
    ))
    .map(row => ({
      id: row.id,
      session_key: row.session_key,
      title: String(row.title || 'Untitled live session').slice(0, 120),
      status: String(row.status || 'unavailable'),
      started_at: Number(row.started_at) || null,
      last_active: Number(row.last_active) || null,
      subagent_id: typeof row.subagent_id === 'string' ? row.subagent_id : null,
      kanban_task_id: typeof row.kanban_task_id === 'string' ? row.kanban_task_id : null
    }))
}

function rebindCandidateForRun(rows, attachedRun, selectedProfile, runtimeProfile, profiles) {
  const exactProfile = exactRuntimeProfile(selectedProfile, runtimeProfile, profiles)
  if (!Array.isArray(rows) || !attachedRun || !exactProfile) return null
  if (normalizeRuntimeProfile(attachedRun.profile) !== exactProfile) return null
  if (
    typeof attachedRun.session_id !== 'string' ||
    !attachedRun.session_id ||
    normalizeRuntimeProfile(attachedRun.runtime_profile) !== exactProfile ||
    typeof attachedRun.runtime_session_id !== 'string' ||
    !attachedRun.runtime_session_id
  ) return null
  return rows.find(row => (
    row &&
    typeof row.id === 'string' &&
    typeof row.session_key === 'string' &&
    row.id !== attachedRun.runtime_session_id &&
    row.session_key === attachedRun.session_id
  )) || null
}

function buildRebindPayload(attachedRun, selectedProfile, runtimeProfile, candidate, profiles) {
  const exactProfile = exactRuntimeProfile(selectedProfile, runtimeProfile, profiles)
  if (!exactProfile || !candidate || typeof candidate.id !== 'string' || !candidate.id) return null
  if (
    normalizeRuntimeProfile(attachedRun?.profile) !== exactProfile ||
    typeof attachedRun?.session_id !== 'string' ||
    !attachedRun.session_id ||
    normalizeRuntimeProfile(attachedRun.runtime_profile) !== exactProfile ||
    typeof attachedRun.runtime_session_id !== 'string' ||
    !attachedRun.runtime_session_id ||
    candidate.session_key !== attachedRun.session_id ||
    candidate.id === attachedRun.runtime_session_id
  ) return null
  return {
    profile: exactProfile,
    session_id: attachedRun.session_id,
    old_runtime_profile: exactProfile,
    old_runtime_session_id: attachedRun.runtime_session_id,
    runtime_profile: exactProfile,
    runtime_session_id: candidate.id,
    permission_scope: 'inherit-only'
  }
}

function receiptLabel(state) {
  return ({
    queued: 'Queued',
    dispatching: 'Dispatching',
    accepted: 'Accepted by Hermes',
    delivered: 'Delivered',
    applied: 'Applied',
    rejected: 'Rejected',
    failed: 'Failed',
    superseded: 'Superseded'
  })[state] || 'Unverified'
}
// STATE_HELPERS_END

function makeRequestId() {
  return globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(36).slice(2)}`
}

function readImageAsDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onerror = () => reject(new Error('The selected image could not be read.'))
    reader.onload = () => {
      const dataUrl = String(reader.result || '')
      if (!dataUrl.startsWith('data:image/')) reject(new Error('The selected file is not a readable image.'))
      else resolve(dataUrl)
    }
    reader.readAsDataURL(file)
  })
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

/*
 * Solving working orb adapted from the 20 px `solving` painter in
 * thinking-orbs 0.2.0 by Jakub Antalik.
 * Source: https://github.com/JakubAntalik/thinking-orbs
 * Copyright (c) 2026 Jakub Antalik
 *
 * Permission is hereby granted, free of charge, to any person obtaining a copy
 * of this software and associated documentation files (the "Software"), to deal
 * in the Software without restriction, including without limitation the rights
 * to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
 * copies of the Software, and to permit persons to whom the Software is
 * furnished to do so, subject to inclusion of this copyright and permission notice.
 * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
 * IMPLIED, INCLUDING BUT NOT LIMITED TO MERCHANTABILITY AND FITNESS FOR A
 * PARTICULAR PURPOSE. See THIRD_PARTY_NOTICES.md for the complete MIT license.
 *
 * The 20 px solving preset is intentionally tuned, not scaled down from 64 px.
 * Agent Dock preserves the upstream monochrome direction with one cyan hue.
 * The dark/light variants change contrast only; they never create multicolor faces.
 */
const RUBIK_SOLVING_20_PRESET = Object.freeze({
  latRings: 4,
  lonDensity: 12,
  moveCount: 14,
  speed: 1.95,
  rBase: 0.6 * 1.9,
  rDepth: 1.7 * 1.9,
  rActive: 0.3 * 1.9,
  rsPow: 0.6,
  rMin: 0.3,
  radius: 0.82
})
const SOLVING_DARK_COLOR = '#38bdf8'
const SOLVING_LIGHT_COLOR = '#0369a1'

function rubikHash(a, b) {
  const value = Math.sin(a * 12.9898 + b * 78.233) * 43758.5453
  return value - Math.floor(value)
}

function rubikProject(yaw, tilt, cx, cy, radius) {
  const sinTilt = Math.sin(tilt)
  const cosTilt = Math.cos(tilt)
  const sinYaw = Math.sin(yaw)
  const cosYaw = Math.cos(yaw)
  return (x, y, z) => {
    const x1 = x * cosYaw + z * sinYaw
    const z1 = -x * sinYaw + z * cosYaw
    const y1 = y * cosTilt - z1 * sinTilt
    const z2 = y * sinTilt + z1 * cosTilt
    return [cx + x1 * radius, cy - y1 * radius, z2]
  }
}

function rubikMoveAmounts(time, moveCount) {
  const moveDuration = 0.42
  const pauseDuration = 1.2
  const period = 2 * moveCount * moveDuration + pauseDuration
  const phase = time % period
  const amount = new Array(moveCount).fill(0)
  let active = -1
  if (phase < 2 * moveCount * moveDuration) {
    const move = Math.floor(phase / moveDuration)
    const progress = (phase - move * moveDuration) / moveDuration
    const eased = 1 - (1 - Math.min(1, progress / 0.7)) ** 3
    if (move < moveCount) {
      for (let index = 0; index < move; index += 1) amount[index] = 1
      amount[move] = eased
      active = move
    } else {
      const reverseMove = 2 * moveCount - 1 - move
      for (let index = 0; index < reverseMove; index += 1) amount[index] = 1
      amount[reverseMove] = 1 - eased
      active = reverseMove
    }
  }
  return { amount, active }
}

function rubikMoveSchedule(moveCount) {
  const moves = []
  for (let index = 0; index < moveCount; index += 1) {
    const axis = Math.min(2, Math.floor(rubikHash(index, 2.3) * 3))
    const low = -1 + 0.5 * Math.min(3, Math.floor(rubikHash(index, 5.9) * 4))
    const direction = rubikHash(index, 7.7) < 0.5 ? 1 : -1
    moves.push({ axis, lo: low, hi: low + 0.5, ang: direction * Math.PI / 2 })
  }
  return moves
}

function rubikApplyMoves(point, moves, state) {
  let [x, y, z] = point
  let active = false
  for (let index = 0; index < moves.length; index += 1) {
    if (state.amount[index] <= 0) continue
    const move = moves[index]
    const coordinate = move.axis === 0 ? x : move.axis === 1 ? y : z
    if (coordinate < move.lo || coordinate >= move.hi) continue
    if (index === state.active) active = true
    const angle = move.ang * state.amount[index]
    const cos = Math.cos(angle)
    const sin = Math.sin(angle)
    if (move.axis === 0) {
      const nextY = y * cos - z * sin
      z = y * sin + z * cos
      y = nextY
    } else if (move.axis === 1) {
      const nextX = x * cos + z * sin
      z = -x * sin + z * cos
      x = nextX
    } else {
      const nextX = x * cos - y * sin
      y = x * sin + y * cos
      x = nextX
    }
  }
  return [x, y, z, active]
}


function rubikCssChannel(value) {
  const text = String(value ?? '').trim()
  const number = Number.parseFloat(text)
  return text.endsWith('%') ? number * 2.55 : number
}

function rubikTextColorIsLight(color) {
  const text = String(color ?? '').trim().toLowerCase()
  let channels = null
  if (text.startsWith('#')) {
    const hex = text.slice(1)
    const expanded = hex.length === 3 || hex.length === 4
      ? [...hex].map(channel => `${channel}${channel}`).join('')
      : hex
    if (/^[0-9a-f]{6,8}$/.test(expanded)) {
      channels = [
        Number.parseInt(expanded.slice(0, 2), 16),
        Number.parseInt(expanded.slice(2, 4), 16),
        Number.parseInt(expanded.slice(4, 6), 16)
      ]
    }
  } else {
    const match = text.match(/^rgba?\(([^)]+)\)$/)
    if (match) channels = match[1].split(/[,\s/]+/).filter(Boolean).slice(0, 3).map(rubikCssChannel)
  }
  if (!channels || channels.length !== 3 || channels.some(channel => !Number.isFinite(channel))) return false
  return (0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]) / 255 >= 0.58
}

function solvingColorForCanvas(canvas) {
  const textColor = getComputedStyle(canvas).color
  return rubikTextColorIsLight(textColor) ? SOLVING_DARK_COLOR : SOLVING_LIGHT_COLOR
}

function drawSolvingWorkingOrb(ctx, size, time) {
  const preset = RUBIK_SOLVING_20_PRESET
  const radius = (size / 2) * preset.radius
  const tilt = 0.35 + 0.1 * Math.sin(time * 0.9)
  const project = rubikProject(time * 0.55, tilt, size / 2, size / 2, radius)
  const radiusScale = (size / 300) ** preset.rsPow
  const moves = rubikMoveSchedule(preset.moveCount)
  const moveState = rubikMoveAmounts(time, preset.moveCount)
  const color = solvingColorForCanvas(ctx.canvas)
  const dots = []

  for (let ring = 0; ring <= preset.latRings; ring += 1) {
    const latitude = -Math.PI / 2 + ring / preset.latRings * Math.PI
    const ringRadius = Math.cos(latitude)
    const height = Math.sin(latitude)
    const longitudeCount = Math.max(1, Math.round(Math.abs(ringRadius) * preset.lonDensity))
    for (let longitudeIndex = 0; longitudeIndex < longitudeCount; longitudeIndex += 1) {
      const longitude = longitudeIndex / longitudeCount * 2 * Math.PI
      const originalX = ringRadius * Math.cos(longitude)
      const originalY = height
      const originalZ = ringRadius * Math.sin(longitude)
      const [movedX, movedY, movedZ, active] = rubikApplyMoves(
        [originalX, originalY, originalZ],
        moves,
        moveState
      )
      const [x, y, z] = project(movedX, movedY, movedZ)
      const depth = (z + 1) / 2
      dots.push({
        x,
        y,
        z,
        radius: (preset.rBase + preset.rDepth * depth + (active ? preset.rActive : 0)) * radiusScale,
        alpha: Math.min(1, 0.28 + 0.72 * depth + (active ? 0.08 : 0)),
        color
      })
    }
  }

  dots.sort((a, b) => a.z - b.z)
  for (const dot of dots) {
    ctx.globalAlpha = dot.alpha
    ctx.fillStyle = dot.color
    ctx.beginPath()
    ctx.arc(dot.x, dot.y, Math.max(preset.rMin, dot.radius), 0, Math.PI * 2)
    ctx.fill()
  }
  ctx.globalAlpha = 1
}

function SolvingWorkingOrb({ label = 'Agent working' }) {
  const canvasRef = useRef(null)
  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return undefined
    const size = 20
    const dpr = Math.min(2, window.devicePixelRatio || 1)
    canvas.width = Math.round(size * dpr)
    canvas.height = Math.round(size * dpr)
    const context = canvas.getContext('2d')
    if (!context) return undefined

    const paintFrame = seconds => {
      context.setTransform(dpr, 0, 0, dpr, 0, 0)
      context.clearRect(0, 0, size, size)
      drawSolvingWorkingOrb(context, size, seconds * RUBIK_SOLVING_20_PRESET.speed)
    }
    if (window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) {
      paintFrame(0.6)
      return undefined
    }

    let animationFrame = 0
    let running = false
    let visible = true
    const stop = () => {
      running = false
      cancelAnimationFrame(animationFrame)
    }
    const loop = () => {
      paintFrame(performance.now() / 1000)
      if (running) animationFrame = requestAnimationFrame(loop)
    }
    const start = () => {
      if (running) return
      running = true
      animationFrame = requestAnimationFrame(loop)
    }
    const onVisibility = () => {
      if (document.visibilityState === 'hidden') stop()
      else if (visible) start()
    }
    const observer = typeof IntersectionObserver === 'undefined'
      ? null
      : new IntersectionObserver(([entry]) => {
          visible = entry.isIntersecting
          if (visible && document.visibilityState !== 'hidden') start()
          else stop()
        })

    paintFrame(performance.now() / 1000)
    observer?.observe(canvas)
    document.addEventListener('visibilitychange', onVisibility)
    if (!observer) start()
    return () => {
      stop()
      observer?.disconnect()
      document.removeEventListener('visibilitychange', onVisibility)
    }
  }, [])

  return jsx('canvas', {
    'aria-label': label,
    className: 'block size-5 shrink-0 text-(--ui-text-secondary)',
    'data-agent-dock-working-orb': 'true',
    ref: canvasRef,
    role: 'img',
    style: { height: 20, width: 20 }
  })
}

function MessageBubble({ message }) {
  const user = message.role === 'user'
  const assistant = message.role === 'assistant'
  const timestamp = formatMessageTimestamp(message.created_at)
  const attachments = Array.isArray(message.attachments)
    ? message.attachments.filter(item => item && typeof item === 'object')
    : []
  const attachmentNames = attachments.map(item => attachmentDisplayName(item.name))
  const attachmentCount = attachments.length
  const copyMessage = async () => {
    const result = await copyTextToClipboard(message.text)
    if (result === 'unavailable') {
      host.notify({ kind: 'error', message: 'Copy is unavailable in this Hermes surface.' })
      return
    }
    if (result === 'copied') {
      host.notify({ kind: 'success', message: 'Assistant message copied.' })
    } else {
      host.notify({ kind: 'error', message: 'Could not copy assistant message.' })
    }
  }
  return jsxs('div', {
    className: cn('flex items-end gap-1.5', user ? 'justify-end' : 'justify-start'),
    children: [
      assistant ? jsx(ProfileAvatar, { profile: message.profile, size: 'sm' }) : null,
      jsxs('div', {
      className: cn(
        'max-w-[88%] rounded-xl px-3 py-1.5 text-[0.75rem] leading-relaxed whitespace-pre-wrap wrap-anywhere',
        user
          ? 'bg-[color-mix(in_srgb,var(--ui-accent)_18%,var(--ui-bg-elevated))] text-(--ui-text-primary)'
          : message.error
            ? 'border border-(--ui-danger,var(--ui-stroke-secondary)) bg-(--ui-bg-elevated) text-(--ui-text-secondary)'
            : 'border border-(--ui-stroke-secondary) bg-(--ui-bg-secondary) text-(--ui-text-primary)'
      ),
      children: [
        jsx('p', { children: message.text }),
        attachmentCount
          ? jsxs('div', {
              'aria-label': `${attachmentCount} image${attachmentCount === 1 ? '' : 's'} attached: ${attachmentNames.join(', ')}`,
              className: 'mt-1.5 flex min-w-0 flex-wrap items-center gap-1',
              'data-agent-dock-attachment-row': 'true',
              children: [
                jsxs('span', {
                  className: 'inline-flex shrink-0 items-center gap-1 text-[0.61rem] font-medium text-(--ui-text-tertiary)',
                  children: [
                    jsx(Codicon, { 'aria-hidden': true, name: 'file-media', size: '0.68rem' }),
                    jsx('span', { children: `${attachmentCount} image${attachmentCount === 1 ? '' : 's'}` })
                  ]
                }),
                jsx('div', {
                  className: 'contents',
                  role: 'list',
                  children: attachments.map((item, index) => jsxs('span', {
                    className: 'inline-flex min-w-0 max-w-28 items-center gap-1 rounded-md border border-(--ui-stroke-secondary) bg-(--ui-bg-secondary) px-1.5 py-0.5 text-[0.61rem] text-(--ui-text-secondary)',
                    role: 'listitem',
                    title: attachmentNames[index],
                    children: [
                      jsx(Codicon, { 'aria-hidden': true, name: 'file-media', size: '0.65rem' }),
                      jsx('span', { className: 'min-w-0 truncate', children: attachmentNames[index] })
                    ]
                  }, `${message.id}:attachment:${index}`))
                })
              ]
            })
          : null,
        message.receipt_state
          ? jsx('p', {
              className: 'mt-1 text-[0.6rem] font-medium uppercase tracking-[0.08em] text-(--ui-accent)',
              children: `${message.intervention ? message.intervention.toUpperCase() : 'CONTROL'} · ${receiptLabel(message.receipt_state)}`
            })
          : null,
        jsxs('div', {
          'aria-label': `${user
            ? message.assignment ? 'You, task assignment' : 'You'
            : profileDisplayLabel(message.profile)}, ${timestamp}`,
          className: 'mt-0.5 flex h-4 items-center gap-1 text-[0.6rem] leading-none text-(--ui-text-tertiary)',
          'data-agent-dock-message-footer': 'true',
          children: [
            jsx('span', {
              className: 'min-w-0 flex-1 truncate tracking-[0.04em]',
              title: user
                ? message.assignment ? 'You · Task assignment' : 'You'
                : profileDisplayLabel(message.profile),
              children: user
                ? message.assignment ? 'You · Task assignment' : 'You'
                : profileDisplayLabel(message.profile)
            }),
            jsx('span', {
              className: 'shrink-0 tabular-nums tracking-[0.04em]',
              children: timestamp
            }),
            assistant
              ? jsx('button', {
                  'aria-label': 'Copy assistant message',
                  className: 'inline-flex size-4 shrink-0 items-center justify-center rounded text-(--ui-text-tertiary) hover:bg-(--ui-control-hover-background) hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-(--ui-accent)',
                  'data-agent-dock-copy': 'true',
                  onClick: () => void copyMessage(),
                  title: 'Copy assistant message',
                  type: 'button',
                  children: jsx(Codicon, { 'aria-hidden': true, name: 'copy', size: '0.68rem' })
                })
              : null
          ]
        })
      ]
      })
    ]
  })
}

function ProfileAvatar({ profile, active = false, size = 'md', label }) {
  const display = profileDisplayLabel(profile) || 'Agent'
  const dimensions = size === 'sm' ? 'size-5 text-[0.6rem]' : 'size-7 text-[0.64rem]'
  return jsxs('span', {
    'aria-label': label || `${display} avatar${active ? ', working' : ''}`,
    className: cn(
      'relative inline-grid shrink-0 place-items-center rounded-lg border font-semibold tracking-[-0.02em]',
      dimensions,
      active
        ? 'border-(--ui-accent) bg-[color-mix(in_srgb,var(--ui-accent)_16%,var(--ui-bg-secondary))] text-(--ui-accent)'
        : 'border-(--ui-stroke-secondary) bg-(--ui-bg-secondary) text-(--ui-text-secondary)'
    ),
    role: 'img',
    title: display,
    children: [
      jsx('span', { 'aria-hidden': true, children: profileAvatarInitials(profile) }),
      active
        ? jsx('span', {
            'aria-hidden': true,
            className: 'absolute -bottom-0.5 -right-0.5 size-2 rounded-full border border-(--ui-surface-background) bg-(--ui-accent)'
          })
        : null
    ]
  })
}

function AgentDock({ mode = DEFAULT_DOCK_MODE, onToggleMode }) {
  const runtimeProfile = normalizeRuntimeProfile(useValue(host.state.profile))
  const foregroundRuntimeSessionId = String(useValue(host.state.activeSessionId) ?? '').trim()
  const profilesQuery = useQuery({ queryKey: [ID, 'profiles'], queryFn: () => rest('/profiles'), refetchInterval: 20_000 })
  const achievementsQuery = useQuery({
    queryKey: [ID, 'achievements'],
    queryFn: () => rest('/achievements'),
    refetchInterval: 30_000
  })
  const [selected, setSelected] = useState(() => storage.get('selected-profile', ''))
  const [histories, setHistories] = useState(() => storage.get('histories', {}))
  const [sessions, setSessions] = useState(() => storage.get('sessions', {}))
  const [selectedModels, setSelectedModels] = useState(() => {
    const migrated = migrateSavedModelSelections(
      storage.get('selected-models', {}),
      storage.get('selected-providers', {})
    )
    storage.set('selected-models', migrated)
    return migrated
  })
  const [selectedEfforts, setSelectedEfforts] = useState(() => storage.get('selected-efforts', {}))
  const [thinkingByProfile, setThinkingByProfile] = useState(() => storage.get('thinking', {}))
  const [fastByProfile, setFastByProfile] = useState(() => storage.get('fast', {}))
  const [assignTask, setAssignTask] = useState(false)
  const [interventionKind, setInterventionKind] = useState('ask')
  const [attachedRunIds, setAttachedRunIds] = useState(() => storage.get('attached-control-runs', {}))
  const [boundRuntimeSessionIds, setBoundRuntimeSessionIds] = useState({})
  const [candidateSessionIds, setCandidateSessionIds] = useState({})
  const [pendingConfirmedAction, setPendingConfirmedAction] = useState(null)
  const [drafts, setDrafts] = useState(() => storage.get('drafts', {}))
  const [attachmentsByProfile, setAttachmentsByProfile] = useState({})
  const [activeJobs, setActiveJobs] = useState(() => {
    const stored = storage.get('active-jobs', {})
    const reconciled = pruneExpiredStartingJobs(stored)
    if (reconciled !== stored) storage.set('active-jobs', reconciled)
    return reconciled
  })
  const [subagentsByProfile, setSubagentsByProfile] = useState(() => storage.get('subagent-runs', {}))
  const [expandedSubagents, setExpandedSubagents] = useState({})
  const [muted, setMuted] = useState(() => storage.get('muted', false))
  const [achievementToast, setAchievementToast] = useState(null)
  const [modelMenuPanel, setModelMenuPanel] = useState('advanced')
  const scrollRef = useRef(null)
  const imageInputRef = useRef(null)
  const notifiedJobIds = useRef(new Set())
  const activeJobsRef = useRef(activeJobs)

  const profiles = profilesQuery.data?.profiles || []
  const supportsIdempotentSubmit = profilesQuery.data?.capabilities?.idempotent_submit !== false
  const supportsModelCatalog = profilesQuery.data?.capabilities?.model_catalog === true
  const supportsImageUpload = profilesQuery.data?.capabilities?.image_upload === true
  const supportsReasoning = profilesQuery.data?.capabilities?.reasoning === true
  const supportsFast = profilesQuery.data?.capabilities?.fast === true
  const supportsKanbanAssignment = profilesQuery.data?.capabilities?.kanban_assignment === true
  const currentProfile = profiles.find(profile => profile.name === selected) || profiles[0] || null
  const currentName = currentProfile?.name || selected
  const activeSessionsQuery = useQuery({
    queryKey: [ID, 'active-sessions', runtimeProfile, foregroundRuntimeSessionId],
    queryFn: () => host.request('session.active_list', { current_session_id: foregroundRuntimeSessionId || undefined }),
    refetchInterval: 2_500
  })
  const liveSessions = useMemo(
    () => liveSessionsForProfile(activeSessionsQuery.data?.sessions, currentName, runtimeProfile, profiles),
    [activeSessionsQuery.data, currentName, runtimeProfile, profiles]
  )
  const controlRunsQuery = useQuery({
    queryKey: [ID, 'control-runs', currentName],
    queryFn: () => rest(`/control/runs?profile=${encodeURIComponent(currentName)}`),
    enabled: Boolean(currentName),
    refetchInterval: 2_500
  })
  const attachedRunId = attachedRunIds[currentName] || ''
  const attachedRun = (controlRunsQuery.data?.runs || []).find(run => run.run_id === attachedRunId) || null
  const attachedRuntimeSessionId = boundRuntimeSessionIds[attachedRunId] || attachedRun?.runtime_session_id || ''
  const attachedControlRun = attachedRun && attachedRuntimeSessionId
    ? { ...attachedRun, runtime_session_id: attachedRuntimeSessionId }
    : attachedRun
  const attachedLiveSession = liveSessions.find(session => session.id === attachedRuntimeSessionId) || null
  const candidateRebindSession = rebindCandidateForRun(liveSessions, attachedRun, currentName, runtimeProfile, profiles)
  const candidateSessionId = candidateSessionIds[currentName] || liveSessions[0]?.id || ''
  const candidateLiveSession = liveSessions.find(session => session.id === candidateSessionId) || liveSessions[0] || null
  const controlHistoryQuery = useQuery({
    queryKey: [ID, 'control-history', attachedRunId, currentName, attachedRun?.session_id],
    queryFn: () => rest(
      `/control/runs/${encodeURIComponent(attachedRunId)}?profile=${encodeURIComponent(currentName)}&session_id=${encodeURIComponent(attachedRun.session_id)}`
    ),
    enabled: Boolean(attachedRunId && currentName && attachedRun?.session_id),
    refetchInterval: 2_500
  })
  const verificationQuery = useQuery({
    queryKey: [ID, 'verification', attachedRun?.session_id],
    queryFn: () => host.request('verification.status', { session_id: attachedRun.session_id }),
    enabled: Boolean(attachedRun?.session_id),
    refetchInterval: 5_000
  })
  const modelsQuery = useQuery({
    queryKey: [ID, 'models', currentName],
    queryFn: () => rest(`/models/${currentName}`),
    enabled: Boolean(currentName),
    refetchInterval: 60_000
  })
  const modelPayload = modelsQuery.data || null
  const modelOptions = useMemo(() => flattenModelOptions(modelPayload), [modelPayload])
  const savedModelSelection = selectedModels[currentName]
  const configuredProvider = modelPayload?.provider || currentProfile?.provider || ''
  const configuredModel = modelPayload?.model || currentProfile?.model || ''
  const savedModelOption = modelOptions.find(option => (
    option.model === savedModelSelection?.model && (
      !savedModelSelection?.provider || option.provider === savedModelSelection.provider
    )
  ))
  const configuredModelOption = modelOptions.find(option => (
    option.provider === configuredProvider && option.model === configuredModel
  ))
  const effectiveModelOption = savedModelOption || configuredModelOption || modelOptions[0] || null
  const effectiveModel = effectiveModelOption?.model || configuredModel
  const effectiveProvider = effectiveModelOption?.provider || configuredProvider
  const selectedModelKey = effectiveModelOption?.key || ''
  const capabilities = selectedModelCapabilities(modelPayload, effectiveProvider, effectiveModel)
  const selectedModelPresentation = modelPresentation(effectiveModel, capabilities)
  const compactSelectedModelLabel = compactModelLabel(selectedModelPresentation.label)
  const thinkingPreference = thinkingByProfile[currentName] !== false
  const effectiveThinking = thinkingPreference && capabilities.reasoning
  const effort = normalizeReasoningEffort(selectedEfforts[currentName] || 'medium')
  const reasoningEffortLevel = REASONING_SLIDER_VALUES[reasoningEffortSliderPosition(effort)]
  const reasoningEffortLabel = WORKLOAD_TIER_LABELS[reasoningEffortLevel]
  const fastPreference = fastByProfile[currentName] === true
  const effectiveFast = fastPreference && capabilities.fast
  const modelControlsReady = supportsModelCatalog && !modelsQuery.isPending && !modelsQuery.isError && modelOptions.length > 0
  const localMessages = histories[currentName] || []
  const durableControlMessages = (controlHistoryQuery.data?.messages || []).map(message => ({
    id: `durable:${message.message_id}`,
    role: 'user',
    profile: currentName,
    text: message.body,
    intervention: message.kind,
    receipt_state: message.state,
    created_at: message.created_at
  }))
  const messages = attachedRun ? durableControlMessages : localMessages
  const draft = drafts[currentName] || ''
  const attachments = attachmentsByProfile[currentName] || []
  const activeJob = activeJobs[currentName] || null
  const subagentSnapshot = subagentsByProfile[currentName] || null
  const visibleSubagents = activeJob && activeJob.id !== subagentSnapshot?.job_id
    ? []
    : normalizeSubagents(subagentSnapshot?.subagents)
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
    setModelMenuPanel('advanced')
  }, [currentName])

  useEffect(() => {
    if (!attachedRun || !attachedLiveSession) return
    void rest(`/control/runs/${encodeURIComponent(attachedRun.run_id)}/observations`, {
      method: 'POST',
      body: {
        profile: currentName,
        session_id: attachedRun.session_id,
        status: attachedLiveSession.status,
        heartbeat_at: attachedLiveSession.last_active,
        detail: { source: 'session.active_list' }
      }
    }).catch(() => undefined)
  }, [attachedRun?.run_id, attachedLiveSession?.status, attachedLiveSession?.last_active, currentName])

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
        setSubagentsByProfile(current => {
          const next = updateProfileSubagents(current, profile, job)
          if (next !== current) storage.set('subagent-runs', next)
          return next
        })
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

  const rememberAttachedRun = (profile, runId) => {
    setAttachedRunIds(current => {
      const next = { ...current }
      if (runId) next[profile] = runId
      else delete next[profile]
      storage.set('attached-control-runs', next)
      return next
    })
  }

  const attachLiveSession = async session => {
    const requestId = makeRequestId()
    const run = await rest('/control/runs', {
      method: 'POST',
      body: {
        request_id: requestId,
        profile: currentName,
        runtime_profile: runtimeProfile,
        runtime_session_id: session.id,
        session_id: session.session_key,
        subagent_id: session.subagent_id,
        kanban_task_id: session.kanban_task_id,
        title: session.title,
        status: session.status,
        objective: 'Continue the existing Hermes run without changing its authority.',
        permission_scope: 'inherit-only'
      }
    })
    rememberAttachedRun(currentName, run.run_id)
    await controlRunsQuery.refetch?.()
    haptic('success')
    host.notify({ kind: 'success', message: `Attached to ${session.title || 'live Hermes run'}` })
  }

  const reattachLiveSession = async () => {
    const payload = buildRebindPayload(attachedRun, currentName, runtimeProfile, candidateRebindSession, profiles)
    if (attachedLiveSession || !payload || !candidateRebindSession) {
      host.notify({ kind: 'error', message: 'Reattach failed: no exact replacement Hermes runtime is available for this run.' })
      return false
    }
    let rebound
    try {
      rebound = await rest(`/control/runs/${encodeURIComponent(attachedRun.run_id)}/rebind`, {
        method: 'POST',
        body: payload
      })
    } catch (error) {
      const detail = String(error?.message || error || 'Hermes did not confirm the rebind.').trim().slice(0, 240)
      host.notify({ kind: 'error', message: `Reattach failed: ${detail}` })
      return false
    }
    const reboundRuntimeProfile = normalizeRuntimeProfile(rebound?.runtime_profile)
    const reboundRuntimeSessionId = typeof rebound?.runtime_session_id === 'string'
      ? rebound.runtime_session_id.trim()
      : ''
    if (reboundRuntimeProfile !== runtimeProfile || reboundRuntimeSessionId !== candidateRebindSession.id) {
      host.notify({ kind: 'error', message: 'Reattach failed: Hermes did not confirm the exact replacement runtime.' })
      return false
    }
    setBoundRuntimeSessionIds(current => ({ ...current, [attachedRun.run_id]: reboundRuntimeSessionId }))
    try {
      await Promise.all([
        controlRunsQuery.refetch?.(),
        activeSessionsQuery.refetch?.(),
        controlHistoryQuery.refetch?.()
      ])
    } catch (error) {
      const detail = String(error?.message || error || 'the refreshed runtime state is unavailable.').trim().slice(0, 200)
      host.notify({ kind: 'error', message: `Reattached live to ${candidateRebindSession.title || 'Hermes run'}, but refresh failed: ${detail}` })
      return true
    }
    haptic('success')
    host.notify({ kind: 'success', message: `Reattached live to ${candidateRebindSession.title || 'Hermes run'}` })
    return true
  }

  const claimControlMessage = messageId => rest(`/control/messages/${encodeURIComponent(messageId)}/claim`, {
    method: 'POST',
    body: {
      dispatcher_id: `desktop:${runtimeProfile}`,
      profile: currentName,
      session_id: attachedRun.session_id,
      runtime_profile: runtimeProfile,
      runtime_session_id: attachedControlRun.runtime_session_id,
      lease_seconds: 300
    }
  })

  const recordControlReceipt = async (messageId, state, detail = {}, dispatchToken = null) => {
    const submit = token => rest(`/control/messages/${encodeURIComponent(messageId)}/receipts`, {
      method: 'POST',
      body: {
        receipt_id: `${messageId}:${state}:hermes-gateway`,
        state,
        source: 'hermes-gateway',
        verification: state === 'unknown' ? 'unverified' : 'observed',
        profile: currentName,
        session_id: attachedRun.session_id,
        runtime_profile: runtimeProfile,
        runtime_session_id: attachedControlRun.runtime_session_id,
        dispatch_token: token,
        detail
      }
    })
    try {
      return await submit(dispatchToken)
    } catch {
      // The stable receipt ID makes this safe when the first response was lost.
      try {
        return await submit(dispatchToken)
      } catch {
        const renewed = await claimControlMessage(messageId)
        return submit(renewed.dispatch_token)
      }
    }
  }

  const dispatchControlMessage = async (kind, text, confirmed = false) => {
    if (!attachedRun || !attachedLiveSession) throw new Error('The attached Hermes run is no longer live.')
    const normalizedKind = normalizeInterventionKind(kind)
    if (interventionNeedsConfirmation(normalizedKind) && !confirmed) {
      setPendingConfirmedAction({ kind: normalizedKind, text, type: 'redirect' })
      return false
    }
    if (normalizedKind === 'ask' && attachedLiveSession.status !== 'idle') {
      throw new Error('ASK waits for an idle run. Use NUDGE to deliver guidance at the next safe tool-result boundary.')
    }

    const messageId = makeRequestId()
    await rest('/control/messages', {
      method: 'POST',
      body: {
        message_id: messageId,
        run_id: attachedRun.run_id,
        profile: currentName,
        session_id: attachedRun.session_id,
        kind: normalizedKind,
        body: text,
        confirmed,
        permission_scope: 'inherit-only'
      }
    })
    const claim = await claimControlMessage(messageId)
    if (claim.state !== 'dispatching') return false

    let terminalReceiptRecorded = false
    try {
      const result = await host.request(interventionMethod(normalizedKind), {
        session_id: attachedControlRun.runtime_session_id,
        text
      })
      const gatewayStatus = String(result?.status || 'accepted')
      if (gatewayStatus === 'rejected') {
        await recordControlReceipt(messageId, 'rejected', {
          method: interventionMethod(normalizedKind),
          gateway_status: gatewayStatus
        }, claim.dispatch_token)
        terminalReceiptRecorded = true
        throw new Error('Hermes rejected this intervention')
      }
      await recordControlReceipt(messageId, 'accepted', {
        method: interventionMethod(normalizedKind),
        gateway_status: gatewayStatus
      }, claim.dispatch_token)
      await controlHistoryQuery.refetch?.()
      append(currentName, {
        id: `control:${messageId}`,
        role: 'user',
        profile: currentName,
        text,
        intervention: normalizedKind,
        receipt_state: 'accepted'
      })
      return true
    } catch (error) {
      if (!terminalReceiptRecorded) {
        await recordControlReceipt(messageId, 'unknown', {
          method: interventionMethod(normalizedKind),
          error: String(error?.message || error).slice(0, 240)
        }, claim.dispatch_token)
      }
      throw error
    }
  }

  const stopAttachedRun = async confirmed => {
    if (!attachedRun || !attachedLiveSession) return
    if (!confirmed) {
      setPendingConfirmedAction({ type: 'stop' })
      return
    }
    const messageId = makeRequestId()
    await rest('/control/messages', {
      method: 'POST',
      body: {
        message_id: messageId,
        run_id: attachedRun.run_id,
        profile: currentName,
        session_id: attachedRun.session_id,
        kind: 'stop',
        body: 'Stop this live Hermes run.',
        confirmed: true,
        permission_scope: 'inherit-only'
      }
    })
    const claim = await claimControlMessage(messageId)
    try {
      const result = await host.request('session.interrupt', { session_id: attachedControlRun.runtime_session_id })
      const gatewayStatus = String(result?.status || 'accepted')
      const receiptState = gatewayStatus === 'rejected' ? 'rejected' : 'accepted'
      await recordControlReceipt(messageId, receiptState, {
        method: 'session.interrupt',
        gateway_status: gatewayStatus
      }, claim.dispatch_token)
      if (receiptState === 'rejected') throw new Error('Hermes rejected this stop request')
    } catch (error) {
      if (String(error?.message || '') !== 'Hermes rejected this stop request') {
        await recordControlReceipt(messageId, 'unknown', {
          method: 'session.interrupt',
          error: String(error?.message || error).slice(0, 240)
        }, claim.dispatch_token)
      }
      throw error
    }
    host.notify({ kind: 'success', message: 'Stop requested. Queued prompts and pending approvals for this session are cleared by Hermes.' })
    await controlHistoryQuery.refetch?.()
    await activeSessionsQuery.refetch?.()
  }

  const selectModel = key => {
    if (!currentName || activeJobsRef.current[currentName]) return
    const option = modelOptions.find(candidate => candidate.key === key)
    if (!option) return
    setSelectedModels(current => {
      const next = { ...current, [currentName]: { provider: option.provider, model: option.model } }
      storage.set('selected-models', next)
      return next
    })
    haptic('tap')
  }

  const ingestImages = async (files, profile = currentName) => {
    if (!profile || !files.length || activeJobsRef.current[profile]) return
    const existing = attachmentsByProfile[profile] || []
    const accepted = []
    for (const file of files) {
      const error = validateImageFileMetadata(file, existing.length + accepted.length)
      if (error) {
        host.notify({ kind: 'error', message: error })
        continue
      }
      try {
        accepted.push({
          id: makeRequestId(),
          name: String(file.name || 'image').slice(0, 180),
          mime_type: String(file.type || '').toLowerCase(),
          size: file.size,
          data_url: await readImageAsDataUrl(file)
        })
      } catch (error) {
        host.notify({ kind: 'error', message: error?.message || String(error) })
      }
    }
    if (!accepted.length) return
    setAttachmentsByProfile(current => ({
      ...current,
      [profile]: [...(current[profile] || []), ...accepted].slice(0, MAX_IMAGE_ATTACHMENTS)
    }))
  }

  const selectImages = event => {
    const files = Array.from(event.target.files || [])
    event.target.value = ''
    void ingestImages(files, currentName)
  }

  const pasteImages = event => {
    const profile = currentName
    if (!profile || activeJobsRef.current[profile] || !supportsImageUpload) return
    const files = extractClipboardImageFiles(event.clipboardData)
    if (!files.length) return

    const existingCount = (attachmentsByProfile[profile] || []).length
    if (shouldConsumeClipboardPaste(event.clipboardData, files, existingCount)) event.preventDefault()
    void ingestImages(files, profile)
  }

  const removeImage = imageId => {
    if (!currentName) return
    setAttachmentsByProfile(current => ({
      ...current,
      [currentName]: (current[currentName] || []).filter(image => image.id !== imageId)
    }))
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
    const images = attachments
    if ((!message && !images.length) || !currentName) return
    if (attachedRun) {
      if (!message || images.length) {
        host.notify({ kind: 'error', message: 'Live-run interventions accept text only. Attachments cannot inherit the run permission scope safely.' })
        return
      }
      try {
        const dispatched = await dispatchControlMessage(interventionKind, message)
        if (dispatched) setDraft(currentName, '')
      } catch (error) {
        append(currentName, {
          id: `control-error:${makeRequestId()}`,
          role: 'assistant',
          profile: currentName,
          error: true,
          text: `Intervention was not applied: ${error?.message || error}`
        })
      }
      return
    }
    if (activeJob) return
    const profile = currentName
    const requestId = makeRequestId()
    const sessionId = sessions[profile] || null
    const assignment = assignTask === true && supportsKanbanAssignment
    const optimistic = {
      id: `local:${requestId}`,
      role: 'user',
      profile,
      text: message || `Attached ${images.length} image${images.length === 1 ? '' : 's'} for analysis.`,
      attachments: messageAttachmentMetadata(images),
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
    setAttachmentsByProfile(current => ({ ...current, [profile]: [] }))
    const body = buildJobPayload({
      profile,
      provider: modelControlsReady ? effectiveProvider : '',
      model: modelControlsReady ? effectiveModel : '',
      thinking: supportsReasoning && effectiveThinking,
      effort,
      fast: supportsFast && effectiveFast,
      message,
      images,
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
    setAttachmentsByProfile(current => ({ ...current, [currentName]: [] }))
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
  const normalizedDockMode = normalizeDockMode(mode)
  const dockAction = dockModeAction(normalizedDockMode)

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
                'aria-label': `${dockAction} Agent Dock`,
                className: 'shrink-0 rounded-md px-2 py-1 text-[0.62rem] font-medium text-(--ui-text-tertiary) hover:bg-(--ui-control-hover-background) hover:text-foreground',
                'data-agent-dock-mode': normalizedDockMode,
                onClick: () => {
                  haptic('tap')
                  onToggleMode?.()
                },
                title: `${dockAction} Agent Dock`,
                type: 'button',
                children: dockAction
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
                                      jsx(ProfileAvatar, {
                                        active: Boolean(activeJob),
                                        label: `${profileDisplayLabel(currentName)} avatar, ${selectedActivityLabel.toLowerCase()}`,
                                        profile: currentName,
                                        size: 'sm'
                                      }),
                                      jsx(SelectValue, { placeholder: 'Choose agent' }),
                                      jsx('span', {
                                        className: cn(
                                          'ml-auto shrink-0 text-[0.6rem] uppercase tracking-[0.08em]',
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
                                          jsx(ProfileAvatar, {
                                            active: profileWorking,
                                            label: `${profileDisplayLabel(profile.name)} avatar, ${profileStatus.toLowerCase()}`,
                                            profile: profile.name,
                                            size: 'sm'
                                          }),
                                          jsx('span', { children: profileDisplayLabel(profile.name) }),
                                          jsx('span', {
                                            className: cn(
                                              'ml-auto pl-3 text-[0.6rem] uppercase tracking-[0.08em]',
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
                        jsx('div', {
                          className: 'min-w-0 self-end',
                          children: jsxs(DropdownMenu, {
                            onOpenChange: open => {
                              if (!open) setModelMenuPanel('advanced')
                            },
                            children: [
                              jsx(DropdownMenuTrigger, {
                                asChild: true,
                                children: jsxs(Button, {
                                  'aria-label': modelControlsReady
                                    ? `${effectiveProvider} · ${selectedModelPresentation.label} · Workload tier: ${selectedModelPresentation.tierLabel} · Reasoning effort: ${reasoningEffortLabel}`
                                    : 'Loading models available through the configured profile provider',
                                  className: 'h-7 w-full min-w-0 justify-start gap-1.5 rounded-md border border-(--ui-stroke-secondary) bg-(--ui-control-hover-background) px-2 text-[0.68rem] font-normal text-(--ui-text-secondary)',
                                  disabled: !modelControlsReady || Boolean(activeJob),
                                  title: modelControlsReady
                                    ? `${effectiveProvider} · ${selectedModelPresentation.label} · Workload tier: ${selectedModelPresentation.tierLabel} · Reasoning effort: ${reasoningEffortLabel}`
                                    : 'Loading models available through the configured profile provider',
                                  type: 'button',
                                  variant: 'ghost',
                                  children: [
                                    jsx(Codicon, { className: 'shrink-0 text-(--ui-text-secondary)', name: 'zap', size: '0.72rem' }),
                                    jsx('span', { className: 'min-w-0 truncate', children: compactSelectedModelLabel }),
                                    supportsReasoning && capabilities.reasoning
                                      ? jsx('span', { className: 'ml-auto shrink-0 text-(--ui-text-tertiary)', children: reasoningEffortLabel })
                                      : null,
                                    jsx(Codicon, { className: 'shrink-0 text-(--ui-text-quaternary)', name: 'chevron-down', size: '0.72rem' })
                                  ]
                                })
                              }),
                              jsx(DropdownMenuContent, {
                                align: 'end',
                                className: 'w-64 p-1',
                                sideOffset: 6,
                                children: modelMenuPanel === 'advanced'
                                  ? [
                                      jsx('div', {
                                        className: 'px-2 py-1 text-[0.62rem] font-medium text-(--ui-text-tertiary)',
                                        children: 'Advanced'
                                      }, 'advanced-label'),
                                      jsxs(DropdownMenuItem, {
                                        className: 'min-h-8',
                                        onSelect: event => {
                                          event.preventDefault()
                                          setModelMenuPanel('model')
                                        },
                                        children: [
                                          jsx('span', { children: 'Model' }),
                                          jsx('span', { className: 'ml-auto min-w-0 truncate text-(--ui-text-tertiary)', children: compactSelectedModelLabel }),
                                          jsx(Codicon, { name: 'chevron-right', size: '0.75rem' })
                                        ]
                                      }, 'advanced-model'),
                                      jsxs(DropdownMenuItem, {
                                        className: 'min-h-8',
                                        disabled: !supportsReasoning || !capabilities.reasoning || !effectiveThinking,
                                        onSelect: event => {
                                          event.preventDefault()
                                          setModelMenuPanel('effort')
                                        },
                                        children: [
                                          jsx('span', { children: 'Effort' }),
                                          jsx('span', {
                                            className: 'ml-auto text-(--ui-text-tertiary)',
                                            children: supportsReasoning && capabilities.reasoning && effectiveThinking ? reasoningEffortLabel : 'Unavailable'
                                          }),
                                          jsx(Codicon, { name: 'chevron-right', size: '0.75rem' })
                                        ]
                                      }, 'advanced-effort')
                                    ]
                                  : modelMenuPanel === 'model'
                                    ? [
                                        jsxs(DropdownMenuItem, {
                                          className: 'min-h-8 font-medium',
                                          onSelect: event => {
                                            event.preventDefault()
                                            setModelMenuPanel('advanced')
                                          },
                                          children: [
                                            jsx(Codicon, { name: 'chevron-left', size: '0.75rem' }),
                                            jsx('span', { children: 'Model' })
                                          ]
                                        }, 'model-back'),
                                        jsx(DropdownMenuSeparator, { key: 'model-separator' }),
                                        ...modelOptions.map(option => {
                                          const presentation = modelPresentation(option.model, option)
                                          return jsxs(DropdownMenuItem, {
                                            className: 'min-h-8',
                                            onSelect: () => selectModel(option.key),
                                            textValue: `${presentation.label} ${presentation.tierLabel} workload`,
                                            children: [
                                              jsx('span', { className: 'min-w-0 truncate', children: compactModelLabel(presentation.label) }),
                                              jsx(Badge, {
                                                className: 'ml-auto shrink-0 px-1 text-[0.6rem]',
                                                title: `Workload tier: ${presentation.tierLabel}`,
                                                variant: 'outline',
                                                children: presentation.tierLabel
                                              }),
                                              option.key === selectedModelKey
                                                ? jsx(Codicon, { className: 'text-(--ui-accent)', name: 'check', size: '0.72rem' })
                                                : null
                                            ]
                                          }, option.key)
                                        })
                                      ]
                                    : [
                                        jsxs(DropdownMenuItem, {
                                          className: 'min-h-8 font-medium',
                                          onSelect: event => {
                                            event.preventDefault()
                                            setModelMenuPanel('advanced')
                                          },
                                          children: [
                                            jsx(Codicon, { name: 'chevron-left', size: '0.75rem' }),
                                            jsx('span', { children: 'Effort' })
                                          ]
                                        }, 'effort-back'),
                                        jsx(DropdownMenuSeparator, { key: 'effort-separator' }),
                                        jsxs('div', {
                                          className: 'space-y-1.5 px-2 py-2',
                                          key: 'effort-slider',
                                          children: [
                                            jsxs('div', {
                                              className: 'flex items-center justify-between gap-2 text-[0.62rem] text-(--ui-text-tertiary)',
                                              children: [
                                                jsx('span', { children: 'Reasoning effort' }),
                                                jsx('span', { className: 'font-medium text-(--ui-text-secondary)', children: reasoningEffortLabel })
                                              ]
                                            }),
                                            jsx('input', {
                                              'aria-label': 'Reasoning effort',
                                              'aria-valuetext': reasoningEffortLabel,
                                              className: 'h-1 w-full appearance-none rounded-full bg-(--ui-stroke-tertiary)',
                                              disabled: !supportsReasoning || !capabilities.reasoning || !effectiveThinking || Boolean(activeJob),
                                              max: 2,
                                              min: 0,
                                              onChange: event => {
                                                haptic('selection')
                                                setEffort(reasoningEffortForSliderPosition(event.target.value))
                                              },
                                              onKeyDown: event => event.stopPropagation(),
                                              step: 1,
                                              style: { accentColor: 'var(--ui-accent)' },
                                              type: 'range',
                                              value: reasoningEffortSliderPosition(effort)
                                            }),
                                            jsxs('div', {
                                              className: 'flex items-center justify-between text-[0.6rem] text-(--ui-text-tertiary)',
                                              children: [
                                                jsx('span', { children: 'Low' }),
                                                jsx('span', { children: 'Medium' }),
                                                jsx('span', { children: 'High' })
                                              ]
                                            })
                                          ]
                                        })
                                      ]
                              })
                            ]
                          })
                        })
                      ]
                    }),
                    modelsQuery.isError
                      ? jsx('p', { className: 'text-[0.62rem] text-(--ui-danger)', children: 'The configured profile model could not be loaded.' })
                      : null,
                    visibleSubagents.length
                      ? jsxs('div', {
                          className: 'rounded-md border border-(--ui-stroke-secondary) bg-(--ui-control-hover-background)',
                          'data-agent-dock-subagents': currentName,
                          children: [
                            jsxs('button', {
                              'aria-expanded': expandedSubagents[currentName] === true,
                              'aria-label': `${visibleSubagents.length} subagent${visibleSubagents.length === 1 ? '' : 's'} spawned by ${profileDisplayLabel(currentName)}`,
                              className: 'flex h-7 w-full items-center gap-1.5 px-2 text-left text-[0.66rem] text-(--ui-text-secondary)',
                              onClick: () => setExpandedSubagents(current => ({ ...current, [currentName]: current[currentName] !== true })),
                              type: 'button',
                              children: [
                                jsx(Codicon, { name: expandedSubagents[currentName] === true ? 'chevron-down' : 'chevron-right', size: '0.68rem' }),
                                jsx('span', { children: `Subagents (${visibleSubagents.length})` }),
                                jsx('span', {
                                  className: 'ml-auto text-[0.6rem] uppercase tracking-[0.08em] text-(--ui-text-tertiary)',
                                  children: visibleSubagents.some(child => child.status === 'running') ? 'Running' : 'Finished'
                                })
                              ]
                            }),
                            expandedSubagents[currentName] === true
                              ? jsx('ul', {
                                  'aria-label': `Subagents spawned by ${profileDisplayLabel(currentName)}`,
                                  className: 'space-y-1 border-t border-(--ui-stroke-secondary) px-2 py-1.5',
                                  children: visibleSubagents.map(child => jsxs('li', {
                                    className: 'rounded-md bg-(--ui-bg-secondary) px-2 py-1.5 text-[0.62rem]',
                                    children: [
                                      jsxs('div', {
                                        className: 'flex min-w-0 items-center gap-1.5',
                                        children: [
                                          jsx('span', {
                                            'aria-hidden': true,
                                            className: cn('size-1.5 shrink-0 rounded-full', child.status === 'running' ? 'animate-pulse bg-(--ui-accent)' : 'bg-(--ui-text-quaternary)')
                                          }),
                                          jsx('span', { className: 'shrink-0 font-medium', children: `Subagent ${child.task_index + 1}` }),
                                          child.current_tool
                                            ? jsx('span', { className: 'min-w-0 truncate text-(--ui-text-quaternary)', children: child.current_tool })
                                            : null,
                                          jsx('span', {
                                            className: 'ml-auto shrink-0 text-(--ui-text-tertiary)',
                                            children: subagentStatusLabel(child.status)
                                          })
                                        ]
                                      }),
                                      jsxs('div', {
                                        className: 'mt-1 flex min-w-0 items-center gap-1.5 text-[0.6rem] text-(--ui-text-tertiary)',
                                        children: [
                                          jsx('span', { children: child.model || 'Model unavailable' }),
                                          jsx('span', { 'aria-hidden': true, children: '·' }),
                                          jsx('span', {
                                            children: child.usage_state === 'reported'
                                              ? `${child.total_tokens.toLocaleString()} tokens · Reported`
                                              : 'Tokens unavailable'
                                          }),
                                          jsx('span', { className: 'ml-auto shrink-0', children: 'Direct chat unavailable' })
                                        ]
                                      })
                                    ]
                                  }, child.subagent_id))
                                })
                              : null
                          ]
                        })
                      : null
                  ]
                })
              : jsx('p', { className: 'mt-2 text-[0.68rem] text-(--ui-text-tertiary)', children: 'No Hermes profiles found.' })
        ]
      }),
      jsxs('div', {
        className: 'flex shrink-0 items-center gap-1.5 border-b border-(--ui-stroke-secondary) px-3 py-1.5',
        children: attachedRun
          ? [
              jsx('span', {
                className: 'size-1.5 shrink-0 rounded-full bg-(--ui-success)',
                title: 'Attached to a real Hermes runtime session'
              }, 'live-dot'),
              jsx('span', {
                className: 'min-w-0 flex-1 truncate text-[0.64rem] font-medium',
                children: attachedLiveSession
                  ? `Live · ${attachedLiveSession.title} · ${attachedLiveSession.status}`
                  : 'Attached run unavailable · dispatch disabled'
              }, 'live-title'),
              !attachedLiveSession && candidateRebindSession
                ? jsx('button', {
                    'aria-label': 'Reattach live',
                    className: 'shrink-0 rounded bg-(--ui-accent) px-2 py-1 text-[0.6rem] font-medium text-(--ui-accent-foreground)',
                    onClick: () => {
                      void reattachLiveSession().catch(error => {
                        const detail = String(error?.message || error || 'Hermes did not confirm the rebind.').trim().slice(0, 240)
                        host.notify({ kind: 'error', message: `Reattach failed: ${detail}` })
                      })
                    },
                    title: `Reattach live to ${candidateRebindSession.title || 'the exact matching Hermes run'}`,
                    type: 'button',
                    children: 'Reattach live'
                  }, 'reattach')
                : null,
              jsx('button', {
                className: 'text-[0.6rem] text-(--ui-text-quaternary) hover:text-foreground',
                disabled: true,
                title: 'UNAVAILABLE — Hermes has no verified per-run pause/resume contract',
                type: 'button',
                children: 'Pause unavailable'
              }, 'pause'),
              jsx('button', {
                className: 'text-[0.6rem] text-(--ui-danger)',
                disabled: !attachedLiveSession,
                onClick: () => void stopAttachedRun(false),
                type: 'button',
                children: 'Stop'
              }, 'stop'),
              jsx('button', {
                className: 'text-[0.6rem] text-(--ui-text-quaternary) hover:text-foreground',
                onClick: () => rememberAttachedRun(currentName, ''),
                type: 'button',
                children: 'Detach'
              }, 'detach')
            ]
          : liveSessions.length
            ? [
                jsxs(Select, {
                  onValueChange: value => setCandidateSessionIds(current => ({ ...current, [currentName]: value })),
                  value: candidateLiveSession?.id || '',
                  children: [
                    jsx(SelectTrigger, {
                      'aria-label': 'Choose a live Hermes run to attach',
                      className: 'h-7 min-w-0 flex-1 text-[0.65rem]',
                      children: jsx(SelectValue, { placeholder: 'Choose live run' })
                    }),
                    jsx(SelectContent, {
                      children: liveSessions.map(session => jsx(SelectItem, {
                        value: session.id,
                        children: `${session.title} · ${session.status}`
                      }, session.id))
                    })
                  ]
                }, 'live-select'),
                jsx('button', {
                  className: 'shrink-0 rounded bg-(--ui-accent) px-2 py-1 text-[0.62rem] font-medium text-(--ui-accent-foreground)',
                  onClick: () => candidateLiveSession && void attachLiveSession(candidateLiveSession),
                  type: 'button',
                  children: 'Attach live'
                }, 'attach')
              ]
            : [
                jsx('span', {
                  className: 'truncate text-[0.61rem] text-(--ui-text-quaternary)',
                  children: currentName === runtimeProfile
                    ? activeSessionsQuery.isError
                      ? 'Live-run attachment unavailable on this Hermes runtime'
                      : 'No live run for this profile · new messages start a Dock session'
                    : `Switch Desktop to ${profileDisplayLabel(currentName)} to inspect its live runs`
                }, 'no-live'),
                jsx('span', { className: 'ml-auto truncate text-[0.61rem] text-(--ui-text-quaternary)', children: agentHeading }, 'heading')
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
                    'aria-label': `${profileDisplayLabel(activeJob.profile)} ${profileActivityLabel(activeJob).toLowerCase()}`,
                    className: 'mx-3 mb-1.5 flex items-center gap-1.5',
                    role: 'status',
                    children: [
                      jsx('span', {
                        className: 'relative grid size-8 shrink-0 place-items-center',
                        title: `${profileDisplayLabel(activeJob.profile)} · ${profileActivityLabel(activeJob)}`,
                        children: jsx(ProfileAvatar, {
                          active: true,
                          label: `${currentProfile?.display_name || profileDisplayLabel(currentName)} ${profileActivityLabel(activeJob)}`,
                          profile: activeJob.profile
                        })
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
                  jsx('input', {
                    accept: 'image/png,image/jpeg,image/gif,image/webp,image/bmp',
                    'aria-label': 'Choose images',
                    className: 'hidden',
                    disabled: !currentName || Boolean(activeJob) || Boolean(attachedRun) || !supportsImageUpload,
                    multiple: true,
                    onChange: event => void selectImages(event),
                    ref: imageInputRef,
                    type: 'file'
                  }),
                  attachments.length
                    ? jsx('div', {
                        className: 'mb-1.5 flex flex-wrap gap-1',
                        children: attachments.map(image => jsxs('span', {
                          className: 'inline-flex max-w-full items-center gap-1 rounded border border-(--ui-stroke-secondary) bg-(--ui-bg-secondary) px-1.5 py-0.5 text-[0.61rem] text-(--ui-text-secondary)',
                          children: [
                            jsx('img', { alt: '', className: 'size-4 rounded object-cover', src: image.data_url }),
                            jsx('span', { className: 'max-w-28 truncate', children: image.name }),
                            jsx('button', {
                              'aria-label': `Remove ${image.name}`,
                              className: 'text-(--ui-text-quaternary) hover:text-foreground',
                              onClick: () => removeImage(image.id),
                              type: 'button',
                              children: '×'
                            })
                          ]
                        }, image.id))
                      })
                    : null,
                  attachedRun
                    ? jsxs('div', {
                        className: 'mb-1.5 space-y-1.5',
                        children: [
                          jsxs('div', {
                            className: 'flex items-center gap-1',
                            role: 'radiogroup',
                            'aria-label': 'Intervention type',
                            children: INTERVENTION_KINDS.map(kind => jsx('button', {
                              'aria-checked': interventionKind === kind,
                              className: cn(
                                'rounded px-2 py-1 text-[0.62rem] font-medium',
                                interventionKind === kind
                                  ? 'bg-(--ui-control-active-background) text-foreground'
                                  : 'text-(--ui-text-quaternary) hover:text-(--ui-text-secondary)'
                              ),
                              onClick: () => setInterventionKind(kind),
                              role: 'radio',
                              title: kind === 'ask'
                                ? 'Read-only question. Available only when the run is idle.'
                                : kind === 'nudge'
                                  ? 'Adjust execution within the current objective at the next safe tool-result boundary.'
                                  : 'Change the current plan or objective. Explicit confirmation required.',
                              type: 'button',
                              children: kind.toUpperCase()
                            }, kind))
                          }),
                          jsx('p', {
                            className: 'text-[0.58rem] leading-relaxed text-(--ui-text-quaternary)',
                            children: interventionKind === 'ask'
                              ? attachedLiveSession?.status === 'idle'
                                ? 'Read-only question · run is idle'
                                : 'ASK unavailable while working · choose NUDGE or wait for idle'
                              : interventionKind === 'nudge'
                                ? 'Preserves the current objective · delivered by Hermes at a safe boundary'
                                : 'Changes plan or objective · confirmation required'
                          }),
                          pendingConfirmedAction
                            ? jsxs('div', {
                                className: 'rounded border border-(--ui-warning) bg-[color-mix(in_srgb,var(--ui-warning)_10%,transparent)] p-2 text-[0.62rem]',
                                children: [
                                  jsx('p', {
                                    className: 'font-medium',
                                    children: pendingConfirmedAction.type === 'stop'
                                      ? 'Stop this live run?'
                                      : 'Confirm objective-changing REDIRECT?'
                                  }),
                                  jsx('p', {
                                    className: 'mt-0.5 text-(--ui-text-tertiary)',
                                    children: pendingConfirmedAction.type === 'stop'
                                      ? 'Hermes will interrupt the run, clear queued prompts, and deny its pending approvals.'
                                      : 'This can change the agent plan but cannot expand its inherited tools, credentials, or approval scope.'
                                  }),
                                  jsxs('div', {
                                    className: 'mt-1.5 flex justify-end gap-2',
                                    children: [
                                      jsx('button', {
                                        className: 'text-(--ui-text-tertiary)',
                                        onClick: () => setPendingConfirmedAction(null),
                                        type: 'button',
                                        children: 'Cancel'
                                      }),
                                      jsx('button', {
                                        className: 'font-medium text-(--ui-danger)',
                                        onClick: async () => {
                                          const action = pendingConfirmedAction
                                          setPendingConfirmedAction(null)
                                          try {
                                            if (action.type === 'stop') await stopAttachedRun(true)
                                            else {
                                              const dispatched = await dispatchControlMessage('redirect', action.text, true)
                                              if (dispatched) setDraft(currentName, '')
                                            }
                                          } catch (error) {
                                            append(currentName, {
                                              id: `control-error:${makeRequestId()}`,
                                              role: 'assistant',
                                              profile: currentName,
                                              error: true,
                                              text: `Confirmed action failed safely: ${error?.message || error}`
                                            })
                                          }
                                        },
                                        type: 'button',
                                        children: pendingConfirmedAction.type === 'stop' ? 'Confirm stop' : 'Confirm redirect'
                                      })
                                    ]
                                  })
                                ]
                              })
                            : null,
                          controlHistoryQuery.data?.receipts?.length
                            ? jsx('p', {
                                className: 'text-[0.58rem] text-(--ui-text-quaternary)',
                                children: `Latest receipt · ${receiptLabel(controlHistoryQuery.data.receipts.at(-1)?.state || controlHistoryQuery.data.receipts.at(-1)?.stage)} · source ${controlHistoryQuery.data.receipts.at(-1)?.source || 'unverified'}`
                              })
                            : jsx('p', {
                                className: 'text-[0.58rem] text-(--ui-text-quaternary)',
                                children: 'Proof · no application receipt observed yet'
                              }),
                          jsx('p', {
                            className: 'text-[0.58rem] text-(--ui-text-quaternary)',
                            children: `Verification · ${verificationQuery.data?.verification?.status || (verificationQuery.isError ? 'unavailable' : 'unknown')}`
                          })
                        ]
                      })
                    : null,
                  jsx(Textarea, {
                    'aria-label': `Message ${profileDisplayLabel(currentName) || 'selected agent'}`,
                    className: 'min-h-[3.25rem] max-h-32 resize-none text-xs',
                    disabled: !currentName || Boolean(activeJob) || Boolean(attachedRun && !attachedLiveSession),
                    onChange: event => setDraft(currentName, event.target.value),
                    onKeyDown: event => {
                      if (event.key === 'Enter' && !event.shiftKey) {
                        event.preventDefault()
                        void send()
                      }
                    },
                    onPaste: pasteImages,
                    placeholder: attachedRun
                      ? `${interventionKind.toUpperCase()} ${profileDisplayLabel(currentName)} on this live run…`
                      : currentName ? `Message ${profileDisplayLabel(currentName)}…` : 'No profiles found',
                    value: draft
                  }),
                  jsxs('div', {
                    className: 'mt-1.5 flex items-center gap-2',
                    children: [
                      jsx('button', {
                        className: 'text-[0.62rem] text-(--ui-text-quaternary) hover:text-(--ui-text-secondary) disabled:opacity-40',
                        disabled: Boolean(activeJob) || Boolean(attachedRun) || !currentName || !supportsImageUpload || attachments.length >= MAX_IMAGE_ATTACHMENTS,
                        onClick: () => imageInputRef.current?.click(),
                        title: supportsImageUpload ? `Attach up to ${MAX_IMAGE_ATTACHMENTS} local images` : 'Image upload is unavailable',
                        type: 'button',
                        children: attachments.length ? `Images ${attachments.length}/${MAX_IMAGE_ATTACHMENTS}` : 'Attach image'
                      }),
                      jsx('button', {
                        className: 'text-[0.62rem] text-(--ui-text-quaternary) hover:text-(--ui-text-secondary) disabled:opacity-40',
                        disabled: Boolean(activeJob) || Boolean(attachedRun) || !messages.length,
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
                        disabled: Boolean(activeJob) || Boolean(attachedRun) || !supportsKanbanAssignment,
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
                        disabled: (!draft.trim() && !attachments.length) || !currentName || Boolean(activeJob) || Boolean(attachedRun && !attachedLiveSession),
                        onClick: send,
                        size: 'sm',
                        children: attachedRun ? interventionKind.toUpperCase() : assignTask ? 'Assign' : 'Send'
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
      working
        ? jsx(SolvingWorkingOrb, { label: `${activities.length} agent${activities.length === 1 ? '' : 's'} working` })
        : jsx(Codicon, { name: 'hubot', size: '0.72rem' }),
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
  description: 'A floating direct-chat card for specialist Hermes profiles with native Dock/Undock mode.',
  defaultEnabled: true,
  register(ctx) {
    rest = ctx.rest
    storage = ctx.storage
    dockPaneDisposer = null
    $dockOpen.set(false)
    const savedDockMode = normalizeDockMode(storage.get('dock-mode', DEFAULT_DOCK_MODE))
    $dockMode.set(savedDockMode)

    const openDock = (mode = $dockMode.get()) => {
      if (dockPaneDisposer) return
      const nextMode = normalizeDockMode(mode)
      $dockMode.set(nextMode)
      dockPaneDisposer = ctx.register({
        id: nextMode === 'floating' ? 'dock-floating-v1' : 'dock-docked-v1',
        area: PANES_AREA,
        order: 80,
        title: 'Agent Dock',
        data: dockPaneData(nextMode),
        render: () => jsx(AgentDock, { mode: nextMode, onToggleMode: toggleDockMode })
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

    const setDockMode = mode => {
      const nextMode = normalizeDockMode(mode)
      $dockMode.set(nextMode)
      storage.set('dock-mode', nextMode)
      if (!dockPaneDisposer) return
      closeDock()
      openDock(nextMode)
    }

    const toggleDockMode = () => setDockMode(nextDockMode($dockMode.get()))
    const toggleDock = () => ($dockOpen.get() ? closeDock() : openDock())

    ctx.registerMany([
      {
        id: 'pet-toggle',
        area: PET_ACTIONS_AREA,
        order: 80,
        data: {
          label: 'Toggle Agent Dock',
          run: () => {
            haptic('tap')
            toggleDock()
          }
        }
      },
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

    storage.set('dock-mode', savedDockMode)
    if (storage.get('dock-open', false)) openDock(savedDockMode)
    ctx.onDispose(() => {
      dockPaneDisposer = null
      $dockOpen.set(false)
      $dockMode.set(savedDockMode)
    })
  }
}
