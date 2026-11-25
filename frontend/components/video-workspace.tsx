"use client";

import {
  FormEvent,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";

import VideoUploader from "@/components/video-uploader";
import {
  AnswerCitation,
  JobProgress,
  VideoAnswer,
  VideoDetail,
  VideoLibraryItem,
  askVideoQuestion,
  cancelJob,
  getVideoDetail,
  getVideoLibrary,
  getVideoMediaUrl,
  recordProcessingConsent,
  resumeJob,
  retryJob,
} from "@/lib/video-upload";

type ChatEntry =
  | { role: "user"; text: string }
  | { role: "assistant"; answer: VideoAnswer };

const STAGE_LABELS: Record<string, string> = {
  media_validation: "Validating media",
  media_preparation: "Preparing media",
  audio_extraction: "Extracting audio",
  audio_segmentation: "Preparing audio segments",
  transcription: "Transcribing",
  transcript_chunking: "Preparing transcript evidence",
  scene_detection: "Detecting scenes",
  frame_extraction: "Selecting frames",
  ocr: "Reading visible text",
  visual_analysis: "Analysing visuals",
  embeddings: "Creating embeddings",
  indexing: "Indexing evidence",
  verification: "Verifying index",
};

const STATUS_LABELS: Record<string, string> = {
  UPLOADED: "Uploaded",
  QUEUED: "Queued",
  PROCESSING: "Processing",
  INDEXING: "Indexing evidence",
  VERIFYING: "Verifying",
  READY: "Ready",
  PAUSED_QUOTA: "Waiting for free-tier quota",
  FAILED: "Failed · retry may be available",
  CANCELLED: "Cancelled",
};

function formatDuration(seconds: number): string {
  const total = Math.floor(seconds);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const remainder = total % 60;
  return hours > 0
    ? `${hours}:${String(minutes).padStart(2, "0")}:${String(remainder).padStart(2, "0")}`
    : `${minutes}:${String(remainder).padStart(2, "0")}`;
}

function formatTimestamp(seconds: number): string {
  const safe = Math.max(0, Math.floor(seconds));
  const hours = Math.floor(safe / 3600);
  const minutes = Math.floor((safe % 3600) / 60);
  const remainder = safe % 60;
  return hours > 0
    ? `${hours}:${String(minutes).padStart(2, "0")}:${String(remainder).padStart(2, "0")}`
    : `${minutes}:${String(remainder).padStart(2, "0")}`;
}

function formatBytes(bytes: number): string {
  return bytes < 1_000_000
    ? `${Math.ceil(bytes / 1_000)} KB`
    : `${(bytes / 1_000_000).toFixed(1)} MB`;
}

function statusLabel(status: string): string {
  return STATUS_LABELS[status] ?? status.replaceAll("_", " ").toLowerCase();
}

function CitationButton({
  citation,
  onSeek,
}: {
  citation: AnswerCitation;
  onSeek: (seconds: number) => void;
}) {
  const modality =
    citation.chunk_type === "TRANSCRIPT"
      ? "Speaker transcript"
      : "Visible frame / OCR";
  return (
    <article className="citation-card">
      <button
        className="timestamp-button"
        type="button"
        onClick={() => onSeek(citation.start_time)}
        aria-label={`Seek to ${formatTimestamp(citation.start_time)}, ${modality}`}
      >
        {formatTimestamp(citation.start_time)} <span>{citation.citation_id}</span>
      </button>
      <p className="citation-modality">{modality}</p>
      <p>{citation.content}</p>
    </article>
  );
}

function ProcessingPanel({
  job,
  onAction,
  busy,
}: {
  job: JobProgress;
  onAction: (action: "cancel" | "retry" | "resume") => void;
  busy: boolean;
}) {
  const resumeBlocked =
    job.resume_after !== null && Date.parse(job.resume_after) > Date.now();
  return (
    <section className="processing-panel" aria-labelledby="processing-title">
      <div className="processing-heading">
        <div>
          <h3 id="processing-title">Processing progress</h3>
          <p>
            {statusLabel(job.status)}
            {job.current_stage
              ? ` · ${STAGE_LABELS[job.current_stage] ?? job.current_stage}`
              : ""}
          </p>
        </div>
        <strong>{job.progress_percent}%</strong>
      </div>
      <progress
        className="stage-progress"
        value={job.progress_percent}
        max={100}
        aria-label={`Processing progress ${job.progress_percent}%`}
      />
      {job.status === "PAUSED_QUOTA" && (
        <p className="quota-note">
          Processing is paused; VideoRAG will not switch to a paid provider.
          {resumeBlocked
            ? ` Retry is available after ${new Date(job.resume_after as string).toLocaleString()}.`
            : " Resume when the provider permits another request."}
        </p>
      )}
      {job.status === "FAILED" && (
        <p className="quota-note">
          This video is not marked ready. Review the failed stage before retrying.
        </p>
      )}
      <ul className="workspace-stage-list">
        {job.stages.map((stage) => (
          <li
            className={`workspace-stage workspace-stage--${stage.status.toLowerCase()}`}
            key={stage.name}
          >
            <span>{STAGE_LABELS[stage.name] ?? stage.name}</span>
            <span>{statusLabel(stage.status)}</span>
            {stage.error_message && <small>{stage.error_message}</small>}
          </li>
        ))}
      </ul>
      <div className="job-actions">
        {job.cancel_available && (
          <button
            className="secondary-button"
            type="button"
            disabled={busy}
            onClick={() => onAction("cancel")}
          >
            Cancel
          </button>
        )}
        {job.retry_available && (
          <button
            className="secondary-button"
            type="button"
            disabled={busy}
            onClick={() => onAction("retry")}
          >
            Retry processing
          </button>
        )}
        {job.status === "PAUSED_QUOTA" && (
          <button
            className="secondary-button"
            type="button"
            disabled={busy || resumeBlocked}
            onClick={() => onAction("resume")}
          >
            Resume after cooldown
          </button>
        )}
      </div>
    </section>
  );
}

export default function VideoWorkspace() {
  const [videos, setVideos] = useState<VideoLibraryItem[]>([]);
  const [libraryLoading, setLibraryLoading] = useState(true);
  const [libraryError, setLibraryError] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<VideoDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState("");
  const [chat, setChat] = useState<ChatEntry[]>([]);
  const [question, setQuestion] = useState("");
  const [answerBusy, setAnswerBusy] = useState(false);
  const [actionBusy, setActionBusy] = useState(false);
  const [consentChecked, setConsentChecked] = useState(false);
  const [consentBusy, setConsentBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const videoRef = useRef<HTMLVideoElement>(null);

  const refreshLibrary = useCallback(async () => {
    try {
      const result = await getVideoLibrary();
      setVideos(result.videos);
      setLibraryError("");
    } catch (reason) {
      setLibraryError(
        reason instanceof Error ? reason.message : "The video library is unavailable.",
      );
    } finally {
      setLibraryLoading(false);
    }
  }, []);

  useEffect(() => {
    void refreshLibrary();
    const interval = window.setInterval(() => void refreshLibrary(), 5000);
    return () => window.clearInterval(interval);
  }, [refreshLibrary]);

  useEffect(() => {
    if (!selectedId) {
      setDetail(null);
      setDetailLoading(false);
      return;
    }
    let active = true;
    let interval = 0;
    const refresh = async () => {
      try {
        const result = await getVideoDetail(selectedId);
        if (active) {
          setDetail(result);
          setDetailError("");
          if (
            ["READY", "FAILED", "CANCELLED"].includes(result.status) &&
            result.job?.status !== "PAUSED_QUOTA"
          ) {
            window.clearInterval(interval);
          }
        }
      } catch (reason) {
        if (active) {
          setDetailError(
            reason instanceof Error
              ? reason.message
              : "Video details are temporarily unavailable.",
          );
        }
      } finally {
        if (active) {
          setDetailLoading(false);
        }
      }
    };
    setDetailLoading(true);
    interval = window.setInterval(() => void refresh(), 3000);
    void refresh();
    return () => {
      active = false;
      window.clearInterval(interval);
    };
  }, [selectedId, detail?.status]);

  function selectVideo(assetId: string) {
    setSelectedId(assetId);
    setDetail(null);
    setChat([]);
    setQuestion("");
    setNotice("");
    setConsentChecked(false);
  }

  function handleAssetAvailable(assetId: string) {
    selectVideo(assetId);
    void refreshLibrary();
  }

  async function submitQuestion(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const submitted = question.trim();
    if (!selectedId || !submitted || answerBusy) {
      return;
    }
    setAnswerBusy(true);
    setNotice("");
    setChat((entries) => [...entries, { role: "user", text: submitted }]);
    setQuestion("");
    try {
      const answer = await askVideoQuestion(selectedId, submitted);
      setChat((entries) => [...entries, { role: "assistant", answer }]);
    } catch (reason) {
      setNotice(
        reason instanceof Error
          ? reason.message
          : "The question could not be answered right now.",
      );
    } finally {
      setAnswerBusy(false);
    }
  }

  function seekTo(seconds: number) {
    const player = videoRef.current;
    if (!player) {
      setNotice("The video player is not available.");
      return;
    }
    player.currentTime = seconds;
    void player.play().catch(() => {
      setNotice("The player is ready at the cited time. Press play to continue.");
    });
  }

  async function runJobAction(action: "cancel" | "retry" | "resume") {
    const jobId = detail?.job?.id;
    if (!jobId || actionBusy) {
      return;
    }
    setActionBusy(true);
    setNotice("");
    try {
      const progress =
        action === "cancel"
          ? await cancelJob(jobId)
          : action === "retry"
            ? await retryJob(jobId)
            : await resumeJob(jobId);
      setDetail((current) =>
        current ? { ...current, status: progress.status, job: progress } : current,
      );
      await refreshLibrary();
    } catch (reason) {
      setNotice(
        reason instanceof Error ? reason.message : "The job action failed.",
      );
    } finally {
      setActionBusy(false);
    }
  }

  async function acceptCurrentConsent() {
    if (!detail || consentBusy || !consentChecked) {
      return;
    }
    setConsentBusy(true);
    setNotice("");
    try {
      await recordProcessingConsent(detail.id);
      setDetail(await getVideoDetail(detail.id));
      setConsentChecked(false);
      await refreshLibrary();
    } catch (reason) {
      setNotice(
        reason instanceof Error
          ? reason.message
          : "External processing consent could not be saved.",
      );
    } finally {
      setConsentBusy(false);
    }
  }

  return (
    <>
    <VideoUploader onAssetAvailable={handleAssetAvailable} />
    <section className="workspace" aria-label="Video library and question answering">
      <aside className="library-panel" aria-labelledby="library-title">
        <div className="library-heading">
          <div>
            <p className="eyebrow">Your collection</p>
            <h2 id="library-title">Video library</h2>
          </div>
          <button
            className="icon-button"
            type="button"
            onClick={() => void refreshLibrary()}
            aria-label="Refresh video library"
          >
            Refresh
          </button>
        </div>
        {libraryLoading && <p className="muted-text">Loading your videos…</p>}
        {libraryError && (
          <div className="inline-error" role="alert">
            <p>{libraryError}</p>
            <button type="button" className="secondary-button" onClick={() => void refreshLibrary()}>
              Try again
            </button>
          </div>
        )}
        {!libraryLoading && !libraryError && videos.length === 0 && (
          <p className="empty-library">
            No videos yet. Add an MP4 above to build your first searchable video.
          </p>
        )}
        {videos.length > 0 && (
          <ul className="video-list">
            {videos.map((video) => (
              <li key={video.id}>
                <button
                  type="button"
                  className={`video-list-item${selectedId === video.id ? " video-list-item--selected" : ""}`}
                  onClick={() => selectVideo(video.id)}
                  aria-current={selectedId === video.id ? "true" : undefined}
                >
                  <span className="video-list-name">{video.original_filename}</span>
                  <span className={`status-pill status-pill--${video.status.toLowerCase()}`}>
                    {statusLabel(video.status)}
                  </span>
                  <span className="video-list-meta">
                    {formatDuration(video.duration_seconds)} ·{" "}
                    {formatBytes(video.file_size_bytes)}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </aside>

      <section className="detail-panel" aria-labelledby="detail-title">
        {!selectedId && (
          <div className="detail-empty">
            <p className="eyebrow">Evidence-grounded Q&amp;A</p>
            <h2 id="detail-title">Select a video to explore</h2>
            <p>
              Ask about what was said or shown. Answers link back to the original
              video timeline.
            </p>
          </div>
        )}
        {selectedId && detailLoading && !detail && (
          <p className="muted-text" role="status">Loading video details…</p>
        )}
        {selectedId && detailError && (
          <div className="inline-error" role="alert">
            <p>{detailError}</p>
            <button
              type="button"
              className="secondary-button"
              onClick={() => selectVideo(selectedId)}
            >
              Reload details
            </button>
          </div>
        )}
        {detail && (
          <>
            <header className="detail-heading">
              <div>
                <p className="eyebrow">Video details</p>
                <h2 id="detail-title">{detail.original_filename}</h2>
                <p className="detail-meta">
                  {formatDuration(detail.duration_seconds)} · {detail.width} ×{" "}
                  {detail.height} · {detail.video_codec.toUpperCase()}
                  {detail.audio_codec
                    ? ` + ${detail.audio_codec.toUpperCase()}`
                    : ""}
                </p>
              </div>
              <span className={`status-pill status-pill--${detail.status.toLowerCase()}`}>
                {statusLabel(detail.status)}
              </span>
            </header>

            <div className="video-detail-grid">
              <div className="video-column">
                <div className="player-frame">
                  <video
                    key={detail.id}
                    ref={videoRef}
                    controls
                    preload="metadata"
                    src={getVideoMediaUrl(detail.id)}
                    aria-label={`Video player for ${detail.original_filename}`}
                    onError={() =>
                      setNotice("The local video file could not be loaded.")
                    }
                  />
                </div>
                {detail.job && (
                  <ProcessingPanel
                    job={detail.job}
                    onAction={(action) => void runJobAction(action)}
                    busy={actionBusy}
                  />
                )}
              </div>

              <section className="chat-panel" aria-labelledby="chat-title">
                <div className="chat-heading">
                  <div>
                    <p className="eyebrow">Ask this video</p>
                    <h3 id="chat-title">Video Q&amp;A</h3>
                  </div>
                  <span>{detail.status === "READY" ? "Evidence verified" : "Not ready"}</span>
                </div>
                {!detail.external_processing_consent && (
                  <div className="consent-panel">
                    <p>
                      Re-consent is required before processing or sending your
                      question and retrieved video evidence to the unpaid AI
                      provider. Google may use submissions and answers to improve
                      products and human reviewers may process them. Do not send
                      sensitive, confidential, or personal content. Unpaid use is
                      unavailable for API clients serving users in the EEA, UK,
                      or Switzerland.
                    </p>
                    <label className="consent-control">
                      <input
                        type="checkbox"
                        checked={consentChecked}
                        onChange={(event) => setConsentChecked(event.currentTarget.checked)}
                      />
                      <span>
                        I confirm I am 18 or older, use this for a professional
                        or business purpose (not consumer use or an application
                        directed to minors), have no active billing on the provider
                        project, and the region is eligible.
                      </span>
                    </label>
                    <button
                      type="button"
                      className="secondary-button"
                      disabled={!consentChecked || consentBusy}
                      onClick={() => void acceptCurrentConsent()}
                    >
                      {consentBusy ? "Saving consent…" : "Accept current terms"}
                    </button>
                  </div>
                )}
                <div className="chat-history" aria-live="polite">
                  {chat.length === 0 && (
                    <p className="chat-empty">
                      {detail.status === "READY" && detail.external_processing_consent
                        ? "Ask a question to find timestamped speech and visual evidence."
                        : "Questions become available after verified indexing and current consent."}
                    </p>
                  )}
                  {chat.map((entry, index) =>
                    entry.role === "user" ? (
                      <p className="chat-user" key={`user-${index}`}>{entry.text}</p>
                    ) : (
                      <article className="chat-answer" key={`answer-${index}`}>
                        {entry.answer.inference && (
                          <p className="inference-label">Inference based on cited evidence</p>
                        )}
                        {entry.answer.abstained && (
                          <p className="abstention-label">Insufficient evidence</p>
                        )}
                        <p>{entry.answer.answer}</p>
                        {entry.answer.citations.length > 0 && (
                          <div className="citation-list">
                            {entry.answer.citations.map((citation) => (
                              <CitationButton
                                key={citation.citation_id}
                                citation={citation}
                                onSeek={seekTo}
                              />
                            ))}
                          </div>
                        )}
                      </article>
                    ),
                  )}
                  {answerBusy && (
                    <p className="muted-text" role="status">Searching evidence and preparing an answer…</p>
                  )}
                </div>
                {notice && <p className="inline-error" role="alert">{notice}</p>}
                <form className="question-form" onSubmit={submitQuestion}>
                  <label htmlFor="video-question">Your question</label>
                  <textarea
                    id="video-question"
                    value={question}
                    onChange={(event) => setQuestion(event.currentTarget.value)}
                    maxLength={1500}
                    rows={3}
                    placeholder="What does the speaker explain about…?"
                    disabled={
                      detail.status !== "READY" ||
                      !detail.external_processing_consent ||
                      answerBusy
                    }
                  />
                  <button
                    className="upload-button"
                    type="submit"
                    disabled={
                      !question.trim() ||
                      detail.status !== "READY" ||
                      !detail.external_processing_consent ||
                      answerBusy
                    }
                  >
                    {answerBusy ? "Thinking…" : "Ask with evidence"}
                  </button>
                </form>
              </section>
            </div>
          </>
        )}
      </section>
    </section>
    </>
  );
}
