import { Routes, Route, Link } from 'react-router-dom'
import Dashboard from './pages/Dashboard'
import ProjectPrompts from './pages/ProjectPrompts'
import BusinessMetrics from './pages/BusinessMetrics'

const DEMO_MODE = import.meta.env.VITE_DEMO_MODE === 'true'

function App() {
  return (
    <>
      {DEMO_MODE && (
        <div className="bg-amber-500 text-slate-900 text-sm text-center py-2 px-4 font-medium">
          Demo Mode — Static Preview (no live backend) &nbsp;|&nbsp; Real platform:{' '}
          <a href="https://github.com/bkumars22/AIPQ" className="underline">
            github.com/bkumars22/AIPQ
          </a>
        </div>
      )}
      <nav className="max-w-5xl mx-auto px-8 pt-4 flex gap-4 text-sm text-slate-400">
        <Link to="/" className="hover:text-slate-200">Projects</Link>
        <Link to="/metrics" className="hover:text-slate-200">Business Metrics</Link>
      </nav>
      <Routes>
        <Route path="/" element={<Dashboard />} />
        <Route path="/projects/:projectId" element={<ProjectPrompts />} />
        <Route path="/metrics" element={<BusinessMetrics />} />
      </Routes>
    </>
  )
}

export default App
