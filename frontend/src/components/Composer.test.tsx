import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'

import { Composer } from './Composer'

const handlers = {
  onSubmit: vi.fn(async () => undefined),
  onPause: vi.fn(async () => undefined),
  onCancel: vi.fn(async () => undefined),
}

describe('Composer 主按钮状态', () => {
  it('空闲状态显示发送按钮', () => {
    const html = renderToStaticMarkup(
      <Composer submitting={false} controlPending={null} {...handlers} />,
    )
    expect(html).toContain('aria-label="发送消息"')
  })

  it('运行状态显示停止和暂停入口', () => {
    const html = renderToStaticMarkup(
      <Composer runStatus="running" submitting={false} controlPending={null} {...handlers} />,
    )
    expect(html).toContain('aria-label="停止当前运行"')
    expect(html).toContain('aria-label="暂停当前运行"')
  })

  it('暂停状态显示继续三角入口', () => {
    const html = renderToStaticMarkup(
      <Composer runStatus="paused" submitting={false} controlPending={null} {...handlers} />,
    )
    expect(html).toContain('aria-label="继续运行"')
  })
})
