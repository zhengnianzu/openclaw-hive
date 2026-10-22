import MarkdownIt from 'markdown-it'
import DOMPurify from 'dompurify'

// html: false —— 原始 HTML 一律转义成文本，这是最强的 XSS 防线；
// DOMPurify 是第二道闸（管理员账号被借用的场景）。
const md = new MarkdownIt({
  html: false,
  linkify: true,
  breaks: true,
  typographer: false,
})

const defaultLinkOpen =
  md.renderer.rules.link_open ||
  ((tokens, idx, options, env, self) => self.renderToken(tokens, idx, options))

md.renderer.rules.link_open = (tokens, idx, options, env, self) => {
  tokens[idx].attrSet('target', '_blank')
  tokens[idx].attrSet('rel', 'noopener noreferrer')
  return defaultLinkOpen(tokens, idx, options, env, self)
}

/**
 * 图片渲染规则。
 *
 * Milkdown Crepe 用 Markdown 的 alt 字段存缩放比例：
 *     ![0.62](/api/wiki/assets/xxx.webp)   → 显示为 62% 宽度
 * 所以 alt 是纯数字（>0 且 <=1）时当作比例处理，否则按普通替代文本渲染。
 * 历史文档里没有纯数字 alt 的图片，不会误伤。
 */
const defaultImage =
  md.renderer.rules.image ||
  ((tokens, idx, options, env, self) => self.renderToken(tokens, idx, options))

md.renderer.rules.image = (tokens, idx, options, env, self) => {
  const token = tokens[idx]
  const alt = (token.content || '').trim()
  const ratio = Number(alt)

  if (alt && Number.isFinite(ratio) && ratio > 0 && ratio <= 1) {
    token.attrSet('style', `width:${(ratio * 100).toFixed(1)}%`)
    token.attrSet('data-ratio', String(ratio))
    token.content = '' // 不要把 "0.62" 显示成替代文本
  }
  return defaultImage(tokens, idx, options, env, self)
}

/**
 * 只保留图片上的 width 样式。
 * DOMPurify 默认会保留 style，但会把任意 CSS 都放进来；这里收窄到 width 百分比，
 * 避免通过 style 注入其它属性（比如 position:fixed 覆盖界面）。
 */
DOMPurify.addHook('afterSanitizeAttributes', (node) => {
  if (node.tagName === 'IMG') {
    const style = node.getAttribute('style') || ''
    const m = style.match(/(?:^|;)\s*width\s*:\s*([\d.]+%)\s*(?:;|$)/i)
    if (m) {
      node.setAttribute('style', `width:${m[1]}`)
      // 比例数字被当作 alt 存着，不要让它残留在 DOM 里
      if (/^[\d.]+$/.test(node.getAttribute('alt') || '')) node.setAttribute('alt', '')
    } else {
      node.removeAttribute('style')
    }
    node.removeAttribute('data-ratio')
  }
  if (node.tagName === 'A') {
    node.setAttribute('target', '_blank')
    node.setAttribute('rel', 'noopener noreferrer')
  }
})

/**
 * 把 Markdown 渲染成可以安全放进 v-html 的 HTML 字符串。
 * 只有 components/MarkdownView.vue 应该调用它。
 */
export function renderMarkdown(src) {
  return DOMPurify.sanitize(md.render(src || ''), {
    FORBID_TAGS: ['style', 'iframe', 'form', 'input', 'object', 'embed', 'script'],
  })
}
