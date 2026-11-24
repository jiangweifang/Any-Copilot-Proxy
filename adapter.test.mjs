import test from 'node:test'
import assert from 'node:assert/strict'
import { copyFileSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { spawn, spawnSync } from 'node:child_process'
import { createServer } from 'node:http'
import { once } from 'node:events'

function configFixture(config) {
  const directory = mkdtempSync(join(import.meta.dirname, '.adapter-test-'))
  for (const file of ['adapter.mjs', 'protocol.mjs', 'start.ps1']) {
    copyFileSync(join(import.meta.dirname, file), join(directory, file))
  }
  writeFileSync(join(directory, 'adapter-config.json'), JSON.stringify(config))
  return directory
}

function startWithConfig(config) {
  const directory = configFixture(config)
  try {
    return spawnSync(process.execPath, [join(directory, 'adapter.mjs')], {
      encoding: 'utf8',
      timeout: 10000,
    })
  } finally {
    rmSync(directory, { recursive: true, force: true })
  }
}

test('refuses to start with a missing, empty or malformed model catalog', () => {
  for (const models of [undefined, [], [null], [{ id: '' }]]) {
    const result = startWithConfig({ models })
    assert.ifError(result.error)
    assert.equal(result.status, 1)
    assert.match(result.stderr, /Populate models/)
  }
})

test('refuses invalid IPv4 listener addresses', () => {
  for (const host of [null, '', 'localhost', 0, ['127.0.0.1'], '256.0.0.1', '127.00.0.1', '127.1']) {
    const result = startWithConfig({ host, models: [{ id: 'test-model' }] })
    assert.ifError(result.error)
    assert.equal(result.status, 1)
    assert.match(result.stderr, /Set host to a valid IPv4 address/)
  }
})

test('refuses invalid or conflicting adapter and bridge ports', () => {
  const config = { models: [{ id: 'test-model' }] }
  for (const name of ['port', 'bridgePort']) {
    for (const value of [null, 0, -1, 65536, 4140.5, '4140']) {
      const result = startWithConfig({ ...config, [name]: value })
      assert.ifError(result.error)
      assert.equal(result.status, 1)
      assert.match(result.stderr, new RegExp(`Set ${name} to an integer`))
    }
  }
  for (const ports of [{ port: 4240, bridgePort: 4240 }, { port: 4141 }, { bridgePort: 4140 }]) {
    const result = startWithConfig({ ...config, ...ports })
    assert.ifError(result.error)
    assert.equal(result.status, 1)
    assert.match(result.stderr, /port and bridgePort must be different/)
  }
})

async function closeServer(server) {
  if (server.listening) {
    await new Promise((resolve, reject) => server.close(error => error ? reject(error) : resolve()))
  }
}

for (const host of [undefined, '127.0.0.1', '0.0.0.0', '127.0.0.2']) {
  test(`serves requests without API key or Origin restrictions with host ${host ?? 'default'}`, { timeout: 15000 }, async t => {
    const expectedHost = host ?? '127.0.0.1'
    const connectHost = expectedHost === '0.0.0.0' ? '127.0.0.1' : expectedHost
    const requests = []
    const bridge = createServer(async (request, response) => {
      let body = ''
      for await (const part of request) body += part
      requests.push({ url: request.url, body: JSON.parse(body) })
      response.writeHead(200, { 'Content-Type': 'application/json' })
      response.end(JSON.stringify({
        id: 'test-completion',
        object: 'chat.completion',
        model: 'test-model',
        choices: [{ index: 0, message: { role: 'assistant', content: 'Test reply' }, finish_reason: 'stop' }],
      }))
    })
    t.after(() => closeServer(bridge))
    bridge.listen(0, connectHost)
    await once(bridge, 'listening')
    const reservation = createServer()
    t.after(() => closeServer(reservation))
    reservation.listen(0, connectHost)
    await once(reservation, 'listening')
    const port = reservation.address().port
    const directory = configFixture({
      host,
      port,
      bridgePort: bridge.address().port,
      models: [{ id: 'test-model' }],
    })
    await closeServer(reservation)
    let child
    t.after(async () => {
      if (child && child.exitCode === null && child.signalCode === null) {
        const exited = once(child, 'exit')
        child.kill()
        await exited
      }
      rmSync(directory, { recursive: true, force: true })
    })
    child = spawn(process.execPath, [join(directory, 'adapter.mjs')], { stdio: ['ignore', 'pipe', 'pipe'] })
    let stderr = ''
    child.stderr.on('data', chunk => { stderr += chunk })
    await new Promise((resolve, reject) => {
      child.once('error', reject)
      child.once('exit', code => reject(new Error(`Adapter exited (${code}): ${stderr}`)))
      child.stdout.on('data', chunk => {
        if (chunk.toString().includes(`http://${expectedHost}:${port}`)) resolve()
      })
    })
    if (process.platform === 'win32') {
      const listener = spawnSync('powershell', ['-NoProfile', '-NonInteractive', '-Command',
        `(Get-NetTCPConnection -State Listen -LocalPort ${port} -OwningProcess ${child.pid} -ErrorAction Stop).LocalAddress`,
      ], { encoding: 'utf8', timeout: 10000 })
      assert.ifError(listener.error)
      assert.equal(listener.status, 0, listener.stderr)
      assert.equal(listener.stdout.trim(), expectedHost)
    }
    const baseURL = `http://${connectHost}:${port}/v1`
    const models = await fetch(`${baseURL}/models`)
    assert.equal(models.status, 200)
    assert.equal((await models.json()).data[0].id, 'test-model')
    const placeholder = await fetch(`${baseURL}/models`, {
      headers: { Authorization: 'Bearer unused' },
    })
    assert.equal(placeholder.status, 200)
    for (const origin of ['https://example.com', 'null', 'not-a-url']) {
      const response = await fetch(`${baseURL}/models`, { headers: { Origin: origin } })
      assert.equal(response.status, 200)
      assert.equal((await response.json()).data[0].id, 'test-model')
    }
    const payload = { model: 'test-model', messages: [{ role: 'user', content: 'Hello' }] }
    const completion = await fetch(`${baseURL}/chat/completions`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Origin: 'https://example.com' },
      body: JSON.stringify(payload),
    })
    assert.equal(completion.status, 200)
    assert.equal((await completion.json()).choices[0].message.content, 'Test reply')
    assert.deepEqual(requests, [{ url: '/v1/chat/completions', body: payload }])
  })
}

test('Windows launcher validates host and ports, uses configured values and preserves legacy defaults', { skip: process.platform !== 'win32' }, () => {
  for (const [config, error] of [
    [{}, null],
    [{ host: '127.0.0.1', port: 4240, bridgePort: 4241 }, null],
    [{ host: '0.0.0.0', port: 4240, bridgePort: 4241 }, null],
    [{ host: '10.172.10.125', port: 4240, bridgePort: 4241 }, null],
    [{ host: null }, /Set host to/],
    [{ host: 'localhost' }, /Set host to/],
    [{ host: ['127.0.0.1'] }, /Set host to/],
    [{ host: '256.0.0.1' }, /Set host to/],
    [{ host: '127.00.0.1' }, /Set host to/],
    [{ host: '127.1' }, /Set host to/],
    [{ port: 0 }, /Set port to an integer/],
    [{ bridgePort: '4141' }, /Set bridgePort to an integer/],
    [{ port: 4240, bridgePort: 4240 }, /port and bridgePort must be different/],
  ]) {
    const directory = configFixture(config)
    try {
      const script = `
        function Get-NetTCPConnection {
          [CmdletBinding()]
          param($State, $LocalPort)
          Write-Host "CHECK_PORT $LocalPort"
        }
        function Start-Process {
          param($FilePath, $ArgumentList, $WorkingDirectory, [switch]$NoNewWindow,
            [switch]$PassThru, $RedirectStandardOutput, $RedirectStandardError)
          Write-Host ("START_ARGS " + ($ArgumentList -join ' '))
          [PSCustomObject]@{ HasExited = $true; Id = 0 }
        }
        try { & '${join(directory, 'start.ps1').replaceAll("'", "''")}' }
        catch {
          if ($_.Exception.Message -notlike 'Copilot service stopped unexpectedly*') { throw }
        }
        exit 0
      `
      const result = spawnSync('powershell', ['-NoProfile', '-NonInteractive', '-Command', script], {
        encoding: 'utf8',
        timeout: 10000,
      })
      assert.ifError(result.error)
      if (error) {
        assert.equal(result.status, 1)
        assert.match(result.stderr, error)
        assert.doesNotMatch(result.stdout, /START_ARGS/)
        continue
      }
      assert.equal(result.status, 0, result.stderr || result.stdout)
      assert.match(result.stdout, new RegExp(`CHECK_PORT ${config.port ?? 4140}\\b`))
      assert.match(result.stdout, new RegExp(`CHECK_PORT ${config.bridgePort ?? 4141}\\b`))
      assert.match(result.stdout, new RegExp(`--port ${config.bridgePort ?? 4141}\\b`))
      assert.ok(result.stdout.includes(`--host ${config.host ?? '127.0.0.1'} `), result.stdout)
    } finally {
      rmSync(directory, { recursive: true, force: true })
    }
  }
})
