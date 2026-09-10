import test from 'node:test'
import assert from 'node:assert/strict'

import { fmtDuration } from '../src/format.js'

test('fmtDuration formats empty, sub-second, and second durations', () => {
  assert.equal(fmtDuration(null), '')
  assert.equal(fmtDuration(Number.NaN), '')
  assert.equal(fmtDuration(0), '<1 ms')
  assert.equal(fmtDuration(0.8), '<1 ms')
  assert.equal(fmtDuration(12.4), '12 ms')
  assert.equal(fmtDuration(1000), '1.0 s')
  assert.equal(fmtDuration(1549), '1.5 s')
})
