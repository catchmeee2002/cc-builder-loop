#!/usr/bin/env python3
"""Minimal Responses API client for the offline Issue triage experiment."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
from pathlib import Path
import time
import tomllib
from typing import Any, Callable
import urllib.error
import urllib.request


EXIT_INPUT = 2
EXIT_CONFIG = 3
EXIT_TRANSPORT = 4
EXIT_RESPONSE = 5

REQUEST_TIMEOUT_SECONDS = 180
RUN_MAX_OUTPUT_TOKENS = 6_000
RUN_REASONING_EFFORT = "high"
RETRYABLE_HTTP_STATUS = {429, 500, 502, 503, 504}


class RunnerError(Exception):
    def __init__(self, kind: str, message: str, exit_code: int) -> None:
        super().__init__(message)
        self.kind = kind
        self.safe_message = message
        self.exit_code = exit_code


@dataclasses.dataclass(frozen=True)
class RuntimeConfig:
    model: str
    provider_name: str
    base_url: str
    api_key: str = dataclasses.field(repr=False)


@dataclasses.dataclass(frozen=True)
class ApiResult:
    value: dict[str, Any]
    request_hash: str


def load_runtime_config(codex_home: Path | None = None) -> RuntimeConfig:
    home = codex_home or Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    config_path = home / "config.toml"
    auth_path = home / "auth.json"
    try:
        config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        raise RunnerError("config", "无法读取有效的 Codex config.toml", EXIT_CONFIG) from None
    try:
        auth = json.loads(auth_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise RunnerError("config", "无法读取有效的 Codex auth.json", EXIT_CONFIG) from None

    model = config.get("model")
    provider_name = config.get("model_provider")
    providers = config.get("model_providers")
    if not isinstance(model, str) or not model:
        raise RunnerError("config", "Codex config.toml 缺少 model", EXIT_CONFIG)
    if not isinstance(provider_name, str) or not provider_name:
        raise RunnerError("config", "Codex config.toml 缺少 model_provider", EXIT_CONFIG)
    if not isinstance(providers, dict) or not isinstance(providers.get(provider_name), dict):
        raise RunnerError("config", "Codex config.toml 缺少当前 provider 配置", EXIT_CONFIG)
    provider = providers[provider_name]
    if provider.get("wire_api") != "responses":
        raise RunnerError("config", "当前 Codex provider 不是 Responses API", EXIT_CONFIG)
    base_url = provider.get("base_url")
    if not isinstance(base_url, str) or not base_url.startswith(("http://", "https://")):
        raise RunnerError("config", "当前 Codex provider 缺少有效 base_url", EXIT_CONFIG)
    api_key = auth.get("OPENAI_API_KEY")
    if not isinstance(api_key, str) or not api_key:
        raise RunnerError("config", "Codex auth.json 缺少 API key", EXIT_CONFIG)
    return RuntimeConfig(model=model, provider_name=provider_name, base_url=base_url, api_key=api_key)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def build_request_body(
    config: RuntimeConfig,
    *,
    developer_prompt: str,
    task_data: dict[str, Any],
    schema_name: str,
    schema: dict[str, Any],
    reasoning_effort: str,
    max_output_tokens: int,
) -> tuple[dict[str, Any], str]:
    user_text = _canonical_json({"task_data": task_data})
    body = {
        "model": config.model,
        "store": False,
        "reasoning": {"effort": reasoning_effort},
        "input": [
            {
                "role": "developer",
                "content": [{"type": "input_text", "text": developer_prompt}],
            },
            {
                "role": "user",
                "content": [{"type": "input_text", "text": user_text}],
            },
        ],
        "tools": [],
        "max_output_tokens": max_output_tokens,
        "text": {
            "format": {
                "type": "json_schema",
                "name": schema_name,
                "strict": True,
                "schema": schema,
            }
        },
    }
    audit_material = {
        "model": config.model,
        "developer": developer_prompt,
        "user": user_text,
        "tools": [],
    }
    request_hash = hashlib.sha256(_canonical_json(audit_material).encode("utf-8")).hexdigest()
    return body, request_hash


class ResponsesClient:
    def __init__(
        self,
        config: RuntimeConfig,
        *,
        timeout_seconds: int = REQUEST_TIMEOUT_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self.timeout_seconds = timeout_seconds
        self._sleep = sleep

    def request(
        self,
        *,
        developer_prompt: str,
        task_data: dict[str, Any],
        schema_name: str,
        schema: dict[str, Any],
        validator: Callable[[Any], dict[str, Any]],
        reasoning_effort: str = RUN_REASONING_EFFORT,
        max_output_tokens: int = RUN_MAX_OUTPUT_TOKENS,
    ) -> ApiResult:
        body, request_hash = build_request_body(
            self.config,
            developer_prompt=developer_prompt,
            task_data=task_data,
            schema_name=schema_name,
            schema=schema,
            reasoning_effort=reasoning_effort,
            max_output_tokens=max_output_tokens,
        )
        response = self._post(body)
        value = validator(_extract_output_json(response))
        return ApiResult(value=value, request_hash=request_hash)

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        payload = _canonical_json(body).encode("utf-8")
        request = urllib.request.Request(
            self.config.base_url.rstrip("/") + "/responses",
            data=payload,
            headers={
                "Authorization": "Bearer " + self.config.api_key,
                "Content-Type": "application/json",
                "User-Agent": "cc-builder-loop-issue-triage/1",
            },
            method="POST",
        )
        for attempt in range(2):
            try:
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with opener.open(request, timeout=self.timeout_seconds) as response:
                    return json.load(response)
            except urllib.error.HTTPError as exc:
                if exc.code in RETRYABLE_HTTP_STATUS and attempt == 0:
                    self._sleep(1.5)
                    continue
                message = (
                    "Responses API 认证失败"
                    if exc.code in {401, 403}
                    else f"Responses API 返回 HTTP {exc.code}"
                )
                raise RunnerError("transport", message, EXIT_TRANSPORT) from None
            except (urllib.error.URLError, TimeoutError, OSError):
                if attempt == 0:
                    self._sleep(1.5)
                    continue
                raise RunnerError("transport", "Responses API 网络请求失败", EXIT_TRANSPORT) from None
            except json.JSONDecodeError:
                raise RunnerError("response", "Responses API 返回了非法 JSON", EXIT_RESPONSE) from None
        raise RunnerError("transport", "Responses API 网络请求失败", EXIT_TRANSPORT)


def _extract_output_json(response: Any) -> Any:
    if not isinstance(response, dict) or not isinstance(response.get("output"), list):
        raise RunnerError("response", "Responses API 响应缺少 output", EXIT_RESPONSE)
    texts: list[str] = []
    for item in response["output"]:
        if not isinstance(item, dict):
            raise RunnerError("response", "Responses API output 结构非法", EXIT_RESPONSE)
        item_type = item.get("type")
        if item_type == "reasoning":
            continue
        if item_type != "message":
            raise RunnerError("response", f"Responses API 返回未允许类型: {item_type}", EXIT_RESPONSE)
        content = item.get("content")
        if not isinstance(content, list):
            raise RunnerError("response", "Responses API message 缺少 content", EXIT_RESPONSE)
        for part in content:
            if isinstance(part, dict) and part.get("type") == "output_text" and isinstance(part.get("text"), str):
                texts.append(part["text"])
    if len(texts) != 1:
        raise RunnerError("response", "Responses API 必须返回且只返回一个 output_text", EXIT_RESPONSE)
    try:
        return json.loads(texts[0])
    except json.JSONDecodeError:
        raise RunnerError("response", "模型输出不是合法 JSON", EXIT_RESPONSE) from None
