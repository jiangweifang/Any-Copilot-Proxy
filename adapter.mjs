import { createServer } from 'node:http'
import { isIPv4 } from 'node:net'
import { readFileSync } from 'node:fs'
import { once } from 'node:events'
import OpenAI from 'openai'
import { toResponses, toChat, toChatStream } from './protocol.mjs'

const config = JSON.parse(readFileSync(new URL('./adapter-config.json', import.meta.url), 'utf8'))
const host = config.host === undefined ? '127.0.0.1' : config.host
if (typeof host !== 'string' || !isIPv4(host)) {
  throw new Error('Set host to a valid IPv4 address in adapter-config.json')
}
const port = config.port === undefined ? 4140 : config.port
const bridgePort = config.bridgePort === undefined ? 4141 : config.bridgePort
for (const [name, value] of Object.entries({ port, bridgePort })) {
  if (!Number.isInteger(value) || value < 1 || value > 65535) {
    throw new Error(`Set ${name} to an integer between 1 and 65535 in adapter-config.json`)
  }
}
if (port === bridgePort) {
  throw new Error('port and bridgePort must be different in adapter-config.json')
}
if (!Array.isArray(config.models) || config.models.length === 0
    || config.models.some(model => !model || typeof model.id !== 'string' || !model.id.trim())) {
  throw new Error('Populate models in adapter-config.json from the Copilot catalog; see README.md')
}
const upstreamHost = host === '0.0.0.0' ? '127.0.0.1' : host
const upstream = new OpenAI({
  apiKey: 'unused-local-bridge',
  baseURL: `http://${upstreamHost}:${bridgePort}/v1`,
  maxRetries: 0,
  timeout: 300000,
})
const models = new Map(config.models.map(model => [model.id, model]))

async function writeEvent(response, value, signal) {
  if (response.destroyed) throw new Error('Client disconnected')
  if (!response.write(`data: ${typeof value === 'string' ? value : JSON.stringify(value)}\n\n`)) {
    await once(response, 'drain', { signal })
  }
}

function json(response, status, value) {
  response.writeHead(status, { 'Content-Type': 'application/json' })
  response.end(JSON.stringify(value))
}

const server = createServer(async (request, response) => {
  const controller = new AbortController()
  response.on('close', () => {
    if (!response.writableEnded) controller.abort()
  })
  try {
    if (request.method === 'GET' && request.url === '/v1/models') {
      return json(response, 200, {
        object: 'list',
        data: config.models.map(model => ({ id: model.id, object: 'model', owned_by: 'GitHub Copilot' })),
      })
    }
    if (request.method !== 'POST' || request.url !== '/v1/chat/completions') {
      return json(response, 404, { error: { message: 'Endpoint not found' } })
    }
    const body = []
    let bytes = 0
    for await (const part of request) {
      bytes += part.length
      if (bytes > 16 * 1024 * 1024) {
        throw Object.assign(new Error('Request body exceeds 16 MiB'), { status: 413 })
      }
      body.push(part)
    }
    let payload
    try { payload = JSON.parse(Buffer.concat(body).toString('utf8')) }
    catch { throw Object.assign(new Error('Invalid JSON request'), { status: 400 }) }
    const model = models.get(payload.model)
    if (!model) throw Object.assign(new Error('Model is not in the official Copilot catalog'), { status: 400 })
    const useResponses = model.supported_endpoints?.includes('/responses')
    const options = { signal: controller.signal }
    const result = useResponses
      ? await upstream.responses.create(toResponses(payload), options)
      : await upstream.chat.completions.create(payload, options)
    if (!payload.stream) {
      return json(response, 200, useResponses ? toChat(result, payload.model) : result)
    }
    response.writeHead(200, {
      'Content-Type': 'text/event-stream',
      'Cache-Control': 'no-cache',
      Connection: 'keep-alive',
    })
    const chunks = useResponses ? toChatStream(result, payload.model) : result
    for await (const chunk of chunks) await writeEvent(response, chunk, controller.signal)
    await writeEvent(response, '[DONE]', controller.signal)
    response.end()
    console.log(`${new Date().toISOString()} ${payload.model} stream completed`)
  } catch (error) {
    if (controller.signal.aborted) return
    const message = error.message || 'Local adapter failed'
    console.error(`${new Date().toISOString()} ${message}`)
    if (response.destroyed) return
    const failure = { error: { message, type: 'copilot_adapter_error' } }
    if (response.headersSent) {
      response.end(`data: ${JSON.stringify(failure)}\n\ndata: [DONE]\n\n`)
    } else {
      json(response, error.status >= 400 && error.status <= 599 ? error.status : 502, failure)
    }
  }
})

server.listen(port, host, () => {
  console.log(`Copilot WorkBuddy adapter: http://${host}:${port} (${models.size} official models)`)
})
