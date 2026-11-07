export default async function LoginPage({ searchParams }: { searchParams: Promise<{ error?: string }> }) {
  const { error } = await searchParams;
  return (
    <div className="card" style={{ maxWidth: 380 }}>
      <h1>Sign in</h1>
      <p className="muted">Local development sign-in through the dev OIDC stub.</p>
      {error && <p className="error">Sign-in failed. Check the username and password.</p>}
      <form action="/api/login" method="post" style={{ display: "grid", gap: 10 }}>
        <label>
          Username
          <input name="username" autoComplete="username" required style={{ width: "100%" }} />
        </label>
        <label>
          Password
          <input name="password" type="password" autoComplete="current-password" required style={{ width: "100%" }} />
        </label>
        <button className="primary" type="submit">
          Sign in
        </button>
      </form>
    </div>
  );
}
