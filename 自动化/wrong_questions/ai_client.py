"""OpenAI 兼容 Chat Completions 客户端。"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from .foundation import SubjectBundle, WorkflowError
from .git_store import read_git_bytes, relative_repo_path
from .repo_paths import read_repo_image, validate_image_bytes


@dataclass(frozen=True)
class AIResult:
    """一次实际 AI 请求的正文和可追溯凭据。"""

    text: str
    role: str
    model: str
    endpoint: str
    provider: str = "openai-compatible"
    request_id: str | None = None

    def metadata(self) -> dict[str, str | None]:
        return {
            "role": self.role,
            "provider": self.provider,
            "model": self.model,
            "endpoint": self.endpoint,
            "request_id": self.request_id,
        }

def extract_chat_response_text(payload: dict[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise WorkflowError("Chat Completions 返回中没有 choices。")
    message = choices[0].get("message", {})
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str) and content.strip():
        return content.strip()
    if isinstance(content, list):
        chunks: list[str] = []
        for part in content:
            if isinstance(part, str):
                chunks.append(part)
            elif isinstance(part, dict):
                text_value = part.get("text") or part.get("content")
                if isinstance(text_value, str):
                    chunks.append(text_value)
        if "\n".join(chunks).strip():
            return "\n".join(chunks).strip()
    if isinstance(message, dict) and message.get("reasoning_content"):
        raise WorkflowError("AI 只返回了 reasoning_content，没有返回可用的正文。")
    raise WorkflowError("Chat Completions 返回中没有可读取的正文内容。")

def chat_completions_endpoint(config: dict[str, Any]) -> str:
    explicit = os.environ.get("OPENAI_CHAT_COMPLETIONS_URL", "").strip()
    if explicit:
        return explicit.rstrip("/")
    base_url = os.environ.get("OPENAI_BASE_URL", "").strip().rstrip("/")
    if base_url:
        if base_url.endswith("/chat/completions"):
            return base_url
        if base_url.endswith("/v1"):
            return base_url + "/chat/completions"
        return base_url + "/v1/chat/completions"
    configured = config.get("ai", {}).get("chat_completions_url", "")
    if configured:
        return str(configured).rstrip("/")
    return "https://api.openai.com/v1/chat/completions"

def _model_for_role(config: dict[str, Any], role: str) -> str:
    if role not in {"generation", "verification"}:
        raise WorkflowError(f"未知 AI 调用角色：{role}")
    ai_config = config.get("ai", {})
    generation_model = os.environ.get("OPENAI_MODEL", "").strip() or str(
        ai_config.get("default_model", "claude-opus-4-8")
    )
    if role == "verification":
        configured = str(ai_config.get("verify_model", "")).strip()
        return os.environ.get("OPENAI_VERIFY_MODEL", "").strip() or configured or generation_model
    return generation_model


def _image_data(path, revision: str | None) -> bytes:
    if revision:
        relative_path = relative_repo_path(path)
        try:
            data = read_git_bytes(revision, relative_path)
        except WorkflowError as exc:
            raise WorkflowError(
                f"无法从提交 `{revision}` 读取题图 `{relative_path}`：{exc}"
            ) from exc
        return validate_image_bytes(data, relative_path)
    return read_repo_image(path)


def call_openai(
    prompt: str,
    bundle: SubjectBundle,
    config: dict[str, Any],
    *,
    role: str = "generation",
) -> AIResult:
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise WorkflowError(
            "未找到 OPENAI_API_KEY。请设置用户环境变量，或复制自动化/.env.example 为自动化/.env 后填写。"
        )
    ai_config = config.get("ai", {})
    model = _model_for_role(config, role)
    endpoint = chat_completions_endpoint(config)
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    grouped_images: dict[tuple[str, str | None], list[Any]] = {}
    for source in bundle.sources:
        if source.image_path:
            key = (relative_repo_path(source.image_path), source.image_revision)
            grouped_images.setdefault(key, []).append(source)
    max_images = int(ai_config.get("max_images_per_request", 12))
    max_total_bytes = int(ai_config.get("max_total_image_bytes", 40_000_000))
    if len(grouped_images) > max_images:
        raise WorkflowError(
            f"本次请求包含 {len(grouped_images)} 张题图，超过 {max_images} 张上限；"
            "请按 Question ID 边界分批。"
        )
    total_image_bytes = 0
    for (relative_path, revision), sources in grouped_images.items():
        image_path = sources[0].image_path
        if image_path is None:  # pragma: no cover - grouped_images only accepts paths
            continue
        mime_type = mimetypes.guess_type(image_path.name)[0] or "application/octet-stream"
        image_data = _image_data(image_path, revision)
        total_image_bytes += len(image_data)
        if total_image_bytes > max_total_bytes:
            raise WorkflowError(
                f"本次请求题图原始总大小超过 {max_total_bytes} 字节上限；"
                "请按 Question ID 边界分批。"
            )
        encoded = base64.b64encode(image_data).decode("ascii")
        source_ids = [source.source_id for source in sources]
        content.append(
            {
                "type": "text",
                "text": f"以下图片对应来源编号：{', '.join(source_ids)}。请结合图片文字与前面的 Markdown。",
            }
        )
        content.append(
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:{mime_type};base64,{encoded}",
                },
            }
        )

    request_body = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "max_completion_tokens": int(ai_config.get("max_output_tokens", 4096)),
        "stream": False,
    }
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(request_body, ensure_ascii=False).encode("utf-8"),
        headers={
            "User-Agent": "daily-wrong-question/1.0",
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    timeout = int(ai_config.get("timeout_seconds", 180))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            request_id = response.headers.get("x-request-id")
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(detail)
            detail = parsed.get("error", {}).get("message", detail)
        except json.JSONDecodeError:
            pass
        raise WorkflowError(f"OpenAI API 返回 HTTP {exc.code}：{detail}") from exc
    except urllib.error.URLError as exc:
        raise WorkflowError(f"无法连接 OpenAI API：{exc.reason}") from exc
    except TimeoutError as exc:
        raise WorkflowError("OpenAI API 请求超时。") from exc

    try:
        payload = json.loads(raw)
        return AIResult(
            text=extract_chat_response_text(payload),
            role=role,
            model=str(payload.get("model") or model),
            endpoint=endpoint,
            request_id=request_id,
        )
    except json.JSONDecodeError as exc:
        raise WorkflowError("OpenAI API 返回的内容不是有效 JSON。") from exc
