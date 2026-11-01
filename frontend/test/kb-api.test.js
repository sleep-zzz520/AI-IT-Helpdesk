import test, { afterEach } from 'node:test'
import assert from 'node:assert/strict'
import { fetchKbJob, isKbJobRunning, pollKbJob, rollbackKb, syncKb } from '../src/api.js'

const originalFetch = globalThis.fetch
const response = (body, status = 200) => ({ ok: status < 400, status, async json() { return body } })
afterEach(() => { globalThis.fetch = originalFetch })

test('202 sync remains a queued job, and rollback uses a separate endpoint', async () => {
  const requests = []
  globalThis.fetch = async (url, options) => {
    requests.push([url, options.method])
    return response({ id: 9, status: 'queued' }, 202)
  }
  const job = await syncKb()
  assert.equal(job.status, 'queued')
  assert.equal(isKbJobRunning(job), true)
  await rollbackKb()
  assert.deepEqual(requests, [['/api/kb/sync', 'POST'], ['/api/kb/rollback', 'POST']])
})

test('poll follows one job until prepared, without submitting sync or publication', async () => {
  const jobs = [
    { id: 9, status: 'queued' },
    { id: 9, status: 'running' },
    { id: 9, status: 'prepared', report: { embedding_inputs: 2, reused_vectors: 12 } },
  ]
  const seen = []
  let calls = 0
  globalThis.fetch = async (url) => {
    assert.equal(url, '/api/kb/jobs/9')
    return response(jobs[calls++])
  }
  const result = await pollKbJob(9, { intervalMs: 1, onUpdate: (job) => seen.push(job.status) })
  assert.equal(calls, 3)
  assert.deepEqual(seen, ['queued', 'running', 'prepared'])
  assert.equal(isKbJobRunning(result), false)
  assert.equal(result.report.reused_vectors, 12)
})

for (const status of ['published', 'failed', 'superseded']) {
  test(`poll stops on ${status} and preserves the server outcome`, async () => {
    let calls = 0
    globalThis.fetch = async () => { calls++; return response({ id: 9, status, error: 'server detail' }) }
    const result = await pollKbJob(9, { intervalMs: 1 })
    assert.equal(calls, 1)
    assert.equal(result.status, status)
    assert.equal(result.error, 'server detail')
    assert.equal(isKbJobRunning(result), false)
  })
}

test('cancelling while waiting prevents another status request', async () => {
  const controller = new AbortController()
  let calls = 0
  globalThis.fetch = async () => { calls++; return response({ id: 9, status: 'running' }) }
  const watching = pollKbJob(9, { signal: controller.signal, intervalMs: 10000 })
  await new Promise((resolve) => setTimeout(resolve, 5))
  controller.abort()
  await assert.rejects(watching, { name: 'AbortError' })
  assert.equal(calls, 1)
})

test('cancelling an in-flight request preserves cancellation', async () => {
  const controller = new AbortController()
  globalThis.fetch = (_url, { signal }) => new Promise((_resolve, reject) => {
    signal.addEventListener('abort', () => reject(signal.reason), { once: true })
  })
  const watching = pollKbJob(9, { signal: controller.signal })
  controller.abort()
  await assert.rejects(watching, { name: 'AbortError' })
})

test('request timeout reports uncertainty about the continuing background job', async () => {
  globalThis.fetch = (_url, { signal }) => new Promise((_resolve, reject) => {
    signal.addEventListener('abort', () => reject(signal.reason), { once: true })
  })
  await assert.rejects(fetchKbJob(9, { timeoutMs: 5 }), /查询超时.*后台任务可能仍在继续/)
})

test('unknown job state and server rejection stop polling with actionable errors', async () => {
  globalThis.fetch = async () => response({ status: 'unexpected' })
  await assert.rejects(pollKbJob(9), /无法识别同步任务状态/)
  globalThis.fetch = async () => response({ detail: '任务不属于当前租户' }, 403)
  await assert.rejects(pollKbJob(9), /403.*任务不属于当前租户/)
})
