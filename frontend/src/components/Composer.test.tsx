import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'

import { Composer } from './Composer'

const handlers = {
  onSubmit: vi.fn(async () => undefined),
  onPause: vi.fn(async () => undefined),
  onResume: vi.fn(async () => undefined),
  onQueryChange: vi.fn(),
}

describe('Composer 主按钮状态', () => {
  it('空闲状态显示发送按钮', () => {
    const html = renderToStaticMarkup(
      <Composer query="" submitting={false} controlPending={null} {...handlers} />,
    )
    expect(html).toContain('aria-label="发送消息"')
  })

  it('运行状态的主按钮表示暂停', () => {
    const html = renderToStaticMarkup(
      <Composer query="" runStatus="running" submitting={false} controlPending={null} {...handlers} />,
    )
    expect(html).toContain('aria-label="暂停当前运行"')
    expect(html).not.toContain('取消当前 Run')
  })

  it('运行时有输入则主按钮表示 Steering', () => {
    const html = renderToStaticMarkup(
      <Composer query="调整方向" runStatus="running" submitting={false} controlPending={null} {...handlers} />,
    )
    expect(html).toContain('aria-label="发送 Steering"')
  })

  it('暂停状态显示继续三角入口', () => {
    const html = renderToStaticMarkup(
      <Composer query="" runStatus="paused" submitting={false} controlPending={null} {...handlers} />,
    )
    expect(html).toContain('aria-label="继续运行"')
  })
})
