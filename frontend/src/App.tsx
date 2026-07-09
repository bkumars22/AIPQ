import { Routes, Route } from 'react-router-dom'
import Dashboard from './pages/Dashboard'
import ProjectPrompts from './pages/ProjectPrompts'

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
      <Routes>
        <Route path="/" element={<Dashboard />} />
        <Route path="/projects/:projectId" element={<ProjectPrompts />} />
      </Routes>
    </>
  )
}

export default App
