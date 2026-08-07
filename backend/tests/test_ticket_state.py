"""工单状态机单元测试（纯逻辑，零外部依赖）。

覆盖 ticket_state.py 的全部护栏规则：
- 合法转移（new→processing→resolved、→handoff、resolved→processing 重开）
- 非法转移拒绝（终态 handoff 不可回流、跳步转移）
- 未知状态防御
"""
import pytest

from app.services import ticket_state as ts


def test_legal_forward_transitions():
    """正向合法流转：new → processing → resolved。"""
    assert ts.can_transition(ts.STATUS_NEW, ts.STATUS_PROCESSING)
    assert ts.can_transition(ts.STATUS_PROCESSING, ts.STATUS_RESOLVED)
    # 不抛异常（assert_transition 是开发期校验）
    ts.assert_transition(ts.STATUS_NEW, ts.STATUS_PROCESSING)


def test_handoff_legal():
    """转人工合法路径：new/processing 都能转人工。"""
    assert ts.can_transition(ts.STATUS_NEW, ts.STATUS_HANDOFF)
    assert ts.can_transition(ts.STATUS_PROCESSING, ts.STATUS_HANDOFF)


def test_handoff_terminal_no_exit():
    """终态 handoff 不可回流（转人工后不回到 Agent）。"""
    assert not ts.can_transition(ts.STATUS_HANDOFF, ts.STATUS_PROCESSING)
    assert not ts.can_transition(ts.STATUS_HANDOFF, ts.STATUS_RESOLVED)
    with pytest.raises(ValueError):
        ts.assert_transition(ts.STATUS_HANDOFF, ts.STATUS_PROCESSING)


def test_skip_step_rejected():
    """跳步非法：new 直接 resolved（跳过处理中）应被拒。"""
    assert not ts.can_transition(ts.STATUS_NEW, ts.STATUS_RESOLVED)


def test_resolved_reopen():
    """闭环：resolved → processing（用户反馈"未解决"重新打开）。"""
    assert ts.can_transition(ts.STATUS_RESOLVED, ts.STATUS_PROCESSING)


def test_unknown_status_defensive():
    """未知状态：宁可拒绝不可放行。"""
    assert not ts.can_transition("open", ts.STATUS_PROCESSING)
    assert not ts.can_transition(ts.STATUS_NEW, "closed")
    assert not ts.is_valid("bogus")
    assert ts.is_valid(ts.STATUS_RESOLVED)


def test_is_valid_all_statuses():
    """全部合法状态都能通过 is_valid。"""
    for s in ts.VALID_STATUS:
        assert ts.is_valid(s)
