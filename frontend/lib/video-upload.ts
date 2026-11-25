export type DuplicateAction =
  | "upload"
  | "uploaded"
  | "reuse_uploaded"
  | "attach_existing_job"
  | "reuse_ready"
  | "reuse_paused_quota"
  | "retry_available";

export type UploadPreflight = {
  can_upload: boolean;
  duplicate_action: DuplicateAction;
  asset_id: string | null;
  job_id: string | null;
  status: string | null;
  retry_available: boolean;
  external_processing_consent: boolean;
};

export type UploadReceipt = {
  asset_id: string;
  job_id: string | null;
  sha256_hash: string;
  original_filename: string;
  file_size_bytes: number;
  duration_seconds: number;
  status: string;
  duplicate: boolean;
  duplicate_action: Exclude<DuplicateAction, "upload">;
};

export type StageProgress = {
  name: string;
  status: string;
  attempt_count: number;
  started_at: string | null;
  finished_at: string | null;
  error_code: string | null;
  error_message: string | null;
};

export type JobProgress = {
  id: string;
  video_id: string;
  status: string;
  current_stage: string | null;
  attempt_count: number;
  completed_stages: number;
  total_stages: number;
  progress_percent: number;
  started_at: string | null;
  finished_at: string | null;
  resume_after: string | null;
  retry_available: boolean;
  cancel_available: boolean;
  stages: StageProgress[];
};

type ApiErrorResponse = {
  error?: {
    message?: string;
  };
};

type HashWorkerResponse =
  | { type: "complete"; sha256: string }
  | { type: "error"; message: string };

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export function hashFile(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const worker = new Worker(new URL("./sha256.worker.ts", import.meta.url), {
      type: "module",
    });

    worker.addEventListener("message", (event: MessageEvent<HashWorkerResponse>) => {
      worker.terminate();
      const response = event.data;
      if (response.type === "complete") {
        resolve(response.sha256);
      } else {
        reject(new Error(response.message));
      }
    });
    worker.addEventListener("error", (event) => {
      worker.terminate();
      reject(new Error(event.message || "The selected file could not be hashed."));
    });
    worker.postMessage({ file });
  });
}

async function parseResponse<T>(response: Response): Promise<T> {
  if (!response.ok) {
    const body = (await response.json()) as ApiErrorResponse;
    throw new Error(body.error?.message ?? `Video API returned ${response.status}.`);
  }
  return (await response.json()) as T;
}

export async function preflightVideo(sha256: string): Promise<UploadPreflight> {
  const response = await fetch(`${API_URL}/api/v1/videos/preflight`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ sha256_hash: sha256 }),
    cache: "no-store",
  });
  return parseResponse<UploadPreflight>(response);
}

export async function uploadVideo(
  file: File,
  sha256: string,
  externalProcessingConsent: boolean,
  onProgress: (percent: number) => void,
): Promise<UploadReceipt> {
  const url = new URL(`${API_URL}/api/v1/videos/upload`);
  url.searchParams.set("filename", file.name);

  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open("POST", url);
    request.setRequestHeader(
      "Content-Type",
      file.type || "application/octet-stream",
    );
    request.setRequestHeader("X-Content-SHA256", sha256);
    request.setRequestHeader(
      "X-External-Processing-Consent",
      String(externalProcessingConsent),
    );
    request.upload.addEventListener("progress", (event) => {
      if (event.lengthComputable) {
        onProgress(Math.round((event.loaded / event.total) * 100));
      }
    });
    request.addEventListener("load", () => {
      if (request.status < 200 || request.status >= 300) {
        let message = `Video API returned ${request.status}.`;
        try {
          const body = JSON.parse(request.responseText) as ApiErrorResponse;
          message = body.error?.message ?? message;
        } catch {
          // Keep the HTTP status message when the server response is not JSON.
        }
        reject(new Error(message));
        return;
      }

      try {
        onProgress(100);
        resolve(JSON.parse(request.responseText) as UploadReceipt);
      } catch {
        reject(new Error("The video API returned an invalid upload response."));
      }
    });
    request.addEventListener("error", () => {
      reject(new Error("The upload could not reach the local VideoRAG API."));
    });
    request.addEventListener("abort", () => {
      reject(new Error("The upload was cancelled."));
    });
    request.send(file);
  });
}

export async function recordProcessingConsent(
  assetId: string,
): Promise<void> {
  const response = await fetch(
    `${API_URL}/api/v1/videos/${assetId}/consent`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ accepted: true }),
      cache: "no-store",
    },
  );
  await parseResponse(response);
}

export async function getJobProgress(jobId: string): Promise<JobProgress> {
  const response = await fetch(`${API_URL}/api/v1/jobs/${jobId}`, {
    cache: "no-store",
  });
  return parseResponse<JobProgress>(response);
}

async function updateJob(
  jobId: string,
  action: "cancel" | "retry" | "resume",
): Promise<JobProgress> {
  const response = await fetch(
    `${API_URL}/api/v1/jobs/${jobId}/${action}`,
    { method: "POST", cache: "no-store" },
  );
  return parseResponse<JobProgress>(response);
}

export function cancelJob(jobId: string): Promise<JobProgress> {
  return updateJob(jobId, "cancel");
}

export function retryJob(jobId: string): Promise<JobProgress> {
  return updateJob(jobId, "retry");
}

export function resumeJob(jobId: string): Promise<JobProgress> {
  return updateJob(jobId, "resume");
}
