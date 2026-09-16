// SPDX-License-Identifier: GPL-3.0-only

import { FormEvent, useEffect, useRef, useState } from "react";

import {
  ApiError,
  createSession,
  decideResult,
  deleteSession,
  editSession,
  getAnswerMap,
  getCurrentIdentity,
  getModels,
  getObservations,
  getSession,
  getShowcaseAnswerMap,
  getShowcaseObservations,
  getShowcaseSession,
  isShowcaseSession,
  listSessions,
  listShowcase,
  regenerateSession,
} from "./api";
import { CandidateAnswerReview } from "./CandidateAnswerReview";
import { DeleteSessionDialog } from "./DeleteSessionDialog";
import { DraftReview } from "./DraftReview";
import { HistoryList } from "./HistoryList";
import { errorMessage } from "./jsonRecords";
import { MapPane } from "./MapPane";
import { QuestionComposer } from "./QuestionComposer";
import { validationLabel } from "./sessionLabels";
import { WorkedExamples } from "./WorkedExamples";
import type {
  ModelChoice,
  AnswerMapFeatureCollection,
  CurrentIdentity,
  ExecutionJob,
  ObservationItem,
  QuestionSession,
  QuestionSessionSummary,
  SessionDraftVersion,
} from "./types";

export function App() {
  return <LiveSandbox />;
}

function LiveSandbox() {
  const initialSessionId = new URLSearchParams(window.location.search).get("session");
  const initialShowcaseId = new URLSearchParams(window.location.search).get("showcase");
  const [activeTab, setActiveTab] = useState<"current" | "history">("current");
  const [question, setQuestion] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [restoring, setRestoring] = useState(Boolean(initialSessionId || initialShowcaseId));
  const [history, setHistory] = useState<QuestionSessionSummary[]>([]);
  const [examples, setExamples] = useState<QuestionSessionSummary[]>([]);
  const [historyLoading, setHistoryLoading] = useState(true);
  const [session, setSession] = useState<QuestionSession | null>(null);
  const [selectedVersion, setSelectedVersion] = useState(1);
  const [instruction, setInstruction] = useState("");
  const [mutating, setMutating] = useState(false);
  const [error, setError] = useState("");
  const [errorStatus, setErrorStatus] = useState<number | null>(null);
  const [answerMap, setAnswerMap] = useState<AnswerMapFeatureCollection | null>(null);
  const [mapLoading, setMapLoading] = useState(false);
  const [mapError, setMapError] = useState("");
  const [observations, setObservations] = useState<ObservationItem[]>([]);
  const [resultFeedback, setResultFeedback] = useState("");
  const [identity, setIdentity] = useState<CurrentIdentity | null>();
  const [deletionTarget, setDeletionTarget] = useState<{
    sessionId: string;
    question: string;
  } | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [models, setModels] = useState<ModelChoice[]>([]);
  const [model, setModel] = useState("");
  // Bumped whenever a 401 wipes the signed-in state; every async handler
  // captures the value before its request and discards the response if the
  // counter moved, so stale replies never repopulate a signed-out view.
  const authenticationFailureVersion = useRef(0);

  useEffect(() => {
    void refreshIdentity();
    void refreshHistory();
    void getModels().then((listing) => {
      setModels(listing.models);
      setModel(listing.default);
    }).catch(() => undefined);
    void listShowcase().then(setExamples).catch(() => undefined);
    if (initialShowcaseId) void openExample(initialShowcaseId);
    else if (initialSessionId) void restoreSession(initialSessionId);
    // The initial URL is intentionally captured only once.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

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
    // A worked example is public: its documents come from the showcase
    // routes and are kept even if the account check signs the visitor out.
    const publicSession = isShowcaseSession(session);
    const requestVersion = authenticationFailureVersion.current;
    const current = () => publicSession || requestVersion === authenticationFailureVersion.current;
    setMapLoading(true);
    setMapError("");
    (publicSession ? getShowcaseAnswerMap : getAnswerMap)(sessionId)
      .then((result) => {
        if (!cancelled && current()) setAnswerMap(result);
      })
      .catch((caught: unknown) => {
        if (cancelled) return;
        if (caught instanceof ApiError && caught.status === 401) captureError(caught);
        else if (current()) setMapError(errorMessage(caught, "The GeoQA Agent request failed."));
      })
      .finally(() => {
        if (!cancelled && current()) setMapLoading(false);
      });
    return () => { cancelled = true; };
  }, [session?.candidate_answer?.candidate_answer_id, session?.session_id]);

  // The raster and satellite data behind an execution, listed for the map
  // once the job exists; a missing listing only leaves the panel empty.
  useEffect(() => {
    const sessionId = session?.session_id;
    const jobId = session?.execution_result?.job_id;
    if (!sessionId || !jobId) {
      setObservations([]);
      return;
    }
    let cancelled = false;
    const publicSession = isShowcaseSession(session);
    const requestVersion = authenticationFailureVersion.current;
    (publicSession ? getShowcaseObservations : getObservations)(sessionId)
      .then((listing) => {
        if (!cancelled && (publicSession || requestVersion === authenticationFailureVersion.current)) {
          setObservations(listing.items);
        }
      })
      .catch((caught: unknown) => {
        if (!cancelled && caught instanceof ApiError && caught.status === 401) captureError(caught);
      });
    return () => { cancelled = true; };
  }, [session?.execution_result?.job_id, session?.session_id]);

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
      const restored = await getSession(sessionId);
      if (requestVersion !== authenticationFailureVersion.current) return;
      applySession(restored);
      setActiveTab("current");
      setSessionUrl(restored.session_id);
    } catch (caught) {
      handleRequestError(caught, requestVersion);
    } finally {
      setRestoring(false);
    }
  }

  async function openExample(sessionId: string) {
    setRestoring(true);
    clearError();
    try {
      const example = await getShowcaseSession(sessionId);
      applySession(example);
      setActiveTab("current");
      setSessionUrl(example.session_id, "showcase");
    } catch (caught) {
      setErrorStatus(caught instanceof ApiError ? caught.status : null);
      setError(
        caught instanceof ApiError && caught.status === 404
          ? "This worked example is no longer published."
          : errorMessage(caught, "The worked example could not be loaded."),
      );
    } finally {
      setRestoring(false);
    }
  }

  function applySession(nextSession: QuestionSession) {
    setSession(nextSession);
    setSelectedVersion(nextSession.current_draft_version);
  }

  function startNewQuestion() {
    setSession(null);
    setQuestion("");
    setInstruction("");
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
      const created = await createSession(question.trim(), model);
      if (requestVersion !== authenticationFailureVersion.current) return;
      applySession(created);
      setSessionUrl(created.session_id);
      void refreshHistory();
    } catch (caught) {
      handleRequestError(caught, requestVersion);
    } finally {
      setSubmitting(false);
    }
  }

  async function applyEdit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!session || !instruction.trim()) return;
    await mutate(() => editSession(session.session_id, instruction.trim()));
  }

  async function regenerate() {
    if (!session) return;
    await mutate(() => regenerateSession(session.session_id));
  }

  async function decide(decision: "accepted" | "rejected") {
    if (!session?.candidate_answer) return;
    await mutate(() => decideResult(session.session_id, decision, resultFeedback.trim() || null));
  }

  function requestDeletion(sessionId: string, questionText: string) {
    setDeletionTarget({ sessionId, question: questionText });
  }

  async function confirmDeletion() {
    if (!deletionTarget) return;
    setDeleting(true);
    const requestVersion = authenticationFailureVersion.current;
    clearError();
    try {
      await deleteSession(deletionTarget.sessionId);
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

  // Shared wrapper for every session mutation: apply the new session on
  // success, clear pending inputs, refresh history.
  async function mutate(action: () => Promise<QuestionSession>) {
    setMutating(true);
    const requestVersion = authenticationFailureVersion.current;
    clearError();
    try {
      const mutated = await action();
      if (requestVersion !== authenticationFailureVersion.current) return;
      applySession(mutated);
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
      setHistory([]);
      // The URL names what is open or being opened. A worked example is
      // public and stays; an owned session goes, and the effects above drop
      // its map and observations with it.
      if (!new URLSearchParams(window.location.search).has("showcase")) {
        setSession(null);
        setSessionUrl(null);
      }
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
          : errorMessage(caught, "The GeoQA Agent request failed."),
    );
  }

  const selectedDraft = session?.draft_versions.find((draft) => draft.version === selectedVersion);
  const latestDraft = session ? currentDraft(session) : undefined;
  const planMutationBlocked = Boolean(session?.candidate_answer || session?.result_decision);
  const showcaseSession = session !== null && isShowcaseSession(session);

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
            <p role="status">Loading Question Session…</p>
          ) : session ? (
            <div className="conversation-flow">
              <section className="question-bubble" aria-label="Question">
                <p>{session.question}</p>
                <small className="session-model">{showcaseSession ? "Worked example · " : ""}Model: {session.model}</small>
              </section>
              {!showcaseSession && (
                <button
                  type="button"
                  className="delete-session-action"
                  disabled={deleting}
                  onClick={() => requestDeletion(session.session_id, session.question)}
                >
                  Delete session
                </button>
              )}

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
                  actions={showcaseSession ? null : (
                    <section className="plan-actions" aria-label="Plan actions">
                      <form onSubmit={applyEdit}>
                        <label htmlFor="instruction">Revision instruction</label>
                        <textarea id="instruction" rows={3} value={instruction} onChange={(event) => setInstruction(event.target.value)} />
                        <button type="submit" disabled={mutating || !instruction.trim() || planMutationBlocked}>Revise</button>
                      </form>
                      <button type="button" className="secondary-action" disabled={mutating || planMutationBlocked} onClick={() => void regenerate()}>Regenerate</button>
                    </section>
                  )}
                />
              )}

              {session.execution_result && <ExecutionSummary job={session.execution_result} />}
              <CandidateAnswerReview
                session={session}
                readOnly={showcaseSession}
                answerMap={answerMap}
                feedback={resultFeedback}
                mutating={mutating}
                onFeedbackChange={setResultFeedback}
                onDecision={(decision) => void decide(decision)}
              />
            </div>
          ) : (
            <div className="composer-flow">
              <QuestionComposer
                question={question}
                submitting={submitting}
                models={models}
                model={model}
                onQuestionChange={setQuestion}
                onModelChange={setModel}
                onSubmit={submitQuestion}
              />
              {identity === null && (
                <p className="sign-in-hint">
                  Asking a question needs a GitHub account: <a href={githubSignInUrl()}>sign in</a> to plan your own,
                  or open a worked example below.
                </p>
              )}
              <WorkedExamples examples={examples} onSelect={(id) => void openExample(id)} />
            </div>
          )}
        </div>
        <footer>{showcaseSession ? "Worked examples are read-only." : "Sessions expire after 7 days."}</footer>
      </aside>
      <MapPane answerMap={answerMap} answerMapLoading={mapLoading} answerMapError={mapError} observations={observations} />
    </main>
    {deletionTarget && (
      <DeleteSessionDialog
        question={deletionTarget.question}
        deleting={deleting}
        onCancel={() => setDeletionTarget(null)}
        onConfirm={() => void confirmDeletion()}
      />
    )}
    </>
  );
}

function ExecutionSummary({ job }: { job: ExecutionJob }) {
  return (
    <article className="lifecycle-card execution-card" aria-labelledby="execution-heading">
      <div className="card-heading">
        <div><h2 id="execution-heading">Execution</h2><p> status: <strong>{job.status}</strong></p></div>
      </div>
      {job.failure && <p role="status"><strong>{job.failure.code}</strong>: {job.failure.message}{job.failure.step_id ? ` (${job.failure.step_id})` : ""}</p>}
    </article>
  );
}

function currentDraft(session: QuestionSession): SessionDraftVersion | undefined {
  return session.draft_versions.find((draft) => draft.version === session.current_draft_version);
}

function setSessionUrl(sessionId: string | null, parameter: "session" | "showcase" = "session") {
  const url = new URL(window.location.href);
  url.searchParams.delete("session");
  url.searchParams.delete("showcase");
  if (sessionId) url.searchParams.set(parameter, sessionId);
  window.history.replaceState({}, "", url);
}

function githubSignInUrl() {
  const returnTo = `${window.location.pathname}${window.location.search}`;
  return `/.auth/login/github?post_login_redirect_uri=${encodeURIComponent(returnTo)}`;
}

