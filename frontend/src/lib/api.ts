import type {
  CatalogueSyncResponse,
  CompetitionListResponse,
  CompetitionModel,
  ForecastModel,
  IngestionJob,
  IngestionStatusResponse,
  ImportSquadResponse,
  OptimizerResponse,
  PlayerDetailModel,
  PlayerListResponse,
  PlayerOrder,
  RefreshRequestBody,
  Role,
  SeasonDetailModel,
  SeasonListResponse,
  TourListResponse,
  TourModel,
} from "./types";

// Server components talk to the backend directly; the browser goes through the
// same-origin /api/backend proxy defined in next.config.mjs (no CORS setup).
function baseUrl(): string {
  if (typeof window === "undefined") {
    return process.env.BACKEND_URL ?? "http://127.0.0.1:8000";
  }
  return "/api/backend";
}

export class ApiError extends Error {
  readonly status: number;
  readonly type: string;
  readonly details: unknown;

  constructor(status: number, type: string, message: string, details?: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.type = type;
    this.details = details;
  }
}

type QueryValue = string | number | boolean | null | undefined;

function buildQuery(params: Record<string, QueryValue>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === null || value === undefined || value === "") continue;
    search.set(key, String(value));
  }
  const qs = search.toString();
  return qs ? `?${qs}` : "";
}

async function request<T>(
  path: string,
  init?: RequestInit & { params?: Record<string, QueryValue> },
): Promise<T> {
  const { params, ...rest } = init ?? {};
  const url = `${baseUrl()}${path}${params ? buildQuery(params) : ""}`;

  let response: Response;
  try {
    response = await fetch(url, {
      ...rest,
      headers: {
        Accept: "application/json",
        ...(rest.body ? { "Content-Type": "application/json" } : {}),
        ...rest.headers,
      },
      cache: "no-store",
    });
  } catch (cause) {
    throw new ApiError(
      0,
      "network_error",
      "Не удалось связаться с сервером аналитики. Проверьте, что API запущен.",
      cause,
    );
  }

  const text = await response.text();
  const payload = text ? safeJson(text) : null;

  if (!response.ok) {
    const envelope = payload as { error?: { type?: string; message?: string; details?: unknown } } | null;
    const error = envelope?.error;
    throw new ApiError(
      response.status,
      error?.type ?? "http_error",
      error?.message ?? `Запрос завершился ошибкой (${response.status}).`,
      error?.details,
    );
  }

  return payload as T;
}

function safeJson(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

export interface PlayerQuery {
  season_id: number;
  tour_id?: number | null;
  model?: ForecastModel;
  role?: Role | null;
  club_id?: number | null;
  status?: string | null;
  min_price?: number | null;
  max_price?: number | null;
  order?: PlayerOrder;
  limit?: number;
  offset?: number;
}

export const api = {
  // Leagues. `imported_only` narrows the catalogue to what the read API can
  // serve, which is what the league switcher offers; the admin screen wants the
  // full catalogue so a league can be imported for the first time.
  listCompetitions: (
    params: { imported_only?: boolean; limit?: number; offset?: number } = {},
  ) => request<CompetitionListResponse>("/competitions", { params }),

  getCompetition: (slug: string) =>
    request<CompetitionModel>(`/competitions/${encodeURIComponent(slug)}`),

  listSeasons: (
    params: { competition_id?: number; limit?: number; offset?: number } = {},
  ) => request<SeasonListResponse>("/seasons", { params }),

  getSeason: (seasonId: number) =>
    request<SeasonDetailModel>(`/seasons/${seasonId}`),

  listTours: (params: {
    season_id?: number;
    status?: string;
    limit?: number;
    offset?: number;
  } = {}) => request<TourListResponse>("/tours", { params }),

  getTour: (tourId: number) => request<TourModel>(`/tours/${tourId}`),

  listPlayers: (query: PlayerQuery) =>
    request<PlayerListResponse>("/players", { params: { ...query } }),

  getPlayer: (playerSeasonId: number, params: { tour_id?: number | null; model?: ForecastModel } = {}) =>
    request<PlayerDetailModel>(`/players/${playerSeasonId}`, { params }),

  optimizeSquad: (body: {
    tour?: string;
    // Naming the season (or the league) is what keeps a tour id unambiguous
    // once several leagues are imported.
    season?: string;
    competition?: string;
    model?: ForecastModel;
    run_id?: number;
    locked?: string[];
    locked_starters?: string[];
    formation?: string;
  }) =>
    request<OptimizerResponse>("/optimizer/squad", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  importSquad: (body: {
    url: string;
    season_id: number;
    tour_id?: number | null;
    competition?: string;
    model?: ForecastModel;
  }) =>
    request<ImportSquadResponse>("/squads/import", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  optimizeTransfers: (body: {
    current_squad: string[];
    tour?: string;
    season?: string;
    competition?: string;
    model?: ForecastModel;
    max_transfers?: number | null;
    run_id?: number;
    locked?: string[];
    locked_starters?: string[];
    formation?: string;
  }) =>
    request<OptimizerResponse>("/optimizer/transfers", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  // Admin ingestion (steps 17 and 22). Both calls are scoped to one league by
  // its tournament slug, so refreshing La Liga never touches the RPL snapshot.
  // The refresh call only enqueues a job: a 202 carries the new job, a 409
  // (thrown as an ApiError with type "conflict") means a refresh of that same
  // league is already running.
  getIngestionStatus: (slug: string) =>
    request<IngestionStatusResponse>(
      `/admin/ingestion/${encodeURIComponent(slug)}/status`,
    ),

  getIngestionJob: (jobId: number) =>
    request<IngestionJob>(`/admin/ingestion/runs/${jobId}`),

  refreshIngestion: (slug: string, body: RefreshRequestBody = {}) =>
    request<IngestionJob>(
      `/admin/ingestion/${encodeURIComponent(slug)}/refresh`,
      {
        method: "POST",
        body: JSON.stringify(body),
      },
    ),

  syncCompetitions: () =>
    request<CatalogueSyncResponse>("/admin/competitions/sync", {
      method: "POST",
    }),
};
