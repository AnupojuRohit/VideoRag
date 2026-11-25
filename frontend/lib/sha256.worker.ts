import { sha256 } from "@noble/hashes/sha256";

type WorkerRequest = {
  file: File;
};

type WorkerResponse =
  | { type: "complete"; sha256: string }
  | { type: "error"; message: string };

self.addEventListener("message", async (event: MessageEvent<WorkerRequest>) => {
  try {
    const reader = event.data.file.stream().getReader();
    const hash = sha256.create();
    while (true) {
      const { done, value } = await reader.read();
      if (done) {
        break;
      }
      hash.update(value);
    }

    const digest = hash.digest();
    const response: WorkerResponse = {
      type: "complete",
      sha256: Array.from(digest, (byte) => byte.toString(16).padStart(2, "0")).join(""),
    };
    self.postMessage(response);
  } catch {
    const response: WorkerResponse = {
      type: "error",
      message: "The selected file could not be hashed.",
    };
    self.postMessage(response);
  }
});
