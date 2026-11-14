import { FormEvent, useState } from "react";
import { Link, Navigate, useNavigate, useSearchParams } from "react-router-dom";
import { api } from "../api";
import { useAuth } from "../auth";
import { Field, Icon, PasswordField, ThemeButton } from "../components/ui";

type Mode = "login" | "signup" | "forgot" | "reset";

const COPY: Record<Mode, { title: string; sub: string; cta: string }> = {
  login: { title: "Welcome back", sub: "Sign in to your drive.", cta: "Sign in" },
  signup: { title: "Create your account", sub: "Just a username, a password and an email for recovery.", cta: "Create account" },
  forgot: { title: "Forgot your password?", sub: "Enter your email and we'll send a reset link.", cta: "Send reset link" },
  reset: { title: "Choose a new password", sub: "Pick something you haven't used before.", cta: "Save password" },
};

export default function Auth({ mode }: { mode: Mode }) {
  const { user, signIn } = useAuth();
  const nav = useNavigate();
  const [params] = useSearchParams();
  const [username, setUsername] = useState("");
  const [email, setEmail] = useState("");
  const [pw, setPw] = useState("");
  const [pw2, setPw2] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [done, setDone] = useState("");
  const c = COPY[mode];

  if (user && mode !== "reset") return <Navigate to="/app" replace />;

  const validate = (): string => {
    if (mode === "signup") {
      if (!/^[A-Za-z0-9_]{3,24}$/.test(username.trim())) return "Username: 3-24 letters, numbers or underscore";
      if (!/^[^@\s]+@[^@\s]+\.[^@\s]{2,}$/.test(email.trim())) return "Enter a valid email address";
    }
    if (mode === "login" && (!username.trim() || !pw)) return "Enter your username and password";
    if ((mode === "signup" || mode === "reset") && pw.length < 8) return "Password must be at least 8 characters";
    if ((mode === "signup" || mode === "reset") && pw !== pw2) return "Passwords don't match";
    if (mode === "forgot" && !email.trim()) return "Enter your email";
    return "";
  };

  const submit = async (e?: FormEvent) => {
    e?.preventDefault();
    const v = validate();
    if (v) return setErr(v);
    setErr(""); setBusy(true);
    try {
      if (mode === "login") {
        const r = await api.login(username.trim(), pw);
        signIn(r.token, r.user); nav("/app", { replace: true });
      } else if (mode === "signup") {
        const r = await api.signup(username.trim(), pw, email.trim());
        signIn(r.token, r.user); nav("/app", { replace: true });
      } else if (mode === "forgot") {
        await api.forgot(email.trim());
        setDone("If that email has an account, a reset link is on its way. It's valid for 30 minutes.");
      } else {
        const token = params.get("token");
        if (!token) throw new Error("This reset link is incomplete");
        await api.reset(token, pw);
        setDone("Password updated. You can sign in now.");
      }
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="auth">
      <aside className="auth-art" aria-hidden="true">
        <div className="blob b1" /><div className="blob b2" /><div className="blob b3" />
        <Link to="/" className="brand light"><span className="brand-mark"><Icon name="cloud" fill /></span><b>GitDrive</b></Link>
        <h2>Files that stay yours.</h2>
        <p>Encrypted per file. Shared only with the people you choose.</p>
      </aside>
      <main className="auth-main">
        <div className="auth-top"><Link to="/" className="btn text sm"><Icon name="arrow_back" /> Home</Link><ThemeButton /></div>
        <form className="auth-card" onSubmit={submit} noValidate>
          <h1>{c.title}</h1>
          <p className="muted">{c.sub}</p>
          {done ? (
            <div className="notice ok"><Icon name="mark_email_read" fill /><span>{done}</span></div>
          ) : (
            <>
              {(mode === "login" || mode === "signup") && <Field label="Username" value={username} onChange={setUsername} autoFocus autoComplete="username" maxLength={24} />}
              {(mode === "signup" || mode === "forgot") && <Field label="Email" type="email" value={email} onChange={setEmail} autoComplete="email" autoFocus={mode === "forgot"} hint={mode === "signup" ? "Only used to reset your password" : undefined} />}
              {mode !== "forgot" && <PasswordField label={mode === "reset" ? "New password" : "Password"} value={pw} onChange={setPw} autoComplete={mode === "login" ? "current-password" : "new-password"} onEnter={mode === "login" ? submit : undefined} />}
              {(mode === "signup" || mode === "reset") && <PasswordField label="Confirm password" value={pw2} onChange={setPw2} autoComplete="new-password" onEnter={submit} />}
              {err && <div className="notice err" role="alert"><Icon name="error" fill /><span>{err}</span></div>}
              <button className="btn filled xl block" disabled={busy}>{busy ? <span className="mini-loader" /> : c.cta}</button>
            </>
          )}
          <div className="auth-links">
            {mode === "login" && <><Link to="/forgot">Forgot password?</Link><span>New here? <Link to="/signup">Create an account</Link></span></>}
            {mode === "signup" && <span>Already have an account? <Link to="/login">Sign in</Link></span>}
            {(mode === "forgot" || mode === "reset") && <Link to="/login">Back to sign in</Link>}
          </div>
        </form>
      </main>
    </div>
  );
}
