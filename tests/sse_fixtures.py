"""固定的 OpenAI Chat Completions SSE fixtures。

模拟 TokenRhythm / 任意 OpenAI-compatible 网关实际返回的 SSE 流式响应格式。
所有 fixture 都是"真实/固定"的文本，不依赖真实 API，供 SSE parser 回归测试使用。
"""

# 标准 OpenAI Chat Completions SSE：多个正常 content chunks + role chunk +
# 空 delta chunk + usage 末尾 chunk + data: [DONE]，事件之间以空行分隔（SSE 标准）。
SSE_STANDARD = (
    'data: {"id":"chatcmpl-abc","object":"chat.completion.chunk","created":1,'
    '"model":"deepseek-v4-flash","choices":[{"index":0,"delta":{"role":"assistant",'
    '"content":""},"finish_reason":null}]}\n'
    "\n"
    'data: {"id":"chatcmpl-abc","object":"chat.completion.chunk","created":1,'
    '"model":"deepseek-v4-flash","choices":[{"index":0,"delta":{"content":"你"},'
    '"finish_reason":null}]}\n'
    "\n"
    'data: {"id":"chatcmpl-abc","object":"chat.completion.chunk","created":1,'
    '"model":"deepseek-v4-flash","choices":[{"index":0,"delta":{"content":"好"},'
    '"finish_reason":null}]}\n'
    "\n"
    'data: {"id":"chatcmpl-abc","object":"chat.completion.chunk","created":1,'
    '"model":"deepseek-v4-flash","choices":[{"index":0,"delta":{},'
    '"finish_reason":"stop"}]}\n'
    "\n"
    'data: {"id":"chatcmpl-abc","object":"chat.completion.chunk","created":1,'
    '"model":"deepseek-v4-flash","choices":[],"usage":{"prompt_tokens":86,'
    '"completion_tokens":12,"total_tokens":98}}\n'
    "\n"
    "data: [DONE]\n"
    "\n"
)

# Case A：多个正常 content chunks → 拼接 "Hello world"
SSE_MULTI_CONTENT = (
    'data: {"choices":[{"delta":{"content":"Hello"}}],"model":"m"}\n'
    'data: {"choices":[{"delta":{"content":" "}}],"model":"m"}\n'
    'data: {"choices":[{"delta":{"content":"world"}}],"model":"m"}\n'
    'data: {"choices":[{"delta":{"content":"!"}}],"model":"m"}\n'
    "data: [DONE]\n"
)

# Case B：前几个 chunk 没有 content（role / 空 content / 空 delta），后续有 content
SSE_LATE_CONTENT = (
    'data: {"choices":[{"delta":{"role":"assistant"}}]}\n'
    'data: {"choices":[{"delta":{"content":""}}]}\n'
    'data: {"choices":[{"delta":{}}]}\n'
    'data: {"choices":[{"delta":{"content":"成功"}}]}\n'
    'data: {"choices":[{"delta":{"content":"内容"}}]}\n'
    "data: [DONE]\n"
)

# Case C：data: [DONE] 正常结束（前面无任何 data 事件）
SSE_DONE_ONLY = "data: [DONE]\n"

# Case D：HTTP 200 但整个 SSE 没有 content（只有 role / 空 delta / usage）
SSE_NO_CONTENT = (
    'data: {"choices":[{"delta":{"role":"assistant"}}]}\n'
    'data: {"choices":[{"delta":{}}]}\n'
    'data: {"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":0,"total_tokens":5}}\n'
    "data: [DONE]\n"
)

# Case E：最后一个 chunk 包含 usage
SSE_USAGE_LAST = (
    'data: {"choices":[{"delta":{"content":"分析"}}],"model":"deepseek-v4-flash"}\n'
    'data: {"choices":[{"delta":{"content":"完成"}}],"model":"deepseek-v4-flash"}\n'
    'data: {"choices":[],"usage":{"prompt_tokens":120,"completion_tokens":30,'
    '"total_tokens":150,"credit":0.0123}}\n'
    "data: [DONE]\n"
)

# malformed SSE：中间夹杂坏 JSON chunk，但后续有正常 content（不应整体失败）
SSE_MALFORMED_MIDDLE = (
    'data: {"choices":[{"delta":{"content":"good"}}]}\n'
    "data: {this is not json}\n"
    'data: {"choices":[{"delta":{"content":" result"}}]}\n'
    "data: [DONE]\n"
)

# malformed SSE：完全不是 SSE（HTML / 纯文本）
SSE_GARBAGE = "<html><body>Internal Server Error</body></html>\n"

# 无空行分隔、data: 后无空格（常见网关变体）
SSE_NO_SPACE_NO_BLANK = (
    'data:{"choices":[{"delta":{"content":"Hi"}}],"model":"m"}\n'
    'data:{"choices":[{"delta":{"content":" there"}}],"model":"m"}\n'
    "data: [DONE]\n"
)

# 带注释行 / event: 字段 / 空行的标准 SSE（OpenAI 官方示例形态）
SSE_WITH_COMMENTS = (
    ": keep-alive comment\n"
    'event: message\n'
    'data: {"choices":[{"delta":{"content":"你"}}]}\n'
    "\n"
    'data: {"choices":[{"delta":{"content":"好"}}]}\n'
    "\n"
    "data: [DONE]\n"
)
