import { execFileSync } from 'node:child_process'

import { describe, it, expect } from 'vitest'

function run(args: string[]): string {
  return execFileSync('node', ['dist/index.mjs', ...args], {
    encoding: 'utf8',
  })
}

describe('foresight cli', () => {
  it('prints version with -v', () => {
    const out = run(['-v'])
    expect(out.trim()).toMatch(/^\d+\.\d+\.\d+/)
  })

  it('prints help with -h', () => {
    const out = run(['-h'])
    expect(out).toContain('foresight')
    expect(out).toContain('store')
    expect(out).toContain('doctor')
  })
})
