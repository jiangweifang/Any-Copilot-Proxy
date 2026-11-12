import { randomUUID } from 'node:crypto'

function invalid(message) {
  throw Object.assign(new Error(message), { status: 400 })
}

function contentParts(content, role) {
  if (typeof content === 'string') return content
  if (content == null) return ''
  if (!Array.isArray(content)) invalid('Message content must be a string or an array')
  return content.map(part => {
    if (part.type === 'text') {
      return { type: role === 'assistant' ? 'output_text' : 'input_text', text: part.text }
    }
    if (part.type === 'image_url') {
      return { type: 'input_image', image_url: part.image_url.url, detail: part.image_url.detail ?? 'auto' }
    }
    if (part.type === 'file') {
      return { type: 'input_file', ...part.file }
    }
    invalid(`Unsupported content type: ${part.type}`)
  })
}

export function toResponses(payload) {
  if (!Array.isArray(payload.messages) || payload.messages.length === 0) invalid('messages is required')
  if (payload.n != null && payload.n !== 1) invalid('Responses models support n=1 only')
  if (payload.stop != null) invalid('Responses models do not support stop sequences')
  const input = []
  for (const message of payload.messages) {
    if (message.role === 'tool') {
      if (!message.tool_call_id) invalid('Tool messages require tool_call_id')
      const output = typeof message.content === 'string'
        ? message.content
        : JSON.stringify(message.content ?? '')
      input.push({ type: 'function_call_output', call_id: message.tool_call_id, output })
      continue
    }
    if (!['user', 'assistant', 'system', 'developer'].includes(message.role)) invalid('Unsupported message role')
    if (message.content != null && message.content !== '') {
      input.push({ role: message.role, content: contentParts(message.content, message.role) })
    }
    for (const call of message.tool_calls ?? []) {
      if (call.type !== 'function') invalid('Only function tools are supported')
      input.push({
        type: 'function_call',
        call_id: call.id,
        name: call.function.name,
        arguments: call.function.arguments,
      })
    }
  }
  const result = { model: payload.model, input, store: false, stream: payload.stream ?? false }
  const maxTokens = payload.max_completion_tokens ?? payload.max_tokens
  if (maxTokens != null) result.max_output_tokens = maxTokens
  for (const key of ['temperature', 'top_p', 'parallel_tool_calls']) {
    if (payload[key] != null) result[key] = payload[key]
  }
  if (payload.reasoning_effort != null) result.reasoning = { effort: payload.reasoning_effort }
  if (payload.tools != null) {
    result.tools = payload.tools.map(tool => {
      if (tool.type !== 'function') invalid('Only function tools are supported')
      return { type: 'function', ...tool.function, strict: tool.function.strict ?? false }
    })
  }
  if (payload.tool_choice != null) {
    result.tool_choice = typeof payload.tool_choice === 'string'
      ? payload.tool_choice
      : { type: 'function', name: payload.tool_choice.function.name }
  }
  const format = payload.response_format
  if (format?.type === 'json_schema') result.text = { format: { type: 'json_schema', ...format.json_schema } }
  else if (format?.type === 'json_object' || format?.type === 'text') result.text = { format }
  else if (format) invalid('Unsupported response_format')
  return result
}

export function usageForChat(usage) {
  if (!usage) return undefined
  return {
    prompt_tokens: usage.input_tokens ?? 0,
    completion_tokens: usage.output_tokens ?? 0,
    total_tokens: usage.total_tokens ?? (usage.input_tokens ?? 0) + (usage.output_tokens ?? 0),
    prompt_tokens_details: usage.input_tokens_details,
    completion_tokens_details: usage.output_tokens_details,
  }
}

function assertSuccessful(response) {
  if (response.status === 'failed' || response.error) {
    throw Object.assign(new Error(response.error?.message ?? 'Upstream response failed'), { status: 502 })
  }
}

export function toChat(response, model) {
  assertSuccessful(response)
  let content = ''
  let reasoning = ''
  let refusal = ''
  const calls = []
  for (const item of response.output ?? []) {
    if (item.type === 'message') {
      for (const part of item.content ?? []) {
        if (part.type === 'output_text') content += part.text
        else if (part.type === 'refusal') refusal += part.refusal
      }
    } else if (item.type === 'function_call') {
      calls.push({ id: item.call_id, type: 'function', function: { name: item.name, arguments: item.arguments } })
    } else if (item.type === 'reasoning') {
      reasoning += (item.summary ?? []).map(part => part.text ?? '').join('')
    }
  }
  const message = { role: 'assistant', content: content || null }
  if (calls.length) message.tool_calls = calls
  if (reasoning) message.reasoning_content = reasoning
  if (refusal) message.refusal = refusal
  return {
    id: `chatcmpl-${response.id ?? randomUUID()}`,
    object: 'chat.completion',
    created: response.created_at ?? Math.floor(Date.now() / 1000),
    model: response.model ?? model,
    choices: [{
      index: 0,
      message,
      finish_reason: response.status === 'incomplete' ? 'length' : calls.length ? 'tool_calls' : 'stop',
    }],
    usage: usageForChat(response.usage),
  }
}

export async function* toChatStream(events, model) {
  const id = `chatcmpl-${randomUUID()}`
  const created = Math.floor(Date.now() / 1000)
  const calls = new Map()
  let completed = false
  const chunk = (delta, finish = null, usage) => ({
    id, object: 'chat.completion.chunk', created, model,
    choices: [{ index: 0, delta, finish_reason: finish }],
    ...(usage ? { usage: usageForChat(usage) } : {}),
  })
  yield chunk({ role: 'assistant', content: '' })
  for await (const event of events) {
    if (event.type === 'response.output_text.delta') {
      yield chunk({ content: event.delta })
    } else if (['response.reasoning_summary_text.delta', 'response.reasoning_text.delta'].includes(event.type)) {
      yield chunk({ reasoning_content: event.delta })
    } else if (event.type === 'response.refusal.delta') {
      yield chunk({ refusal: event.delta })
    } else if (event.type === 'response.output_item.added' && event.item.type === 'function_call') {
      const call = { index: calls.size, arguments: event.item.arguments ?? '' }
      calls.set(event.output_index, call)
      yield chunk({ tool_calls: [{
        index: call.index, id: event.item.call_id, type: 'function',
        function: { name: event.item.name, arguments: call.arguments },
      }] })
    } else if (event.type === 'response.function_call_arguments.delta') {
      const call = calls.get(event.output_index)
      if (!call) throw new Error('Received tool arguments without a tool call')
      call.arguments += event.delta
      yield chunk({ tool_calls: [{ index: call.index, function: { arguments: event.delta } }] })
    } else if (event.type === 'response.output_item.done' && event.item.type === 'function_call') {
      const call = calls.get(event.output_index)
      if (!call) throw new Error('Completed tool call without a start event')
      if (!call.arguments && event.item.arguments) {
        call.arguments = event.item.arguments
        yield chunk({ tool_calls: [{ index: call.index, function: { arguments: call.arguments } }] })
      }
    } else if (['response.completed', 'response.incomplete'].includes(event.type)) {
      assertSuccessful(event.response)
      completed = true
      yield chunk({}, event.response.status === 'incomplete' ? 'length' : calls.size ? 'tool_calls' : 'stop', event.response.usage)
    } else if (event.type === 'response.failed' || event.type === 'error') {
      throw new Error(event.response?.error?.message ?? event.message ?? 'Upstream stream failed')
    }
  }
  if (!completed) throw new Error('Upstream stream ended without a completed response')
}
