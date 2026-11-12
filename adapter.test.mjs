import test from 'node:test'
import assert from 'node:assert/strict'
import { copyFileSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { spawnSync } from 'node:child_process'

function startWithConfig(config) {
  const directory = mkdtempSync(join(import.meta.dirname, '.adapter-test-'))
  try {
    for (const file of ['adapter.mjs', 'protocol.mjs']) {
      copyFileSync(join(import.meta.dirname, file), join(directory, file))
    }
    writeFileSync(join(directory, 'adapter-config.json'), JSON.stringify(config))
    return spawnSync(process.execPath, [join(directory, 'adapter.mjs')], {
      encoding: 'utf8',
      timeout: 10000,
    })
  } finally {
    rmSync(directory, { recursive: true, force: true })
  }
}

test('refuses to start without a non-empty local key', () => {
  for (const apiKey of [undefined, '', '   ']) {
    const result = startWithConfig({ apiKey, models: [{ id: 'test-model' }] })
    assert.ifError(result.error)
    assert.equal(result.status, 1)
    assert.match(result.stderr, /Set a non-empty local apiKey/)
  }
})

test('refuses to start with a missing, empty or malformed model catalog', () => {
  for (const models of [undefined, [], [null], [{ id: '' }]]) {
    const result = startWithConfig({ apiKey: 'test-only-not-a-real-key', models })
    assert.ifError(result.error)
    assert.equal(result.status, 1)
    assert.match(result.stderr, /Populate models/)
  }
})
