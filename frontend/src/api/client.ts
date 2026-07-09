import { DEMO_DRIFT, DEMO_PROJECTS, DEMO_PROMPTS, DEMO_VERSIONS } from './demoData'

const API_URL = import.meta.env.VITE_API_URL || 'http://localhost:8001'
const DEV_JWT = import.meta.env.VITE_DEV_JWT || ''
const DEMO_MODE = import.meta.env.VITE_DEMO_MODE === 'true'

// Demo builds (GitHub Pages) have no live backend — a tiny artificial delay
// keeps the loading state from being an imperceptible flash, same as a real request.
const demoDelay = <T,>(value: T): Promise<T> => new Promise(resolve => setTimeout(() => resolve(value), 200))

async function apiGet<T>(path: string): Promise<T> {
  const res = await fetch(`${API_URL}${path}`, {
    headers: { Authorization: `Bearer ${DEV_JWT}` },
  })
  if (!res.ok) {
    throw new Error(`${res.status} ${res.statusText} — ${await res.text()}`)
  }
  return res.json() as Promise<T>
}

export interface ProjectSummary {
  id: number
  name: string
  pipeline_type: string
  prompt_count: number
  avg_quality_score: number | null
  created_at: string
}

export interface PromptSummary {
  id: number
  prompt_name: string
  description: string | null
  current_version_number: number | null
  quality_score: number | null
  status: string | null
  deployed_at: string | null
}

export interface PromptVersionSummary {
  id: number
  version_number: number
  quality_score: number | null
  status: string
  changed_by: string
  change_message: string | null
  created_at: string
  deployed_at: string | null
}

export interface DriftStatus {
  prompt_id: number
  prompt_name: string
  current_version_id: number | null
  current_version_number: number | null
  deployed_at: string | null
  quality_score: number | null
  recent_drift_severity: string | null
  changed_recently: boolean
  root_cause_hint: string
}

export const api = {
  listProjects: () =>
    DEMO_MODE ? demoDelay(DEMO_PROJECTS)
      : apiGet<{ projects: ProjectSummary[] }>('/projects').then(r => r.projects),

  listPrompts: (projectId: number) =>
    DEMO_MODE ? demoDelay(DEMO_PROMPTS[projectId] ?? [])
      : apiGet<{ prompts: PromptSummary[] }>(`/projects/${projectId}/prompts`).then(r => r.prompts),

  listVersions: (promptId: number) =>
    DEMO_MODE ? demoDelay(DEMO_VERSIONS[promptId] ?? [])
      : apiGet<{ versions: PromptVersionSummary[] }>(`/prompts/${promptId}/versions`).then(r => r.versions),

  driftStatus: (projectId: number, promptName: string) =>
    DEMO_MODE ? demoDelay(DEMO_DRIFT[`${projectId}:${promptName}`])
      : apiGet<DriftStatus>(`/drift/status?project_id=${projectId}&prompt_name=${encodeURIComponent(promptName)}`),
}
