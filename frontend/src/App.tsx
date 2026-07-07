import { Routes, Route } from 'react-router-dom'
import Dashboard from './pages/Dashboard'
import ProjectPrompts from './pages/ProjectPrompts'

function App() {
  return (
    <Routes>
      <Route path="/" element={<Dashboard />} />
      <Route path="/projects/:projectId" element={<ProjectPrompts />} />
    </Routes>
  )
}

export default App
