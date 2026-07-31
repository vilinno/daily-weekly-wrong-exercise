"""OpenAI 兼容 Chat Completions 客户端。"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import urllib.error
import urllib.request
from typing import Any

from .foundation import SubjectBundle, WorkflowError
from .markdown_tools import unique_image_paths

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

def call_openai(prompt: str, bundle: SubjectBundle, config: dict[str, Any]) -> str:
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise WorkflowError(
            "未找到 OPENAI_API_KEY。请设置用户环境变量，或复制自动化/.env.example 为自动化/.env 后填写。"
        )
    ai_config = config.get("ai", {})
    model = os.environ.get("OPENAI_MODEL", "").strip() or ai_config.get(
        "default_model", "claude-opus-4-8"
    )
    endpoint = chat_completions_endpoint(config)
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for image_path in unique_image_paths(bundle):
        mime_type = mimetypes.guess_type(image_path.name)[0] or "application/octet-stream"
        encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
        source_ids = [
            source.source_id
            for source in bundle.sources
            if source.image_path == image_path
        ]
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
        return extract_chat_response_text(json.loads(raw))
    except json.JSONDecodeError as exc:
        raise WorkflowError("OpenAI API 返回的内容不是有效 JSON。") from exc
