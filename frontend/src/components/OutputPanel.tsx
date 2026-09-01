import { Bot, Sparkles } from 'lucide-react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

interface OutputPanelProps {
  outputText: string
  isRunning: boolean
}

/** 流式展示模型文本，并在空状态说明页面的首要操作。 */
export function OutputPanel({ outputText, isRunning }: OutputPanelProps) {
  if (!outputText) {
    return (
      <section className="empty-state">
        <div className="empty-orbit"><Sparkles size={24} /></div>
        <span className="eyebrow">AGENT WORKSPACE</span>
        <h1>把目标交给 Hexgent</h1>
        <p>模型推理、工具执行、Plan 进度和上下文整理都会实时汇聚在这里。</p>
      </section>
    )
  }

  return (
    <article className="agent-output">
      <header>
        <span className="agent-avatar"><Bot size={18} /></span>
        <div><strong>Hexgent</strong><small>{isRunning ? '正在响应' : '本轮输出'}</small></div>
        {isRunning && <span className="typing-dots"><i /><i /><i /></span>}
      </header>
      <div className="markdown-body">
        <ReactMarkdown remarkPlugins={[remarkGfm]}>{outputText}</ReactMarkdown>
      </div>
    </article>
  )
}
