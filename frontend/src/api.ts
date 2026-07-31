export type WorkflowResult = {
  success: boolean
  topic: string
  summary: string | null
  google_drive_link: string | null
  upload_status: string
  email_status: string
  processing_time_seconds: number
  error: string | null
}

const API_BASE =
  import.meta.env.VITE_API_BASE_URL?.replace(/\/$/, '') ||
  'http://localhost:8000'

export async function searchAndSummarize(topic: string): Promise<WorkflowResult> {
  const controller = new AbortController()
  const timeoutMs = 5 * 60 * 1000
  const timer = window.setTimeout(() => controller.abort(), timeoutMs)

  try {
    const response = await fetch(`${API_BASE}/api/search-and-summarize`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ topic }),
      signal: controller.signal,
    })

    let data: unknown
    try {
      data = await response.json()
    } catch {
      throw new Error(
        response.ok
          ? 'Backend returned an invalid response.'
          : `Request failed (${response.status}). Is the API running on port 8000?`,
      )
    }

    if (!response.ok) {
      const detail =
        typeof data === 'object' &&
        data !== null &&
        'detail' in data &&
        typeof (data as { detail: unknown }).detail === 'string'
          ? (data as { detail: string }).detail
          : `Request failed with status ${response.status}`
      throw new Error(detail)
    }

    return data as WorkflowResult
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') {
      throw new Error(
        'Request timed out after 5 minutes. The workflow may still be running on the server.',
      )
    }
    if (err instanceof TypeError) {
      throw new Error(
        'Could not reach the API. Start the backend with: .venv/bin/uvicorn backend.api:app --reload --port 8000',
      )
    }
    throw err
  } finally {
    window.clearTimeout(timer)
  }
}
