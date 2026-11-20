import { corsHeaders, getUserClient, jsonResponse } from "../_shared/http.ts";

Deno.serve(async (request: Request) => {
  if (request.method === "OPTIONS") return new Response("ok", { headers: corsHeaders });
  if (request.method !== "POST") return jsonResponse({ error: "method_not_allowed" }, 405);
  const client = getUserClient(request);
  if (!client) return jsonResponse({ error: "authentication_required" }, 401);
  const { data: { user }, error: authError } = await client.auth.getUser();
  if (authError || !user) return jsonResponse({ error: "authentication_required" }, 401);

  let ttlDays = 7;
  try {
    const body = await request.json();
    if (Number.isInteger(body?.ttl_days)) ttlDays = Math.max(1, Math.min(30, body.ttl_days));
  } catch { /* use default expiry */ }

  const { data, error } = await client.rpc("issue_access_invite", { p_ttl_days: ttlDays });
  if (error) {
    const status = error.code === "42501" ? 403 : 400;
    return jsonResponse({ error: status === 403 ? "not_authorized" : "invite_creation_failed" }, status);
  }
  // The clear code is returned once. Store only its SHA-256 digest in Postgres.
  return jsonResponse({ code: data, expires_in_days: ttlDays });
});
