/** Shell and routing for the three frozen routes. */

import { NavLink, Route, Routes } from 'react-router-dom'

import { Exceptions } from './routes/Exceptions'
import { Metrics } from './routes/Metrics'
import { ReviewQueue } from './routes/ReviewQueue'
import { FOCUS_RING } from './components/ui'

const LINKS: readonly { to: string; label: string }[] = [
  { to: '/', label: 'Review queue' },
  { to: '/exceptions', label: 'Exceptions' },
  { to: '/metrics', label: 'Metrics' },
]

function navClass({ isActive }: { isActive: boolean }): string {
  return `rounded-md px-3 py-1.5 text-sm font-medium ${FOCUS_RING} ${
    isActive ? 'bg-slate-900 text-white' : 'text-slate-700 hover:bg-slate-100'
  }`
}

export function App() {
  return (
    <div className="min-h-screen bg-slate-50 text-slate-900">
      <a
        href="#main"
        className={`sr-only focus:not-sr-only focus:absolute focus:m-2 focus:rounded focus:bg-white focus:px-3 focus:py-2 ${FOCUS_RING}`}
      >
        Skip to content
      </a>
      <header className="border-b border-slate-200 bg-white">
        <div className="mx-auto flex max-w-7xl flex-wrap items-center gap-4 px-4 py-3">
          <span className="text-lg font-semibold">Crossfoot</span>
          <nav aria-label="Main" className="flex gap-1">
            {LINKS.map((link) => (
              <NavLink key={link.to} to={link.to} end={link.to === '/'} className={navClass}>
                {link.label}
              </NavLink>
            ))}
          </nav>
        </div>
      </header>
      <main id="main" className="mx-auto max-w-7xl px-4 py-4">
        <Routes>
          <Route path="/" element={<ReviewQueue />} />
          <Route path="/exceptions" element={<Exceptions />} />
          <Route path="/metrics" element={<Metrics />} />
          <Route
            path="*"
            element={<p className="text-sm text-slate-600">There is no page at that address.</p>}
          />
        </Routes>
      </main>
    </div>
  )
}

export default App
