import test from 'node:test'
import assert from 'node:assert/strict'
import { toResponses, toChat, toChatStream } from './protocol.mjs'

test('preserves images, system prompts, tools and tool-result history', () => {
  const result = toResponses({
    model: 'test-model',
    messages: [
      { role: 'system', content: 'Be concise' },
      { role: 'user', content: [{ type: 'text', text: 'Look' }, { type: 'image_url', image_url: { url: 'data:image/png;base64,abc' } }] },
      { role: 'assistant', content: null, tool_calls: [{ id: 'call_1', type: 'function', function: { name: 'check', arguments: '{}' } }] },
      { role: 'tool', tool_call_id: 'call_1', content: 'OK' },
    ],
    tools: [{ type: 'function', function: { name: 'check', parameters: { type: 'object' } } }],
    tool_choice: { type: 'function', function: { name: 'check' } },
    reasoning_effort: 'low',
    max_tokens: 123,
    stream: true,
  })
  assert.equal(result.input[1].content[1].type, 'input_image')
  assert.equal(result.input[2].call_id, result.input[3].call_id)
  assert.equal(result.input[3].type, 'function_call_output')
  assert.equal(result.tools[0].name, 'check')
  assert.equal(result.tools[0].strict, false)
  assert.deepEqual(result.tool_choice, { type: 'function', name: 'check' })
  assert.deepEqual(result.reasoning, { effort: 'low' })
  assert.equal(result.max_output_tokens, 123)
  assert.equal(result.store, false)
})

test('maps non-streaming tool calls, reasoning, usage and incomplete output', () => {
  const result = toChat({
    id: 'resp_1', status: 'completed', output: [
      { type: 'reasoning', summary: [{ text: 'Thinking' }] },
      { type: 'function_call', call_id: 'call_1', name: 'check', arguments: '{}' },
    ],
    usage: { input_tokens: 10, output_tokens: 5, total_tokens: 15 },
  }, 'test-model')
  assert.equal(result.choices[0].finish_reason, 'tool_calls')
  assert.equal(result.choices[0].message.tool_calls[0].id, 'call_1')
  assert.equal(result.choices[0].message.reasoning_content, 'Thinking')
  assert.equal(result.usage.total_tokens, 15)
  assert.equal(toChat({ status: 'incomplete', output: [] }, 'x').choices[0].finish_reason, 'length')
})

test('streams parallel tool calls and preserves incremental arguments', async () => {
  const events = [
    { type: 'response.output_item.added', output_index: 1, item: { type: 'function_call', call_id: 'call_1', name: 'one', arguments: '' } },
    { type: 'response.output_item.added', output_index: 2, item: { type: 'function_call', call_id: 'call_2', name: 'two', arguments: '' } },
    { type: 'response.function_call_arguments.delta', output_index: 2, delta: '{"b":2}' },
    { type: 'response.function_call_arguments.delta', output_index: 1, delta: '{"a":1}' },
    { type: 'response.completed', response: { status: 'completed', usage: { input_tokens: 1, output_tokens: 2 } } },
  ]
  const chunks = await Array.fromAsync(toChatStream(events, 'test-model'))
  assert.equal(chunks[3].choices[0].delta.tool_calls[0].index, 1)
  assert.equal(chunks[4].choices[0].delta.tool_calls[0].function.arguments, '{"a":1}')
  assert.equal(chunks.at(-1).choices[0].finish_reason, 'tool_calls')
  assert.equal(chunks.at(-1).usage.total_tokens, 3)
})

test('streams text and reasoning and never treats truncated or failed streams as success', async () => {
  const chunks = await Array.fromAsync(toChatStream([
    { type: 'response.reasoning_summary_text.delta', delta: 'Think' },
    { type: 'response.output_text.delta', delta: 'OK' },
    { type: 'response.completed', response: { status: 'completed' } },
  ], 'test-model'))
  assert.equal(chunks[1].choices[0].delta.reasoning_content, 'Think')
  assert.equal(chunks[2].choices[0].delta.content, 'OK')
  await assert.rejects(Array.fromAsync(toChatStream([], 'x')), /without a completed/)
  await assert.rejects(Array.fromAsync(toChatStream([{ type: 'error', message: 'Denied' }], 'x')), /Denied/)
  assert.throws(() => toChat({ status: 'failed', error: { message: 'Failed' } }, 'x'), /Failed/)
})

test('rejects unsupported request behavior explicitly', () => {
  assert.throws(() => toResponses({ messages: [], model: 'x' }), /messages/)
  assert.throws(() => toResponses({ messages: [{ role: 'user', content: 'Hi' }], n: 2 }), /n=1/)
  assert.throws(() => toResponses({ messages: [{ role: 'tool', content: 'Hi' }] }), /tool_call_id/)
})
