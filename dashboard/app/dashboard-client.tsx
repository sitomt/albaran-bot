"use client";

import { useEffect, useMemo, useState, useTransition } from "react";
import { useRouter } from "next/navigation";

type Counts = {
  pending: number;
  processing: number;
  needsReview: number;
  failed: number;
  confirmedMonth: number;
};

type CostOperation = {
  operation: string;
  model: string;
  calls: number;
  costUsd: number;
};

type Ingestion = {
  id: string;
  reference: string;
  status: string;
  sourceType: string;
  receivedAt: string;
  supplier?: string | null;
  total?: number | null;
  issue?: string | null;
};

type AuditEvent = {
  id: string;
  eventType: string;
  actorType: string;
  actorId?: string | null;
  createdAt: string;
  reference?: string | null;
};

type Feedback = {
  id: string;
  message: string;
  actorId?: string | null;
  createdAt: string;
};

export type DashboardSnapshot = {
  generatedAt: string;
  connected: boolean;
  counts: Counts;
  costs: {
    todayUsd: number;
    monthUsd: number;
    fixedMonthUsd: number;
    budgetUsd: number;
    events: number;
    pages: number;
    inputTokens: number;
    outputTokens: number;
    pendingLedgerEvents: number;
    byOperation: CostOperation[];
  };
  ingestions: Ingestion[];
  audit: AuditEvent[];
  feedback: Feedback[];
  backup: { completedAt: string; verified: boolean } | null;
  warning?: string | null;
};

type View = "inicio" | "documentos" | "costes" | "auditoria";

const statusCopy: Record<string, { label: string; tone: string }> = {
  received: { label: "Recibido", tone: "neutral" },
  queued: { label: "En cola", tone: "info" },
  processing: { label: "Procesando", tone: "info" },
  extracted: { label: "Extraído", tone: "warning" },
  needs_review: { label: "Revisar", tone: "warning" },
  confirmed: { label: "Confirmado", tone: "success" },
  rejected: { label: "Rechazado", tone: "neutral" },
  failed: { label: "Error", tone: "danger" },
};

const operationCopy: Record<string, string> = {
  ocr: "Lectura OCR",
  document_classification: "Clasificación",
  extraction: "Estructuración",
  query_classification: "Consultas",
  query_response: "Respuestas",
  pricing_adjustment: "Ajuste de tarifas",
};

function money(value: number): string {
  return new Intl.NumberFormat("es-ES", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: value < 1 ? 4 : 2,
    maximumFractionDigits: value < 1 ? 4 : 2,
  }).format(value);
}

function compact(value: number): string {
  return new Intl.NumberFormat("es-ES", { notation: "compact" }).format(value);
}

function dateTime(value: string): string {
  return new Intl.DateTimeFormat("es-ES", {
    timeZone: "Europe/Madrid",
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

function shortEvent(eventType: string): string {
  return eventType
    .replace(/^ingestion\./, "")
    .replace(/^schema\./, "migración · ")
    .replaceAll("_", " ")
    .replace(/^./, (letter) => letter.toUpperCase());
}

export default function DashboardClient({
  initialSnapshot,
}: {
  initialSnapshot: DashboardSnapshot;
}) {
  const [view, setView] = useState<View>("inicio");
  const [statusFilter, setStatusFilter] = useState("all");
  const [isRefreshing, startRefresh] = useTransition();
  const router = useRouter();
  const snapshot = initialSnapshot;
  const totalMonth = snapshot.costs.monthUsd + snapshot.costs.fixedMonthUsd;
  const budgetPercent = snapshot.costs.budgetUsd
    ? Math.min(100, (totalMonth / snapshot.costs.budgetUsd) * 100)
    : 0;
  const visibleIngestions = useMemo(
    () =>
      statusFilter === "all"
        ? snapshot.ingestions
        : snapshot.ingestions.filter((item) => item.status === statusFilter),
    [snapshot.ingestions, statusFilter],
  );

  const navigation: { id: View; label: string }[] = [
    { id: "inicio", label: "Resumen" },
    { id: "documentos", label: "Documentos" },
    { id: "costes", label: "Costes" },
    { id: "auditoria", label: "Trazabilidad" },
  ];

  const refresh = () => startRefresh(() => router.refresh());

  useEffect(() => {
    const interval = window.setInterval(() => {
      if (document.visibilityState === "visible") refresh();
    }, 30_000);
    return () => window.clearInterval(interval);
  // `router` es estable; evitamos reiniciar el intervalo en cada transición.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [router]);

  return (
    <main className="shell">
      <header className="topbar">
        <div className="brand">
          <span className="brandMark" aria-hidden="true">MC</span>
          <div>
            <p>Mesa Clara</p>
            <span>Control de albaranes</span>
          </div>
        </div>
        <div className="topbarRight">
          <span className={`connection ${snapshot.connected ? "isOnline" : "isOffline"}`}>
            <i aria-hidden="true" />
            {snapshot.connected ? "Datos en directo" : "Sin conexión"}
          </span>
          <span className="updated">Actualizado {dateTime(snapshot.generatedAt)}</span>
          <button className="refresh" type="button" onClick={refresh} disabled={isRefreshing}>
            {isRefreshing ? "Actualizando…" : "Actualizar"}
          </button>
        </div>
      </header>

      <nav className="tabs" aria-label="Secciones del panel">
        {navigation.map((item) => (
          <button
            key={item.id}
            className={view === item.id ? "active" : ""}
            onClick={() => setView(item.id)}
            type="button"
          >
            {item.label}
            {item.id === "documentos" && snapshot.counts.needsReview > 0 ? (
              <span className="navBadge">{snapshot.counts.needsReview}</span>
            ) : null}
          </button>
        ))}
      </nav>

      {snapshot.warning ? (
        <section className="notice" role="status">
          <strong>Atención</strong>
          <span>{snapshot.warning}</span>
        </section>
      ) : null}

      {view === "inicio" ? (
        <>
          <section className="heroRow">
            <div>
              <p className="eyebrow">Situación de hoy</p>
              <h1>
                {snapshot.counts.needsReview > 0
                  ? `${snapshot.counts.needsReview} documento${snapshot.counts.needsReview === 1 ? "" : "s"} espera revisión`
                  : "Todo está bajo control"}
              </h1>
              <p className="subhead">
                Solo los documentos confirmados por una persona entran en la contabilidad.
              </p>
            </div>
            <div className="heroActions">
              <button type="button" className="primary" onClick={() => setView("documentos")}>
                Abrir revisiones
              </button>
              <button type="button" className="secondary" onClick={() => setView("costes")}>
                Ver gasto
              </button>
            </div>
          </section>

          <section className="metricGrid" aria-label="Indicadores principales">
            <article className="metric attention">
              <span>Por revisar</span>
              <strong>{snapshot.counts.needsReview}</strong>
              <small>Requieren decisión humana</small>
            </article>
            <article className="metric">
              <span>En cola</span>
              <strong>{snapshot.counts.pending + snapshot.counts.processing}</strong>
              <small>{snapshot.counts.processing} procesando ahora</small>
            </article>
            <article className={`metric ${snapshot.counts.failed ? "problem" : ""}`}>
              <span>Con error</span>
              <strong>{snapshot.counts.failed}</strong>
              <small>{snapshot.counts.failed ? "Necesitan intervención" : "Sin incidencias abiertas"}</small>
            </article>
            <article className="metric">
              <span>Confirmados totales</span>
              <strong>{snapshot.counts.confirmedMonth}</strong>
              <small>Datos contables publicados</small>
            </article>
          </section>

          <section className="twoColumns">
            <article className="panel">
              <div className="panelHead">
                <div>
                  <p className="eyebrow">Actividad reciente</p>
                  <h2>Últimos documentos</h2>
                </div>
                <button type="button" className="textButton" onClick={() => setView("documentos")}>Ver todos</button>
              </div>
              <DocumentTable items={snapshot.ingestions.slice(0, 6)} />
            </article>

            <article className="panel costSummary">
              <div className="panelHead">
                <div>
                  <p className="eyebrow">Control de gasto</p>
                  <h2>Coste del mes</h2>
                </div>
                <span className="estimateTag">Estimación</span>
              </div>
              <strong className="bigMoney">{money(totalMonth)}</strong>
              <div className="costSplit">
                <span><i className="dot ai" />IA medida <b>{money(snapshot.costs.monthUsd)}</b></span>
                <span><i className="dot fixed" />Fijo configurado <b>{money(snapshot.costs.fixedMonthUsd)}</b></span>
              </div>
              <div className="budget">
                <div>
                  <span>Presupuesto mensual</span>
                  <b>{snapshot.costs.budgetUsd ? money(snapshot.costs.budgetUsd) : "Sin configurar"}</b>
                </div>
                <div className="budgetTrack" aria-label={`${budgetPercent.toFixed(0)}% del presupuesto`}>
                  <span style={{ width: `${budgetPercent}%` }} />
                </div>
              </div>
              <p className="finePrint">
                Consumo medido casi en tiempo real. La factura final puede incluir IVA, créditos y excesos del proveedor.
              </p>
            </article>
          </section>
        </>
      ) : null}

      {view === "documentos" ? (
        <section className="panel pagePanel">
          <div className="pageTitle">
            <div>
              <p className="eyebrow">Cola y revisión</p>
              <h1>Documentos</h1>
              <p>Consulta el estado exacto de cada foto sin alterar su evidencia original.</p>
            </div>
            <label className="filter">
              Estado
              <select value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)}>
                <option value="all">Todos</option>
                <option value="needs_review">Por revisar</option>
                <option value="queued">En cola</option>
                <option value="processing">Procesando</option>
                <option value="confirmed">Confirmados</option>
                <option value="failed">Con error</option>
                <option value="rejected">Rechazados</option>
              </select>
            </label>
          </div>
          <DocumentTable items={visibleIngestions} expanded />
          <p className="actionHint">
            Las correcciones y confirmaciones se realizan con las operaciones auditadas del bot; nunca editando una tabla directamente.
          </p>
        </section>
      ) : null}

      {view === "costes" ? (
        <section className="costPage">
          <div className="pageTitle standalone">
            <div>
              <p className="eyebrow">Transparencia económica</p>
              <h1>Costes</h1>
              <p>Qué se ha consumido, en qué operación y con qué modelo.</p>
            </div>
            <span className="estimateTag">USD · estimación operativa</span>
          </div>
          <div className="metricGrid costMetrics">
            <article className="metric"><span>Hoy</span><strong>{money(snapshot.costs.todayUsd)}</strong><small>Consumo IA registrado</small></article>
            <article className="metric"><span>Este mes</span><strong>{money(snapshot.costs.monthUsd)}</strong><small>{snapshot.costs.events} llamadas facturables</small></article>
            <article className="metric"><span>Páginas OCR</span><strong>{compact(snapshot.costs.pages)}</strong><small>Lecturas procesadas</small></article>
            <article className="metric"><span>Tokens</span><strong>{compact(snapshot.costs.inputTokens + snapshot.costs.outputTokens)}</strong><small>{compact(snapshot.costs.inputTokens)} entrada · {compact(snapshot.costs.outputTokens)} salida</small></article>
          </div>
          <article className="panel operationPanel">
            <div className="panelHead">
              <div><p className="eyebrow">Desglose</p><h2>Por operación y modelo</h2></div>
            </div>
            {snapshot.costs.byOperation.length ? (
              <div className="operationList">
                {snapshot.costs.byOperation.map((item, index) => {
                  const maxCost = Math.max(...snapshot.costs.byOperation.map((op) => op.costUsd), 0.000001);
                  return (
                    <div className="operation" key={`${item.operation}-${item.model}-${index}`}>
                      <div className="operationLabel">
                        <strong>{operationCopy[item.operation] ?? item.operation}</strong>
                        <span>{item.model} · {item.calls} llamadas</span>
                      </div>
                      <div className="operationBar"><span style={{ width: `${(item.costUsd / maxCost) * 100}%` }} /></div>
                      <b>{money(item.costUsd)}</b>
                    </div>
                  );
                })}
              </div>
            ) : <Empty text="Todavía no hay consumo registrado este mes." />}
          </article>
        </section>
      ) : null}

      {view === "auditoria" ? (
        <section className="auditPage">
          <div className="pageTitle standalone">
            <div>
              <p className="eyebrow">Registro inmutable</p>
              <h1>Trazabilidad</h1>
              <p>Acciones humanas y automáticas, ordenadas de más reciente a más antigua.</p>
            </div>
            <div className={`backupState ${snapshot.backup?.verified ? "verified" : "unknown"}`}>
              <span>Último backup</span>
              <b>{snapshot.backup ? dateTime(snapshot.backup.completedAt) : "Sin señal"}</b>
              <small>{snapshot.backup?.verified ? "Verificado" : "Pendiente de monitor externo"}</small>
            </div>
          </div>
          <div className="twoColumns auditColumns">
            <article className="panel">
              <div className="panelHead"><div><p className="eyebrow">Historial</p><h2>Últimos eventos</h2></div></div>
              {snapshot.audit.length ? (
                <ol className="timeline">
                  {snapshot.audit.map((event) => (
                    <li key={event.id}>
                      <i aria-hidden="true" />
                      <div>
                        <strong>{shortEvent(event.eventType)}</strong>
                        <span>{event.reference ? `${event.reference} · ` : ""}{event.actorType}{event.actorId ? ` ${event.actorId}` : ""}</span>
                      </div>
                      <time>{dateTime(event.createdAt)}</time>
                    </li>
                  ))}
                </ol>
              ) : <Empty text="No hay eventos disponibles." />}
            </article>
            <article className="panel">
              <div className="panelHead"><div><p className="eyebrow">Usuarios</p><h2>Feedback recibido</h2></div></div>
              {snapshot.feedback.length ? (
                <div className="feedbackList">
                  {snapshot.feedback.map((item) => (
                    <blockquote key={item.id}>
                      <p>“{item.message}”</p>
                      <footer>Usuario {item.actorId ?? "desconocido"} · {dateTime(item.createdAt)}</footer>
                    </blockquote>
                  ))}
                </div>
              ) : <Empty text="Todavía no se ha recibido feedback." />}
            </article>
          </div>
        </section>
      ) : null}

      <footer className="footer">
        <span>Mesa Clara · Datos originales preservados</span>
        <span>Panel de solo lectura · acciones contables auditadas</span>
      </footer>
    </main>
  );
}

function DocumentTable({ items, expanded = false }: { items: Ingestion[]; expanded?: boolean }) {
  if (!items.length) return <Empty text="No hay documentos en esta vista." />;
  return (
    <div className="tableWrap">
      <table>
        <thead>
          <tr>
            <th>Referencia</th>
            <th>Estado</th>
            <th>Proveedor</th>
            {expanded ? <th>Origen</th> : null}
            <th>Total</th>
            <th>Recibido</th>
          </tr>
        </thead>
        <tbody>
          {items.map((item) => {
            const status = statusCopy[item.status] ?? { label: item.status, tone: "neutral" };
            return (
              <tr key={item.id}>
                <td><b>{item.reference}</b>{item.issue ? <small>{item.issue}</small> : null}</td>
                <td><span className={`status ${status.tone}`}><i />{status.label}</span></td>
                <td>{item.supplier ?? "—"}</td>
                {expanded ? <td>{item.sourceType === "manual" ? "Manual" : "Foto"}</td> : null}
                <td>{item.total == null ? "—" : new Intl.NumberFormat("es-ES", { style: "currency", currency: "EUR" }).format(item.total)}</td>
                <td><time>{dateTime(item.receivedAt)}</time></td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function Empty({ text }: { text: string }) {
  return <div className="empty"><span aria-hidden="true">·</span><p>{text}</p></div>;
}
