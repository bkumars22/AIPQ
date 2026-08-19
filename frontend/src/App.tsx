import { Routes, Route, Link } from 'react-router-dom'
import Dashboard from './pages/Dashboard'
import ProjectPrompts from './pages/ProjectPrompts'
import BusinessMetrics from './pages/BusinessMetrics'
import ABTestDetail from './pages/ABTestDetail'

function App() {
  return (
    <>
      <nav className="max-w-5xl mx-auto px-8 pt-4 flex gap-4 text-sm text-slate-400">
        <Link to="/" className="hover:text-slate-200">Projects</Link>
        <Link to="/metrics" className="hover:text-slate-200">Business Metrics</Link>
      </nav>
      <Routes>
        <Route path="/" element={<Dashboard />} />
        <Route path="/projects/:projectId" element={<ProjectPrompts />} />
        <Route path="/metrics" element={<BusinessMetrics />} />
        <Route path="/ab-tests/:id" element={<ABTestDetail />} />
      </Routes>
    </>
  )
}

export default App
