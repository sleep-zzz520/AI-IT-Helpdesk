// 后端 API 封装（fetch）。BASE 指向 FastAPI 服务。
const BASE = 'http://127.0.0.1:8000';

async function request(path, options) {
  const resp = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  if (!resp.ok) {
    const detail = await resp.text().catch(() => '');
    throw new Error(`API ${resp.status}: ${detail}`);
  }
  return resp.json();
}

export const createConversation = (userId) =>
  request('/api/conversations', { method: 'POST', body: JSON.stringify({ user_id: userId }) });

export const sendMessage = (convId, content) =>
  request(`/api/conversations/${convId}/messages`, { method: 'POST', body: JSON.stringify({ content }) });

export const fetchConversation = (convId) =>
  request(`/api/conversations/${convId}`);

export const fetchTraces = (convId) =>
  request(`/api/conversations/${convId}/traces`);
