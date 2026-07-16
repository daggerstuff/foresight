import { Command } from 'commander'

import { registerContextBlocks } from './context-blocks.js'
import { registerDoctor } from './doctor.js'
import { registerGet } from './get.js'
import { registerInjectContext } from './inject-context.js'
import { registerList } from './list.js'
import { registerQuery } from './query.js'
import { registerStatus } from './status.js'
import { registerStore } from './store.js'

export function registerAll(root: Command): void {
  registerStore(root)
  registerList(root)
  registerGet(root)
  registerQuery(root)
  registerInjectContext(root)
  registerContextBlocks(root)
  registerDoctor(root)
  registerStatus(root)
}
