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
if (config.models !== undefined && (!Array.isArray(config.models)
    || config.models.some(model => !model || typeof model.id !== 'string' || !model.id.trim()))) {
  throw new Error('models in adapter-config.json must be a list of objects with an id, or be removed; see README.md')
}
const overrides = new Map((config.models ?? []).map(model => [model.id, model]))
const upstreamHost = host === '0.0.0.0' ? '127.0.0.1' : host
const bridgeURL = `http://${upstreamHost}:${bridgePort}/v1`
const upstream = new OpenAI({
  apiKey: 'unused-local-bridge',
  baseURL: bridgeURL,
  maxRetries: 0,
  timeout: 300000,
})

const CATALOG_TTL = 5 * 60 * 1000
const FORCED_REFRESH_INTERVAL = 10 * 1000
let catalog = null
let catalogFetchedAt = 0
let forcedRefreshAt = 0
let catalogRequest = null
// Models without advertised endpoints that upstream rejected on Chat Completions.
const responsesOnly = new Set()

async function fetchCatalog() {
  const response = await fetch(`${bridgeURL}/models`, { signal: AbortSignal.timeout(30000) })
  if (!response.ok) throw new Error(`Bridge returned ${response.status} for /v1/models`)
  const body = await response.json()
  if (!Array.isArray(body?.data)) throw new Error('Bridge returned an invalid model catalog')
  const models = body.data.filter(model => typeof model?.id === 'string' && model.id.trim())
  if (models.length === 0) throw new Error('Bridge returned an empty model catalog')
  return new Map(models.map(model => [model.id, { ...model, ...overrides.get(model.id) }]))
}

async function getCatalog({ refresh = false } = {}) {
  const now = Date.now()
  const stale = !catalog || now - catalogFetchedAt >= CATALOG_TTL
  const forced = !stale && refresh && now - forcedRefreshAt >= FORCED_REFRESH_INTERVAL
  if (!stale && !forced && !catalogRequest) return catalog
  if (forced) forcedRefreshAt = now
  catalogRequest ??= (async () => {
    try {
      const next = await fetchCatalog()
      const added = catalog ? [...next.keys()].filter(id => !catalog.has(id)).length : next.size
      if (added) console.log(`${new Date().toISOString()} model catalog: ${next.size} models (${added} new)`)
      catalog = next
      catalogFetchedAt = Date.now()
    } catch (error) {
      if (!catalog) {
        throw Object.assign(new Error(`Copilot model catalog is unavailable: ${error.message}`), { status: 503 })
      }
      console.error(`${new Date().toISOString()} model catalog refresh failed, using cached list: ${error.message}`)
    } finally {
      catalogRequest = null
    }
    return catalog
  })()
  return catalogRequest
}

function requiresResponses(error) {
  return error?.status === 400 && /unsupported_api_for_model|not (?:accessible|supported) (?:via|by|on) (?:the )?\/?chat\/completions|does not support (?:the )?\/?chat\/completions/i
    .test(`${error.message} ${JSON.stringify(error.error ?? '')}`)
}

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
      const models = await getCatalog()
      return json(response, 200, {
        object: 'list',
        data: [...models.keys()].map(id => ({ id, object: 'model', owned_by: 'GitHub Copilot' })),
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
    let model = (await getCatalog()).get(payload.model)
    model ??= (await getCatalog({ refresh: true })).get(payload.model)
    if (!model) throw Object.assign(new Error('Model is not in the official Copilot catalog'), { status: 400 })
    const endpoints = Array.isArray(model.supported_endpoints) && model.supported_endpoints.length
      ? model.supported_endpoints
      : null
    let useResponses = endpoints ? endpoints.includes('/responses') : responsesOnly.has(model.id)
    const options = { signal: controller.signal }
    let result
    if (!useResponses) {
      try {
        result = await upstream.chat.completions.create(payload, options)
      } catch (error) {
        if (endpoints || !requiresResponses(error)) throw error
        responsesOnly.add(model.id)
        useResponses = true
        console.log(`${new Date().toISOString()} ${model.id} requires Responses; switching endpoint`)
      }
    }
    if (useResponses) result = await upstream.responses.create(toResponses(payload), options)
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
  console.log(`Copilot WorkBuddy adapter: http://${host}:${port}`)
  getCatalog().catch(error => {
    console.error(`${new Date().toISOString()} ${error.message}; will retry on the next request`)
  })
})
