import ApiStatus from "@/components/api-status";
import VideoUploader from "@/components/video-uploader";

export default function Home() {
  return (
    <main className="shell">
      <section className="hero" aria-labelledby="page-title">
        <p className="eyebrow">Local-first video knowledge</p>
        <h1 id="page-title">VideoRAG</h1>
        <p className="intro">
          Search what was said and shown in your videos, with evidence you can
          return to.
        </p>
        <ApiStatus />
      </section>
      <VideoUploader />
      <section className="milestone" aria-labelledby="milestone-title">
        <h2 id="milestone-title">What happens next</h2>
        <p>
          The local API validates and saves uploads, and exact-byte duplicates
          reuse the original asset. Background transcription and visual
          processing are not enabled yet; uploaded videos remain in the
          <code> UPLOADED </code> state.
        </p>
      </section>
    </main>
  );
}
