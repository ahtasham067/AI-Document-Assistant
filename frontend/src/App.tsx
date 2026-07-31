import { useEffect, useRef, useState, type FormEvent } from 'react'
import { searchAndSummarize, type WorkflowResult } from './api'
import './App.css'

const PROGRESS_STEPS = [
  'Searching for topic…',
  'Generating summary with Ollama…',
  'Uploading document to Google Drive…',
  'Sending email notification…',
] as const

function formatDuration(seconds: number): string {
  if (seconds < 60) return `${seconds.toFixed(1)}s`
  const m = Math.floor(seconds / 60)
  const s = Math.round(seconds % 60)
  return `${m}m ${s}s`
}

export default function App() {
  const [topic, setTopic] = useState('')
  const [loading, setLoading] = useState(false)
  const [progressIndex, setProgressIndex] = useState(0)
  const [result, setResult] = useState<WorkflowResult | null>(null)
  const [requestError, setRequestError] = useState<string | null>(null)
  const inFlight = useRef(false)

  useEffect(() => {
    if (!loading) return
    setProgressIndex(0)
    const id = window.setInterval(() => {
      setProgressIndex((i) => Math.min(i + 1, PROGRESS_STEPS.length - 1))
    }, 4500)
    return () => window.clearInterval(id)
  }, [loading])

  async function handleSubmit(event: FormEvent) {
    event.preventDefault()
    const trimmed = topic.trim()
    if (!trimmed || inFlight.current) return

    inFlight.current = true
    setLoading(true)
    setRequestError(null)
    setResult(null)

    try {
      const data = await searchAndSummarize(trimmed)
      setResult(data)
      if (data.error && !data.summary) {
        setRequestError(data.error)
      }
    } catch (err) {
      setRequestError(err instanceof Error ? err.message : String(err))
    } finally {
      setLoading(false)
      inFlight.current = false
    }
  }

  const showResults = result && (result.summary || result.error || result.google_drive_link)
  const bannerError =
    requestError ||
    (result && !result.success && result.error ? result.error : null) ||
    (result && !result.success && !result.error
      ? [result.upload_status, result.email_status]
          .filter((s) => s && s !== 'Success' && s !== 'Sent' && s !== '(not available)')
          .join(' · ') || 'Workflow completed with errors.'
      : null)

  return (
    <div className="page">
      <div className="atmosphere" aria-hidden="true" />
      <main className="shell">
        <header className="hero">
          <p className="brand">AI Document Assistant</p>
          <h1 className="headline">Search, summarize, share</h1>
          <p className="lede">
            Enter a topic. We summarize it, upload the document to Drive, and email you
            the link.
          </p>
        </header>

        <form className="composer" onSubmit={handleSubmit}>
          <label className="field-label" htmlFor="topic">
            Topic
          </label>
          <div className="field-row">
            <input
              id="topic"
              name="topic"
              type="text"
              value={topic}
              onChange={(e) => setTopic(e.target.value)}
              placeholder="e.g. Artificial Intelligence agents"
              disabled={loading}
              autoComplete="off"
              required
            />
            <button type="submit" disabled={loading || !topic.trim()}>
              {loading ? 'Working…' : 'Search & Summarize'}
            </button>
          </div>
        </form>

        {loading && (
          <div className="status-panel" role="status" aria-live="polite">
            <div className="spinner" aria-hidden="true" />
            <p className="status-text">{PROGRESS_STEPS[progressIndex]}</p>
            <p className="status-hint">This can take a few minutes. Keep this tab open.</p>
          </div>
        )}

        {bannerError && !loading && (
          <div className={`notice ${result?.success ? 'notice-warn' : 'notice-error'}`} role="alert">
            {bannerError}
          </div>
        )}

        {result?.success && !loading && (
          <div className="notice notice-ok" role="status">
            Summary ready — Drive upload and email completed.
          </div>
        )}

        {showResults && !loading && (
          <section className="results" aria-labelledby="results-heading">
            <h2 id="results-heading">Results</h2>

            <dl className="result-grid">
              <div>
                <dt>Topic</dt>
                <dd>{result.topic}</dd>
              </div>

              {result.summary && (
                <div className="span-all">
                  <dt>Summary</dt>
                  <dd className="summary-body">{result.summary}</dd>
                </div>
              )}

              <div>
                <dt>Google Drive link</dt>
                <dd>
                  {result.google_drive_link ? (
                    <a
                      href={result.google_drive_link}
                      target="_blank"
                      rel="noopener noreferrer"
                    >
                      {result.google_drive_link}
                    </a>
                  ) : (
                    <span className="muted">Not available</span>
                  )}
                </dd>
              </div>

              <div>
                <dt>Upload status</dt>
                <dd>{result.upload_status}</dd>
              </div>

              <div>
                <dt>Email status</dt>
                <dd>{result.email_status}</dd>
              </div>

              <div>
                <dt>Processing time</dt>
                <dd>{formatDuration(result.processing_time_seconds)}</dd>
              </div>
            </dl>
          </section>
        )}
      </main>
    </div>
  )
}
