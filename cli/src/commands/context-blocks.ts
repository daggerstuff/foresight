import { Command, Option } from 'commander'

import { loadConfig } from '../lib/config.js'
import { withClient } from '../lib/db.js'
import { fail, info, spinner } from '../lib/ui.js'

interface CBOpts {
  local?: boolean
}

export function registerContextBlocks(cmd: Command): void {
  const sub = cmd
    .command('context-blocks')
    .description('manage context blocks (guidance, preferences, context)')
    .addOption(new Option('--local', 'internal: local-only mode').hideHelp())

  sub
    .command('list')
    .description('list all context blocks for the identity')
    .action(async (_: unknown, opts: CBOpts) => {
      const cfg = loadConfig()
      const s = spinner('loading blocks')
      try {
        const rows = await withClient(cfg, async (client) => {
          const res = await client.query(
            `SELECT label, updated_at, content FROM context_blocks
              WHERE tenant_id = $1 AND user_id = $2
              ORDER BY label`,
            [cfg.account, cfg.userId],
          )
          return res.rows
        })
        s.stop()
        if (rows.length === 0) {
          info('no context blocks')
          return
        }
        for (const b of rows) {
          console.log(`\n# ${b.label}  (${b.updated_at})`)
          console.log(b.content)
        }
      } catch (e) {
        s.stop()
        fail(e instanceof Error ? e.message : String(e))
        process.exitCode = 1
      }
    })

  sub
    .command('get <label>')
    .description('print a single context block')
    .action(async (label: string, _: unknown, opts: CBOpts) => {
      const cfg = loadConfig()
      const s = spinner('loading block')
      try {
        const row = await withClient(cfg, async (client) => {
          const res = await client.query(
            `SELECT content FROM context_blocks
              WHERE tenant_id = $1 AND user_id = $2 AND label = $3`,
            [cfg.account, cfg.userId, label],
          )
          return res.rows[0]
        })
        s.stop()
        if (!row) {
          fail(`block ${label} not found`)
          process.exitCode = 1
          return
        }
        console.log(row.content)
      } catch (e) {
        s.stop()
        fail(e instanceof Error ? e.message : String(e))
        process.exitCode = 1
      }
    })

  sub
    .command('update <label>')
    .description('create or replace a context block')
    .requiredOption('-c, --content <text>', 'block content')
    .action(async (label: string, opts: { content: string } & CBOpts) => {
      const cfg = loadConfig()
      const s = spinner('saving block')
      try {
        await withClient(cfg, async (client) => {
          await client.query(
            `INSERT INTO context_blocks (tenant_id, user_id, label, content, updated_at)
             VALUES ($1, $2, $3, $4, NOW()::text)
             ON CONFLICT (tenant_id, user_id, label)
             DO UPDATE SET content = EXCLUDED.content, updated_at = EXCLUDED.updated_at`,
            [cfg.account, cfg.userId, label, opts.content],
          )
        })
        s.stop()
        info(`saved ${label}`)
      } catch (e) {
        s.stop()
        fail(e instanceof Error ? e.message : String(e))
        process.exitCode = 1
      }
    })

  sub
    .command('delete <label>')
    .description('delete a context block')
    .action(async (label: string, _: unknown, opts: CBOpts) => {
      const cfg = loadConfig()
      const s = spinner('deleting block')
      try {
        await withClient(cfg, async (client) => {
          await client.query(
            `DELETE FROM context_blocks
              WHERE tenant_id = $1 AND user_id = $2 AND label = $3`,
            [cfg.account, cfg.userId, label],
          )
        })
        s.stop()
        info(`deleted ${label}`)
      } catch (e) {
        s.stop()
        fail(e instanceof Error ? e.message : String(e))
        process.exitCode = 1
      }
    })
}
