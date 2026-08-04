// 后端 API 封装（fetch）。BASE 指向 FastAPI 服务。
const BASE = 'http://127.0.0.1:8000';

async function request(path, options) {
  let resp;
  try {
    resp = await fetch(`${BASE}${path}`, {
      headers: { 'Content-Type': 'application/json' },
      ...options,
    });
  } catch {
    // 网络层失败（后端没启动/崩溃/端口不对）——翻译成人话 + 解决办法
    throw new Error(
      `无法连接后端服务（${BASE}）。请确认后端已启动：cd backend && source ../.venv/bin/activate && uvicorn app.main:app --reload`,
    );
  }
  if (!resp.ok) {
    let detail = ''
    try { detail = (await resp.json()).detail || '' } catch { /* 非 JSON 响应 */ }
    const hint = resp.status >= 500
      ? '（后端处理出错，可能是模型限流，请稍后重试）'
      : ''
    throw new Error(`后端返回错误（${resp.status}）${detail ? `：${detail}` : ''}${hint}`);
  }
  return resp.json();
}

export const createConversation = (userId) =>
  request('/api/conversations', { method: 'POST', body: JSON.stringify({ user_id: userId }) });

export const sendMessage = (convId, content, image) =>
  request(`/api/conversations/${convId}/messages`, {
    method: 'POST',
    body: JSON.stringify({ content, image: image ?? null }),
  });

export const fetchConversation = (convId) =>
  request(`/api/conversations/${convId}`);

export const fetchTraces = (convId) =>
  request(`/api/conversations/${convId}/traces`);
