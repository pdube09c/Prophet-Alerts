// Client config for the static selection page. Served publicly by GitHub Pages,
// so it is intentionally COMMITTED (unlike a normal deployment where it would be
// git-ignored). These are the *public* client values only:
//   - SUPABASE_URL       : the Supabase project URL.
//   - SUPABASE_ANON_KEY  : the Supabase ANON (publishable) key. Public by design;
//                          RLS restricts it to reading survivors + inserting bets
//                          (see config/schema.sql). NOT the service_role key.
//   - STAGE              : "paper" | "stage1" | "stage2" — labels the UI.
//
// The service_role key (SUPABASEKEY) must NEVER appear here — it is server-side
// only (GitHub Actions Secret) and would bypass RLS if exposed in a browser.
window.APP_CONFIG = {
  SUPABASE_URL: "https://fmcvbwlsvpzfdckydtzf.supabase.co",
  SUPABASE_ANON_KEY: "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImZtY3Zid2xzdnB6ZmRja3lkdHpmIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODAwMjA4MjMsImV4cCI6MjA5NTU5NjgyM30.ME5DocYZiTz-xU_IUSvFA7jgVCBqt-ozcak8V6uNHWA",
  STAGE: "paper",
};
