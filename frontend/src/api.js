// 后端 API 封装（fetch）。
// BASE 用相对路径（同源）——本地开发由 vite proxy 转发，Docker 由 nginx 反代，
// 代码零差异（生产标准做法：不用写死地址、无 CORS 问题）。
// Node 内置测试运行器没有 Vite 注入的 import.meta.env；可选链让 API 封装
// 在测试环境和 Vite 构建环境都保持同一行为。
const BASE = import.meta.env?.VITE_API_BASE || '';
const TOKEN_KEY = 'helpdesk_token';

// ===== token 管理（登录态持久化：刷新页面不丢）=====
export function getToken() {
  try { return localStorage.getItem(TOKEN_KEY) } catch { return null }
}
export function setToken(token) {
  try { localStorage.setItem(TOKEN_KEY, token) } catch { /* 隐私模式兜底 */ }
}
export function clearToken() {
  try { localStorage.removeItem(TOKEN_KEY) } catch { /* ignore */ }
}

// 统一请求：自动带 Authorization 头；401 清除本地 token（登录态失效）
async function request(path, options = {}, authorized = true) {
  const headers = { 'Content-Type': 'application/json', ...(options.headers || {}) };
  if (authorized && getToken()) headers.Authorization = `Bearer ${getToken()}`;
  let resp;
  try {
    resp = await fetch(`${BASE}${path}`, { ...options, headers });
  } catch {
    throw new Error(
      `无法连接后端服务（${BASE}）。请确认后端已启动：cd backend && source ../.venv/bin/activate && uvicorn app.main:app --reload`,
    );
  }
  if (resp.status === 401 && authorized) {
    clearToken();
    // 让上层跳回登录页（通过自定义错误标记，避免所有调用方重复判断）
    const err = new Error('登录已过期，请重新登录');
    err.unauthorized = true;
    throw err;
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

// ===== 认证 =====
export const login = (username, password) =>
  request('/api/auth/login', {
    method: 'POST',
    body: JSON.stringify({ username, password }),
  }, false);  // 登录接口不带 token

export const fetchMe = () => request('/api/auth/me');

export const createConversation = () =>
  request('/api/conversations', { method: 'POST', body: JSON.stringify({}) });

// SSE 流式发消息：Agent 每完成一个节点就通过 onNode 回调推送（执行链路实时跳动），
// 全部完成返回 done 事件（完整消息 + trace + 总耗时）。
export async function sendMessageStream(convId, content, image, mode, onNode, signal) {
  let resp;
  try {
    resp = await fetch(`${BASE}/api/conversations/${convId}/messages`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...(getToken() ? { Authorization: `Bearer ${getToken()}` } : {}),
      },
      body: JSON.stringify({ content, image: image ?? null, mode }),
      signal,  // 可取消：开新会话时 abort，旧请求立即断开
    });
  } catch {
    throw new Error(
      `无法连接后端服务（${BASE}）。请确认后端已启动：cd backend && source ../.venv/bin/activate && uvicorn app.main:app --reload`,
    );
  }
  if (resp.status === 401) {
    clearToken();
    const err = new Error('登录已过期，请重新登录');
    err.unauthorized = true;
    throw err;
  }
  if (!resp.ok || !resp.body) {
    let detail = '';
    try { detail = (await resp.json()).detail || '' } catch { /* 非 JSON 响应 */ }
    const hint = resp.status >= 500 ? '（后端处理出错，可能是模型限流，请稍后重试）' : '';
    throw new Error(`后端返回错误（${resp.status}）${detail ? `：${detail}` : ''}${hint}`);
  }

  // 逐块读取 SSE 流，按空行分割事件块，解析 event:/data: 两行
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buf = '';
  let donePayload = null;
  let streamError = null;

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf('\n\n')) >= 0) {
        const block = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        const data = block.split('\n').find((l) => l.startsWith('data:'))?.slice(5).trim();
        const event = block.split('\n').find((l) => l.startsWith('event:'))?.slice(6).trim();
        if (!data) continue;
        let payload;
        try {
          payload = JSON.parse(data);
        } catch {
          continue; // 单条数据损坏：跳过，不中断整个流
        }
        if (event === 'node') {
          onNode(payload)
        } else if (event === 'done') {
          donePayload = payload;
        } else if (event === 'error') {
          streamError = payload.message || 'Agent 执行失败';
        }
      }
    }
  } catch {
    // 流读取中断（网络抖动/后端异常）
    if (donePayload) return donePayload;
    throw new Error('Agent 响应中断（连接断开），请重试');
  }
  if (streamError) throw new Error(streamError);  // 后端明确报错：透传（如"执行失败，消息已保存"）
  if (!donePayload) throw new Error('Agent 响应中断，请重试');
  return donePayload;
}

export const fetchConversation = (convId) =>
  request(`/api/conversations/${convId}`);

export const fetchTraces = (convId) =>
  request(`/api/conversations/${convId}/traces`);

// 工单状态流转历史（状态机履历）
export const fetchStatusLogs = (convId) =>
  request(`/api/conversations/${convId}/status_logs`);

// ===== 👍/👎 反馈闭环 =====
export const submitFeedback = (msgId, feedback) =>
  request(`/api/messages/${msgId}/feedback`, {
    method: 'POST',
    body: JSON.stringify({ feedback }),
  });

export const fetchFeedbackAnalysis = () =>
  request('/api/feedback/analysis');

// ===== 知识库管理（台账/同步/蓝绿/检索调试）=====
export const fetchKbDocuments = () => request('/api/kb/documents');
export const fetchKbStats = () => request('/api/kb/stats');
export const syncKb = () => request('/api/kb/sync', { method: 'POST' });
export const switchKb = () => request('/api/kb/switch', { method: 'POST' });
export const debugKbQuery = (body) =>
  request('/api/kb/debug', { method: 'POST', body: JSON.stringify(body) });

// ===== 审计日志（admin only）=====
export const fetchAuditLogs = (params = {}) => {
  const qs = new URLSearchParams();
  Object.entries(params).forEach(([k, v]) => { if (v != null && v !== '') qs.set(k, v) });
  return request(`/api/audit/logs?${qs.toString()}`);
};
export const fetchAuditActions = () => request('/api/audit/actions');
