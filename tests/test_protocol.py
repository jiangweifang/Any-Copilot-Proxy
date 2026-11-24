import unittest

from copilot_proxy.protocol import ProtocolError, ResponseStream, to_chat, to_responses


class ProtocolTests(unittest.TestCase):
    def test_images_tools_history_and_request_options(self):
        result = to_responses({
            "model": "m", "messages": [
                {"role": "system", "content": "Be concise"},
                {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}}]},
                {"role": "assistant", "content": None, "tool_calls": [{"id": "call1", "type": "function", "function": {"name": "check", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": "call1", "content": "OK"}],
            "tools": [{"type": "function", "function": {"name": "check", "parameters": {"type": "object"}}}],
            "tool_choice": {"type": "function", "function": {"name": "check"}},
            "reasoning_effort": "low", "max_tokens": 123, "stream": True})
        self.assertEqual(result["input"][1]["content"][0]["type"], "input_image")
        self.assertEqual(result["input"][2]["call_id"], result["input"][3]["call_id"])
        self.assertFalse(result["tools"][0]["strict"])
        self.assertEqual(result["reasoning"], {"effort": "low"})
        self.assertEqual(result["max_output_tokens"], 123)
        self.assertFalse(result["store"])

    def test_reply_reasoning_tools_usage_and_incomplete(self):
        result = to_chat({"id": "r", "status": "completed", "output": [
            {"type": "reasoning", "summary": [{"text": "Thinking"}]},
            {"type": "function_call", "call_id": "c", "name": "check", "arguments": "{}"}],
            "usage": {"input_tokens": 10, "output_tokens": 5}}, "m")
        self.assertEqual(result["choices"][0]["finish_reason"], "tool_calls")
        self.assertEqual(result["choices"][0]["message"]["reasoning_content"], "Thinking")
        self.assertEqual(result["usage"]["total_tokens"], 15)
        self.assertEqual(to_chat({"status": "incomplete"}, "m")["choices"][0]["finish_reason"], "length")

    def test_parallel_streamed_tools_and_errors(self):
        stream = ResponseStream("m")
        for index in (1, 2):
            stream.convert({"type": "response.output_item.added", "output_index": index,
                            "item": {"type": "function_call", "call_id": f"c{index}", "name": f"f{index}", "arguments": ""}})
        chunk = stream.convert({"type": "response.function_call_arguments.delta", "output_index": 2, "delta": '{"b":2}'})
        self.assertEqual(chunk["choices"][0]["delta"]["tool_calls"][0]["index"], 1)
        last = stream.convert({"type": "response.completed", "response": {"status": "completed", "usage": {"input_tokens": 1, "output_tokens": 2}}})
        self.assertTrue(stream.completed)
        self.assertEqual(last["usage"]["total_tokens"], 3)
        self.assertEqual(last["choices"][0]["finish_reason"], "tool_calls")
        with self.assertRaises(RuntimeError):
            ResponseStream("m").convert({"type": "error", "message": "Denied"})
        self.assertFalse(ResponseStream("m").completed)

    def test_rejects_unsupported_request_behavior(self):
        for payload in ({"model": "m", "messages": []},
                        {"model": "m", "messages": [{"role": "tool", "content": "missing id"}]},
                        {"model": "m", "messages": [{"role": "user", "content": "x"}], "n": 2},
                        {"model": "m", "messages": [{"role": "user", "content": "x"}], "stop": ["x"]}):
            with self.assertRaises(ProtocolError):
                to_responses(payload)
