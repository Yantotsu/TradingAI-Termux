import { corsHeaders, getUserClient, jsonResponse } from "../_shared/http.ts";

Deno.serve(async (request: Request) => {
  if (request.method === "OPTIONS") return new Response("ok", { headers: corsHeaders });
  if (request.method !== "POST") return jsonResponse({ error: "method_not_allowed" }, 405);
  const client = getUserClient(request);
  if (!client) return jsonResponse({ error: "authentication_required" }, 401);
  const { data: { user }, error: authError } = await client.auth.getUser();
  if (authError || !user) return jsonResponse({ error: "authentication_required" }, 401);

  let code = "";
  try { code = String((await request.json())?.code ?? "").trim().toUpperCase(); }
  catch { return jsonResponse({ error: "invalid_request" }, 400); }
  if (!/^[A-F0-9]{48}$/.test(code)) return jsonResponse({ error: "invalid_or_expired_code" }, 400);

  const { data, error } = await client.rpc("redeem_access_invite", { p_code: code });
  if (error) return jsonResponse({ error: "invite_redemption_failed" }, 400);
  if (data !== true) return jsonResponse({ error: "invalid_or_expired_code" }, 400);
  return jsonResponse({ access: "granted" });
});
