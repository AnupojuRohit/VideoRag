import ApiStatus from "@/components/api-status";
import VideoWorkspace from "@/components/video-workspace";

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
      <VideoWorkspace />
    </main>
  );
}
