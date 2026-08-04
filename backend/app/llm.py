"""LLM 客户端：用 openai SDK 指向智谱 GLM 的 OpenAI 兼容端点。

设计要点：
- 模型无关：换 DeepSeek / OpenAI / 本地模型，只改 .env，代码零改动
- 故障转移（failover）：模型链按优先级排列，限流/连接失败/模型不存在
  时【立即切换下一个模型】，不再 sleep 退避白等——免费模型限流的现实解法
"""
import json
import re
import threading
import time

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    OpenAI,
)

from app.config import settings

client = OpenAI(
    api_key=settings.ZHIPU_API_KEY,
    base_url=settings.GLM_BASE_URL,
)

# 进程级调用计数器（Eval 报告/审计用）
CALL_COUNT = 0
# 最近一次实际使用的模型（可观测性）
LAST_MODEL = None

# 触发切换的异常：网络/超时/所有 HTTP 错误（429 限流、403 无权限、
# 404 模型不存在、5xx 服务端错误都算"模型不可用"→ 切下一个）
_FAILOVER_EXCEPTIONS = (APIConnectionError, APITimeoutError, APIStatusError)

# ===== 故障转移的「记忆」（解决"每次都从链头重新踩限流"）=====
# _TRY_MODEL：最后尝试/成功的模型名，下次从它在链中的位置开始（避开仍在限流的链头）
# 用「模型名」而非「链内索引」：速度/能力两套链长度不同，索引会串位（实测 bug）
# 429 是临时限流 → 游标避让；403/404 是账号级问题 → 拉黑更久，避免反复尝试
_TRY_MODEL = ""
_TRY_CHAIN = None            # 上次尝试的链（tuple）；跨链切换时重置游标
_CURSOR_AT = 0.0
_CURSOR_TTL = 300          # 秒：游标有效期内不回链头探测（限流通常分钟级）
_BLACKLIST_TTL = 3600      # 秒：403/404 账号级故障拉黑 1 小时
_RATE_TTL = 60             # 秒：429/5xx 临时故障短拉黑（1 分钟后自动重试探测）
_BLACKLISTED: dict[str, float] = {}   # model → 拉黑截止时间戳（账号级问题全局共享）
# 游标/黑名单是进程级共享状态，多请求并发访问需加锁（只锁状态读写，不锁网络调用）
_LLM_LOCK = threading.Lock()


def _pick_chain(model_chain: list[str], now: float) -> list[str]:
    """生成本次尝试顺序：游标避让 + 黑名单过滤 + TTL 到期回链头探测。

    游标按链独立：前端切换「速度/准确」时链变了，应回到新链的链头
    （fast 链永远是 glm-4-flash 优先），而不是继承另一条链的游标位置。
    """
    global _TRY_MODEL, _TRY_CHAIN
    with _LLM_LOCK:
        key = tuple(model_chain)
        if _TRY_CHAIN != key:
            _TRY_MODEL, _TRY_CHAIN = "", key  # 换链 → 重置游标
        if _TRY_MODEL and now - _CURSOR_AT > _CURSOR_TTL:
            _TRY_MODEL = ""  # 定期回链头，防止模型恢复后一直被跳过
        if _TRY_MODEL in model_chain:
            idx = model_chain.index(_TRY_MODEL)
            ordered = model_chain[idx:] + model_chain[:idx]
        else:
            ordered = model_chain
    alive = [m for m in ordered if _BLACKLISTED.get(m, 0) <= now]
    return alive or model_chain  # 全被拉黑就硬试（总比报错强）


def _create(model_chain: list[str], **kwargs) -> tuple[object, str]:
    """按模型链调用，失败立即切换下一个。全部失败抛最后一个错误。

    返回 (resp, used_model)：used_model 是本次调用实际使用的模型名，
    供调用方做精准拉黑等后续操作（不要用全局 LAST_MODEL，并发下会读串）。

    记忆策略（本次优化）：
    - 游标：成功后记住位置，下次直接从那开始——限流高峰不再每次白踩链头 429
    - 拉黑：403/404 是账号/模型级错误，不是临时限流，进程内拉黑 1 小时
    """
    global CALL_COUNT, LAST_MODEL, _TRY_MODEL, _CURSOR_AT
    last_error = None
    chain = _pick_chain(model_chain, time.time())
    for model in chain:
        try:
            resp = client.chat.completions.create(model=model, **kwargs)
            with _LLM_LOCK:
                CALL_COUNT += 1
                LAST_MODEL = model
                _TRY_MODEL = model          # 记住成功位置
                _CURSOR_AT = time.time()
                _BLACKLISTED.pop(model, None)  # 能成功说明恢复了，解除拉黑
            return resp, model
        except _FAILOVER_EXCEPTIONS as e:  # noqa: PERF203
            last_error = e
            with _LLM_LOCK:
                _TRY_MODEL = model          # 记住位置；拉黑让它从下一个开始
                _CURSOR_AT = time.time()
                if isinstance(e, APIStatusError) and e.status_code in (403, 404):
                    _BLACKLISTED[model] = time.time() + _BLACKLIST_TTL
                    print(f"[llm] 模型 {model} 拉黑（{e.status_code} 账号/模型级错误）")
                else:
                    # 429/5xx 临时故障：短拉黑，避免同一请求内外的反复踩
                    _BLACKLISTED[model] = time.time() + _RATE_TTL
                    print(f"[llm] 模型 {model} 不可用({type(e).__name__})，切换下一个")
            continue
    # 全部失败。模型链为空时 last_error 是 None，不能直接 raise（会抛 TypeError）
    raise last_error or RuntimeError("模型链为空，无法调用 LLM")


def chat(messages: list[dict], temperature: float = 0.3) -> str:
    """最简对话封装。后续 LangGraph Agent 会基于它扩展。"""
    resp, _ = _create(
        settings.GLM_MODELS,
        messages=messages,
        temperature=temperature,
    )
    return resp.choices[0].message.content


def chat_json(messages: list[dict], temperature: float = 0.1, model_chain: list[str] | None = None) -> dict:
    """让模型输出 JSON 并解析（带容错 + 空内容重试）。

    用 response_format 强制 JSON 模式，再兜底清洗：
    即使模型偶尔返回 ```json {...} ``` 或夹带废话，也能解析出字典。
    max_tokens=256：结构化输出只需少量 token（intent/extract 都 <100），
    限制输出长度能防止模型写长篇 reason 拖慢响应（限流时段更敏感）。

    model_chain：可选覆盖（前端"速度/准确"切换传 GLM_MODELS_FAST）；None = 默认能力链。

    重试：免费模型高峰会【间歇性返回空内容】（实测遇到），此时 JSON 解析必失败。
    与其直接兜底转人工，不如重试一次——游标已前进，下次会换到下一个模型，
    大概率拿到正常结果。最多 2 次调用，成本可控。
    """
    chain = model_chain or settings.GLM_MODELS

    def _call() -> tuple[object, str]:
        """调用一次，返回 (resp, used_model)——used_model 是本次实际用的模型。"""
        return _create(
            chain,
            messages=messages,
            temperature=temperature,
            max_tokens=256,
            response_format={"type": "json_object"},
        )

    def _parse(raw: str) -> dict | None:
        """解析 JSON；失败时剥离 markdown 代码块再试。返回 None = 无法解析。"""
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            m = re.search(r"\{.*\}", raw, re.S)
            if m:
                try:
                    return json.loads(m.group())
                except json.JSONDecodeError:
                    return None
            return None

    resp, used_model = _call()
    raw = (resp.choices[0].message.content or "").strip()
    parsed = _parse(raw)
    if parsed is not None:
        return parsed
    # 解析失败（空内容或垃圾内容，免费模型高峰的已知坑）：
    # HTTP 200 成功返回但内容异常 → 该模型没被 API 层拉黑，游标仍在它身上。
    # 这里用【本次实际使用的模型】短拉黑 60s，重试时 _pick_chain 才会真正跳过它。
    # （不能用全局 LAST_MODEL：并发下可能读到别的请求刚用的模型，拉黑打偏）
    with _LLM_LOCK:
        _BLACKLISTED[used_model] = time.time() + _RATE_TTL
    print(f"[llm] 模型 {used_model} 返回异常内容({raw[:40]!r})，短拉黑并重试（换下一个模型）")
    resp, used_model = _call()
    raw = (resp.choices[0].message.content or "").strip()
    parsed = _parse(raw)
    if parsed is not None:
        return parsed
    raise ValueError(f"模型未返回合法 JSON: {raw[:200]}")


def chat_with_image(image_data_url: str, prompt: str) -> str:
    """带图片调用视觉模型（OCR 用）。

    image_data_url: 完整 data URL（如 data:image/png;base64,xxx）。
    注意：MIME 由 data URL 自带（png/jpeg/webp 都支持），不能写死。
    OpenAI 兼容格式：content 是 [文本, 图片] 列表。
    """
    resp, _ = _create(
        settings.GLM_VISION_MODELS,
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": image_data_url}},
            ],
        }],
    )
    return resp.choices[0].message.content
