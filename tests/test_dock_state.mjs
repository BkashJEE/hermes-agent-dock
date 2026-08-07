import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

const pluginSource = await readFile(new URL('../plugin.js', import.meta.url), 'utf8')
const helpers = pluginSource.match(/\/\/ STATE_HELPERS_START[^]*?\/\/ STATE_HELPERS_END/)?.[0]
assert.ok(helpers, 'state helper block must remain present in plugin.js')
const exported = [
  'activeJobActivities',
  'activitySummary',
  'appendUniqueMessage',
  'buildJobPayload',
  'flattenModelOptions',
  'formatMessageTimestamp',
  'groupModelOptions',
  'modelOptionKey',
  'profileActivityLabel',
  'profileDisplayLabel',
  'reconcileIdempotentSubmission',
  'pruneExpiredStartingJobs',
  'removeProfileJob',
  'replaceStartingJob',
  'reserveProfileJob',
  'resolveActiveJobActivities',
  'resolveModelSettings',
  'stampMessage',
  'submitWithIdempotentRetry',
  'upsertProfileJob',
  'workingProfileNames'
].join(', ')
const state = await import(`data:text/javascript;base64,${Buffer.from(`${helpers}\nexport { ${exported} }`).toString('base64')}`)

const catalog = {
  provider: 'openai-codex',
  model: 'gpt-5.6-terra',
  providers: [
    {
      slug: 'openai-codex',
      name: 'OpenAI Codex',
      models: ['gpt-5.6-terra', 'gpt-5.6-luna'],
      capabilities: {
        'gpt-5.6-terra': { reasoning: true, fast: true },
        'gpt-5.6-luna': { reasoning: true, fast: false }
      }
    },
    {
      slug: 'local',
      name: 'Local',
      models: [{ model: 'qwen3' }],
      capabilities: { qwen3: { reasoning: false, fast: false } }
    }
  ]
}

test('A and B reserve independent job slots and identity removal cannot clear the other job', () => {
  let jobs = {}
  let result = state.reserveProfileJob(jobs, 'jarvis', { id: 'job-a', profile: 'jarvis' })
  assert.equal(result.reserved, true)
  jobs = result.jobs
  result = state.reserveProfileJob(jobs, 'atlas', { id: 'job-b', profile: 'atlas' })
  assert.equal(result.reserved, true)
  jobs = result.jobs
  assert.deepEqual(Object.keys(jobs).sort(), ['atlas', 'jarvis'])
  assert.equal(state.reserveProfileJob(jobs, 'jarvis', { id: 'duplicate' }).reserved, false)
  assert.strictEqual(state.removeProfileJob(jobs, 'jarvis', 'stale-job'), jobs)
  jobs = state.removeProfileJob(jobs, 'jarvis', 'job-a')
  assert.equal(jobs.jarvis, undefined)
  assert.equal(jobs.atlas.id, 'job-b')
})

test('out-of-order and duplicate terminal messages remain profile-scoped and idempotent', () => {
  let histories = { jarvis: [{ id: 'seed-a' }], atlas: [{ id: 'seed-b' }] }
  histories = state.appendUniqueMessage(histories, 'atlas', { id: 'job-b:assistant', text: 'B done' })
  histories = state.appendUniqueMessage(histories, 'jarvis', { id: 'job-a:assistant', text: 'A done' })
  const duplicate = state.appendUniqueMessage(histories, 'jarvis', { id: 'job-a:assistant', text: 'duplicate' })
  assert.strictEqual(duplicate, histories)
  assert.deepEqual(histories.jarvis.map(item => item.id), ['seed-a', 'job-a:assistant'])
  assert.deepEqual(histories.atlas.map(item => item.id), ['seed-b', 'job-b:assistant'])
})

test('chat messages persist exact timestamps and format local date, time, seconds, and zone', () => {
  const createdAt = Date.UTC(2026, 7, 6, 15, 35, 55)
  const stamped = state.stampMessage({ id: 'message-1', text: 'hello' }, createdAt)
  assert.equal(stamped.created_at, createdAt)
  assert.strictEqual(state.stampMessage(stamped, createdAt + 1000), stamped)
  const label = state.formatMessageTimestamp(createdAt, 'en-US', 'UTC')
  assert.match(label, /Aug 6, 2026/)
  assert.match(label, /3:35:55 PM/)
  assert.match(label, /UTC/)
  assert.equal(state.formatMessageTimestamp(null, 'en-US', 'UTC'), 'Date unavailable')
})

test('idempotent submit retries once and non-idempotent submit never retries', async () => {
  let attempts = 0
  const recovered = await state.submitWithIdempotentRetry(async () => {
    attempts += 1
    if (attempts === 1) throw new Error('response lost')
    return { id: 'existing-job' }
  }, true)
  assert.equal(recovered.id, 'existing-job')
  assert.equal(attempts, 2)

  attempts = 0
  await assert.rejects(
    state.submitWithIdempotentRetry(async () => {
      attempts += 1
      throw new Error('network down')
    }, false),
    /network down/
  )
  assert.equal(attempts, 1)
})

test('lost POST responses retain the reservation and reconcile the stable request ID', async () => {
  let attempts = 0
  let recovered = false
  let accepted = null
  let pending = 0
  const scheduled = []
  const options = {
    allowRetry: true,
    isReserved: () => true,
    onAccepted: job => { accepted = job },
    onPending: () => { pending += 1 },
    schedule: callback => { scheduled.push(callback) }
  }
  const submit = async () => {
    attempts += 1
    if (!recovered) throw new Error('response lost')
    return { id: 'existing-job', request_id: 'stable-request' }
  }

  await state.reconcileIdempotentSubmission(submit, options)
  assert.equal(attempts, 2)
  assert.equal(pending, 1)
  assert.equal(scheduled.length, 1)
  assert.equal(accepted, null)

  recovered = true
  await scheduled[0]()
  assert.equal(attempts, 3)
  assert.equal(accepted.id, 'existing-job')
})

test('agent labels are proper case while payload identity stays raw', () => {
  assert.equal(state.profileDisplayLabel('atlas'), 'Atlas')
  assert.equal(state.profileDisplayLabel('proof-engineer'), 'Proof Engineer')
  const payload = state.buildJobPayload({
    profile: 'atlas', provider: 'openai-codex', model: 'gpt-5.6-terra',
    thinking: true, effort: 'xhigh', fast: true, message: 'hello',
    session_id: null, request_id: 'req-1', assign_task: true, modelPayload: catalog
  })
  assert.equal(payload.profile, 'atlas')
  assert.equal(payload.reasoning_effort, 'xhigh')
  assert.equal(payload.fast, true)
  assert.equal(payload.assign_task, true)
})

test('native catalog is flattened completely and remains provider grouped', () => {
  const options = state.flattenModelOptions(catalog)
  assert.equal(options.length, 3)
  assert.deepEqual(options.map(option => option.provider), ['openai-codex', 'openai-codex', 'local'])
  const groups = state.groupModelOptions(options)
  assert.deepEqual(groups.map(group => [group.provider, group.options.length]), [['openai-codex', 2], ['local', 1]])
  assert.notEqual(state.modelOptionKey('a', 'same'), state.modelOptionKey('b', 'same'))
})

test('thinking, effort, and fast are capability gated', () => {
  assert.deepEqual(
    state.resolveModelSettings({ modelPayload: catalog, provider: 'openai-codex', model: 'gpt-5.6-terra', thinking: true, effort: 'xhigh', fast: true }),
    { reasoning: true, fast: true, thinking: true, reasoning_effort: 'xhigh', fast_enabled: true }
  )
  assert.deepEqual(
    state.resolveModelSettings({ modelPayload: catalog, provider: 'local', model: 'qwen3', thinking: true, effort: 'ultra', fast: true }),
    { reasoning: false, fast: false, thinking: false, reasoning_effort: 'none', fast_enabled: false }
  )
})

test('working profile highlight includes only active job states', () => {
  const now = 1_000_000
  assert.deepEqual(
    state.workingProfileNames({
      jarvis: { id: 'job-a', status: 'running' },
      atlas: { id: null, status: 'starting', started_at: now - 1_000 },
      buzz: { id: 'job-b', status: 'done' },
      warren: { id: 'job-c', status: 'cancelled' }
    }, now),
    ['atlas', 'jarvis']
  )
})

test('stale interrupted starts expire while fresh starts survive hydration', () => {
  const now = 1_000_000
  const jobs = {
    fresh: { id: null, status: 'starting', started_at: now - 59_000 },
    stale: { id: null, status: 'starting', started_at: now - 60_001 },
    legacy: { id: null, status: 'starting' },
    running: { id: 'job-a', status: 'running' }
  }
  assert.deepEqual(state.workingProfileNames(jobs, now), ['fresh', 'running'])
  assert.deepEqual(Object.keys(state.pruneExpiredStartingJobs(jobs, now)).sort(), ['fresh', 'running'])
})

test('activity reconciliation handles terminal, structured 404, transient errors, and multiple profiles', async () => {
  const jobs = {
    jarvis: { id: 'running', status: 'queued' },
    atlas: { id: 'done', status: 'running' },
    buzz: { id: 'missing', status: 'running' },
    warren: { id: 'transient', status: 'running' }
  }
  const activities = await state.resolveActiveJobActivities(jobs, async jobId => {
    if (jobId === 'running') return { id: jobId, status: 'running' }
    if (jobId === 'done') return { id: jobId, status: 'done' }
    if (jobId === 'missing') throw { response: { status: 404 }, message: 'Request failed' }
    throw new Error('temporary network failure')
  })
  assert.deepEqual(activities.map(activity => [activity.profile, activity.status]), [
    ['jarvis', 'running'],
    ['warren', 'running']
  ])
})

test('activity labels stay truthful during cancellation and reservation settlement is identity safe', () => {
  assert.equal(state.activitySummary([{ profile: 'jarvis', status: 'cancelling' }]), 'Jarvis is cancelling')
  assert.equal(state.activitySummary([{ profile: 'jarvis', status: 'queued' }]), 'Jarvis has an active session')
  assert.equal(
    state.activitySummary([{ profile: 'jarvis', status: 'running' }, { profile: 'atlas', status: 'queued' }]),
    '2 agents have active sessions: Jarvis, Atlas'
  )
  const jobs = { jarvis: { id: null, request_id: 'request-new', status: 'starting' } }
  assert.strictEqual(state.replaceStartingJob(jobs, 'jarvis', 'request-old', { id: 'stale' }), jobs)
  assert.equal(state.replaceStartingJob(jobs, 'jarvis', 'request-new', { id: 'job-new', status: 'queued' }).jarvis.id, 'job-new')
})

test('Agent selector exposes truthful Working, Cancelling, and Idle states', () => {
  assert.equal(state.profileActivityLabel(null), 'Idle')
  assert.equal(state.profileActivityLabel({ status: 'queued' }), 'Working')
  assert.equal(state.profileActivityLabel({ status: 'running' }), 'Working')
  assert.equal(state.profileActivityLabel({ status: 'finalizing' }), 'Working')
  assert.equal(state.profileActivityLabel({ status: 'cancelling' }), 'Cancelling')
  assert.equal(state.profileActivityLabel({ status: 'success' }), 'Idle')
  assert.match(pluginSource, /Select agent\. \$\{profileDisplayLabel\(currentName\)\} is \$\{selectedActivityLabel\.toLowerCase\(\)\}/)
  assert.match(pluginSource, /activeJob \? 'animate-pulse bg-\(--ui-accent\)' : 'bg-\(--ui-text-quaternary\)'/)
})

test('transient polling failures retain the active job and continue reconciliation', () => {
  assert.match(pluginSource, /if \(isNotFoundError\(error\)\) \{\s+removeActiveJob\(profile, jobId\)/)
  assert.match(pluginSource, /Monitoring continues\./)
  assert.match(pluginSource, /window\.setTimeout\(\(\) => void poll\(profile, jobId, 0\), 10_000\)/)
  assert.doesNotMatch(pluginSource, /session status could not be read\./)
  assert.match(pluginSource, /reconcileIdempotentSubmission\(submit/)
  assert.doesNotMatch(pluginSource, /removeProfileJob\(stored, profile, null\)/)
})

test('achievement sound only represents a real unlock and toast expiry is independent', () => {
  assert.doesNotMatch(pluginSource, /if\s*\(!next\)\s*playAchievementChime\(\)/)
  assert.match(pluginSource, /if \(!achievementToast\) return\s+const timer = window\.setTimeout\(\(\) => setAchievementToast\(null\), 6500\)/)
  assert.match(pluginSource, /\[achievementToast\?\.id, achievementToast\?\.unlocked_at\]/)
})

test('plugin registers a persistent launcher and a native half-height split above Files', () => {
  assert.match(pluginSource, /area:\s*STATUSBAR_AREAS\.right/)
  assert.match(pluginSource, /id:\s*'dock-v3'/)
  assert.match(pluginSource, /placement:\s*'right'/)
  assert.match(pluginSource, /dock:\s*\{\s*pane:\s*'files',\s*pos:\s*'top'\s*\}/)
  assert.match(pluginSource, /height:\s*'50%'/)
  assert.doesNotMatch(pluginSource, /placement:\s*'floating'/)
  assert.doesNotMatch(pluginSource, /Achievements 47\/60/)
  assert.match(pluginSource, /width:\s*'clamp\(19rem, 24vw, 22rem\)'/)
  assert.match(pluginSource, /minWidth:\s*'18rem'/)
  assert.match(pluginSource, /maxWidth:\s*'26rem'/)
  assert.match(pluginSource, /relative flex h-full min-h-0 flex-col/)
  assert.doesNotMatch(pluginSource, /dock-height-ratio/)
  assert.doesNotMatch(pluginSource, /Resize Agent Dock height/)
  assert.match(pluginSource, /storage\.get\('active-jobs'/)
  assert.match(pluginSource, /working \? 'loading' : 'hubot'/)
  assert.match(pluginSource, /window\.clearTimeout\(timer\)/)
  assert.match(pluginSource, /window\.clearInterval\(timer\)/)
  assert.match(pluginSource, /min-h-\[3\.25rem\]/)
  assert.match(pluginSource, /profileWorking \? 'font-medium text-\(--ui-accent\)'/)
  assert.match(pluginSource, /children:\s*assignTask \? 'Task ✓' : 'Assign task'/)
  assert.match(pluginSource, /Task \$\{job\.kanban_task_id\} was added/)
  assert.doesNotMatch(pluginSource, /aria-label': 'Enable thinking'/)
  assert.doesNotMatch(pluginSource, /aria-label': 'Thinking strength'/)
  assert.doesNotMatch(pluginSource, /aria-label': 'Enable fast mode'/)
  assert.doesNotMatch(pluginSource, /--chrome-background/)
  assert.doesNotMatch(pluginSource, /--ui-control-background/)
})
