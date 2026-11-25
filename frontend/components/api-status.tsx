"use client";

import { useEffect, useState } from "react";

type ApiState = "checking" | "online" | "offline";

export default function ApiStatus() {
  const [state, setState] = useState<ApiState>("checking");

  useEffect(() => {
    const apiUrl = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
    const controller = new AbortController();

    fetch(`${apiUrl}/health`, { signal: controller.signal, cache: "no-store" })
      .then((response) => {
        if (!response.ok) {
          throw new Error(`API returned ${response.status}`);
        }
        return response.json() as Promise<{ status?: string }>;
      })
      .then((body) => setState(body.status === "ok" ? "online" : "offline"))
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === "AbortError") {
          return;
        }
        setState("offline");
      });

    return () => controller.abort();
  }, []);

  const label = {
    checking: "Checking API…",
    online: "API connected",
    offline: "API unavailable",
  }[state];

  return (
    <p
      className={`api-status api-status--${state}`}
      role="status"
      aria-live="polite"
    >
      <span className="status-dot" aria-hidden="true" />
      {label}
    </p>
  );
}
