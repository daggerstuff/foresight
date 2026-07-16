import pc from 'picocolors'

// 5-row × 6-col block glyphs. Filled cell = full block.
// Word: F O R E S I G H T
const GLYPHS: Record<string, string[]> = {
  F: ['██████', '█     ', '████  ', '█     ', '█     '],
  O: [' ████ ', '█    █', '█    █', '█    █', ' ████ '],
  R: ['█████ ', '█    █', '█████ ', '█   █ ', '█    █'],
  E: ['██████', '█     ', '████  ', '█     ', '██████'],
  S: [' ████ ', '█     ', ' ████ ', '     █', ' ████ '],
  I: ['██████', '  ██  ', '  ██  ', '  ██  ', '██████'],
  G: [' ████ ', '█     ', '█  ██ ', '█   █ ', ' ████ '],
  H: ['█    █', '█    █', '██████', '█    █', '█    █'],
  T: ['██████', '  ██  ', '  ██  ', '  ██  ', '  ██  '],
}

const WORD = 'FORESIGHT'
const WIDTH = 150

function stripAnsiLen(s: string): number {
  return s.replace(new RegExp(String.fromCharCode(27) + '\\[[0-9;]*m', 'g'), '')
    .length
}

function center(text: string, width: number): string {
  const pad = Math.max(0, Math.floor((width - stripAnsiLen(text)) / 2))
  return ' '.repeat(pad) + text
}

export interface BannerOptions {
  /** compact single-line banner for narrow terminals */
  narrow?: boolean
  version?: string
}

export function renderBanner(opts: BannerOptions = {}): string {
  const { narrow = false, version } = opts

  if (narrow) {
    const line = version ? `foresight v${version}` : 'foresight'
    return pc.gray(line)
  }

  const rule = pc.gray('─'.repeat(WIDTH))
  const tag = pc.gray(center('persistent memory for ai agents', WIDTH))

  const rows: string[] = []
  for (let r = 0; r < 5; r++) {
    let line = ''
    for (const ch of WORD) {
      const g = GLYPHS[ch]
      line += (g ? g[r] : '      ') + ' '
    }
    rows.push(pc.gray(center(line.replace(/\s+$/, ''), WIDTH)))
  }

  const ver = version ? pc.gray(center(`v${version}`, WIDTH)) : ''

  return [rule, ...rows, ver, tag, rule].filter(Boolean).join('\n')
}
