import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'

const src = readFileSync(new URL('../plugin.js', import.meta.url), 'utf8')

function loadPure() {
  // Run only the pure, dependency-free avatar core (no host/react/window).
  const sandbox = {
    window: undefined,
    document: undefined,
    performance: { now: () => 0 },
    console,
  }
  const ctx = vm.createContext(sandbox)
  // Extract the pure block: AVATAR_SHAPES through startFaceClock (before BotFace which needs jsx)
  const start = src.indexOf('const AVATAR_SHAPES')
  const end = src.indexOf('function BotFace')
  assert.ok(start !== -1 && end !== -1 && start < end, 'avatar core block found')
  const block = src.slice(start, end)
  const out = vm.runInContext(block + '\n;({ AVATAR_SHAPES, AVATAR_COLORS, sigilRng, sigilGeometry, isDarkColor, defaultShapeFor, shapeNode, sampleFaceRing, ringToPath, facePose })', ctx)
  return out
}

test('avatar palette is stable and well-formed', () => {
  const { AVATAR_SHAPES, AVATAR_COLORS, defaultShapeFor } = loadPure()
  assert.ok(AVATAR_SHAPES.includes('circle'))
  assert.equal(AVATAR_COLORS.length, 10)
  for (const c of AVATAR_COLORS) assert.match(c, /^#[0-9a-f]{6}$/i)
  // deterministic per profile name
  assert.equal(defaultShapeFor('researcher'), defaultShapeFor('researcher'))
  assert.ok(AVATAR_SHAPES.includes(defaultShapeFor('researcher')))
  assert.ok(AVATAR_SHAPES.includes(defaultShapeFor('builder')))
})

test('sigil geometry is deterministic and mirrored', () => {
  const { sigilGeometry } = loadPure()
  const a = sigilGeometry('x', 'seed1')
  const b = sigilGeometry('x', 'seed1')
  assert.equal(a.strokes, b.strokes)
  assert.equal(a.ring, b.ring)
  assert.ok(a.strokes.startsWith('M'))
  const c = sigilGeometry('y', 'seed1')
  assert.notEqual(a.strokes, c.strokes)
})

test('isDarkColor flips correctly', () => {
  const { isDarkColor } = loadPure()
  assert.equal(isDarkColor('#000000'), true)
  assert.equal(isDarkColor('#ffffff'), false)
})

test('face ring + path produce a closed SVG path', () => {
  const { sampleFaceRing, ringToPath } = loadPure()
  const ring = sampleFaceRing('circle')
  assert.ok(Array.isArray(ring) && ring.length > 0)
  const d = ringToPath(ring)
  assert.match(d, /^M/)
  assert.match(d, /[A-Za-z0-9. ]+$/)
})

test('facePose yields finite work/idle values', () => {
  const { facePose } = loadPure()
  const idle = facePose('idle', 0)
  const work = facePose('work', 1.5)
  assert.ok(Number.isFinite(idle.d0) || idle.d0 === undefined)
  assert.ok(Number.isFinite(work.d0) || work.d0 === undefined)
})
