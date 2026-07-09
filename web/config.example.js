// Client config for the static selection page. COPY to web/config.js
// (git-ignored) and fill in. These are the *public* client values:
//   - SUPABASE_URL       : your Supabase project URL (same as SUPABASEURL).
//   - SUPABASE_ANON_KEY  : the Supabase ANON (publishable) key — NOT the
//                          service_role key. RLS restricts it to reading
//                          survivors + inserting bets (see config/schema.sql).
//   - STAGE              : "paper" | "stage1" | "stage2" — labels the UI.
//
// The service_role key (SUPABASEKEY) must NEVER appear here — it is server-side
// only and would bypass RLS if exposed in a browser.
window.APP_CONFIG = {
  SUPABASE_URL: "",
  SUPABASE_ANON_KEY: "",
  STAGE: "paper",
};
