import { useState, type FormEvent } from "react";
import { LogIn, UserPlus, Loader2 } from "lucide-react";
import { useAuth } from "../hooks/useAuth";
import { ApiError } from "../api/client";
import styles from "./LoginPage.module.css";

type Mode = "login" | "register";

const MIN_PASSWORD_LENGTH = 8;

export default function LoginPage() {
  const { login, register } = useAuth();
  const [mode, setMode] = useState<Mode>("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const isRegister = mode === "register";

  function switchMode(next: Mode) {
    setMode(next);
    setError(null);
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);

    const trimmedEmail = email.trim();
    if (!trimmedEmail || !password) {
      setError("Enter your email and password.");
      return;
    }
    if (isRegister && password.length < MIN_PASSWORD_LENGTH) {
      setError(`Password must be at least ${MIN_PASSWORD_LENGTH} characters.`);
      return;
    }

    setSubmitting(true);
    try {
      if (isRegister) {
        // Create the account, then immediately sign in for a seamless flow.
        await register(trimmedEmail, password);
      }
      await login(trimmedEmail, password);
      // On success the auth status flips to "authenticated" and the app shell
      // replaces this screen — no navigation needed.
    } catch (err) {
      setError(resolveErrorMessage(err, isRegister));
      setSubmitting(false);
    }
  }

  return (
    <div className={styles.page}>
      <main className={styles.card} aria-labelledby="auth-title">
        <div className={styles.brand}>
          <span className={styles.mark} aria-hidden="true">
            <span />
            <span />
            <span />
          </span>
          <span className={styles.brandText}>RAG Console</span>
        </div>

        <h1 id="auth-title" className={styles.title}>
          {isRegister ? "Create your account" : "Sign in"}
        </h1>
        <p className={styles.subtitle}>
          {isRegister
            ? "Register to access the console."
            : "Enter your credentials to continue."}
        </p>

        <form className={styles.form} onSubmit={handleSubmit} noValidate>
          <div className={styles.field}>
            <label htmlFor="email" className={styles.label}>
              Email
            </label>
            <input
              id="email"
              name="email"
              type="email"
              autoComplete="email"
              className={styles.input}
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              disabled={submitting}
              required
              autoFocus
            />
          </div>

          <div className={styles.field}>
            <label htmlFor="password" className={styles.label}>
              Password
            </label>
            <input
              id="password"
              name="password"
              type="password"
              autoComplete={isRegister ? "new-password" : "current-password"}
              className={styles.input}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              disabled={submitting}
              minLength={isRegister ? MIN_PASSWORD_LENGTH : undefined}
              required
            />
            {isRegister && (
              <span className={styles.hint}>
                At least {MIN_PASSWORD_LENGTH} characters.
              </span>
            )}
          </div>

          {error && (
            <p className={styles.error} role="alert" aria-live="assertive">
              {error}
            </p>
          )}

          <button type="submit" className={styles.submit} disabled={submitting}>
            {submitting ? (
              <Loader2 className={styles.spinner} size={16} aria-hidden="true" />
            ) : isRegister ? (
              <UserPlus size={16} aria-hidden="true" />
            ) : (
              <LogIn size={16} aria-hidden="true" />
            )}
            <span>
              {submitting
                ? isRegister
                  ? "Creating account…"
                  : "Signing in…"
                : isRegister
                  ? "Create account"
                  : "Sign in"}
            </span>
          </button>
        </form>

        <p className={styles.switch}>
          {isRegister ? "Already have an account?" : "Need an account?"}{" "}
          <button
            type="button"
            className={styles.switchButton}
            onClick={() => switchMode(isRegister ? "login" : "register")}
            disabled={submitting}
          >
            {isRegister ? "Sign in" : "Create one"}
          </button>
        </p>
      </main>
    </div>
  );
}

function resolveErrorMessage(err: unknown, isRegister: boolean): string {
  if (err instanceof ApiError) {
    if (err.status === 409) return "An account with this email already exists.";
    if (err.status === 401) return "Incorrect email or password.";
    if (err.status === 422) return "Please enter a valid email and password.";
    if (err.status >= 500) return "The server is unavailable. Try again shortly.";
    return err.detail;
  }
  return isRegister
    ? "Could not create the account. Check your connection and try again."
    : "Could not sign in. Check your connection and try again.";
}
