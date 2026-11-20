export const corsHeaders = {
  "Access-Control-Allow-Origin": "https://yantotsu.github.io",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
  "Vary": "Origin",
};

export function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { ...corsHeaders, "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" },
  });
}

export function getUserClient(request: Request) {
  const url = Deno.env.get("SUPABASE_URL");
  const keys = Deno.env.get("SUPABASE_PUBLISHABLE_KEYS");
  const publishableKey = keys ? JSON.parse(keys).default : (Deno.env.get("SUPABASE_PUBLISHABLE_KEY") ?? Deno.env.get("SUPABASE_ANON_KEY"));
  const authorization = request.headers.get("Authorization");
  if (!url || !publishableKey || !authorization?.startsWith("Bearer ")) return null;
  return createClient(url, publishableKey, { global: { headers: { Authorization: authorization } } });
}

import { createClient } from "npm:@supabase/supabase-js@2.119.1";
