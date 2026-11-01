"""真实本地 HTTP/SSE + SQLite；可控慢 Agent 替身，测过载保护，非真实 LLM 容量。

python -m scripts.benchmark_agent_http --requests 100 --concurrency 100 --report /tmp/http.json
加 --baseline 仅在本实验进程关闭准入，对照相同的模拟上游并发预算。
"""
import argparse
import asyncio
import json
import logging
import os
import platform
import socket
import tempfile
import threading
import time
from collections import Counter
from pathlib import Path

os.environ["ZHIPU_API_KEY"] = "offline-benchmark"
os.environ["MONITOR_MODE"] = "mock"

import httpx
import uvicorn
from fastapi import FastAPI
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app import db, security
from app.api import conversations
from app.config import settings
from app.models import AgentRequest, Conversation, Tenant, User
from app.services.agent_admission import Lease
from app.services.agent_ingress import AgentIngressMiddleware


def percentile(values, fraction):
    values = sorted(values)
    return round(values[min(len(values) - 1, int((len(values) - 1) * fraction))], 2) if values else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requests", type=int, default=100)
    parser.add_argument("--concurrency", type=int, default=100)
    parser.add_argument("--capacity", type=int, default=8)
    parser.add_argument("--pool-size", type=int, default=16)
    parser.add_argument("--provider-seconds", type=float, default=0.15)
    parser.add_argument("--baseline", action="store_true")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    logging.getLogger("app.api.conversations").setLevel(logging.CRITICAL)
    if min(args.requests, args.concurrency, args.capacity) < 1 or args.provider_seconds < 0:
        parser.error("请求数、并发和容量必须为正数")
    root = Path(tempfile.mkdtemp(prefix="agent_http_benchmark_"))
    settings.AGENT_LOCK_DIR = str(root / "locks")
    settings.AGENT_MAX_CONCURRENT = args.capacity
    engine = create_engine(f"sqlite:///{root / 'benchmark.sqlite3'}",
                           connect_args={"check_same_thread": False, "timeout": 15},
                           pool_size=args.pool_size, max_overflow=0, pool_timeout=2)

    @event.listens_for(engine, "connect")
    def configure_sqlite(connection, _record):
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=15000")

    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db.Base.metadata.create_all(engine)
    with factory() as session:
        tenant = Tenant(code="benchmark", name="合成测评")
        session.add(tenant)
        session.flush()
        user = User(username="benchmark", password_hash="unused", tenant_id=tenant.id, active=1)
        session.add(user)
        session.flush()
        token = security.create_token(user.id)
        convs = [Conversation(user_id=user.username, tenant_id=tenant.id) for _ in range(args.requests)]
        session.add_all(convs)
        session.commit()
        ids = [conv.id for conv in convs]

    app = FastAPI()
    if not args.baseline:
        app.add_middleware(AgentIngressMiddleware)
    app.include_router(conversations.router)

    def get_db():
        with factory() as session:
            yield session

    app.dependency_overrides[conversations.get_db] = get_db
    app.dependency_overrides[security.get_db] = get_db
    if args.baseline:
        conversations.acquire_slot = lambda: Lease([])
        conversations.acquire_conversation = lambda *_: Lease([])

    stats = {"active": 0, "peak": 0, "provider_overload": 0, "agent_calls": 0}
    guard = threading.Lock()

    def slow_agent(state):
        with guard:
            stats["agent_calls"] += 1
            stats["active"] += 1
            stats["peak"] = max(stats["peak"], stats["active"])
            overloaded = stats["active"] > args.capacity
            if overloaded:
                stats["provider_overload"] += 1
        try:
            if overloaded:
                raise RuntimeError("synthetic provider concurrency exceeded")
            time.sleep(args.provider_seconds)
            trace = {"node": "greeting", "result": {"reply": "合成成功"}}
            final = {**state, "trace": [trace], "messages": [*state["messages"],
                     {"role": "assistant", "content": "合成成功"}]}
            yield "node", [trace], final
            yield "done", final, round(args.provider_seconds * 1000)
        finally:
            with guard:
                stats["active"] -= 1

    conversations._run_agent_stream = slow_agent
    # 端口只绑定loopback，由操作系统分配；不运行生产lifespan和迁移。
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="critical", lifespan="off"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    ready_deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > ready_deadline:
            raise RuntimeError("local benchmark server did not start")
        time.sleep(0.01)

    async def exercise():
        semaphore = asyncio.Semaphore(args.concurrency)
        limits = httpx.Limits(max_connections=args.concurrency, max_keepalive_connections=args.concurrency)
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", limits=limits,
                                     timeout=30, trust_env=False) as client:
            async def one(index, conv_id):
                async with semaphore:
                    start = time.perf_counter()
                    try:
                        response = await client.post(f"/api/conversations/{conv_id}/messages",
                            headers={"Authorization": f"Bearer {token}"},
                            json={"content": "合成负载", "request_id": f"benchmark-{index:08d}"})
                        outcome = ("done" if "event: done" in response.text else
                                   "agent_error" if "event: error" in response.text else str(response.status_code))
                        return {"outcome": outcome, "latency_ms": (time.perf_counter() - start) * 1000}
                    except httpx.HTTPError as exc:
                        return {"outcome": type(exc).__name__, "latency_ms": (time.perf_counter() - start) * 1000}
            return await asyncio.gather(*(one(i, cid) for i, cid in enumerate(ids)))

    started = time.perf_counter()
    try:
        results = asyncio.run(exercise())
    finally:
        server.should_exit = True
        thread.join(timeout=20)
        sock.close()
    elapsed = time.perf_counter() - started
    with factory() as session:
        statuses = Counter(row.status for row in session.query(AgentRequest).all())
    report = {
        "scope": "local HTTP + real auth/SSE/SQLite; synthetic slow Agent, no external API; not production capacity",
        "mode": "baseline_admission_disabled" if args.baseline else "bounded_admission",
        "environment": {"platform": platform.platform(), "python": platform.python_version(),
                        "cpu_count": os.cpu_count(), "workers": 1, "database": "temporary SQLite WAL",
                        "pool_size": args.pool_size},
        "requests": args.requests, "client_concurrency": args.concurrency,
        "admission_capacity": args.capacity, "synthetic_provider_budget": args.capacity,
        "synthetic_provider_seconds": args.provider_seconds,
        "elapsed_seconds": round(elapsed, 3),
        "outcomes": dict(Counter(item["outcome"] for item in results)),
        "execution": stats, "request_ledger": dict(statuses),
        "latencies_by_outcome_ms": {},
    }
    for outcome in report["outcomes"]:
        values = [r["latency_ms"] for r in results if r["outcome"] == outcome]
        report["latencies_by_outcome_ms"][outcome] = {
            "p50": percentile(values, .5), "p95": percentile(values, .95), "p99": percentile(values, .99)}
    if not args.baseline:
        report["checks"] = {
            "bounded_execution": stats["peak"] <= args.capacity,
            "no_provider_overload": stats["provider_overload"] == 0,
            "only_success_or_explicit_rejection": set(report["outcomes"]) <= {"done", "503"},
            "no_inflight_work_remaining": stats["active"] == 0 and not thread.is_alive(),
            "all_admitted_requests_completed": stats["agent_calls"] == report["outcomes"].get("done", 0),
        }
        report["checks_passed"] = all(report["checks"].values())
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    engine.dispose()
    if not args.baseline and not report["checks_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
