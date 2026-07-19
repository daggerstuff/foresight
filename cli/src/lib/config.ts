export interface Identity {
  user: string
  account: string
}

export interface ForesightConfig {
  dbUrl?: string
  redisUrl?: string
  userId: string
  account: string
  bankId: string
  identity?: string
}

function fallbackUser(): string {
  return process.env.USER ?? process.env.USERNAME ?? 'default'
}

// Mimics the Python config: FORESIGHT_IDENTITY = "user@account"
//   -> user_id = user, tenant_id (account) = account
function parseIdentity(raw: string | undefined): Identity {
  const user = fallbackUser()
  if (!raw)
    return { user: process.env.FORESIGHT_USER_ID ?? user, account: 'default' }
  const [idPart, accountPart] = raw.split('@')
  return {
    user: process.env.FORESIGHT_USER_ID ?? idPart ?? user,
    account: accountPart ?? 'default',
  }
}

export function loadConfig(): ForesightConfig {
  const identity = parseIdentity(process.env.FORESIGHT_IDENTITY)
  return {
    dbUrl: process.env.FORESIGHT_DB_URL,
    redisUrl: process.env.FORESIGHT_REDIS_URL,
    userId: identity.user,
    account: identity.account,
    bankId: process.env.FORESIGHT_BANK_ID ?? 'default',
    identity: process.env.FORESIGHT_IDENTITY,
  }
}
