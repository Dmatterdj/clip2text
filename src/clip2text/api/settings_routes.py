"""设置与用量接口（契约 docs/SETTINGS-CONTRACT.md §6）。

安全上是这个文件最敏感：key 只进不出。
    · GET 只给 `mask_key()` 之后的值，原文永不进入响应体
    · 所有 `log.*` 都只打「哪个分节被改了」「key 设了没有」，不打值
    · 写入走 settings_store.save_overrides()，落盘 0600

热更新：写完后 `AppState.reload_settings()`，同一进程内立刻生效，
不需要重启（契约 §3）。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

from ..config import env_settings, settings_file
from ..models import SettingsView, UsageSummary
from ..settings_store import (
    SECTION_DEEPSEEK,
    SECTION_LLM,
    SECTION_PRICING,
    deepseek_view,
    llm_view,
    load_overrides,
    mask_key,
    save_overrides,
    validate_api_key,
    validate_base_url,
    validate_model,
    validate_pricing_value,
)
from ..state import get_state
from ..usage import DEFAULT_DAYS, MAX_DAYS, build_summary

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1")


# ---------------------------------------------------------------- 组装视图
def _current_overrides() -> dict[str, Any]:
    # 用 AppState 的 data_dir 而不是 get_settings() 的：测试会注入临时目录，
    # 生产里两者相同，但测试里差一点就会写进真实的 ./.data。
    return load_overrides(get_state().settings.data_dir)


def _build_view() -> SettingsView:
    st = get_state()
    # 先同步（外部改了 settings.json 也能反映），再读两份配置：
    # effective 是生效值，env 是「只吃 .env」的基线，用后者判断
    # key 的真实来源（界面写入 / 环境变量 / 没配）。
    st.sync_settings()
    effective = st.settings
    env = env_settings()
    overrides = load_overrides(effective.data_dir)
    analysis = st.analysis_config()
    return SettingsView(
        llm=llm_view(effective, env, overrides),
        deepseek=deepseek_view(effective, env, overrides),
        analysis_ready=analysis.configured,
        config_path=str(settings_file(effective.data_dir)),
    )


def _apply(new_overrides: dict[str, Any], *, reason: str) -> SettingsView:
    """落盘 + 热更新 + 返回新视图。"""
    st = get_state()
    path = save_overrides(st.settings.data_dir, new_overrides)
    st.reload_settings()
    # 只记「改了哪」，绝不记值
    log.info("设置已保存：%s（%s）→ %s", reason, "已生效", path)
    return _build_view()


def _bad_request(exc: ValueError) -> HTTPException:
    return HTTPException(400, str(exc))


def _test_result(ok: bool, target: str, reason: str = "") -> dict[str, Any]:
    """测试连接的统一返回体。

    同时给 `reason` 和 `detail`：`detail` 与项目其它错误响应的字段名一致
    （前端读的是 `res.detail`），`reason` 与 capabilities 里的字段名一致。
    两个都带，前端不需要为这个接口写特例。
    """
    out: dict[str, Any] = {"ok": ok, "target": target, "reason": reason}
    if reason:
        out["detail"] = reason
    return out


def _redact_secret(text: str, secret: str) -> str:
    """把文本里的密钥替换成掩码。

    为什么需要这一层：`POST /settings/test` 会把上游的报错原文带回前端，
    而某些网关的错误 body 会回显收到的 Authorization。虽然主流服务不会，
    但「key 永不进入响应体」是硬约束，不能寄希望于上游。
    """
    if not secret or len(secret) < 8:
        return text
    return text.replace(secret, mask_key(secret))


# ---------------------------------------------------------------- 读取
@router.get("/settings", response_model=SettingsView, summary="读取配置（key 掩码）")
def get_settings_view() -> SettingsView:
    return _build_view()


# ---------------------------------------------------------------- LLM
@router.put("/settings/llm", response_model=SettingsView, summary="写入 LLM 配置")
def put_llm_settings(payload: dict[str, Any]) -> SettingsView:
    overrides = _current_overrides()
    block = dict(overrides.get(SECTION_LLM) or {})

    base_url_raw = payload.get("base_url")
    model_raw = payload.get("model")
    api_key_raw = payload.get("api_key")

    # 空字符串 / None / 不传 = 保持原值（契约 §6）；要清除用 DELETE
    if isinstance(base_url_raw, str) and base_url_raw.strip():
        try:
            block["base_url"] = validate_base_url(base_url_raw)
        except ValueError as exc:
            raise _bad_request(exc) from None
    if isinstance(model_raw, str) and model_raw.strip():
        try:
            block["model"] = validate_model(model_raw)
        except ValueError as exc:
            raise _bad_request(exc) from None
    if isinstance(api_key_raw, str) and api_key_raw.strip():
        try:
            block["api_key"] = validate_api_key(api_key_raw)
        except ValueError as exc:
            raise _bad_request(exc) from None

    # 三个都空 = 用户什么都没填，不该悄悄把文件写成空分节
    if not block:
        raise HTTPException(400, "没有可保存的字段（base_url / api_key / model 至少填一个）")

    # 保存后立刻校验整体可用性：只填了 key 却没 base_url/model 时，
    # 界面上会显示「已保存但不可用」，用户能看出还差什么。
    if not (block.get("base_url") and block.get("model")):
        log.info("LLM 配置已保存，但 base_url / model 仍缺失，分析暂不可用")

    overrides[SECTION_LLM] = block
    return _apply(overrides, reason="llm")


@router.delete("/settings/llm", response_model=SettingsView, summary="清除 LLM 配置")
def delete_llm_settings() -> SettingsView:
    overrides = _current_overrides()
    overrides.pop(SECTION_LLM, None)
    return _apply(overrides, reason="llm 已清除（回落到环境变量）")


# ---------------------------------------------------------------- DeepSeek
@router.put(
    "/settings/deepseek", response_model=SettingsView, summary="写入 DeepSeek 配置"
)
def put_deepseek_settings(payload: dict[str, Any]) -> SettingsView:
    overrides = _current_overrides()
    block = dict(overrides.get(SECTION_DEEPSEEK) or {})

    api_key_raw = payload.get("api_key")
    base_url_raw = payload.get("base_url")

    if isinstance(api_key_raw, str) and api_key_raw.strip():
        try:
            block["api_key"] = validate_api_key(api_key_raw)
        except ValueError as exc:
            raise _bad_request(exc) from None
    if isinstance(base_url_raw, str) and base_url_raw.strip():
        try:
            block["base_url"] = validate_base_url(base_url_raw)
        except ValueError as exc:
            raise _bad_request(exc) from None

    if not block:
        raise HTTPException(400, "没有可保存的字段（api_key 不能为空）")

    overrides[SECTION_DEEPSEEK] = block
    return _apply(overrides, reason="deepseek")


@router.delete(
    "/settings/deepseek", response_model=SettingsView, summary="清除 DeepSeek 配置"
)
def delete_deepseek_settings() -> SettingsView:
    overrides = _current_overrides()
    overrides.pop(SECTION_DEEPSEEK, None)
    return _apply(overrides, reason="deepseek 已清除（回落到环境变量）")


# ---------------------------------------------------------------- 单价
@router.put("/settings/pricing", response_model=SettingsView, summary="修改单价")
def put_pricing_settings(payload: dict[str, Any]) -> SettingsView:
    """单价也是「估算」的一部分，用户可改。

    支持两种形状：
      · 直接给四个键：{"asr_per_hour": 0.78, ...}
      · 包一层：{"pricing": {...}}（前端表单整体提交时更自然）
    """
    raw = payload.get("pricing", payload)
    if not isinstance(raw, dict):
        raise HTTPException(400, "pricing 必须是对象")

    overrides = _current_overrides()
    block = dict(overrides.get(SECTION_PRICING) or {})
    for name, value in raw.items():
        if not isinstance(name, str):
            continue
        try:
            block[name] = validate_pricing_value(name, value)
        except ValueError as exc:
            raise _bad_request(exc) from None

    if not block:
        raise HTTPException(400, "没有可保存的单价")

    overrides[SECTION_PRICING] = block
    return _apply(overrides, reason="pricing")


# ---------------------------------------------------------------- 测试连接
@router.post("/settings/test", summary="测试当前配置是否可用")
async def test_settings(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """发一次最小的 chat 请求验证配置（契约里是可选项，但实现它成本很低）。

    只读当前生效配置，**不写入**。任何失败都返回 ok=false + 人话原因，
    而不是 500 —— 测试连接失败是正常业务结果，不是服务器错误。
    """
    from ..analysis import chat_completion

    st = get_state()
    target = str((payload or {}).get("target") or "llm").lower()
    st.sync_settings()

    if target == "deepseek":
        llm = st.analysis_config()
        config = llm.config
        if config is None:
            return _test_result(False, target, llm.reason or "未配置")
    else:
        config = st.llm
        if not config.configured:
            return _test_result(False, target, "未配置 base_url / model")

    try:
        _, usage = await chat_completion(
            config, [{"role": "user", "content": "ping"}], temperature=0.0
        )
    except Exception as exc:  # noqa: BLE001 - 连接失败是预期结果之一
        # 上游错误 body 可能回显 key（有些网关会原样回显 Authorization）。
        # 我们在本地再兜一层：把当前配置里的 key 替换成掩码后再返回。
        detail = _redact_secret(str(exc), config.api_key)[:200]
        log.info("配置测试失败：target=%s type=%s", target, type(exc).__name__)
        return _test_result(False, target, detail)

    return {
        "ok": True,
        "target": target,
        "model": config.model,
        "usage": usage,
    }


@router.post("/settings/models", summary="列出这条 key 能用的模型")
def list_available_models(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """用给定的（或当前生效的）配置去拉 `GET /v1/models`。

    为什么需要这个端点：一个 key 底下往往挂着多个模型，
    用户不可能记得住模型名。以前只能手填，填错就在调用时报 400。

    请求体可传 `base_url` / `api_key` 覆盖当前配置 —— 这样用户在填表单
    的过程中就能「先拉到列表再选」，不必先保存一份错误的配置。
    不传则用当前生效配置。

    返回 ok=false 而不是 500：拉不到列表是正常业务结果（key 没填、
    服务不支持 /v1/models 等），前端据此提示「请手工填写模型名」。
    """
    from ..llm import LLMError, list_models

    st = get_state()
    st.sync_settings()
    body = payload or {}

    base_url = str(body.get("base_url") or "").strip()
    api_key = str(body.get("api_key") or "").strip()

    if not base_url or not api_key:
        # 回落到当前生效配置
        current = st.llm if st.llm.configured else (
            st.analysis_config().config
        )
        if current is not None:
            base_url = base_url or current.base_url
            api_key = api_key or current.api_key

    if not base_url:
        return {"ok": False, "models": [], "reason": "请先填写 Base URL"}
    if not api_key:
        return {"ok": False, "models": [], "reason": "请先填写 API Key"}

    try:
        models = list_models(base_url, api_key)
    except LLMError as exc:
        reason = _redact_secret(str(exc), api_key)[:300]
        log.info("拉取模型列表失败：%s", type(exc).__name__)
        return {"ok": False, "models": [], "reason": reason}
    except Exception as exc:  # noqa: BLE001 - 网络/解析异常都算拉取失败
        log.warning("拉取模型列表异常：%s", type(exc).__name__)
        return {
            "ok": False,
            "models": [],
            "reason": _redact_secret(f"{type(exc).__name__}: {exc}", api_key)[:300],
        }

    log.info("拉取模型列表成功：%d 个（base_url=%s）", len(models), base_url)
    return {
        "ok": True,
        "models": [m.model_dump() for m in models],
        # 告诉前端「当前生效的模型是哪个」，方便下拉预选
        "current": st.llm.model or (st.analysis_config().config.model if st.analysis_config().config else ""),
    }


# ---------------------------------------------------------------- 用量
@router.get("/usage", response_model=UsageSummary, summary="用量汇总（估算）")
def get_usage(days: int = Query(DEFAULT_DAYS, ge=1, le=MAX_DAYS, description="最近 N 天")) -> UsageSummary:
    st = get_state()
    st.sync_settings()
    # 单价显式取自当前 AppState，而不是让 build_summary 去读全局
    # get_settings()：测试注入的临时 data_dir 与全局那份可能不是同一个。
    return build_summary(st.db, days=days, pricing=st.settings.pricing)


@router.delete("/usage", status_code=204, summary="清空用量记录")
def delete_usage() -> Response:
    """清空账本。**不**随删任务触发，只有用户显式点「清空」才删。"""
    get_state().db.delete_all_usage()
    log.info("用量记录已由用户清空")
    return Response(status_code=204)


__all__ = ["router"]
