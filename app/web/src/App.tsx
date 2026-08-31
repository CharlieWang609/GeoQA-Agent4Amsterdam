// SPDX-License-Identifier: GPL-3.0-only

import { FormEvent, useEffect, useRef, useState } from "react";

import {
  ApiError,
  createSession,
  decideResult,
  deleteSession,
  editSession,
  getAnswerMap,
  getExecutionJob,
  getCurrentIdentity,
  getSession,
  listSessions,
  regenerateSession,
} from "./api";
import { CandidateAnswerReview } from "./CandidateAnswerReview";
import { DraftReview } from "./DraftReview";
import { MapPane } from "./MapPane";
import { Modal } from "./Modal";
import { hasActiveJob } from "./sessionState";
import type {
  AnswerMapFeatureCollection,
  CurrentIdentity,
  ExecutionJob,
  QuestionSession,
  QuestionSessionSummary,
  SessionDraftVersion,
} from "./types";

const EXAMPLE_QUESTIONS = [
  "Which Amsterdam neighborhoods have no registered public sports locations?",
  "For each gymnasium in Amsterdam, how far is the nearest swimming pool?",
  "For each registered public sports location in Amsterdam, how far away is the nearest swimming pool?",
] as const;

export function App() {
  return <LiveSandbox />;
}

function LiveSandbox() {
  const initialSessionId = new URLSearchParams(window.location.search).get("session");
  const [activeTab, setActiveTab] = useState<"current" | "history">("current");
  const [question, setQuestion] = useState("");
  const [hoveredExample, setHoveredExample] = useState<string | null>(null);
  const [focusedExample, setFocusedExample] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [restoring, setRestoring] = useState(Boolean(initialSessionId));
  const [history, setHistory] = useState<QuestionSessionSummary[]>([]);
  const [historyLoading, setHistoryLoading] = useState(true);
  const [session, setSession] = useState<QuestionSession | null>(null);
  const [etag, setEtag] = useState("");
  const [selectedVersion, setSelectedVersion] = useState(1);
  const [instruction, setInstruction] = useState("");
  const [mutating, setMutating] = useState(false);
  const [job, setJob] = useState<ExecutionJob | null>(null);
  const [error, setError] = useState("");
  const [errorStatus, setErrorStatus] = useState<number | null>(null);
  const [answerMap, setAnswerMap] = useState<AnswerMapFeatureCollection | null>(null);
  const [mapLoading, setMapLoading] = useState(false);
  const [mapError, setMapError] = useState("");
  const [resultFeedback, setResultFeedback] = useState("");
  const [identity, setIdentity] = useState<CurrentIdentity | null>();
  const [deletionTarget, setDeletionTarget] = useState<{
    sessionId: string;
    question: string;
    etag?: string;
  } | null>(null);
  const [deleting, setDeleting] = useState(false);
  const questionInput = useRef<HTMLTextAreaElement>(null);
  // Bumped whenever a 401 wipes the signed-in state; every async handler
  // captures the value before its request and discards the response if the
  // counter moved, so stale replies never repopulate a signed-out view.
  const authenticationFailureVersion = useRef(0);

  useEffect(() => {
    void refreshIdentity();
    void refreshHistory();
    if (initialSessionId) void restoreSession(initialSessionId);
    // The initial URL is intentionally captured only once.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
  }, [selectedVersion, session?.session_id]);

  useEffect(() => {
    const sessionId = session?.session_id;
    const candidateAnswerId = session?.candidate_answer?.candidate_answer_id;
    if (!sessionId || !candidateAnswerId) {
      setAnswerMap(null);
      setMapLoading(false);
      setMapError("");
      return;
    }
    let cancelled = false;
    const requestVersion = authenticationFailureVersion.current;
    setMapLoading(true);
    setMapError("");
    getAnswerMap(sessionId)
      .then((result) => {
        if (!cancelled && requestVersion === authenticationFailureVersion.current) {
          setAnswerMap(result);
        }
      })
      .catch((caught: unknown) => {
        if (cancelled) return;
        if (caught instanceof ApiError && caught.status === 401) captureError(caught);
        else if (requestVersion === authenticationFailureVersion.current) setMapError(errorMessage(caught));
      })
      .finally(() => {
        if (!cancelled && requestVersion === authenticationFailureVersion.current) {
          setMapLoading(false);
        }
      });
    return () => { cancelled = true; };
  }, [session?.candidate_answer?.candidate_answer_id, session?.session_id]);

  // Poll the execution job every 2s while it is active; once terminal,
  // re-fetch the session so the aggregated result and Candidate Answer land.
  useEffect(() => {
    const sessionId = session?.session_id;
    const jobReference = session?.job_reference;
    if (!sessionId || !jobReference) {
      setJob(null);
      return;
    }
    if (session.execution_result) {
      setJob(session.execution_result);
      return;
    }
    let cancelled = false;
    let timer: number | undefined;
    const requestVersion = authenticationFailureVersion.current;
    async function poll() {
      try {
        const latest = await getExecutionJob(sessionId!);
        if (cancelled || requestVersion !== authenticationFailureVersion.current) return;
        setJob(latest);
        if (latest.status === "queued" || latest.status === "running") {
          timer = window.setTimeout(poll, 2_000);
        } else {
          const refreshed = await getSession(sessionId!);
          if (cancelled || requestVersion !== authenticationFailureVersion.current) return;
          applySession(refreshed.session, refreshed.etag);
          void refreshHistory();
        }
      } catch (caught) {
        if (!cancelled) handleRequestError(caught, requestVersion);
      }
    }
    void poll();
    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [session?.execution_result, session?.job_reference, session?.session_id]);

  async function refreshHistory() {
    const requestVersion = authenticationFailureVersion.current;
    setHistoryLoading(true);
    try {
      const sessions = await listSessions();
      if (requestVersion === authenticationFailureVersion.current) setHistory(sessions);
    } catch (caught) {
      if (requestVersion === authenticationFailureVersion.current) {
        captureError(caught, true);
      }
    } finally {
      setHistoryLoading(false);
    }
  }

  async function refreshIdentity() {
    const requestVersion = authenticationFailureVersion.current;
    try {
      const currentIdentity = await getCurrentIdentity();
      if (requestVersion === authenticationFailureVersion.current) {
        setIdentity(currentIdentity);
      }
    } catch (caught) {
      captureError(caught, true);
    }
  }

  async function restoreSession(sessionId: string) {
    const requestVersion = authenticationFailureVersion.current;
    setRestoring(true);
    clearError();
    try {
      const result = await getSession(sessionId);
      if (requestVersion !== authenticationFailureVersion.current) return;
      applySession(result.session, result.etag);
      setActiveTab("current");
      setSessionUrl(result.session.session_id);
    } catch (caught) {
      handleRequestError(caught, requestVersion);
    } finally {
      setRestoring(false);
    }
  }

  function applySession(nextSession: QuestionSession, nextEtag: string) {
    setSession(nextSession);
    setEtag(nextEtag);
    setSelectedVersion(nextSession.current_draft_version);
  }

  function startNewQuestion() {
    setSession(null);
    setEtag("");
    setQuestion("");
    setInstruction("");
    setJob(null);
    setAnswerMap(null);
    setMapError("");
    setResultFeedback("");
    setActiveTab("current");
    clearError();
    setSessionUrl(null);
  }

  async function submitQuestion(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!question.trim()) return;
    setSubmitting(true);
    const requestVersion = authenticationFailureVersion.current;
    clearError();
    try {
      const result = await createSession(question.trim());
      if (requestVersion !== authenticationFailureVersion.current) return;
      applySession(result.session, result.etag);
      setSessionUrl(result.session.session_id);
      void refreshHistory();
    } catch (caught) {
      handleRequestError(caught, requestVersion);
    } finally {
      setSubmitting(false);
    }
  }

  function selectExampleQuestion(exampleQuestion: string) {
    setQuestion(exampleQuestion);
    questionInput.current?.focus();
  }

  async function applyEdit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!session || !instruction.trim()) return;
    await mutate(() => editSession(session.session_id, etag, instruction.trim()));
  }

  async function regenerate() {
    if (!session) return;
    await mutate(() => regenerateSession(session.session_id, etag));
  }

  async function decide(decision: "accepted" | "rejected") {
    if (!session?.candidate_answer) return;
    await mutate(() => decideResult(
      session.session_id,
      etag,
      decision,
      resultFeedback.trim() || null,
    ));
  }

  function requestDeletion(sessionId: string, questionText: string) {
    setDeletionTarget({
      sessionId,
      question: questionText,
      etag: session?.session_id === sessionId ? etag : undefined,
    });
  }

  async function confirmDeletion() {
    if (!deletionTarget) return;
    setDeleting(true);
    const requestVersion = authenticationFailureVersion.current;
    clearError();
    try {
      let targetEtag = deletionTarget.etag;
      if (!targetEtag) {
        const inspected = await getSession(deletionTarget.sessionId);
        if (requestVersion !== authenticationFailureVersion.current) return;
        targetEtag = inspected.etag;
      }
      await deleteSession(deletionTarget.sessionId, targetEtag);
      if (requestVersion !== authenticationFailureVersion.current) return;
      const deletedOpenSession = session?.session_id === deletionTarget.sessionId;
      setDeletionTarget(null);
      if (deletedOpenSession) startNewQuestion();
      await refreshHistory();
    } catch (caught) {
      setDeletionTarget(null);
      handleRequestError(caught, requestVersion);
    } finally {
      setDeleting(false);
    }
  }

  // Shared wrapper for every ETag-guarded session mutation: apply the new
  // session + ETag on success, clear pending inputs, refresh history.
  async function mutate(action: () => Promise<{ session: QuestionSession; etag: string }>) {
    setMutating(true);
    const requestVersion = authenticationFailureVersion.current;
    clearError();
    try {
      const result = await action();
      if (requestVersion !== authenticationFailureVersion.current) return;
      applySession(result.session, result.etag);
      setInstruction("");
      setResultFeedback("");
      void refreshHistory();
    } catch (caught) {
      handleRequestError(caught, requestVersion);
    } finally {
      setMutating(false);
    }
  }

  function clearError() {
    setError("");
    setErrorStatus(null);
  }

  function handleRequestError(caught: unknown, requestVersion: number) {
    if (
      requestVersion === authenticationFailureVersion.current ||
      (caught instanceof ApiError && caught.status === 401)
    ) captureError(caught);
  }

  function captureError(caught: unknown, silentUnauthorized = false) {
    const status = caught instanceof ApiError ? caught.status : null;
    // A 401 signs the user out client-side: wipe all owned state and
    // invalidate in-flight requests via the version counter.
    if (status === 401) {
      authenticationFailureVersion.current += 1;
      setIdentity(null);
      setSession(null);
      setEtag("");
      setHistory([]);
      setJob(null);
      setAnswerMap(null);
      setMapError("");
      setSessionUrl(null);
    }
    if (status === 401 && silentUnauthorized) {
      setErrorStatus(null);
      setError("");
      return;
    }
    setErrorStatus(status);
    setError(
      status === 404
        ? "This session is not available to the current account."
        : status === 410
          ? "This Question Session has expired. No answer is available."
          : errorMessage(caught),
    );
  }

  const selectedDraft = session?.draft_versions.find((draft) => draft.version === selectedVersion);
  const latestDraft = session ? currentDraft(session) : undefined;
  const warnings = latestDraft?.validation.status === "pass_with_warnings"
    ? latestDraft.validation.diagnostics.filter((diagnostic) => diagnostic.severity === "advisory")
    : [];
  const activeJob = session ? hasActiveJob(session, job) : false;
  const planMutationBlocked = Boolean(
    activeJob ||
    session?.candidate_answer ||
    session?.result_decision,
  );

  return (
    <>
    <main className="sandbox-shell">
      <aside className="session-panel" aria-label="Session workspace">
        <header className="session-header">
          <h1>GeoQA Agent for Amsterdam</h1>
          <button type="button" className="new-question" onClick={startNewQuestion}>New Question</button>
          <div className="account-indicator">
            {identity ? (
              <>
                <span>Signed in as <strong>{identity.display_name}</strong></span>
                <a href="/.auth/logout?post_logout_redirect_uri=/">Sign out</a>
              </>
            ) : identity === null ? (
              <a href={githubSignInUrl()}>Sign in with GitHub</a>
            ) : (
              <span aria-label="Checking account">Checking account…</span>
            )}
          </div>
          <div role="tablist" aria-label="Session views">
            <button type="button" role="tab" aria-selected={activeTab === "current"} onClick={() => setActiveTab("current")}>Current Session</button>
            <button type="button" role="tab" aria-selected={activeTab === "history"} onClick={() => setActiveTab("history")}>History</button>
          </div>
        </header>

        <div className="session-scroll">
          {error && (
            <div className="feedback-banner">
              <p role="alert">{error}</p>
              {errorStatus === 401 && <a href={githubSignInUrl()}>Sign in with GitHub</a>}
            </div>
          )}

          {activeTab === "history" ? (
            <HistoryList
              history={history}
              loading={historyLoading}
              onSelect={(id) => void restoreSession(id)}
              onDelete={requestDeletion}
            />
          ) : restoring ? (
            <p role="status">Loading owned Question Session…</p>
          ) : session ? (
            <div className="conversation-flow">
              <section className="question-bubble" aria-label="Question"><p>{session.question}</p></section>
              <button
                type="button"
                className="delete-session-action"
                disabled={deleting}
                onClick={() => requestDeletion(session.session_id, session.question)}
              >
                Delete session
              </button>

              {selectedDraft && (
                <DraftReview
                  draft={selectedDraft}
                  versionControl={(
                    <div className="version-picker">
                      <label htmlFor="draft-version">Draft version</label>
                      <select id="draft-version" value={selectedVersion} onChange={(event) => setSelectedVersion(Number(event.target.value))}>
                        {session.draft_versions.map((draft) => (
                          <option key={draft.draft_version_id} value={draft.version}>
                            Version {draft.version} · {validationLabel(draft.validation.status)}
                          </option>
                        ))}
                      </select>
                    </div>
                  )}
                  actions={(
                    <section className="plan-actions" aria-label="Plan actions">
                      <form onSubmit={applyEdit}>
                        <label htmlFor="instruction">Revision instruction</label>
                        <textarea id="instruction" rows={3} value={instruction} onChange={(event) => setInstruction(event.target.value)} />
                        <button type="submit" disabled={mutating || !instruction.trim() || planMutationBlocked}>Revise</button>
                      </form>
                      <button type="button" className="secondary-action" disabled={mutating || planMutationBlocked} onClick={() => void regenerate()}>Regenerate</button>
                      {latestDraft?.validation.status === "pass_with_warnings" && (
                        <div className="warning-banner">
                          <strong>Semantic verification warnings</strong>
                          <ul>{warnings.map((diagnostic, index) => <li key={`${String(diagnostic.code)}-${index}`}>{String(diagnostic.code)}: {String(diagnostic.message)}</li>)}</ul>
                        </div>
                      )}
                    </section>
                  )}
                />
              )}

              {session.job_reference && <JobMonitor job={job} fallbackStatus={session.job_reference.status} executedWithWarnings={Boolean(session.execution_authorization?.advisory_override)} />}
              <CandidateAnswerReview
                session={session}
                feedback={resultFeedback}
                mutating={mutating}
                onFeedbackChange={setResultFeedback}
                onDecision={(decision) => void decide(decision)}
              />
            </div>
          ) : (
            <section className="question-composer" aria-labelledby="question-heading">
              <h2 id="question-heading">Ask a geo-analytical question</h2>
              <form onSubmit={submitQuestion}>
                <label htmlFor="question">Geo-analytical question</label>
                <textarea ref={questionInput} id="question" rows={5} value={question} onChange={(event) => setQuestion(event.target.value)} placeholder={EXAMPLE_QUESTIONS[0]} />
                <div className="example-question-list" aria-label="Example questions">
                  {EXAMPLE_QUESTIONS.map((exampleQuestion) => {
                    const expanded = hoveredExample === exampleQuestion || focusedExample === exampleQuestion;
                    return (
                      <button
                        key={exampleQuestion}
                        type="button"
                        className="example-question-chip"
                        aria-expanded={expanded}
                        onClick={() => selectExampleQuestion(exampleQuestion)}
                        onMouseEnter={() => setHoveredExample(exampleQuestion)}
                        onMouseLeave={() => setHoveredExample(null)}
                        onFocus={() => setFocusedExample(exampleQuestion)}
                        onBlur={() => setFocusedExample(null)}
                      >
                        {exampleQuestion}
                      </button>
                    );
                  })}
                </div>
                <button disabled={submitting || !question.trim()} type="submit">{submitting ? "Planning…" : "Plan question"}</button>
              </form>
            </section>
          )}
        </div>
        <footer>Sessions expire after 7 days.</footer>
      </aside>
      <MapPane answerMap={answerMap} answerMapLoading={mapLoading} answerMapError={mapError} />
    </main>
    {deletionTarget && (
      <Modal title="Delete Question Session" onClose={() => setDeletionTarget(null)}>
        <div className="delete-confirmation">
          <p>This permanently removes the session from your history:</p>
          <blockquote>{deletionTarget.question}</blockquote>
          <div className="delete-confirmation-actions">
            <button
              type="button"
              className="secondary-action"
              disabled={deleting}
              onClick={() => setDeletionTarget(null)}
            >
              Cancel
            </button>
            <button
              type="button"
              className="danger-action"
              disabled={deleting}
              onClick={() => void confirmDeletion()}
            >
              {deleting ? "Deleting…" : "Delete session"}
            </button>
          </div>
        </div>
      </Modal>
    )}
    </>
  );
}

function HistoryList({
  history,
  loading,
  onSelect,
  onDelete,
}: {
  history: QuestionSessionSummary[];
  loading: boolean;
  onSelect: (id: string) => void;
  onDelete: (id: string, question: string) => void;
}) {
  return (
    <section className="history-view" aria-labelledby="history-heading">
      <h2 id="history-heading">Question history</h2>
      {loading ? <p role="status">Loading history…</p> : history.length ? (
        <ul>{history.map((item) => (
          <li key={item.session_id}>
            <button className="history-session" type="button" onClick={() => onSelect(item.session_id)}>
              <strong>{item.question}</strong>
              <time dateTime={item.created_at}>{formatDate(item.created_at)}</time>
              <span className={`status-badge status-${item.latest_validation_status}`}>{historyStatus(item)}</span>
            </button>
            <button
              type="button"
              className="history-delete danger-action"
              aria-label={`Delete ${item.question}`}
              onClick={() => onDelete(item.session_id, item.question)}
            >
              Delete
            </button>
          </li>
        ))}</ul>
      ) : <p>No saved sessions.</p>}
    </section>
  );
}

function JobMonitor({ job, fallbackStatus, executedWithWarnings }: { job: ExecutionJob | null; fallbackStatus: ExecutionJob["status"]; executedWithWarnings: boolean }) {
  const status = job?.status ?? fallbackStatus;
  return (
    <article className="lifecycle-card execution-card" aria-labelledby="execution-heading" aria-live="polite">
      <div className="card-heading">
        <div><h2 id="execution-heading">Execution</h2><p> status: <strong>{status}</strong></p></div>
        <div className="badge-row">
          {(status === "queued" || status === "running") && <span className="progress-indicator" aria-hidden="true" />}
          {executedWithWarnings && <span className="status-badge status-pass_with_warnings">Executed with semantic warnings</span>}
        </div>
      </div>
      {job?.failure && <p role="status"><strong>{job.failure.code}</strong>: {job.failure.message}{job.failure.step_id ? ` (${job.failure.step_id})` : ""}</p>}
    </article>
  );
}

function currentDraft(session: QuestionSession): SessionDraftVersion | undefined {
  return session.draft_versions.find((draft) => draft.version === session.current_draft_version);
}

function historyStatus(item: QuestionSessionSummary) {
  if (item.has_result_decision) return "Reviewed";
  if (item.has_candidate_answer) return "Result ready";
  if (item.has_execution_job) return "Executing";
  return validationLabel(item.latest_validation_status);
}

function validationLabel(status: SessionDraftVersion["validation"]["status"]) {
  if (status === "pass") return "Passed";
  if (status === "pass_with_warnings") return "Warnings";
  return "Failed";
}

function setSessionUrl(sessionId: string | null) {
  const url = new URL(window.location.href);
  if (sessionId) url.searchParams.set("session", sessionId);
  else url.searchParams.delete("session");
  window.history.replaceState({}, "", url);
}

function errorMessage(caught: unknown) {
  return caught instanceof Error ? caught.message : "The GeoQA Agent request failed.";
}

function githubSignInUrl() {
  const returnTo = `${window.location.pathname}${window.location.search}`;
  return `/.auth/login/github?post_login_redirect_uri=${encodeURIComponent(returnTo)}`;
}

function formatDate(value: string) {
  return new Intl.DateTimeFormat("en", { dateStyle: "medium", timeZone: "UTC" }).format(new Date(value));
}
