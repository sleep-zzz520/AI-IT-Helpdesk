"""用户自定义 LLM 配置 API。"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from openai import APIConnectionError, APIStatusError, APITimeoutError
from sqlalchemy.orm import Session

from app.models import LLMConfig, User
from app.schemas import LLMConfigCreate, LLMConfigOut, LLMConnectionTestOut
from app.security import get_current_user, get_db
from app.services.audit_service import audit
from app.services.llm_config_service import (
    LLMConfigSecretError,
    encrypt_api_key,
    profile_from_values,
    test_connection,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/llm-configs", tags=["llm-configs"])


def _out(config: LLMConfig) -> dict:
    """白名单响应，防止密文或明文 API Key 被误回传。"""
    return {
        "id": config.id,
        "name": config.name,
        "base_url": config.base_url,
        "model": config.model,
        "json_mode": config.json_mode,
        "has_api_key": bool(config.api_key_ciphertext),
        "created_at": config.created_at.isoformat() if config.created_at else None,
        "updated_at": config.updated_at.isoformat() if config.updated_at else None,
    }


def get_owned_config_or_404(db: Session, config_id: int, user: User) -> LLMConfig:
    config = db.get(LLMConfig, config_id)
    # 返回 404，避免向其他用户泄露某个配置是否存在。
    if config is None or config.owner_user_id != user.id:
        raise HTTPException(status_code=404, detail="模型配置不存在")
    return config


@router.get("", response_model=list[LLMConfigOut])
def list_llm_configs(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    rows = (db.query(LLMConfig)
            .filter_by(owner_user_id=user.id)
            .order_by(LLMConfig.updated_at.desc(), LLMConfig.id.desc())
            .all())
    return [_out(row) for row in rows]


@router.post("", response_model=LLMConfigOut, status_code=status.HTTP_201_CREATED)
def create_llm_config(
    body: LLMConfigCreate,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        ciphertext = encrypt_api_key(body.api_key)
    except LLMConfigSecretError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    duplicate = (db.query(LLMConfig)
                 .filter_by(owner_user_id=user.id, name=body.name)
                 .first())
    if duplicate is not None:
        raise HTTPException(status_code=409, detail="你已使用这个配置名称")

    config = LLMConfig(
        owner_user_id=user.id,
        name=body.name,
        base_url=body.base_url,
        model=body.model,
        api_key_ciphertext=ciphertext,
        json_mode=body.json_mode,
    )
    db.add(config)
    db.commit()
    db.refresh(config)
    # 审计只记录安全的元数据，绝不记录 API Key 或密文。
    audit(db, user, "create_llm_config", {
        "llm_config_id": config.id,
        "name": config.name,
        "model": config.model,
    }, request=request)
    return _out(config)


@router.post("/test", response_model=LLMConnectionTestOut)
def test_llm_config(
    body: LLMConfigCreate,
    user: User = Depends(get_current_user),
):
    """测试未保存的输入，不会产生数据库记录或审计中的密钥痕迹。"""
    profile = profile_from_values(
        name=body.name,
        base_url=body.base_url,
        model=body.model,
        api_key=body.api_key,
        json_mode=body.json_mode,
    )
    try:
        latency_ms = test_connection(profile)
    except APIStatusError as exc:
        # 保留状态码方便排查授权/模型名，响应体可能含敏感上下文，不能透传。
        code = exc.status_code or "未知"
        raise HTTPException(status_code=400, detail=f"模型服务拒绝了连接（HTTP {code}）") from exc
    except (APIConnectionError, APITimeoutError) as exc:
        raise HTTPException(status_code=400, detail="无法连接模型服务或请求超时") from exc
    except Exception as exc:
        logger.warning("用户 %s 测试模型配置失败: %s", user.username, type(exc).__name__)
        raise HTTPException(status_code=400, detail="模型连接失败，请检查地址、模型名和 API Key") from exc
    return {"ok": True, "latency_ms": latency_ms}


@router.delete("/{config_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_llm_config(
    config_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    config = get_owned_config_or_404(db, config_id, user)
    name, model = config.name, config.model
    db.delete(config)
    db.commit()
    audit(db, user, "delete_llm_config", {
        "llm_config_id": config_id,
        "name": name,
        "model": model,
    }, request=request)
