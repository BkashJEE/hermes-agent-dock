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
  'compactModelLabel',
  'dockModeAction',
  'dockPaneData',
  'extractClipboardImageFiles',
  'flattenModelOptions',
  'formatMessageTimestamp',
  'groupModelOptions',
  'migrateSavedModelSelections',
  'modelOptionKey',
  'modelPresentation',
  'nextDockMode',
  'normalizeDockMode',
  'reasoningEffortForSliderPosition',
  'reasoningEffortSliderPosition',
  'profileActivityLabel',
  'profileDisplayLabel',
  'reconcileIdempotentSubmission',
  'pruneExpiredStartingJobs',
  'removeProfileJob',
  'replaceStartingJob',
  'reserveProfileJob',
  'resolveActiveJobActivities',
  'resolveModelSettings',
  'shouldConsumeClipboardPaste',
  'stampMessage',
  'submitWithIdempotentRetry',
  'upsertProfileJob',
  'validateImageFileMetadata',
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

test('floating mode is the explicit default and Dock/Undock mode changes are normalized', () => {
  assert.equal(state.normalizeDockMode(undefined), 'floating')
  assert.equal(state.normalizeDockMode('floating'), 'floating')
  assert.equal(state.normalizeDockMode('docked'), 'docked')
  assert.equal(state.normalizeDockMode('right'), 'floating')
  assert.equal(state.nextDockMode('floating'), 'docked')
  assert.equal(state.nextDockMode('docked'), 'floating')
  assert.equal(state.nextDockMode('invalid'), 'docked')
  assert.equal(state.dockModeAction('floating'), 'Dock')
  assert.equal(state.dockModeAction('docked'), 'Undock')
  assert.deepEqual(state.dockPaneData('floating'), {
    placement: 'floating',
    anchor: 'top-right',
    width: '380px',
    height: '540px',
    uncloseable: true
  })
  assert.deepEqual(state.dockPaneData('docked'), {
    placement: 'bottom',
    dock: { pane: 'workspace', pos: 'bottom' },
    height: '42vh',
    minHeight: '18rem',
    maxHeight: '70vh',
    uncloseable: true
  })
  assert.deepEqual(state.dockPaneData('invalid'), state.dockPaneData('floating'))
  assert.match(pluginSource, /const DEFAULT_DOCK_MODE = 'floating'/)
  assert.match(pluginSource, /storage\.get\('dock-mode', DEFAULT_DOCK_MODE\)/)
  assert.match(pluginSource, /storage\.set\('dock-mode', nextMode\)/)
  assert.match(pluginSource, /data-agent-dock-mode': normalizedDockMode/)
  assert.match(pluginSource, /children: dockAction/)
})

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
    images: [{ name: 'proof.png', mime_type: 'image/png', size: 12, data_url: 'data:image/png;base64,AAAA', private: 'omit' }],
    session_id: null, request_id: 'req-1', assign_task: true, modelPayload: catalog
  })
  assert.equal(payload.profile, 'atlas')
  assert.equal(payload.reasoning_effort, 'xhigh')
  assert.equal(payload.fast, true)
  assert.equal(payload.assign_task, true)
  assert.deepEqual(payload.images, [{ name: 'proof.png', mime_type: 'image/png', data_url: 'data:image/png;base64,AAAA' }])
})

test('image selection is type, size, and count bounded before transport', () => {
  assert.equal(state.validateImageFileMetadata({ type: 'image/png', size: 100 }, 0), null)
  assert.match(state.validateImageFileMetadata({ type: 'text/plain', size: 100 }, 0), /PNG/)
  assert.match(state.validateImageFileMetadata({ type: 'image/png', size: 0 }, 0), /empty/)
  assert.match(state.validateImageFileMetadata({ type: 'image/png', size: 10 * 1024 * 1024 + 1 }, 0), /10 MB/)
  assert.match(state.validateImageFileMetadata({ type: 'image/png', size: 100 }, 4), /at most 4/)
})

test('clipboard image extraction accepts image files, deduplicates Chromium mirrors, and ignores text', () => {
  const image = { name: 'clipboard.png', type: 'image/png', size: 512, lastModified: 7 }
  const mirrored = {
    items: [
      { kind: 'string', type: 'text/plain', getAsFile: () => null },
      { kind: 'file', type: 'image/png', getAsFile: () => image }
    ],
    files: [image]
  }
  assert.deepEqual(state.extractClipboardImageFiles(mirrored), [image])

  const fallback = { items: [], files: [image, { name: 'notes.txt', type: 'text/plain', size: 20 }] }
  assert.deepEqual(state.extractClipboardImageFiles(fallback), [image])
  assert.deepEqual(state.extractClipboardImageFiles({ items: [], files: [] }), [])
})

test('clipboard paste consumes accepted image-only data without swallowing text or invalid images', () => {
  const image = { name: 'clipboard.png', type: 'image/png', size: 512, lastModified: 7 }
  const imageOnly = { getData: () => '' }
  const mixed = { getData: type => type === 'text/plain' ? 'keep this text' : '' }
  assert.equal(state.shouldConsumeClipboardPaste(imageOnly, [image], 0), true)
  assert.equal(state.shouldConsumeClipboardPaste(mixed, [image], 0), false)
  assert.equal(state.shouldConsumeClipboardPaste(imageOnly, [{ type: 'text/plain', size: 20 }], 0), false)
  assert.equal(state.shouldConsumeClipboardPaste(imageOnly, [image], 4), false)
})

test('native catalog is flattened completely and remains provider grouped', () => {
  const options = state.flattenModelOptions(catalog)
  assert.equal(options.length, 3)
  assert.deepEqual(options.map(option => option.provider), ['openai-codex', 'openai-codex', 'local'])
  const groups = state.groupModelOptions(options)
  assert.deepEqual(groups.map(group => [group.provider, group.options.length]), [['openai-codex', 2], ['local', 1]])
  assert.notEqual(state.modelOptionKey('a', 'same'), state.modelOptionKey('b', 'same'))
})

test('legacy model selections migrate to provider-scoped objects without widening the catalog', () => {
  assert.deepEqual(
    state.migrateSavedModelSelections(
      { jarvis: ' gpt-5.6-terra ', buzz: { provider: 'openai-codex', model: 'gpt-5.6-luna' } },
      { jarvis: 'openai-codex' }
    ),
    {
      jarvis: { provider: 'openai-codex', model: 'gpt-5.6-terra' },
      buzz: { provider: 'openai-codex', model: 'gpt-5.6-luna' }
    }
  )
  assert.deepEqual(state.migrateSavedModelSelections({ sanvith: 'qwen-local' }), {
    sanvith: { provider: '', model: 'qwen-local' }
  })
  assert.deepEqual(state.migrateSavedModelSelections({ empty: '', bad: 42 }), {})
})

test('model presentation assigns exact workload tiers to the ten supported GPT models and falls back by capability', () => {
  const expected = [
    ['gpt-5.6-sol', 'GPT 5.6 Sol', 'high', 'High'],
    ['gpt-5.6-sol-pro', 'GPT 5.6 Sol Pro', 'high', 'High'],
    ['gpt-5.6-terra-pro', 'GPT 5.6 Terra Pro', 'high', 'High'],
    ['gpt-5.6-terra', 'GPT 5.6 Terra', 'medium', 'Medium'],
    ['gpt-5.6-luna-pro', 'GPT 5.6 Luna Pro', 'medium', 'Medium'],
    ['gpt-5.5', 'GPT 5.5', 'medium', 'Medium'],
    ['gpt-5.4', 'GPT 5.4', 'medium', 'Medium'],
    ['gpt-5.6-luna', 'GPT 5.6 Luna', 'low', 'Low'],
    ['gpt-5.4-mini', 'GPT 5.4 Mini', 'low', 'Low'],
    ['gpt-5.3-codex-spark', 'GPT 5.3 Codex Spark', 'low', 'Low']
  ]
  for (const [model, label, tier, tierLabel] of expected) {
    assert.deepEqual(state.modelPresentation(model), { label, tier, tierLabel })
  }
  assert.deepEqual(
    state.modelPresentation('unknown-model', { reasoning: true }),
    { label: 'unknown-model', tier: 'medium', tierLabel: 'Medium' }
  )
  assert.deepEqual(
    state.modelPresentation('unknown-model', { reasoning: false }),
    { label: 'unknown-model', tier: 'low', tierLabel: 'Low' }
  )
})

test('reasoning effort slider maps only to Low, Medium, and High values', () => {
  assert.equal(state.reasoningEffortSliderPosition('minimal'), 0)
  assert.equal(state.reasoningEffortSliderPosition('low'), 0)
  assert.equal(state.reasoningEffortSliderPosition('medium'), 1)
  assert.equal(state.reasoningEffortSliderPosition('high'), 2)
  assert.equal(state.reasoningEffortSliderPosition('xhigh'), 2)
  assert.equal(state.reasoningEffortForSliderPosition(0), 'low')
  assert.equal(state.reasoningEffortForSliderPosition(1), 'medium')
  assert.equal(state.reasoningEffortForSliderPosition(2), 'high')
  assert.equal(state.reasoningEffortForSliderPosition(99), 'medium')
})

test('compact model labels strip only the leading GPT family prefix', () => {
  assert.equal(state.compactModelLabel('GPT 5.6 Sol'), '5.6 Sol')
  assert.equal(state.compactModelLabel('Claude Opus 4.1'), 'Claude Opus 4.1')
  assert.equal(state.compactModelLabel(''), 'No model')
})

test('compact model menu separates the model workload tier from reasoning effort', () => {
  assert.match(pluginSource, /DropdownMenuTrigger/)
  assert.match(pluginSource, /name: 'zap'/)
  assert.match(pluginSource, /children: compactSelectedModelLabel/)
  assert.match(pluginSource, /children: 'Advanced'/)
  assert.match(pluginSource, /children: 'Model'/)
  assert.match(pluginSource, /children: 'Effort'/)
  assert.match(pluginSource, /setModelMenuPanel\('model'\)/)
  assert.match(pluginSource, /setModelMenuPanel\('effort'\)/)
  assert.match(pluginSource, /setModelMenuPanel\('advanced'\)/)
  assert.match(pluginSource, /textValue: `\$\{presentation\.label\} \$\{presentation\.tierLabel\} workload`/)
  assert.match(pluginSource, /children: presentation\.tierLabel/)
  assert.match(pluginSource, /title: `Workload tier: \$\{presentation\.tierLabel\}`/)
  assert.match(pluginSource, /Reasoning effort: \$\{reasoningEffortLabel\}/)
  assert.match(pluginSource, /type: 'range'/)
  assert.match(pluginSource, /min: 0/)
  assert.match(pluginSource, /max: 2/)
  assert.match(pluginSource, /step: 1/)
  assert.match(pluginSource, /'aria-label': 'Reasoning effort'/)
  assert.match(pluginSource, /'aria-valuetext': reasoningEffortLabel/)
  assert.match(pluginSource, /h-1 w-full appearance-none rounded-full bg-\(--ui-stroke-tertiary\)/)
  assert.match(pluginSource, /accentColor: 'var\(--ui-accent\)'/)
  assert.match(pluginSource, /haptic\('selection'\)/)
  assert.match(pluginSource, /setEffort\(reasoningEffortForSliderPosition\(event\.target\.value\)\)/)
  assert.match(pluginSource, /onKeyDown: event => event\.stopPropagation\(\)/)
  assert.match(pluginSource, /disabled: !supportsReasoning \|\| !capabilities\.reasoning \|\| !effectiveThinking \|\| Boolean\(activeJob\)/)
  assert.match(pluginSource, /children: 'Reasoning effort'/)
  assert.match(pluginSource, /children: 'Low'/)
  assert.match(pluginSource, /children: 'Medium'/)
  assert.match(pluginSource, /children: 'High'/)
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
  assert.match(pluginSource, /activeJob\s+\? jsx\(RubikWorkingOrb, \{ label: `\$\{profileDisplayLabel\(currentName\)\} working` \}\)/)
  assert.match(pluginSource, /className: 'inline-block size-1\.5 shrink-0 rounded-full bg-\(--ui-text-quaternary\)'/)
})

test('chat activity uses the compact square orb without a duplicate status sentence', () => {
  assert.match(pluginSource, /className: 'grid size-8 shrink-0 place-items-center rounded border border-\(--ui-stroke-secondary\) bg-\(--ui-bg-secondary\)'/)
  assert.match(pluginSource, /role: 'status'/)
  assert.match(pluginSource, /title: `\$\{profileDisplayLabel\(activeJob\.profile\)\} · \$\{profileActivityLabel\(activeJob\)\}`/)
  assert.doesNotMatch(pluginSource, /is working in a direct session/)
  assert.doesNotMatch(pluginSource, /is starting a direct session/)
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

test('plugin registers floating and docked PANES_AREA modes plus pet, status-bar, and palette fallbacks', () => {
  assert.match(pluginSource, /const PET_ACTIONS_AREA = 'pet\.actions'/)
  assert.match(pluginSource, /ctx\.registerMany\(\[\s*\{\s*id:\s*'pet-toggle',\s*area:\s*PET_ACTIONS_AREA,\s*order:\s*80,\s*data:\s*\{\s*label:\s*'Toggle Agent Dock',\s*run:\s*\(\)\s*=>\s*\{\s*haptic\('tap'\)\s*toggleDock\(\)/)
  assert.match(pluginSource, /id:\s*'launcher',\s*area:\s*STATUSBAR_AREAS\.right/)
  assert.match(pluginSource, /id:\s*'focus',\s*area:\s*PALETTE_AREA/)
  assert.match(pluginSource, /id:\s*nextMode === 'floating' \? 'dock-floating-v1' : 'dock-docked-v1'/)
  assert.match(pluginSource, /placement:\s*'floating'/)
  assert.match(pluginSource, /anchor:\s*'top-right'/)
  assert.match(pluginSource, /width:\s*'380px'/)
  assert.match(pluginSource, /height:\s*'540px'/)
  assert.match(pluginSource, /placement:\s*'bottom'/)
  assert.match(pluginSource, /dock:\s*\{\s*pane:\s*'workspace',\s*pos:\s*'bottom'\s*\}/)
  assert.match(pluginSource, /height:\s*'42vh'/)
  assert.match(pluginSource, /minHeight:\s*'18rem'/)
  assert.match(pluginSource, /maxHeight:\s*'70vh'/)
  assert.match(pluginSource, /data:\s*dockPaneData\(nextMode\)/)
  assert.match(pluginSource, /render:\s*\(\) => jsx\(AgentDock, \{ mode: nextMode, onToggleMode: toggleDockMode \}\)/)
  assert.match(pluginSource, /const toggleDockMode = \(\) => setDockMode\(nextDockMode\(\$dockMode\.get\(\)\)\)/)
  assert.match(pluginSource, /closeDock\(\)\s*openDock\(nextMode\)/)
  assert.doesNotMatch(pluginSource, /placement:\s*'right'/)
  assert.doesNotMatch(pluginSource, /dock:\s*\{\s*pane:\s*'files',\s*pos:\s*'top'\s*\}/)
  assert.doesNotMatch(pluginSource, /Achievements 47\/60/)
  assert.doesNotMatch(pluginSource, /dock-height-ratio/)
  assert.doesNotMatch(pluginSource, /Resize Agent Dock height/)
  assert.match(pluginSource, /relative flex h-full min-h-0 flex-col/)
  assert.match(pluginSource, /storage\.get\('active-jobs'/)
  assert.match(pluginSource, /storage\.get\('selected-models', \{\}\)/)
  assert.match(pluginSource, /modelOptions\.length > 0/)
  assert.match(pluginSource, /Workload tier: \$\{selectedModelPresentation\.tierLabel\} · Reasoning effort: \$\{reasoningEffortLabel\}/)
  assert.equal((pluginSource.match(/jsx\(RubikWorkingOrb/g) || []).length, 3)
  assert.doesNotMatch(pluginSource, /\bWorkingOrb\b/)
  assert.match(pluginSource, /profileActivityLabel\(activeJob\)/)
  assert.doesNotMatch(pluginSource, /activeStatusLabel/)
  assert.match(pluginSource, /data-agent-dock-rubik-orb': 'true'/)
  assert.doesNotMatch(pluginSource, /data-agent-dock-working-orb/)
  assert.match(pluginSource, /RUBIK_SOLVING_20_PRESET = Object\.freeze\(\{/)
  assert.match(pluginSource, /latRings: 4/)
  assert.match(pluginSource, /lonDensity: 12/)
  assert.match(pluginSource, /moveCount: 14/)
  assert.match(pluginSource, /speed: 1\.95/)
  assert.match(pluginSource, /rBase: 0\.6 \* 1\.9/)
  assert.match(pluginSource, /rDepth: 1\.7 \* 1\.9/)
  assert.match(pluginSource, /rActive: 0\.3 \* 1\.9/)
  assert.match(pluginSource, /rsPow: 0\.6/)
  assert.match(pluginSource, /rMin: 0\.3/)
  assert.match(pluginSource, /radius: 0\.82/)
  const paletteValues = paletteName => {
    const body = pluginSource.match(new RegExp(`const ${paletteName} = Object\\.freeze\\(\\[([\\s\\S]*?)\\]\\)`))?.[1] || ''
    return body.match(/'#[0-9a-f]{6}'/gi) || []
  }
  assert.equal(paletteValues('RUBIK_DARK_PALETTE').length, 6)
  assert.equal(paletteValues('RUBIK_LIGHT_PALETTE').length, 6)
  assert.notEqual(paletteValues('RUBIK_DARK_PALETTE').join(','), paletteValues('RUBIK_LIGHT_PALETTE').join(','))
  assert.match(pluginSource, /function rubikOriginalAxisIndex\(x, y, z\)/)
  assert.match(pluginSource, /if \(absX >= absY && absX >= absZ\) return x >= 0 \? 0 : 1/)
  assert.match(pluginSource, /if \(absY >= absZ\) return y >= 0 \? 2 : 3/)
  assert.match(pluginSource, /color: palette\[rubikOriginalAxisIndex\(originalX, originalY, originalZ\)\]/)
  assert.match(pluginSource, /thinking-orbs paints this geometry monochrome/)
  assert.match(pluginSource, /seconds \* RUBIK_SOLVING_20_PRESET\.speed/)
  assert.match(pluginSource, /extractClipboardImageFiles\(event\.clipboardData\)/)
  assert.match(pluginSource, /onPaste: pasteImages/)
  assert.match(pluginSource, /style: \{ height: 20, width: 20 \}/)
  assert.match(pluginSource, /prefers-reduced-motion: reduce/)
  assert.match(pluginSource, /paintFrame\(0\.6\)/)
  assert.match(pluginSource, /Math\.min\(2, window\.devicePixelRatio \|\| 1\)/)
  assert.match(pluginSource, /new IntersectionObserver/)
  assert.match(pluginSource, /document\.visibilityState === 'hidden'/)
  assert.match(pluginSource, /cancelAnimationFrame\(animationFrame\)/)
  assert.match(pluginSource, /observer\?\.disconnect\(\)/)
  assert.match(pluginSource, /document\.removeEventListener\('visibilitychange', onVisibility\)/)
  assert.match(pluginSource, /thinking-orbs 0\.2\.0 by Jakub Antalik/)
  assert.match(pluginSource, /Copyright \(c\) 2026 Jakub Antalik/)
  assert.match(pluginSource, /THIRD_PARTY_NOTICES\.md/)
  assert.doesNotMatch(pluginSource, /name: working \? 'loading'/)
  assert.match(pluginSource, /window\.clearTimeout\(timer\)/)
  assert.match(pluginSource, /window\.clearInterval\(timer\)/)
  assert.match(pluginSource, /min-h-\[3\.25rem\]/)
  assert.match(pluginSource, /profileWorking \? 'font-medium text-\(--ui-accent\)'/)
  assert.match(pluginSource, /children:\s*assignTask \? 'Task ✓' : 'Assign task'/)
  assert.match(pluginSource, /Task \$\{job\.kanban_task_id\} was added/)
  assert.doesNotMatch(pluginSource, /aria-label': 'Enable thinking'/)
  assert.doesNotMatch(pluginSource, /aria-label': 'Enable fast mode'/)
  assert.doesNotMatch(pluginSource, /--chrome-background/)
  assert.doesNotMatch(pluginSource, /--ui-control-background/)
})
