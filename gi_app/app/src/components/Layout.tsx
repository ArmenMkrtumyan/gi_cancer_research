import { NavLink, Outlet } from 'react-router-dom'
import { LayoutDashboard, Database, ExternalLink, Microscope, DownloadCloud } from 'lucide-react'
import { API_BASE_URL } from '@/lib/api'
import { cn } from '@/lib/utils'

const navItems = [
  { to: '/', label: 'Overview', icon: LayoutDashboard, end: true },
  { to: '/add-data', label: 'Data', icon: DownloadCloud, end: false },
]

// External developer tools (defaults match docker-compose port mappings).
const tools = [
  { label: 'API docs', href: `${API_BASE_URL}/docs` },
  { label: 'pgAdmin', href: 'http://localhost:5050' },
  { label: 'MinIO console', href: 'http://localhost:9001' },
]

/** App shell: brand sidebar + scrollable main content (mirrors university-app). */
export default function Layout() {
  return (
    <div className="min-h-screen bg-background flex">
      <aside className="w-64 bg-brand text-white flex flex-col sticky top-0 h-screen flex-shrink-0">
        <div className="p-6 border-b border-white/10">
          <div className="flex items-center gap-2">
            <Microscope className="h-6 w-6 text-gold" />
            <h1 className="text-lg font-bold leading-tight">GI Cancer Data Platform</h1>
          </div>
        </div>

        <nav className="flex-1 p-4 overflow-y-auto">
          <ul className="space-y-1">
            {navItems.map((item) => {
              const Icon = item.icon
              return (
                <li key={item.to}>
                  <NavLink
                    to={item.to}
                    end={item.end}
                    className={({ isActive }) =>
                      cn(
                        'w-full flex items-center gap-3 px-4 py-2.5 rounded-lg transition-colors',
                        isActive
                          ? 'bg-gold text-brand font-semibold'
                          : 'text-white/70 hover:bg-white/10 hover:text-white',
                      )
                    }
                  >
                    <Icon className="h-5 w-5" />
                    <span>{item.label}</span>
                  </NavLink>
                </li>
              )
            })}
          </ul>

          <div className="mt-6 px-4">
            <p className="text-xs uppercase tracking-wide text-white/40 mb-2 flex items-center gap-1">
              <Database className="h-3 w-3" /> Developer tools
            </p>
            <ul className="space-y-1">
              {tools.map((tool) => (
                <li key={tool.label}>
                  <a
                    href={tool.href}
                    target="_blank"
                    rel="noreferrer"
                    className="flex items-center justify-between text-sm text-white/70 hover:text-gold py-1"
                  >
                    <span>{tool.label}</span>
                    <ExternalLink className="h-3.5 w-3.5" />
                  </a>
                </li>
              ))}
            </ul>
          </div>
        </nav>

      </aside>

      <main className="flex-1 min-w-0 overflow-auto">
        <div className="max-w-[1400px] mx-auto px-6 py-8">
          <Outlet />
        </div>
      </main>
    </div>
  )
}
