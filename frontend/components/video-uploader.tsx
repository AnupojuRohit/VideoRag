"use client";

import { FormEvent, useEffect, useState } from "react";

import {
  DuplicateAction,
  JobProgress,
  UploadPreflight,
  UploadReceipt,
  cancelJob,
  getJobProgress,
  hashFile,
  preflightVideo,
  recordProcessingConsent,
  resumeJob,
  retryJob,
  uploadVideo,
} from "@/lib/video-upload";

type UploadPhase =
  | "idle"
  | "hashing"
  | "checking"
  | "uploading"
  | "uploaded"
  | "duplicate"
  | "consent"
  | "error";

function duplicateMessage(action: DuplicateAction): string {
  switch (action) {
    case "reuse_ready":
      return "This exact file is already indexed and ready. The existing asset will be reused.";
    case "reuse_paused_quota":
      return "This exact file already exists and is paused for provider quota. No duplicate upload was created.";
    case "retry_available":
      return "This exact file already exists with a failed or cancelled job. Use the job panel below to retry when eligible.";
    case "attach_existing_job":
      return "This exact file is already being processed. Its existing job will be reused.";
    case "reuse_uploaded":
    case "uploaded":
      return "This exact file is already uploaded. Its existing asset and job will be reused.";
    case "upload":
      return "No matching upload exists.";
  }
}

function formatBytes(bytes: number): string {
  if (bytes < 1_000_000) {
    return `${Math.ceil(bytes / 1_000)} KB`;
  }
  return `${(bytes / 1_000_000).toFixed(1)} MB`;
}

export default function VideoUploader() {
  const [file, setFile] = useState<File | null>(null);
  const [phase, setPhase] = useState<UploadPhase>("idle");
  const [progress, setProgress] = useState(0);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [preflight, setPreflight] = useState<UploadPreflight | null>(null);
  const [receipt, setReceipt] = useState<UploadReceipt | null>(null);
  const [consentAccepted, setConsentAccepted] = useState(false);
  const [jobProgress, setJobProgress] = useState<JobProgress | null>(null);
  const [jobActionBusy, setJobActionBusy] = useState(false);
  const [jobRefreshKey, setJobRefreshKey] = useState(0);
  const jobId = receipt?.job_id ?? preflight?.job_id ?? null;
  const busy =
    phase === "hashing" || phase === "checking" || phase === "uploading";

  useEffect(() => {
    if (!jobId) {
      setJobProgress(null);
      return;
    }

    let active = true;
    const refresh = async () => {
      try {
        const progress = await getJobProgress(jobId);
        if (active) {
          setJobProgress(progress);
          if (
            ["READY", "FAILED", "CANCELLED", "PAUSED_QUOTA"].includes(
              progress.status,
            ) ||
            (progress.status === "PROCESSING" && progress.current_stage === null)
          ) {
            window.clearInterval(interval);
          }
        }
      } catch {
        if (active) {
          setError("Job progress is temporarily unavailable. The job state is saved locally.");
        }
      }
    };
    const interval = window.setInterval(() => void refresh(), 2500);
    void refresh();
    return () => {
      active = false;
      window.clearInterval(interval);
    };
  }, [jobId, jobRefreshKey]);

  function selectFile(selected: File | null) {
    setFile(selected);
    setPhase("idle");
    setProgress(0);
    setMessage("");
    setError("");
    setPreflight(null);
    setReceipt(null);
    setJobProgress(null);
    setJobRefreshKey(0);
    setConsentAccepted(false);
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!file || busy) {
      return;
    }

    setError("");
    setMessage("");
    setPreflight(null);
    setReceipt(null);

    try {
      setPhase("hashing");
      const sha256 = await hashFile(file);

      setPhase("checking");
      const result = await preflightVideo(sha256);
      setPreflight(result);
      if (!result.can_upload) {
        if (
          result.asset_id &&
          result.status !== "READY" &&
          !result.external_processing_consent
        ) {
          if (!consentAccepted) {
            setMessage(
              "Accept the external AI processing terms below before this existing job can continue.",
            );
            setPhase("consent");
            return;
          }
          await recordProcessingConsent(result.asset_id);
        }
        setMessage(duplicateMessage(result.duplicate_action));
        setPhase("duplicate");
        return;
      }

      if (!consentAccepted) {
        setMessage(
          "Accept the external AI processing terms below before uploading.",
        );
        setPhase("consent");
        return;
      }

      setPhase("uploading");
      const uploaded = await uploadVideo(
        file,
        sha256,
        consentAccepted,
        setProgress,
      );
      setReceipt(uploaded);
      setMessage(
        uploaded.duplicate
          ? duplicateMessage(uploaded.duplicate_action)
          : "Upload validated and saved. Durable background processing has been queued.",
      );
      setPhase(uploaded.duplicate ? "duplicate" : "uploaded");
    } catch (reason) {
      setError(
        reason instanceof Error
          ? reason.message
          : "The upload could not be completed.",
      );
      setPhase("error");
    }
  }

  const activity = {
    idle: "",
    hashing: "Computing SHA-256 in your browser…",
    checking: "Checking for an exact-byte duplicate…",
    uploading: `Uploading video… ${progress}%`,
    uploaded: "Upload complete",
    duplicate: "Existing asset found",
    consent: "Consent is required before external processing",
    error: "Upload needs attention",
  }[phase];

  async function runJobAction(
    action: "cancel" | "retry" | "resume",
  ) {
    if (!jobId || jobActionBusy) {
      return;
    }
    setJobActionBusy(true);
    setError("");
    try {
      const update =
        action === "cancel"
          ? await cancelJob(jobId)
          : action === "retry"
            ? await retryJob(jobId)
            : await resumeJob(jobId);
      setJobProgress(update);
      setJobRefreshKey((key) => key + 1);
    } catch (reason) {
      setError(
        reason instanceof Error
          ? reason.message
          : "The job action could not be completed.",
      );
    } finally {
      setJobActionBusy(false);
    }
  }

  return (
    <section className="upload-panel" aria-labelledby="upload-title">
      <div>
        <p className="eyebrow">Video ingestion</p>
        <h2 id="upload-title">Add a video</h2>
        <p className="upload-description">
          Select an MP4. VideoRAG checks its SHA-256 before upload and validates
          the actual media streams on the local server.
        </p>
      </div>

      <form className="upload-form" onSubmit={submit} aria-busy={busy}>
        <label className="file-picker" htmlFor="video-file">
          <span>{file ? file.name : "Choose an MP4 video"}</span>
          <input
            id="video-file"
            type="file"
            accept=".mp4,video/mp4"
            disabled={busy}
            onChange={(event) => selectFile(event.currentTarget.files?.[0] ?? null)}
          />
        </label>
        {file && (
          <p className="file-details">
            {formatBytes(file.size)} · MP4 files only
          </p>
        )}
        <label className="consent-control">
          <input
            type="checkbox"
            checked={consentAccepted}
            disabled={busy}
            onChange={(event) => setConsentAccepted(event.currentTarget.checked)}
          />
          <span>
            I agree to send audio, transcripts, selected frames, and OCR text to
            the configured free-tier AI provider. Unpaid Gemini content may be
            used to improve Google products and may be human-reviewed. I will not
            submit sensitive, confidential, or personal content.
            {" "}Gemini API use requires an operator aged 18 or older and a
            professional or business purpose; it is not for consumer-use apps
            or apps directed to minors.
          </span>
        </label>
        <button className="upload-button" type="submit" disabled={!file || busy}>
          {busy ? "Working…" : phase === "error" ? "Try again" : "Check and upload"}
        </button>
      </form>

      {phase === "uploading" && (
        <progress
          className="upload-progress"
          value={progress}
          max={100}
          aria-label="Video upload progress"
        />
      )}
      <p className="upload-status" role="status" aria-live="polite">
        {activity}
      </p>
      {message && (
        <div
          className={`upload-result${phase === "duplicate" ? " upload-result--duplicate" : ""}`}
          role="status"
        >
          <p>{message}</p>
          {(receipt || preflight?.asset_id) && (
            <dl className="asset-details">
              <div>
                <dt>Asset</dt>
                <dd>{receipt?.asset_id ?? preflight?.asset_id}</dd>
              </div>
              {(receipt?.job_id ?? preflight?.job_id) && (
                <div>
                  <dt>Job</dt>
                  <dd>{receipt?.job_id ?? preflight?.job_id}</dd>
                </div>
              )}
              <div>
                <dt>Status</dt>
                <dd>{receipt?.status ?? preflight?.status ?? "Existing"}</dd>
              </div>
            </dl>
          )}
          {jobProgress && (
            <div className="job-progress" aria-labelledby="job-progress-title">
              <div className="job-progress-heading">
                <div>
                  <p className="eyebrow">Background processing</p>
                  <h3 id="job-progress-title">{jobProgress.status}</h3>
                  <p>
                    {jobProgress.completed_stages} of {jobProgress.total_stages}{" "}
                    stages complete · {jobProgress.progress_percent}%
                  </p>
                </div>
                {jobProgress.cancel_available && (
                  <button
                    type="button"
                    className="secondary-button"
                    disabled={jobActionBusy}
                    onClick={() => void runJobAction("cancel")}
                  >
                    Cancel
                  </button>
                )}
                {jobProgress.retry_available && (
                  <button
                    type="button"
                    className="secondary-button"
                    disabled={jobActionBusy}
                    onClick={() => void runJobAction("retry")}
                  >
                    Retry
                  </button>
                )}
                {jobProgress.status === "PAUSED_QUOTA" && (
                  <button
                    type="button"
                    className="secondary-button"
                    disabled={
                      jobActionBusy ||
                      (jobProgress.resume_after !== null &&
                        Date.parse(jobProgress.resume_after) > Date.now())
                    }
                    onClick={() => void runJobAction("resume")}
                  >
                    Resume when allowed
                  </button>
                )}
              </div>
              <ol className="stage-list">
                {jobProgress.stages.map((stage) => (
                  <li
                    key={stage.name}
                    className={`stage-item stage-item--${stage.status.toLowerCase()}`}
                  >
                    <span>{stage.name.replace(/_/g, " ")}</span>
                    <span>{stage.status.toLowerCase().replace(/_/g, " ")}</span>
                    {stage.error_message && <small>{stage.error_message}</small>}
                  </li>
                ))}
              </ol>
              {jobProgress.status === "PROCESSING" &&
                jobProgress.stages.some(
                  (stage) =>
                    stage.name === "scene_detection" &&
                    stage.status === "PENDING",
                ) && (
                  <p className="job-progress-note">
                    Audio processing is implemented in this phase. Visual analysis
                    and shared indexing remain pending, so this asset is not marked
                    ready yet.
                  </p>
                )}
              {jobProgress.resume_after && jobProgress.status === "PAUSED_QUOTA" && (
                <p>
                  Provider cooldown ends{" "}
                  <time dateTime={jobProgress.resume_after}>
                    {new Date(jobProgress.resume_after).toLocaleString()}
                  </time>
                  .
                </p>
              )}
            </div>
          )}
        </div>
      )}
      {error && (
        <p className="upload-error" role="alert">
          {error}
        </p>
      )}
    </section>
  );
}
