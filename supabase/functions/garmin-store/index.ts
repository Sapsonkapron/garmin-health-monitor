const baseUrl = Deno.env.get("SUPABASE_URL");
const serviceKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY");
const monitorVerifier = Deno.env.get("MONITOR_STORE_VERIFIER");

function json(data: unknown, status = 200) {
  return new Response(JSON.stringify(data), { status, headers: { "Content-Type": "application/json" } });
}

async function rest(path: string, method = "GET", body?: unknown, extraHeaders: Record<string, string> = {}) {
  const response = await fetch(`${baseUrl}/rest/v1/${path}`, {
    method,
    headers: {
      apikey: serviceKey!,
      Authorization: `Bearer ${serviceKey}`,
      "Content-Type": "application/json",
      ...extraHeaders,
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await response.text();
  if (!response.ok) {
    console.error("Database request failed", response.status, text.slice(0, 500));
    throw new Error("database request failed");
  }
  return text ? JSON.parse(text) : [];
}

Deno.serve(async (request) => {
  if (request.method !== "POST") return json({ ok: false }, 405);
  const authorization = request.headers.get("Authorization") ?? "";
  const token = authorization.startsWith("Bearer ") ? authorization.slice(7) : "";
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(token));
  const verifier = Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
  if (!baseUrl || !serviceKey || !token || !monitorVerifier || verifier !== monitorVerifier) {
    return json({ ok: false }, 401);
  }
  try {
    const input = await request.json();
    const action = input.action;
    let data: unknown = null;
    if (action === "state") {
      if (!["assistant", "alerts", "weekly"].includes(input.key)) throw new Error("invalid key");
      const rows = await rest(`assistant_state?select=value&key=eq.${input.key}&limit=1`);
      data = rows[0]?.value ?? {};
    } else if (action === "merge") {
      if (!input.changes || typeof input.changes !== "object" || JSON.stringify(input.changes).length > 100000) throw new Error("invalid changes");
      data = await rest("rpc/merge_assistant_state", "POST", { p_key: input.key, p_changes: input.changes });
    } else if (action === "claim" || action === "release") {
      if (typeof input.kind !== "string" || !/^[a-z0-9_-]{1,80}$/.test(input.kind)) throw new Error("invalid kind");
      let day = new Intl.DateTimeFormat("en-CA", { timeZone: "Europe/Kyiv", year: "numeric", month: "2-digit", day: "2-digit" }).format(new Date());
      if (/^weekly_\d{4}-w\d{2}$/.test(input.kind)) {
        const localDate = new Date(`${day}T00:00:00Z`);
        localDate.setUTCDate(localDate.getUTCDate() - ((localDate.getUTCDay() + 6) % 7));
        day = localDate.toISOString().slice(0, 10);
      }
      if (action === "claim") {
        const rows = await rest("notification_delivery?on_conflict=user_id,kind,local_date", "POST", { user_id: "serhii", kind: input.kind, local_date: day }, { Prefer: "resolution=ignore-duplicates,return=representation" });
        data = rows.length > 0;
      } else {
        await rest(`notification_delivery?kind=eq.${input.kind}&local_date=eq.${day}`, "DELETE");
        data = true;
      }
    } else if (action === "food_list") {
      data = await rest("food_entries?select=id,eaten_at,source,description,analysis,uncertainty&order=eaten_at.desc&limit=1000");
    } else if (action === "food_confirm") {
      data = await rest("rpc/confirm_food_entry", "POST", {
        p_id: input.id, p_eaten_at: input.eaten_at, p_source: input.source,
        p_description: input.description, p_analysis: input.analysis, p_uncertainty: input.uncertainty,
      });
    } else if (action === "food_delete_last") {
      const rows = await rest("food_entries?select=id&order=eaten_at.desc&limit=1");
      if (rows.length) await rest(`food_entries?id=eq.${rows[0].id}`, "DELETE");
      data = rows.length > 0;
    } else if (action === "profile") {
      const rows = await rest("user_profile?select=sex,age_years,height_cm,target_weight_kg,activity_factor,restrictions,confirmed_at&limit=1");
      data = rows[0] ?? {};
    } else if (action === "profile_set") {
      const sex = input.sex;
      const age = Number(input.age_years);
      const height = Number(input.height_cm);
      const target = Number(input.target_weight_kg);
      const activity = Number(input.activity_factor);
      if (!["male", "female"].includes(sex) || !Number.isInteger(age) || age < 18 || age > 120 ||
          !Number.isInteger(height) || height < 100 || height > 250 || !Number.isFinite(target) || target < 35 || target > 300 ||
          !Number.isFinite(activity) || activity < 1 || activity > 2.5) throw new Error("invalid profile");
      const rows = await rest("user_profile?on_conflict=user_id", "POST", {
        user_id: "serhii", sex, age_years: age, height_cm: height,
        target_weight_kg: target, activity_factor: activity, confirmed_at: new Date().toISOString(),
      }, { Prefer: "resolution=merge-duplicates,return=representation" });
      data = rows[0] ?? {};
    } else {
      return json({ ok: false }, 400);
    }
    return json({ ok: true, data });
  } catch (error) {
    console.error("Monitor store error", error);
    return json({ ok: false }, 500);
  }
});
