import { expect, test } from "@playwright/test";

const videoId = "00000000-0000-4000-8000-000000000001";

test("asks a ready video and seeks to a cited original timestamp", async ({
  page,
}) => {
  await page.route("**/api/v1/videos?limit=100", async (route) => {
    await route.fulfill({
      json: {
        videos: [
          {
            id: videoId,
            original_filename: "lecture.mp4",
            file_size_bytes: 1000,
            duration_seconds: 3600,
            status: "READY",
            created_at: "2026-01-01T00:00:00Z",
            updated_at: "2026-01-01T00:00:00Z",
          },
        ],
        limit: 100,
        offset: 0,
      },
    });
  });
  await page.route(`**/api/v1/videos/${videoId}`, async (route) => {
    await route.fulfill({
      json: {
        id: videoId,
        original_filename: "lecture.mp4",
        file_size_bytes: 1000,
        duration_seconds: 3600,
        status: "READY",
        created_at: "2026-01-01T00:00:00Z",
        updated_at: "2026-01-01T00:00:00Z",
        mime_type: "video/mp4",
        video_codec: "h264",
        audio_codec: "aac",
        width: 1280,
        height: 720,
        external_processing_consent: true,
        job: {
          id: "00000000-0000-4000-8000-000000000002",
          video_id: videoId,
          status: "READY",
          current_stage: null,
          attempt_count: 1,
          completed_stages: 13,
          total_stages: 13,
          progress_percent: 100,
          started_at: "2026-01-01T00:00:00Z",
          finished_at: "2026-01-01T00:01:00Z",
          resume_after: null,
          retry_available: false,
          cancel_available: false,
          stages: [],
        },
      },
    });
  });
  await page.route(`**/api/v1/videos/${videoId}/media`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "video/mp4",
      body: "",
    });
  });
  await page.route(`**/api/v1/videos/${videoId}/answer`, async (route) => {
    await route.fulfill({
      json: {
        video_id: videoId,
        answer: "The speaker says RRF combines ranked lists.",
        abstained: false,
        inference: false,
        citations: [
          {
            citation_id: "E1",
            chunk_id: "00000000-0000-4000-8000-000000000003",
            chunk_type: "TRANSCRIPT",
            content: "RRF combines ranked lists.",
            start_time: 725,
            end_time: 730,
          },
        ],
      },
    });
  });

  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "Video library" }),
  ).toBeVisible();
  await page.getByRole("button").filter({ hasText: "lecture.mp4" }).click();
  await expect(
    page.getByRole("heading", { name: "Video Q&A" }),
  ).toBeVisible();
  await page.getByLabel("Your question").fill("What does RRF do?");
  await page.getByRole("button", { name: "Ask with evidence" }).click();
  await expect(
    page.getByText("The speaker says RRF combines ranked lists."),
  ).toBeVisible();

  await page.locator("video").evaluate((element) => {
    const player = element as HTMLVideoElement;
    Object.defineProperty(player, "currentTime", {
      configurable: true,
      set(value: number) {
        document.documentElement.dataset.lastSeek = String(value);
      },
    });
    player.play = () => Promise.resolve();
  });
  await page
    .getByRole("button", { name: "Seek to 12:05, Speaker transcript" })
    .click();
  await expect(page.locator("html")).toHaveAttribute("data-last-seek", "725");
});

test("shows an explicit empty-library state", async ({ page }) => {
  await page.route("**/api/v1/videos?limit=100", async (route) => {
    await route.fulfill({
      json: { videos: [], limit: 100, offset: 0 },
    });
  });

  await page.goto("/");
  await expect(
    page.getByText("No videos yet. Add an MP4 above"),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Select a video to explore" }),
  ).toBeVisible();
});
