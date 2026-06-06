import unittest

from gemini_webapi.server.app import (
    ChatCompletionRequest,
    ChatMessage,
    CompletionRequest,
    ResponsesRequest,
    _append_chat_token_limit_instruction,
    _append_response_format_instructions,
    _append_sampling_parameter_instructions,
    _append_tool_instructions,
    _apply_stop_sequences,
    _legacy_function_call_from_tool_call,
    _messages_to_prompt,
    _messages_file_ids,
    _normalized_chat_request_tools,
    _responses_input_to_messages,
    _responses_output,
    _responses_prompt,
    _tool_calls_from_output_text,
)


WEATHER_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get current weather for a city.",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    },
}


class ServerToolCallTests(unittest.TestCase):
    def test_responses_input_converts_to_messages(self):
        messages = _responses_input_to_messages(
            [
                {
                    "role": "system",
                    "content": [{"type": "input_text", "text": "保持简洁"}],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "看图"},
                        {"type": "input_image", "image_url": "https://example.com/a.png"},
                    ],
                },
            ]
        )
        prompt = _messages_to_prompt(messages)

        self.assertIn("System: 保持简洁", prompt)
        self.assertIn("User: 看图", prompt)
        self.assertIn("Image URL: https://example.com/a.png", prompt)

    def test_responses_prompt_supports_instructions_and_text_format(self):
        request = ResponsesRequest.model_validate(
            {
                "instructions": "保持简洁，只输出结构化结果",
                "input": "返回状态",
                "text": {
                    "format": {
                        "type": "json_schema",
                        "schema": {
                            "name": "status",
                            "schema": {
                                "type": "object",
                                "properties": {"status": {"type": "string"}},
                                "required": ["status"],
                            },
                        },
                    }
                },
            }
        )
        prompt = _responses_prompt(request)

        self.assertIn("System: 保持简洁，只输出结构化结果", prompt)
        self.assertIn("User: 返回状态", prompt)
        self.assertIn("JSON response mode is enabled.", prompt)
        self.assertIn('"required":["status"]', prompt)

    def test_responses_prompt_accepts_token_limit_aliases(self):
        official = ResponsesRequest.model_validate(
            {"input": "短回答", "max_output_tokens": 48}
        )
        compatible = ResponsesRequest.model_validate(
            {"input": "短回答", "max_tokens": 32}
        )

        self.assertIn("approximately 48 tokens", _responses_prompt(official))
        self.assertIn("approximately 32 tokens", _responses_prompt(compatible))

    def test_responses_output_shape(self):
        data = _responses_output(
            response_id="resp_1",
            model="gemini-3.1-pro",
            text="hello",
            created=123,
        )

        self.assertEqual(data["id"], "resp_1")
        self.assertEqual(data["object"], "response")
        self.assertEqual(data["created_at"], 123)
        self.assertEqual(data["output_text"], "hello")
        self.assertEqual(data["output"][0]["content"][0]["type"], "output_text")

    def test_messages_treat_developer_role_as_system_instruction(self):
        prompt = _messages_to_prompt(
            [
                ChatMessage(role="developer", content="始终返回简洁答案"),
                ChatMessage(role="user", content="你好"),
            ]
        )

        self.assertIn("System: 始终返回简洁答案", prompt)
        self.assertIn("User: 你好", prompt)
        self.assertNotIn("User: 始终返回简洁答案", prompt)

    def test_messages_include_multimodal_image_urls(self):
        prompt = _messages_to_prompt(
            [
                ChatMessage(
                    role="user",
                    content=[
                        {"type": "text", "text": "请分析这张图"},
                        {
                            "type": "image_url",
                            "image_url": {"url": "https://example.com/cat.png"},
                        },
                        {
                            "type": "input_image",
                            "image_url": "https://example.com/dog.png",
                        },
                    ],
                )
            ]
        )

        self.assertIn("请分析这张图", prompt)
        self.assertIn("Image URL: https://example.com/cat.png", prompt)
        self.assertIn("Image URL: https://example.com/dog.png", prompt)

    def test_messages_include_file_references_and_collect_file_ids(self):
        messages = [
            ChatMessage(
                role="user",
                content=[
                    {"type": "input_text", "text": "总结附件"},
                    {"type": "input_file", "file_id": "file-one"},
                    {"type": "file", "id": "file-two"},
                    {"type": "input_file", "file_id": "file-one"},
                ],
            )
        ]
        prompt = _messages_to_prompt(messages)

        self.assertIn("Attached file: file-one", prompt)
        self.assertIn("Attached file: file-two", prompt)
        self.assertEqual(_messages_file_ids(messages), ["file-one", "file-two"])

    def test_messages_include_audio_references_and_collect_file_ids(self):
        messages = [
            ChatMessage(
                role="user",
                content=[
                    {"type": "input_text", "text": "转写这段音频"},
                    {
                        "type": "input_audio",
                        "input_audio": {"file_id": "file-audio"},
                    },
                    {
                        "type": "input_audio",
                        "input_audio": {"url": "https://example.com/audio.mp3"},
                    },
                    {"type": "input_audio", "file_id": "file-audio"},
                ],
            )
        ]
        prompt = _messages_to_prompt(messages)

        self.assertIn("Attached audio file: file-audio", prompt)
        self.assertIn("Audio URL: https://example.com/audio.mp3", prompt)
        self.assertEqual(_messages_file_ids(messages), ["file-audio"])

    def test_chat_request_accepts_openai_tools(self):
        request = ChatCompletionRequest.model_validate(
            {
                "model": "gemini-3.1-pro",
                "messages": [{"role": "user", "content": "北京天气怎么样？"}],
                "tools": [WEATHER_TOOL],
                "tool_choice": {
                    "type": "function",
                    "function": {"name": "get_weather"},
                },
            }
        )

        self.assertEqual(request.tools[0].function.name, "get_weather")
        self.assertIn("get_weather", _append_tool_instructions("User: hi", request))

    def test_chat_request_accepts_legacy_functions(self):
        request = ChatCompletionRequest.model_validate(
            {
                "model": "gemini-3.1-pro",
                "messages": [{"role": "user", "content": "北京天气怎么样？"}],
                "functions": [WEATHER_TOOL["function"]],
                "function_call": {"name": "get_weather"},
            }
        )

        tools, tool_choice = _normalized_chat_request_tools(request)
        self.assertEqual(tools[0].function.name, "get_weather")
        self.assertEqual(tool_choice["function"]["name"], "get_weather")
        self.assertIn("You must call the tool named get_weather.", _append_tool_instructions("User: hi", request))
        calls = _tool_calls_from_output_text(
            '{"tool_calls":[{"name":"get_weather","arguments":{"city":"北京"}}]}',
            tools,
        )
        self.assertEqual(calls[0]["function"]["name"], "get_weather")

    def test_chat_request_accepts_response_format_json_schema(self):
        request = ChatCompletionRequest.model_validate(
            {
                "messages": [{"role": "user", "content": "返回 JSON"}],
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "answer",
                        "schema": {
                            "type": "object",
                            "properties": {"ok": {"type": "boolean"}},
                            "required": ["ok"],
                        },
                    },
                },
            }
        )
        prompt = _append_response_format_instructions("User: hi", request)

        self.assertIn("JSON response mode is enabled.", prompt)
        self.assertIn('"required":["ok"]', prompt)

    def test_chat_request_accepts_max_completion_tokens_alias(self):
        request = ChatCompletionRequest.model_validate(
            {
                "messages": [{"role": "user", "content": "短回答"}],
                "max_completion_tokens": 32,
            }
        )
        prompt = _append_chat_token_limit_instruction("User: hi", request)

        self.assertEqual(request.max_completion_tokens, 32)
        self.assertIn("approximately 32 tokens", prompt)

    def test_openai_sampling_parameters_are_explicitly_prompted(self):
        chat = ChatCompletionRequest.model_validate(
            {
                "messages": [{"role": "user", "content": "写一段文案"}],
                "temperature": 0.2,
                "top_p": 0.8,
                "presence_penalty": 0.3,
                "frequency_penalty": 0.4,
                "seed": 123,
            }
        )
        completion = CompletionRequest.model_validate(
            {
                "prompt": "补全文案",
                "presence_penalty": 0.1,
                "frequency_penalty": 0.2,
            }
        )
        responses = ResponsesRequest.model_validate(
            {
                "input": "生成标题",
                "seed": 456,
                "temperature": 0,
            }
        )

        chat_prompt = _append_sampling_parameter_instructions("User: hi", chat)
        completion_prompt = _append_sampling_parameter_instructions("User: hi", completion)
        responses_prompt = _append_sampling_parameter_instructions("User: hi", responses)

        self.assertIn("temperature=0.2", chat_prompt)
        self.assertIn("top_p=0.8", chat_prompt)
        self.assertIn("presence_penalty=0.3", chat_prompt)
        self.assertIn("frequency_penalty=0.4", chat_prompt)
        self.assertIn("seed=123", chat_prompt)
        self.assertIn("presence_penalty=0.1", completion_prompt)
        self.assertIn("frequency_penalty=0.2", completion_prompt)
        self.assertIn("temperature=0", responses_prompt)
        self.assertIn("seed=456", responses_prompt)

    def test_common_openai_tracking_fields_are_accepted(self):
        chat = ChatCompletionRequest.model_validate(
            {
                "messages": [{"role": "user", "content": "hello"}],
                "user": "external-user-1",
                "metadata": {"trace_id": "trace-chat"},
                "store": False,
            }
        )
        completion = CompletionRequest.model_validate(
            {
                "prompt": "hello",
                "user": "external-user-2",
                "metadata": {"trace_id": "trace-completion"},
                "store": False,
            }
        )
        responses = ResponsesRequest.model_validate(
            {
                "input": "hello",
                "user": "external-user-3",
                "metadata": {"trace_id": "trace-response"},
                "store": True,
            }
        )

        self.assertEqual(chat.user, "external-user-1")
        self.assertEqual(chat.metadata["trace_id"], "trace-chat")
        self.assertFalse(chat.store)
        self.assertEqual(completion.user, "external-user-2")
        self.assertEqual(completion.metadata["trace_id"], "trace-completion")
        self.assertFalse(completion.store)
        self.assertEqual(responses.user, "external-user-3")
        self.assertEqual(responses.metadata["trace_id"], "trace-response")
        self.assertTrue(responses.store)

    def test_apply_stop_sequences_truncates_at_earliest_match(self):
        self.assertEqual(
            _apply_stop_sequences("hello<END>hidden", "<END>"),
            ("hello", True),
        )
        self.assertEqual(
            _apply_stop_sequences("a STOP b END c", ["END", "STOP"]),
            ("a ", True),
        )
        self.assertEqual(
            _apply_stop_sequences("hello", None),
            ("hello", False),
        )

    def test_messages_include_tool_history(self):
        prompt = _messages_to_prompt(
            [
                ChatMessage(
                    role="assistant",
                    content=None,
                    tool_calls=[
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "get_weather",
                                "arguments": '{"city":"北京"}',
                            },
                        }
                    ],
                ),
                ChatMessage(
                    role="tool",
                    tool_call_id="call_1",
                    content='{"temperature":"22C"}',
                ),
            ]
        )

        self.assertIn("Assistant tool calls:", prompt)
        self.assertIn("Tool result (call_1):", prompt)

    def test_parses_openai_tool_calls_from_model_json(self):
        request = ChatCompletionRequest.model_validate(
            {
                "messages": [{"role": "user", "content": "天气"}],
                "tools": [WEATHER_TOOL],
            }
        )
        calls = _tool_calls_from_output_text(
            '{"tool_calls":[{"name":"get_weather","arguments":{"city":"北京"}}]}',
            request.tools,
        )

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["type"], "function")
        self.assertEqual(calls[0]["function"]["name"], "get_weather")
        self.assertEqual(calls[0]["function"]["arguments"], '{"city":"北京"}')
        self.assertEqual(
            _legacy_function_call_from_tool_call(calls[0]),
            {"name": "get_weather", "arguments": '{"city":"北京"}'},
        )

    def test_ignores_unknown_tool_names(self):
        request = ChatCompletionRequest.model_validate(
            {
                "messages": [{"role": "user", "content": "天气"}],
                "tools": [WEATHER_TOOL],
            }
        )
        calls = _tool_calls_from_output_text(
            '{"tool_calls":[{"name":"delete_everything","arguments":{}}]}',
            request.tools,
        )

        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
