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
 * 把 Markdown 渲染成可以安全放进 v-html 的 HTML 字符串。
 * 只有 components/MarkdownView.vue 应该调用它。
 */
export function renderMarkdown(src) {
  return DOMPurify.sanitize(md.render(src || ''), {
    FORBID_TAGS: ['style', 'iframe', 'form', 'input', 'object', 'embed', 'script'],
    FORBID_ATTR: ['style'],
  })
}
