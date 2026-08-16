import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'

const src = readFileSync(new URL('../plugin.js', import.meta.url), 'utf8')

function loadPure() {
  const ctx = vm.createContext({ console, Date, Math })
  const start = src.indexOf('// ── Bot-Mode port: routines + profile actions (pure logic')
  const end = src.indexOf('// ── Bot-Mode port: Routines row UI')
  assert.ok(start !== -1 && end !== -1 && start < end, 'routines pure block found')
  const block = src.slice(start, end)
  return vm.runInContext(
    block + '\n;({ routineTitle, isLegacyDelegatedRoutine, scheduleLabel, routineActive, selectRoutineJobs, duplicateName, relativeRoutineTime })',
    ctx
  )
}

function loadMention() {
  const ctx = vm.createContext({ console })
  const start = src.indexOf('function parseMentionProfile')
  const end = src.indexOf('// ── Bot-Mode port: Routines row UI')
  assert.ok(start !== -1 && end !== -1 && start < end, 'mention parser found')
  return vm.runInContext(src.slice(start, end) + '\n;parseMentionProfile', ctx)
}

test('scheduleLabel humanizes Hermes schedule strings', () => {
  const { scheduleLabel } = loadPure()
  assert.equal(scheduleLabel('every 60m'), 'Hourly')
  assert.equal(scheduleLabel('every 120m'), 'Every 2h')
  assert.equal(scheduleLabel('every 30m'), 'Every 30m')
  assert.equal(scheduleLabel('every 1440m'), 'Daily')
  assert.equal(scheduleLabel('every 2880m'), 'Every 2 days')
  assert.equal(scheduleLabel('30m'), 'Once (30m)')
  assert.equal(scheduleLabel('2h'), 'Once (2h)')
  assert.equal(scheduleLabel('0 9 * * *'), '0 9 * * *')
  assert.equal(scheduleLabel(''), '')
})

test('routineTitle strips the [bot:x] tag and falls back', () => {
  const { routineTitle } = loadPure()
  assert.equal(routineTitle({ name: '[bot:jarvis] Morning brief' }), 'Morning brief')
  assert.equal(routineTitle({ name: 'plain' }), 'plain')
  assert.equal(routineTitle({}), 'Untitled routine')
  assert.equal(routineTitle(null), 'Untitled routine')
})

test('isLegacyDelegatedRoutine only fires for tagged + prefixed jobs', () => {
  const { isLegacyDelegatedRoutine } = loadPure()
  assert.equal(
    isLegacyDelegatedRoutine({ name: '[bot:jarvis] x', prompt_preview: 'You are running the scheduled routine "x"' }),
    true
  )
  assert.equal(isLegacyDelegatedRoutine({ name: 'untagged', prompt_preview: 'You are running the scheduled routine "x"' }), false)
  assert.equal(isLegacyDelegatedRoutine({ name: '[bot:jarvis] x', prompt_preview: 'normal' }), false)
})

test('routineActive reflects enabled/state and blocks legacy jobs', () => {
  const { routineActive } = loadPure()
  assert.equal(routineActive({}), true)
  assert.equal(routineActive({ enabled: false }), false)
  assert.equal(routineActive({ state: 'paused' }), false)
  assert.equal(
    routineActive({ name: '[bot:jarvis] x', prompt_preview: 'You are running the scheduled routine "x"', enabled: true }),
    false
  )
})

test('selectRoutineJobs scopes by [bot:x] tag, default for untagged', () => {
  const { selectRoutineJobs } = loadPure()
  const jobs = [
    { name: '[bot:jarvis] A', job_id: '1' },
    { name: '[bot:other] B', job_id: '2' },
    { name: 'global', job_id: '3' }
  ]
  assert.deepEqual(selectRoutineJobs(jobs, 'jarvis').map(j => j.job_id), ['1'])
  assert.deepEqual(selectRoutineJobs(jobs, 'default').map(j => j.job_id), ['3'])
  assert.equal(selectRoutineJobs(jobs, null).length, 3)
  assert.equal(selectRoutineJobs(undefined, 'jarvis').length, 0)
})

test('duplicateName truncates the base, never the suffix (#19)', () => {
  const { duplicateName } = loadPure()
  assert.equal(duplicateName('jarvis', []), 'jarvis-2')
  assert.equal(duplicateName('jarvis', ['jarvis-2']), 'jarvis-3')
  assert.equal(duplicateName('a'.repeat(80), ['x'], 64).endsWith('-2'), true)
  const long = duplicateName('a'.repeat(80), [], 64)
  assert.equal(long.length, 64)
  assert.ok(long.endsWith('-2'))
  assert.equal(duplicateName('full', ['full-2', 'full-3', 'full-4', 'full-5', 'full-6', 'full-7', 'full-8', 'full-9'], 64), 'full-10')
  assert.equal(duplicateName('', [], 64), '-2')
})

test('relativeRoutineTime formats upcoming runs', () => {
  const { relativeRoutineTime } = loadPure()
  assert.equal(relativeRoutineTime(''), '')
  assert.equal(relativeRoutineTime('not-a-date'), '')
  const soon = new Date(Date.now() + 5 * 60000).toISOString()
  assert.equal(relativeRoutineTime(soon), 'next in 5m')
  const hours = new Date(Date.now() + 3 * 3600000).toISOString()
  assert.equal(relativeRoutineTime(hours), 'next in 3h')
})

test('parseMentionProfile routes leading @profile to a known profile', () => {
  const parse = loadMention()
  const profiles = [{ name: 'jarvis' }, { name: 'CFO' }, { name: 'default' }]
  assert.equal(parse('@jarvis run the brief', profiles), 'jarvis')
  assert.equal(parse('@CFO summarize the ledger', profiles), 'CFO')
  assert.equal(parse('hello @jarvis', profiles), null, 'mid-message mention is not a route')
  assert.equal(parse('@ghost do it', profiles), null, 'unknown profile is not a route')
  assert.equal(parse('plain message', profiles), null)
  assert.equal(parse('', profiles), null)
  assert.equal(parse(null, profiles), null)
})
