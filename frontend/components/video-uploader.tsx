"use client";

import { FormEvent, useState } from "react";

import {
  DuplicateAction,
  UploadPreflight,
  UploadReceipt,
  hashFile,
  preflightVideo,
  uploadVideo,
} from "@/lib/video-upload";

type UploadPhase =
  | "idle"
  | "hashing"
  | "checking"
  | "uploading"
  | "uploaded"
  | "duplicate"
  | "error";

function duplicateMessage(action: DuplicateAction): string {
  switch (action) {
    case "reuse_ready":
      return "This exact file is already indexed and ready. The existing asset will be reused.";
    case "reuse_paused_quota":
      return "This exact file already exists and is paused for provider quota. No duplicate upload was created.";
    case "retry_available":
      return "This exact file already exists with a failed or cancelled job. Retry controls are not available yet.";
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
  const busy =
    phase === "hashing" || phase === "checking" || phase === "uploading";

  function selectFile(selected: File | null) {
    setFile(selected);
    setPhase("idle");
    setProgress(0);
    setMessage("");
    setError("");
    setPreflight(null);
    setReceipt(null);
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
        setMessage(duplicateMessage(result.duplicate_action));
        setPhase("duplicate");
        return;
      }

      setPhase("uploading");
      const uploaded = await uploadVideo(file, sha256, setProgress);
      setReceipt(uploaded);
      setMessage(
        uploaded.duplicate
          ? duplicateMessage(uploaded.duplicate_action)
          : "Upload validated and saved. Processing will start when the next pipeline phase is enabled.",
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
    error: "Upload needs attention",
  }[phase];

  return (
    <section className="upload-panel" aria-labelledby="upload-title">
      <div>
        <p className="eyebrow">Phase 2 · Local upload</p>
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
