/**
 * 轻量 Markdown 渲染器：只覆盖诊断/告警报告实际用到的语法。
 *
 * 不引第三方库的原因：报告体量小、语法固定，自研可省掉一个依赖；
 * 安全性靠「先整体转义、再套受控标签」保证——输出里出现的标签只有本文件生成的，
 * 原文里的 <script> 之类一律变成 &lt;script&gt;。
 *
 * 支持：h1~h4、段落、无序/有序列表、表格、围栏代码块、行内代码、粗体、斜体、
 * 链接、引用、分隔线。
 */

const ESCAPES: Record<string, string> = {
  '&': '&amp;',
  '<': '&lt;',
  '>': '&gt;',
  '"': '&quot;',
  "'": '&#39;'
}

function escapeHtml(text: string): string {
  return text.replace(/[&<>"']/g, (ch) => ESCAPES[ch])
}

/** 行内语法：先把行内代码抽成占位符，避免其中的 * _ 被当作强调。 */
function renderInline(raw: string): string {
  const codes: string[] = []
  let out = escapeHtml(raw)

  out = out.replace(/`([^`]+)`/g, (_m, code: string) => {
    codes.push(code)
    return `\u0000${codes.length - 1}\u0000`
  })

  out = out.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
  out = out.replace(/\*([^*\n]+)\*/g, '<em>$1</em>')

  out = out.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (_m, label: string, href: string) => {
    const safe = /^(https?:\/\/|mailto:|\/|#)/i.test(href) ? href : '#'
    return `<a href="${safe}" target="_blank" rel="noopener noreferrer">${label}</a>`
  })

  return out.replace(/\u0000(\d+)\u0000/g, (_m, idx: string) => `<code>${codes[Number(idx)]}</code>`)
}

function splitRow(line: string): string[] {
  const trimmed = line.trim().replace(/^\|/, '').replace(/\|$/, '')
  return trimmed.split('|').map((cell) => cell.trim())
}

const TABLE_DIVIDER = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/
const HEADING = /^(#{1,4})\s+(.*)$/
const UL_ITEM = /^[-*+]\s+(.*)$/
const OL_ITEM = /^\d+\.\s+(.*)$/
const FENCE = /^```/

export function renderMarkdown(source: string): string {
  if (!source) return ''

  const lines = source.replace(/\r\n?/g, '\n').split('\n')
  const out: string[] = []
  let para: string[] = []
  let listType: 'ul' | 'ol' | null = null

  const flushPara = () => {
    if (para.length) {
      out.push(`<p>${renderInline(para.join(' '))}</p>`)
      para = []
    }
  }

  const closeList = () => {
    if (listType) {
      out.push(`</${listType}>`)
      listType = null
    }
  }

  let i = 0
  while (i < lines.length) {
    const line = lines[i]
    const trimmed = line.trim()

    if (FENCE.test(trimmed)) {
      flushPara()
      closeList()
      const body: string[] = []
      i += 1
      while (i < lines.length && !FENCE.test(lines[i].trim())) {
        body.push(lines[i])
        i += 1
      }
      i += 1
      out.push(`<pre><code>${escapeHtml(body.join('\n'))}</code></pre>`)
      continue
    }

    if (!trimmed) {
      flushPara()
      closeList()
      i += 1
      continue
    }

    const heading = HEADING.exec(trimmed)
    if (heading) {
      flushPara()
      closeList()
      const level = heading[1].length
      out.push(`<h${level}>${renderInline(heading[2])}</h${level}>`)
      i += 1
      continue
    }

    if (/^(---+|\*\*\*+)$/.test(trimmed)) {
      flushPara()
      closeList()
      out.push('<hr />')
      i += 1
      continue
    }

    // 表格：当前行含 | 且下一行是分隔行
    if (
      trimmed.includes('|') &&
      i + 1 < lines.length &&
      TABLE_DIVIDER.test(lines[i + 1]) &&
      lines[i + 1].includes('-')
    ) {
      flushPara()
      closeList()
      const head = splitRow(lines[i])
      i += 2
      const rows: string[][] = []
      while (i < lines.length && lines[i].trim().includes('|')) {
        rows.push(splitRow(lines[i]))
        i += 1
      }
      const thead = `<thead><tr>${head.map((c) => `<th>${renderInline(c)}</th>`).join('')}</tr></thead>`
      const tbody = rows.length
        ? `<tbody>${rows
            .map((r) => `<tr>${r.map((c) => `<td>${renderInline(c)}</td>`).join('')}</tr>`)
            .join('')}</tbody>`
        : ''
      out.push(`<div class="md-table"><table>${thead}${tbody}</table></div>`)
      continue
    }

    if (trimmed.startsWith('>')) {
      flushPara()
      closeList()
      const quoted: string[] = []
      while (i < lines.length && lines[i].trim().startsWith('>')) {
        quoted.push(lines[i].trim().replace(/^>\s?/, ''))
        i += 1
      }
      out.push(`<blockquote>${renderInline(quoted.join(' '))}</blockquote>`)
      continue
    }

    const ul = UL_ITEM.exec(trimmed)
    const ol = OL_ITEM.exec(trimmed)
    if (ul || ol) {
      flushPara()
      const want: 'ul' | 'ol' = ul ? 'ul' : 'ol'
      if (listType && listType !== want) closeList()
      if (!listType) {
        listType = want
        out.push(`<${want}>`)
      }
      out.push(`<li>${renderInline((ul || ol)![1])}</li>`)
      i += 1
      continue
    }

    closeList()
    para.push(trimmed)
    i += 1
  }

  flushPara()
  closeList()
  return out.join('\n')
}
