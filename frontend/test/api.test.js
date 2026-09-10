import test, { afterEach, beforeEach } from 'node:test'
import assert from 'node:assert/strict'

import {
  clearToken,
  fetchMe,
  getToken,
  login,
  sendMessageStream,
  setToken,
} from '../src/api.js'

function response({ status = 200, body = {}, ok = status >= 200 && status < 300 } = {}) {
  return {
    status,
    ok,
    body: null,
    async json() { return body },
  }
}

function sseResponse(chunks) {
  let index = 0
  return {
    status: 200,
    ok: true,
    body: {
      getReader() {
        return {
          async read() {
            if (index >= chunks.length) return { done: true, value: undefined }
            return { done: false, value: new TextEncoder().encode(chunks[index++]) }
          },
        }
      },
    },
  }
}

beforeEach(() => {
  const values = new Map()
  globalThis.localStorage = {
    getItem(key) { return values.get(key) ?? null },
    setItem(key, value) { values.set(key, String(value)) },
    removeItem(key) { values.delete(key) },
  }
  globalThis.fetch = async () => response()
})

afterEach(() => {
  delete globalThis.localStorage
  delete globalThis.fetch
})

test('token helpers persist and clear the login token', () => {
  assert.equal(getToken(), null)
  setToken('token-123')
  assert.equal(getToken(), 'token-123')
  clearToken()
  assert.equal(getToken(), null)
})

test('login sends credentials without an Authorization header', async () => {
  const calls = []
  globalThis.fetch = async (url, options) => {
    calls.push({ url, options })
    return response({ body: { token: 'new-token' } })
  }

  await login('zhangsan', 'secret')

  assert.equal(calls.length, 1)
  assert.equal(calls[0].url, '/api/auth/login')
  assert.equal(calls[0].options.method, 'POST')
  assert.deepEqual(JSON.parse(calls[0].options.body), {
    username: 'zhangsan',
    password: 'secret',
  })
  assert.equal(calls[0].options.headers.Authorization, undefined)
})

test('401 clears token and exposes unauthorized marker', async () => {
  setToken('expired-token')
  globalThis.fetch = async () => response({
    status: 401,
    ok: false,
    body: { detail: '登录已过期' },
  })

  await assert.rejects(fetchMe(), (error) => {
    assert.equal(error.unauthorized, true)
    assert.equal(error.message, '登录已过期，请重新登录')
    return true
  })
  assert.equal(getToken(), null)
})

test('SSE parser emits node events and returns done payload', async () => {
  setToken('valid-token')
  const calls = []
  const nodes = []
  const done = {
    conversation_id: 7,
    reply: '已完成',
    messages: [],
    traces: [],
    elapsed_ms: 37,
  }
  globalThis.fetch = async (url, options) => {
    calls.push({ url, options })
    return sseResponse([
      'event: node\ndata: {"traces":[{"node":"intent"}]}\n\n',
      'event: done\ndata: ' + JSON.stringify(done) + '\n\n',
    ])
  }

  const result = await sendMessageStream(
    7, 'VPN 连不上', null, 'accurate', (payload) => nodes.push(payload),
  )

  assert.deepEqual(result, done)
  assert.deepEqual(nodes, [{ traces: [{ node: 'intent' }] }])
  assert.equal(calls[0].url, '/api/conversations/7/messages')
  assert.equal(calls[0].options.headers.Authorization, 'Bearer valid-token')
  assert.deepEqual(JSON.parse(calls[0].options.body), {
    content: 'VPN 连不上', image: null, mode: 'accurate',
  })
})

test('SSE error event is surfaced to the caller', async () => {
  globalThis.fetch = async () => sseResponse([
    'event: error\ndata: {"message":"Agent 执行失败，消息已保存"}\n\n',
  ])

  await assert.rejects(
    sendMessageStream(7, '失败用例', null, 'accurate', () => {}),
    /Agent 执行失败，消息已保存/,
  )
})
