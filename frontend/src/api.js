// 后端 API 封装（fetch）。
// BASE 用相对路径（同源）——本地开发由 vite proxy 转发，Docker 由 nginx 反代，
// 代码零差异（生产标准做法：不用写死地址、无 CORS 问题）。
const BASE = import.meta.env.VITE_API_BASE || '';

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

// SSE 流式发消息：Agent 每完成一个节点就通过 onNode 回调推送（执行链路实时跳动），
// 全部完成返回 done 事件（完整消息 + trace + 总耗时）。
export async function sendMessageStream(convId, content, image, mode, onNode, signal) {
  let resp;
  try {
    resp = await fetch(`${BASE}/api/conversations/${convId}/messages`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content, image: image ?? null, mode }),
      signal,  // 可取消：开新会话时 abort，旧请求立即断开
    });
  } catch {
    throw new Error(
      `无法连接后端服务（${BASE}）。请确认后端已启动：cd backend && source ../.venv/bin/activate && uvicorn app.main:app --reload`,
    );
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
