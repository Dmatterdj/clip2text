"""`.data/settings.json` 的读写、覆盖与掩码。

为什么单独一个文件
    配置有三层来源：代码默认值 < .env/环境变量 < settings.json（界面写入）。
    这里只负责第三层：读写那个 JSON 文件、把它「纵深覆盖」到 Settings 上、
    以及把 key 掩码后交给前端。不碰 pydantic-settings 的 source 顺序
    （改 source 顺序要 monkeypatch 内部实现，脆弱且难测），
    而是在 `config.get_settings()` 里显式 apply 一次。

安全约定（契约 §2，不可协商）
    · key 永不完整返回：`mask_key()` 是唯一出口
    · 文件权限 0600：创建即 0600，已存在则写完再 chmod
    · 本模块**任何日志都不打印字段值**，只打印「保存了哪个分节」

契约：docs/SETTINGS-CONTRACT.md
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # 只为类型标注：config 会 import 本模块，不能反向运行时依赖
    from .config import Settings

log = logging.getLogger(__name__)

SETTINGS_FILENAME = "settings.json"
FILE_MODE = 0o600

# 掩码：固定 8 个中点，不随原文长度变化（否则长度本身就是信息）
MASK_CHAR = "·"
MASK_LEN = 8
FULL_MASK = MASK_CHAR * MASK_LEN
MASK_HEAD = 6
MASK_TAIL = 4
# 短于这个长度的 key 一律全掩码：保留前后各若干字符等于把整个 key 拼出来
MASK_MIN_LEN = 18  # 6 头 + 8 藏 + 4 尾，任何情况下都不该比这更短
MASK_REVEAL_RATIO = 3  # 露出的 10 个字符不能超过总长的 1/3

MAX_KEY_LEN = 500
MAX_MODEL_LEN = 100
MAX_BASE_URL_LEN = 500

# settings.json 的顶层分节。其它键一律忽略（防止手工编辑的文件带坏配置）
SECTION_LLM = "llm"
SECTION_DEEPSEEK = "deepseek"
SECTION_PRICING = "pricing"
SECTIONS = (SECTION_LLM, SECTION_DEEPSEEK, SECTION_PRICING)

# settings.json 字段名 → Settings 属性名
LLM_FIELD_MAP = {
    "base_url": "llm_base_url",
    "api_key": "llm_api_key",
    "model": "llm_model",
}
DEEPSEEK_FIELD_MAP = {
    "api_key": "deepseek_api_key",
    "base_url": "deepseek_base_url",
}


# ---------------------------------------------------------------- 路径与读写
def settings_path(data_dir: Path | str) -> Path:
    """settings.json 与数据库同目录（.data 已在 .gitignore 内）。"""
    return Path(data_dir) / SETTINGS_FILENAME


def load_overrides(data_dir: Path | str) -> dict[str, Any]:
    """读 settings.json。

    任何异常都只 warning 并返回空 dict：一个手抖改坏的配置文件不该让
    整个服务起不来（此时会退回 .env，用户还能从界面上重新保存）。
    """
    path = settings_path(data_dir)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        log.warning("读取 %s 失败（已忽略，退回环境变量）：%s", path, exc)
        return {}

    try:
        data = json.loads(raw)
    except ValueError as exc:
        log.warning("%s 不是合法 JSON（已忽略，退回环境变量）：%s", path, exc)
        return {}
    if not isinstance(data, dict):
        log.warning("%s 顶层不是对象（已忽略，退回环境变量）", path)
        return {}
    return _sanitize(data)


def _sanitize(data: dict[str, Any]) -> dict[str, Any]:
    """只保留认识的分节和字段，且丢掉空值。

    空字符串在语义上等于「没配置」：写空等于删掉，避免出现
    `llm.api_key=""` 这种既不是「有值」也不是「没有」的第三种状态。
    """
    out: dict[str, Any] = {}

    for section, mapping in ((SECTION_LLM, LLM_FIELD_MAP), (SECTION_DEEPSEEK, DEEPSEEK_FIELD_MAP)):
        block = data.get(section)
        if not isinstance(block, dict):
            continue
        clean = {}
        for name in mapping:
            value = block.get(name)
            if isinstance(value, str) and value.strip():
                clean[name] = value.strip()
        if clean:
            out[section] = clean

    pricing = data.get(SECTION_PRICING)
    if isinstance(pricing, dict):
        clean_pricing: dict[str, float] = {}
        for name, value in pricing.items():
            if not isinstance(name, str):
                continue
            number = _as_price(value)
            if number is not None:
                clean_pricing[name] = number
        if clean_pricing:
            out[SECTION_PRICING] = clean_pricing

    return out


def _as_price(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    if number != number or number in (float("inf"), float("-inf")):  # NaN / inf
        return None
    return number if number >= 0 else None


def save_overrides(data_dir: Path | str, overrides: dict[str, Any]) -> Path:
    """把 overrides 落盘，权限 0600。

    · 文件不存在：`os.open(..., 0o600)` 创建时就是 0600
    · 文件已存在：open 的 mode 参数**不会**改已有文件的权限，所以写完再 chmod
    两步都做，才能同时覆盖「首次创建」和「已有 0644 的老文件」。
    """
    path = settings_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(_sanitize(overrides), ensure_ascii=False, indent=2, sort_keys=True)

    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(payload)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(path, FILE_MODE)
    return path


# ---------------------------------------------------------------- 覆盖
def apply_overrides(base: Settings, overrides: dict[str, Any]) -> Settings:
    """用 settings.json 里的**非空值**覆盖 base（= 环境变量 + .env）。

    「纵深」的含义：不是把 JSON 当作一份完整配置去替换，而是逐字段判断
    「这一层有没有值」，有才覆盖。所以 JSON 里只写 api_key 时，
    base_url / model 仍然来自 .env 或默认值。
    """
    if not overrides:
        return base

    updates: dict[str, Any] = {}

    for section, mapping in ((SECTION_LLM, LLM_FIELD_MAP), (SECTION_DEEPSEEK, DEEPSEEK_FIELD_MAP)):
        block = overrides.get(section)
        if not isinstance(block, dict):
            continue
        for name, attr in mapping.items():
            value = block.get(name)
            if isinstance(value, str) and value.strip():
                updates[attr] = value.strip()

    json_pricing = overrides.get(SECTION_PRICING)
    if isinstance(json_pricing, dict) and json_pricing:
        merged = dict(base.pricing)
        merged.update({k: float(v) for k, v in json_pricing.items()})
        updates["pricing"] = merged

    if not updates:
        return base
    # model_copy 是浅拷贝 + 直接改字段，不重跑校验：这些值已经在
    # validate_* 里过了一遍，重跑校验只会让「老配置文件 + 新约束」互相打架。
    return base.model_copy(update=updates)


# ---------------------------------------------------------------- 校验
def validate_base_url(value: str) -> str:
    text = (value or "").strip()
    if not text:
        raise ValueError("base_url 不能为空")
    if len(text) > MAX_BASE_URL_LEN:
        raise ValueError(f"base_url 不能超过 {MAX_BASE_URL_LEN} 个字符")
    if any(ch.isspace() for ch in text):
        raise ValueError("base_url 不能包含空白字符")
    if not text.startswith(("http://", "https://")):
        raise ValueError("base_url 必须以 http:// 或 https:// 开头")
    return text


def validate_model(value: str) -> str:
    text = (value or "").strip()
    if not text:
        raise ValueError("model 不能为空")
    if len(text) > MAX_MODEL_LEN:
        raise ValueError(f"model 不能超过 {MAX_MODEL_LEN} 个字符")
    if any(ch.isspace() for ch in text):
        raise ValueError("model 不能包含空白字符")

    # 现实里真的有人把 key 粘进模型名（截图见过）。模型名是「deepseek-chat」
    # 这类短标识，而动辄 40+ 字符、以 sk- 开头的几乎必然是 key。
    # 提前拦住，比让他保存成功然后在调用时报 401 好。
    if text.lower().startswith(("sk-", "sk_", "bearer ")) and len(text) >= 20:
        raise ValueError(
            "这看起来是一个 API Key，不是模型名。模型名应形如 deepseek-chat；"
            "Key 请填在上一栏。"
        )
    return text


def validate_api_key(value: str) -> str:
    """key 只做长度/空白校验，不做格式猜测（各家前缀都不一样）。"""
    text = (value or "").strip()
    if not text:
        raise ValueError("api_key 不能为空")
    if len(text) > MAX_KEY_LEN:
        raise ValueError(f"api_key 不能超过 {MAX_KEY_LEN} 个字符")
    if any(ch.isspace() for ch in text):
        raise ValueError("api_key 不能包含空白字符")
    return text


def validate_pricing_value(name: str, value: Any) -> float:
    number = _as_price(value)
    if number is None:
        raise ValueError(f"单价 {name} 必须是非负数字")
    return number


# ---------------------------------------------------------------- 掩码
def mask_key(key: str) -> str:
    """前 6 后 4 的掩码；太短的 key 一律全掩码。

    阈值不是拍脑袋定的：露头尾的总字符数是固定的 6 + 4 = 10，
    所以要求「露出的 ≤ 总长的 1/3」，即 len >= 30 才允许露头尾。

    实测反例：12 字符的 key 按「前 6 后 4」会露出 10 个、只藏 2 个，
    掩码形同虚设 —— 原实现的 docstring 里写了这个问题，但代码没改。
    真实 API key（OpenAI 51 字符、DeepSeek 40 左右）都在阈值之上，
    正常展示不受影响。
    """
    text = (key or "").strip()
    if not text:
        return ""
    revealed = MASK_HEAD + MASK_TAIL
    if len(text) < max(MASK_MIN_LEN, revealed * MASK_REVEAL_RATIO):
        return FULL_MASK
    return f"{text[:MASK_HEAD]}{MASK_CHAR * MASK_LEN}{text[-MASK_TAIL:]}"


# ---------------------------------------------------------------- 视图
def pick_source(settings_value: str, env_value: str) -> str:
    """key 到底从哪来的：界面写入 > 环境变量 > 没有。"""
    if (settings_value or "").strip():
        return "settings"
    if (env_value or "").strip():
        return "env"
    return "none"


def llm_view(effective: Settings, env: Settings, overrides: dict[str, Any]) -> dict[str, Any]:
    # 先看「原始出处」再看生效值：settings.json 里的 key 会覆盖 .env，
    # 这时 source 必须是 settings，否则用户以为改界面没生效。
    raw_key = (overrides.get(SECTION_LLM) or {}).get("api_key", "")
    api_key = (effective.llm_api_key or "").strip()
    return {
        "base_url": effective.llm_base_url or "",
        "api_key_masked": mask_key(api_key),
        "api_key_set": bool(api_key),
        "model": effective.llm_model or "",
        "source": pick_source(raw_key, env.llm_api_key),
    }


def deepseek_view(
    effective: Settings, env: Settings, overrides: dict[str, Any]
) -> dict[str, Any]:
    raw_key = (overrides.get(SECTION_DEEPSEEK) or {}).get("api_key", "")
    api_key = (effective.deepseek_api_key or "").strip()
    return {
        "api_key_masked": mask_key(api_key),
        "api_key_set": bool(api_key),
        "base_url": effective.deepseek_base_url or "",
        "source": pick_source(raw_key, env.deepseek_api_key),
    }


__all__ = [
    "DEEPSEEK_FIELD_MAP",
    "FILE_MODE",
    "FULL_MASK",
    "LLM_FIELD_MAP",
    "SECTION_DEEPSEEK",
    "SECTION_LLM",
    "SECTION_PRICING",
    "SETTINGS_FILENAME",
    "apply_overrides",
    "deepseek_view",
    "llm_view",
    "load_overrides",
    "mask_key",
    "pick_source",
    "save_overrides",
    "settings_path",
    "validate_api_key",
    "validate_base_url",
    "validate_model",
    "validate_pricing_value",
]
