import { Redis } from 'ioredis'
import { Pool } from 'pg'
import type { PoolClient } from 'pg'

import type { ForesightConfig } from './config.js'

let pool: Pool | undefined
let redis: Redis | undefined

function withNoVerifySsl(dbUrl: string): string {
  const normalized = dbUrl.replace(/([?&]sslmode=)[^&]+/i, '$1no-verify')
  if (/[?&]sslmode=no-verify/i.test(normalized)) return normalized
  const sep = normalized.includes('?') ? '&' : '?'
  return `${normalized}${sep}sslmode=no-verify`
}

export function getPool(cfg: ForesightConfig): Pool {
  if (!cfg.dbUrl) {
    throw new Error(
      'FORESIGHT_DB_URL is not set. Export the Ghost Postgres connection string.',
    )
  }
  if (!pool) {
    const connectionString = withNoVerifySsl(cfg.dbUrl)
    pool = new Pool({
      connectionString,
      max: 4,
      ssl: { rejectUnauthorized: false },
    })
  }
  return pool
}

export function getRedis(cfg: ForesightConfig): Redis {
  redis ??= new Redis(cfg.redisUrl ?? 'redis://localhost:6379/0', {
    lazyConnect: true,
    maxRetriesPerRequest: 2,
  })
  return redis
}

export async function withClient<T>(
  cfg: ForesightConfig,
  fn: (client: PoolClient) => Promise<T>,
): Promise<T> {
  const p = getPool(cfg)
  const client = await p.connect()
  try {
    return await fn(client)
  } finally {
    client.release()
  }
}

export async function closeConnections(): Promise<void> {
  await Promise.all([
    pool ? pool.end() : Promise.resolve(),
    redis ? redis.quit() : Promise.resolve(),
  ])
  pool = undefined
  redis = undefined
}
