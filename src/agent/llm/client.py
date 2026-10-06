"""LLM 客户端。

按 OpenAI 兼容的 `/chat/completions` 格式实现，兼容 OpenAI 官方接口、
Azure OpenAI（网关层兼容时）、以及绝大多数国内中转/自建 vLLM / DeepSeek
等服务——只要它们暴露的是 OpenAI 兼容的 chat completions 接口，
不需要改这个文件，只需要设置三个环境变量：

    LLM_API_KEY   密钥
    LLM_BASE_URL  服务的 base url（到 /chat/completions 之前的部分，
                  如 https://api.deepseek.com/v1）
    LLM_MODEL     模型名（如 deepseek-v4-flash）

也可把这三个变量写在项目根目录的 `.env`（已 gitignore），客户端启动时
会自动加载——未在 shell 里 export 也能用。

设计成「未配置就明确不可用」而不是「静默失败」：
planner 与 verifier 检查 is_configured() 为假时会自动降级到规则实现，
并在 payload / note 里注明降级原因。这样系统在没有 LLM 的情况下依然完整可跑，
配好三个环境变量后把 configs/agent.yaml 里 planner.mode / verification.mode
改成 llm 或 hybrid 即可生效，不需要再改代码。

如果对接的服务不是 OpenAI 兼容格式（请求体、鉴权方式、响应结构不同），
需要改的只是 complete() 里发请求和解析响应那几行，接口边界
（is_configured / complete / complete_json / dump_calls）不用变。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from evidence_core.http import SourceUnavailable, request_json

API_KEY_ENV = "LLM_API_KEY"
_PROJECT_ROOT = Path(__file__).resolve().parents[3]   # .../SearchAndVerifyCycle_agents
_ENV_LOADED = False


def _load_dotenv() -> None:
    """把项目根目录 `.env` 里的 KEY=VALUE 写进 os.environ（不覆盖已有环境变量）。"""
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    _ENV_LOADED = True
    path = _PROJECT_ROOT / ".env"
    if not path.is_file():
        return
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _env(name: str, default: str = "") -> str:
    _load_dotenv()
    return os.environ.get(name, default) or default


@dataclass
class LLMCall:
    """一次调用的完整记录。保存它是为了让 LLM 参与的结论也能回放（Q52）。"""

    purpose: str
    prompt: str
    response: str
    model: str
    tokens_in: int = 0
    tokens_out: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "purpose": self.purpose,
            "model": self.model,
            "prompt": self.prompt,
            "response": self.response,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
        }


@dataclass
class LLMClient:
    base_url: str = ""
    model: str = ""
    temperature: float = 0.0        # 规划与验证都要可复现，默认贪心
    max_tokens: int = 1024
    calls: list[LLMCall] = field(default_factory=list)

    def __post_init__(self) -> None:
        # 延迟读环境变量，保证先加载 .env，也方便测试时临时改 os.environ。
        if not self.base_url:
            self.base_url = _env("LLM_BASE_URL")
        if not self.model:
            self.model = _env("LLM_MODEL")

    def is_configured(self) -> bool:
        return bool(self.base_url and self.model and _env(API_KEY_ENV))

    def _uses_deepseek(self) -> bool:
        blob = f"{self.base_url} {self.model}".lower()
        return "deepseek" in blob

    def complete(self, prompt: str, *, purpose: str = "") -> str:
        """发一次补全请求。未配置时抛 SourceUnavailable，由调用方降级。"""
        if not self.is_configured():
            raise SourceUnavailable(
                "llm",
                f"LLM 未接入（需要 {API_KEY_ENV} / LLM_BASE_URL / LLM_MODEL）",
            )

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        # DeepSeek V4 默认开启 thinking；规划/验证要的是稳定 JSON，关掉更稳也更省。
        # 对非 DeepSeek 服务不传这个字段，避免被当成未知参数拒绝。
        if self._uses_deepseek():
            payload["thinking"] = {"type": "disabled"}

        data = request_json(
            f"{self.base_url.rstrip('/')}/chat/completions",
            source_id="llm",
            method="POST",
            body=payload,
            headers={"Authorization": f"Bearer {_env(API_KEY_ENV)}"},
            use_cache=False,
        )
        message = (((data.get("choices") or [{}])[0]).get("message") or {})
        text = message.get("content") or ""
        # 个别情况下 content 为空但 reasoning_content 有内容，做一次兜底（仍优先 content）。
        if not text and message.get("reasoning_content"):
            text = str(message["reasoning_content"])
        usage = data.get("usage") or {}
        self.calls.append(LLMCall(
            purpose=purpose, prompt=prompt, response=text, model=self.model,
            tokens_in=int(usage.get("prompt_tokens", 0)),
            tokens_out=int(usage.get("completion_tokens", 0)),
        ))
        return text

    def complete_json(self, prompt: str, *, purpose: str = "") -> dict[str, Any]:
        """要求返回 JSON。解析失败抛错，由调用方降级到规则实现。

        对应 Q54：结构化输出约束。真正接入时建议同时使用服务端的
        json_schema / response_format 约束，双保险防 schema drift。
        """
        raw = self.complete(prompt, purpose=purpose)
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("```")[1]
            cleaned = cleaned[4:] if cleaned.startswith("json") else cleaned
        # 容忍模型在 JSON 前后夹杂少量说明文字：取第一个 {...} 块。
        if not cleaned.startswith("{"):
            start, end = cleaned.find("{"), cleaned.rfind("}")
            if start >= 0 and end > start:
                cleaned = cleaned[start:end + 1]
        return json.loads(cleaned)

    def dump_calls(self) -> list[dict[str, Any]]:
        return [c.to_dict() for c in self.calls]
