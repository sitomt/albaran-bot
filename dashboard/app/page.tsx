import type { Metadata } from "next";
import DashboardClient, { type DashboardSnapshot } from "./dashboard-client";

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Mesa Clara — Control de albaranes",
  description: "Trazabilidad, revisiones y costes del bot de albaranes.",
};

const emptySnapshot: DashboardSnapshot = {
  generatedAt: new Date().toISOString(),
  connected: false,
  counts: {
    pending: 0,
    processing: 0,
    needsReview: 0,
    failed: 0,
    confirmedMonth: 0,
  },
  costs: {
    todayUsd: 0,
    monthUsd: 0,
    fixedMonthUsd: 0,
    budgetUsd: 0,
    events: 0,
    pages: 0,
    inputTokens: 0,
    outputTokens: 0,
    pendingLedgerEvents: 0,
    byOperation: [],
  },
  ingestions: [],
  audit: [],
  feedback: [],
  backup: null,
  warning: "El panel aún no tiene acceso de servidor a Supabase.",
};

function numberFromEnv(name: string): number {
  const parsed = Number(process.env[name] ?? 0);
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : 0;
}

function object(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function list(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value) ? value.map(object) : [];
}

async function loadSnapshot(): Promise<DashboardSnapshot> {
  const url = process.env.SUPABASE_URL?.replace(/\/$/, "");
  const serviceKey = process.env.SUPABASE_SERVICE_ROLE_KEY;
  const fixedMonthUsd =
    numberFromEnv("HOSTING_MONTHLY_COST_USD") +
    numberFromEnv("SUPABASE_MONTHLY_COST_USD") +
    numberFromEnv("OTHER_MONTHLY_COST_USD");
  const budgetUsd = numberFromEnv("MONTHLY_TOTAL_BUDGET_USD");

  if (!url || !serviceKey) {
    return {
      ...emptySnapshot,
      costs: { ...emptySnapshot.costs, fixedMonthUsd, budgetUsd },
    };
  }

  try {
    const response = await fetch(`${url}/rest/v1/rpc/dashboard_snapshot_v1`, {
      method: "POST",
      headers: {
        apikey: serviceKey,
        Authorization: `Bearer ${serviceKey}`,
        "Content-Type": "application/json",
      },
      body: "{}",
      cache: "no-store",
      signal: AbortSignal.timeout(8_000),
    });
    if (!response.ok) {
      throw new Error(`Supabase respondió ${response.status}`);
    }
    const raw = object(await response.json());
    const ingestionCounts = object(raw.ingestions);
    const jobCounts = object(raw.jobs);
    const reviews = object(raw.reviews);
    const reviewCounts = object(reviews.by_status);
    const costs = object(raw.ai_costs);
    const breakdown = list(costs.by_operation_model);
    const recentIngestions = list(raw.recent_ingestions);
    const audit = list(raw.recent_audit_events);
    const feedback = list(raw.feedback);
    const backup = object(raw.last_backup);
    const pages = breakdown.reduce((sum, item) => sum + Number(item.pages ?? 0), 0);
    const inputTokens = breakdown.reduce((sum, item) => sum + Number(item.input_tokens ?? 0), 0);
    const outputTokens = breakdown.reduce((sum, item) => sum + Number(item.output_tokens ?? 0), 0);
    return {
      connected: true,
      generatedAt: String(raw.as_of ?? new Date().toISOString()),
      counts: {
        pending: Number(jobCounts.pendiente ?? 0),
        processing: Number(jobCounts.procesando ?? 0),
        needsReview: Number(reviewCounts.open ?? 0),
        failed: Number(ingestionCounts.failed ?? 0),
        confirmedMonth: Number(ingestionCounts.confirmed ?? 0),
      },
      costs: {
        todayUsd: Number(costs.today_estimated_usd ?? 0),
        monthUsd: Number(costs.month_estimated_usd ?? 0),
        fixedMonthUsd,
        budgetUsd,
        events: Number(costs.calls_month ?? 0),
        pages,
        inputTokens,
        outputTokens,
        pendingLedgerEvents: 0,
        byOperation: breakdown.map((item) => ({
          operation: String(item.operation ?? "unknown"),
          model: String(item.model ?? "unknown"),
          calls: Number(item.calls ?? 0),
          costUsd: Number(item.month_estimated_usd ?? 0),
        })),
      },
      ingestions: recentIngestions.map((item) => {
        const id = String(item.id ?? "");
        return {
          id,
          reference: id ? id.slice(0, 8).toUpperCase() : "SIN-REF",
          status: String(item.status ?? "unknown"),
          sourceType: String(item.source_type ?? "photo"),
          receivedAt: String(item.received_at ?? new Date().toISOString()),
          issue: item.duplicate_reason ? String(item.duplicate_reason) : null,
          supplier: null,
          total: null,
        };
      }),
      audit: audit.map((item) => {
        const ingestionId = item.ingestion_id ? String(item.ingestion_id) : null;
        return {
          id: String(item.id ?? crypto.randomUUID()),
          eventType: String(item.event_type ?? "unknown"),
          actorType: String(item.actor_type ?? "system"),
          actorId: item.actor_id ? String(item.actor_id) : null,
          createdAt: String(item.created_at ?? new Date().toISOString()),
          reference: ingestionId ? ingestionId.slice(0, 8).toUpperCase() : null,
        };
      }),
      feedback: feedback.map((item) => ({
        id: String(item.id ?? crypto.randomUUID()),
        message: String(item.message ?? ""),
        actorId: item.actor_id ? String(item.actor_id) : null,
        createdAt: String(item.created_at ?? new Date().toISOString()),
      })),
      backup: backup.created_at
        ? { completedAt: String(backup.created_at), verified: backup.verified === true }
        : null,
      warning: null,
    };
  } catch (error) {
    return {
      ...emptySnapshot,
      costs: { ...emptySnapshot.costs, fixedMonthUsd, budgetUsd },
      warning:
        error instanceof Error
          ? `No se pudo actualizar: ${error.message}`
          : "No se pudo actualizar el panel.",
    };
  }
}

export default async function Home() {
  const snapshot = await loadSnapshot();
  return <DashboardClient initialSnapshot={snapshot} />;
}
