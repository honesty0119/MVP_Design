from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any, Callable


SYSTEM_PROMPT = """你是市场线索运营分析 Agent。
你必须使用提供的工具回答数据问题，不得编造数据库结果。
你不能生成或执行任意 SQL，也不能请求工具定义之外的字段。
回答必须简洁，并主动说明指标口径与当前数据边界。
如果工具返回错误，请解释错误，不要猜测结果。"""

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "channel_sql_conversion",
            "description": "查询各渠道 SQL 转化率及最高渠道。",
            "parameters": {
                "type": "object",
                "properties": {
                    "period": {
                        "type": "string",
                        "enum": ["all", "current_week"],
                        "description": "all 为全部当前数据，current_week 为本周进入的线索。",
                    }
                },
                "required": ["period"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "overdue_leads",
            "description": "查询超过指定小时未跟进的线索数量。",
            "parameters": {
                "type": "object",
                "properties": {
                    "hours": {"type": "integer", "minimum": 1, "maximum": 168}
                },
                "required": ["hours"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "funnel_snapshot",
            "description": "查询当前各状态的漏斗数量和核心转化率。",
            "parameters": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
    },
]


class AgentError(RuntimeError):
    pass


def _validate_arguments(name: str, arguments: dict[str, Any]) -> None:
    if name == "channel_sql_conversion":
        if set(arguments) != {"period"} or arguments["period"] not in {"all", "current_week"}:
            raise AgentError("channel_sql_conversion 参数不合法")
    elif name == "overdue_leads":
        if set(arguments) != {"hours"}:
            raise AgentError("overdue_leads 参数不合法")
        hours = arguments["hours"]
        if not isinstance(hours, int) or isinstance(hours, bool) or not 1 <= hours <= 168:
            raise AgentError("hours 必须是 1–168 的整数")
    elif name == "funnel_snapshot":
        if arguments:
            raise AgentError("funnel_snapshot 不接受参数")
    else:
        raise AgentError(f"未知工具：{name}")


def _http_request(payload: dict[str, Any]) -> dict[str, Any]:
    base_url = os.getenv(
        "AGENT_LLM_BASE_URL",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
    ).rstrip("/")
    api_key_env = os.getenv("AGENT_LLM_API_KEY_ENV", "OPENAI_API_KEY")
    api_key = os.getenv(api_key_env, "")
    if not api_key:
        raise AgentError(f"未配置密钥环境变量 {api_key_env}")
    payload = dict(payload)
    payload["model"] = os.getenv("AGENT_LLM_MODEL", "qwen-plus")
    request = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code not in {429, 500, 502, 503, 504}:
                break
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
        if attempt < 2:
            time.sleep(0.4 * (2**attempt))
    raise AgentError(f"模型请求失败：{type(last_error).__name__}")


def run_query_agent(
    question: str,
    tool_executor: Callable[[str, dict[str, Any]], dict[str, Any]],
    fallback: Callable[[str], dict[str, Any]],
    request_fn: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    mode = os.getenv("AGENT_LLM_MODE", "auto").lower()
    api_key_env = os.getenv("AGENT_LLM_API_KEY_ENV", "OPENAI_API_KEY")
    if mode == "rule" or (mode == "auto" and not os.getenv(api_key_env)):
        result = fallback(question)
        result["agent"] = {
            "mode": "rule-fallback",
            "tool_calls": [],
            "trace": [
                {"step": "收到问题", "detail": question},
                {"step": "选择执行模式", "detail": "未配置 LLM 密钥或主动设置规则模式，使用确定性规则查询。"},
                {"step": "安全边界", "detail": "没有把用户问题转换成任意 SQL。"},
            ],
        }
        return result

    request_fn = request_fn or _http_request
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]
    try:
        first = request_fn(
            {
                "messages": messages,
                "tools": TOOL_DEFINITIONS,
                "tool_choice": "auto",
                "temperature": 0.1,
            }
        )
        message = first["choices"][0]["message"]
        calls = message.get("tool_calls") or []
        if not calls:
            return {
                "answer": message.get("content") or "模型未选择数据查询工具。",
                "agent": {"mode": "llm", "tool_calls": []},
            }
        messages.append(message)
        executed = []
        for call in calls[:3]:
            name = call["function"]["name"]
            arguments = json.loads(call["function"].get("arguments") or "{}")
            _validate_arguments(name, arguments)
            result = tool_executor(name, arguments)
            executed.append({"name": name, "arguments": arguments, "result": result})
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "name": name,
                    "content": json.dumps(result, ensure_ascii=False),
                }
            )
        final = request_fn({"messages": messages, "temperature": 0.1})
        answer = final["choices"][0]["message"].get("content") or "查询完成。"
        return {
            "answer": answer,
            "definition": executed[0]["result"].get("definition", ""),
            "data_boundary": executed[0]["result"].get("data_boundary", ""),
            "agent": {
                "mode": "llm",
                "model": os.getenv("AGENT_LLM_MODEL", "qwen-plus"),
                "tool_calls": [
                    {"name": item["name"], "arguments": item["arguments"]}
                    for item in executed
                ],
                "trace": [
                    {"step": "收到问题", "detail": question},
                    {
                        "step": "模型选择工具",
                        "detail": "；".join(
                            f"{item['name']}({json.dumps(item['arguments'], ensure_ascii=False)})"
                            for item in executed
                        ),
                    },
                    {"step": "服务端校验", "detail": "只允许 JSON Schema 内的参数，并执行预定义参数化查询。"},
                    {"step": "结果回填", "detail": "把真实工具结果发回模型，由模型生成最终业务回答。"},
                ],
            },
        }
    except (AgentError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        result = fallback(question)
        result["agent"] = {
            "mode": "rule-fallback",
            "fallback_reason": str(exc),
            "tool_calls": [],
            "trace": [
                {"step": "收到问题", "detail": question},
                {"step": "LLM 调用失败", "detail": str(exc)},
                {"step": "自动降级", "detail": "回退到确定性规则查询，保证 Demo 不因模型或网络不可用而中断。"},
            ],
        }
        return result
