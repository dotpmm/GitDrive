import { Link } from "react-router-dom";
import { Icon, ThemeButton, Wavy } from "../components/ui";
import { useAuth } from "../auth";

export const REPO_URL = "https://github.com/dotpmm/GitDrive";

const FEATURES = [
  { icon: "bolt", title: "Fast, on purpose", text: "Chunks upload in parallel and land in a single commit. Downloads prefetch ahead over a pooled connection, so big files feel instant.", tone: "primary" },
  { icon: "lock", title: "Encrypted, really", text: "Every file gets its own AES-256 key. Delete a file and its key is destroyed, so what's left on GitHub is unreadable noise.", tone: "tertiary" },
  { icon: "groups", title: "Spaces for people", text: "A private drive, a public space everyone sees, and shared spaces where only the people you invite by username can look inside.", tone: "secondary" },
  { icon: "link", title: "Links that embed", text: "Share a video and paste the link in Discord. It plays right there, with seeking, no download needed.", tone: "tertiary" },
  { icon: "folder_zip", title: "Zip & unzip", text: "Download any folder as a zip, or drop a .zip in and extract it on the server, safely.", tone: "secondary" },
  { icon: "savings", title: "Costs nothing", text: "Built on GitHub repositories and free tiers. No cards, no trials, no surprise bills.", tone: "primary" },
];

const STEPS = [
  { n: "1", title: "Make an account", text: "A username, a password and an email for resets. That's it." },
  { n: "2", title: "Drop your files", text: "Drag files or whole folders in. Watch the wavy bar race." },
  { n: "3", title: "Share how you like", text: "Keep it private, put it in a space, or copy a link that embeds anywhere." },
];

export default function Landing() {
  const { user } = useAuth();
  const tryHref = user ? "/app" : "/signup";
  return (
    <div className="landing">
      <header className="l-nav">
        <Link to="/" className="brand"><span className="brand-mark"><Icon name="cloud" fill /></span><b>GitDrive</b></Link>
        <nav>
          <a href="#features" className="btn text">Features</a>
          <a href={REPO_URL} target="_blank" rel="noreferrer" className="btn text">Repository</a>
          <ThemeButton />
          {user ? <Link to="/app" className="btn filled">Open drive</Link> : <><Link to="/login" className="btn tonal">Sign in</Link></>}
        </nav>
      </header>

      <section className="hero-l">
        <div className="hero-copy">
          <span className="pill"><Icon name="auto_awesome" fill /> Free, encrypted, open source</span>
          <h1>Your files.<br /><em>Encrypted.</em><br />Ridiculously fast.</h1>
          <p>GitDrive is a Google&nbsp;Drive-style cloud for you and your friends, stored on GitHub, protected by per-file encryption, and wrapped in links that play anywhere.</p>
          <div className="hero-cta">
            <Link to={tryHref} className="btn filled xl"><Icon name="rocket_launch" /> Try GitDrive</Link>
            <a href={REPO_URL} target="_blank" rel="noreferrer" className="btn tonal xl"><Icon name="code" /> Visit the repository</a>
          </div>
          <small className="muted">Open source. Read every line before you trust it.</small>
        </div>

        <div className="hero-art" aria-hidden="true">
          <div className="blob b1" /><div className="blob b2" /><div className="blob b3" />
          <div className="mock">
            <div className="mock-bar"><span className="dot" /><span className="dot" /><span className="dot" /><b>My Drive</b></div>
            {[
              ["movie", "holiday-cut.mp4", "184 MB", 100],
              ["folder", "Project Phoenix", "42 files", 100],
              ["picture_as_pdf", "thesis-final.pdf", "12 MB", 100],
            ].map(([ic, n, s]) => (
              <div className="mock-row" key={n as string}>
                <span className="file-ico"><Icon name={ic as string} /></span>
                <div><b>{n}</b><small>{s}</small></div>
                <Icon name="more_vert" />
              </div>
            ))}
            <div className="mock-up">
              <div className="mock-up-head"><Icon name="upload_file" /> <b>raw-footage.zip</b><small>72%</small></div>
              <Wavy value={72} />
            </div>
          </div>
        </div>
      </section>

      <section id="features" className="l-section">
        <h2>Everything you expect from a drive. Plus a few things you don't.</h2>
        <div className="feat-grid">
          {FEATURES.map((f, i) => (
            <article key={f.title} className={`feat ${f.tone} s${i % 3}`}>
              <span className="feat-ico"><Icon name={f.icon} fill /></span>
              <h3>{f.title}</h3>
              <p>{f.text}</p>
            </article>
          ))}
        </div>
      </section>

      <section className="l-section">
        <h2>Up and running in a minute</h2>
        <div className="steps">
          {STEPS.map((s) => (
            <div className="step" key={s.n}><span className="step-n">{s.n}</span><h3>{s.title}</h3><p>{s.text}</p></div>
          ))}
        </div>
      </section>

      <section className="l-cta">
        <h2>Ready to move your stuff in?</h2>
        <div className="hero-cta center">
          <Link to={tryHref} className="btn filled xl"><Icon name="rocket_launch" /> Try GitDrive</Link>
          <a href={REPO_URL} target="_blank" rel="noreferrer" className="btn tonal xl"><Icon name="code" /> Visit the repository</a>
        </div>
      </section>

      <footer className="l-foot">
        <span>GitDrive</span>
        <a href={REPO_URL} target="_blank" rel="noreferrer">GitHub</a>
      </footer>
    </div>
  );
}
