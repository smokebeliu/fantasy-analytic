// TypeScript contracts mirroring the FastAPI schemas in docs/openapi.json
// (Fantasy Analytics API 0.2.0). Kept in sync manually; only the fields the UI
// consumes are modelled, plus the shared envelopes.

export type Role = "GOALKEEPER" | "DEFENDER" | "MIDFIELDER" | "FORWARD";

export type ForecastModel = "poisson_events" | "season_mean" | "recent_form";

export type PlayerOrder =
  | "projection"
  | "price"
  | "name"
  | "selected_by"
  | "season_score"
  | "prior_points";

export interface PageMeta {
  limit: number;
  offset: number;
  total: number;
  count: number;
}

export interface SnapshotMeta {
  run_id: number;
  season_id?: number | null;
  data_freshness?: string | null;
  quality_checked_at?: string | null;
}

export interface SeasonRulesModel {
  total_budget?: number | null;
  total_players?: number | null;
  starting_players?: number | null;
  full_roster_constraints?: unknown;
  starting_roster_constraints?: unknown;
}

export interface SeasonModel {
  season_id: number;
  fantasy_season_id: string;
  stat_season_id: string;
  name: string;
  // Unique within the competition: the season name, with the fantasy id
  // appended when a tournament splits a season into phases (Champions League
  // league phase vs knockout stage both being "2025/2026").
  label: string;
  competition_id?: number | null;
  competition_name?: string | null;
  competition_slug?: string | null;
  is_active: boolean;
  starts_at?: string | null;
  ends_at?: string | null;
  snapshot?: SnapshotMeta | null;
}

export interface SeasonDetailModel extends SeasonModel {
  rules?: SeasonRulesModel | null;
}

export interface SeasonListResponse {
  items: SeasonModel[];
  pagination: PageMeta;
}

// Competitions (leagues). The catalogue lists every league Sports.ru offers;
// `seasons` holds only what has actually been imported, so the UI can tell a
// league it can show from one it can merely offer for import.

export interface CatalogueSeasonModel {
  fantasy_season_id: string;
  stat_season_id?: string | null;
  name: string;
  label: string;
  is_active: boolean;
}

export interface CompetitionModel {
  competition_id: number;
  fantasy_tournament_id: string;
  slug: string;
  name: string;
  sort_order: number;
  catalogue_synced_at?: string | null;
  available_seasons: CatalogueSeasonModel[];
  has_active_season: boolean;
  seasons: SeasonModel[];
  latest_season?: SeasonModel | null;
  snapshot?: SnapshotMeta | null;
  is_imported: boolean;
}

export interface CompetitionListResponse {
  items: CompetitionModel[];
  pagination: PageMeta;
}

export interface CatalogueSyncResponse {
  synced_at: string;
  competitions: number;
  seasons: number;
  slugs: string[];
}

export interface TourModel {
  tour_id: number;
  season_id: number;
  fantasy_tour_id: string;
  name: string;
  status: string;
  starts_at?: string | null;
  finishes_at?: string | null;
  transfers_start_at?: string | null;
  transfers_deadline_at?: string | null;
  total_transfers?: number | null;
  max_same_team_players?: number | null;
}

export interface TourListResponse {
  items: TourModel[];
  pagination: PageMeta;
}

export interface ProjectionModel {
  model_name: string;
  model_version: string;
  feature_version?: string | null;
  scoring_version?: string | null;
  // Cross-season provenance (step 14): where the underlying history came from
  // and whether the projection is backed by any real history.
  stat_source?: string | null;
  has_history?: boolean | null;
  match_id?: number | null;
  expected_points?: number | null;
  uncertainty?: number | null;
  p_appearance?: number | null;
  expected_minutes?: number | null;
  components?: Record<string, number> | null;
}

// What the same player did in the previous season. Early in a new season the
// current-season columns are still empty, so this is what a manager judges a
// player by; it is absent when only one season has been imported.
export interface PriorSeasonModel {
  season_id: number;
  season_name?: string | null;
  player_season_id: number;
  role?: Role | null;
  club_name?: string | null;
  points?: number | null;
  average_points?: number | null;
  rank?: number | null;
  price?: number | null;
  matches?: number | null;
  minutes?: number | null;
  goals?: number | null;
  assists?: number | null;
  saves?: number | null;
  ball_recoveries?: number | null;
  yellow_cards?: number | null;
  red_cards?: number | null;
  goals_conceded?: number | null;
}

export interface PlayerModel {
  player_season_id: number;
  role: Role;
  fantasy_player_id?: string | null;
  player_name?: string | null;
  club_id?: number | null;
  club_name?: string | null;
  price?: number | null;
  availability_status?: string | null;
  status_description?: string | null;
  selected_by?: number | null;
  form?: number | null;
  season_score?: number | null;
  average_score?: number | null;
  last_tour_score?: number | null;
  rank?: number | null;
  projection?: ProjectionModel | null;
  prior_season?: PriorSeasonModel | null;
}

export interface PlayerListResponse {
  items: PlayerModel[];
  pagination: PageMeta;
  snapshot?: SnapshotMeta | null;
}

export interface MissingImportedPlayer {
  fantasy_player_id: string;
  player_name?: string | null;
  role?: Role | null;
}

export interface RemoteTourRef {
  fantasy_tour_id: string;
  name: string;
  status: string;
}

export interface ImportSquadResponse {
  squad_id: string;
  squad_name: string;
  competition_slug: string;
  competition_name?: string | null;
  remote_season_id: string;
  remote_tour?: RemoteTourRef | null;
  players: PlayerModel[];
  missing: MissingImportedPlayer[];
}

export interface PlayerHistoryEntry {
  match_id: number;
  tour_id?: number | null;
  scheduled_at?: string | null;
  minutes: number;
  points: number;
  goals: number;
  assists: number;
  saves: number;
  ball_recoveries: number;
  yellow_cards: number;
  red_cards: number;
  goals_conceded: number;
}

export interface PlayerDetailModel extends PlayerModel {
  season_id: number;
  history: PlayerHistoryEntry[];
  snapshot?: SnapshotMeta | null;
}

// The optimizer response keeps its nested explanation permissive on the wire.
// The UI reads a well-known subset, so those fields are typed explicitly.

export interface OptimizerCandidate {
  player_season_id: number;
  fantasy_player_id?: string | null;
  player_name?: string | null;
  role: Role;
  club_id: number;
  club_name?: string | null;
  price: number;
  expected_points: number;
  opponent_name?: string | null;
  is_home?: boolean | null;
  stat_source?: string | null;
  is_newcomer?: boolean | null;
  is_starter?: boolean;
  is_captain?: boolean;
  is_vice_captain?: boolean;
  // Step 15: the player was pinned by the user, not chosen by the solver.
  is_locked?: boolean;
  bench_order?: number | null;
}

export interface OptimizerConstraints {
  locked: number[];
  locked_starters: number[];
  formation?: string | null;
}

// One side of a swap. Arriving players always carry full details; a departing one
// may be `unavailable` — he has no candidate row for the tour because he left the
// league or his club has no fixture — in which case only his id is known.
export interface OptimizerTransferPlayer {
  player_season_id: number;
  fantasy_player_id?: string | null;
  player_name?: string | null;
  role?: Role | null;
  club_id?: number | null;
  club_name?: string | null;
  price?: number | null;
  expected_points?: number | null;
  unavailable?: boolean;
}

// One swap: the player leaving and the player arriving in his place, with what
// the change costs in money and gains in expected points.
export interface OptimizerTransferPair {
  out: OptimizerTransferPlayer;
  in: OptimizerTransferPlayer;
  delta_expected_points: number;
  delta_price: number;
}

export interface OptimizerTransfers {
  allowed: number;
  made: number;
  kept: number;
  in: OptimizerTransferPlayer[];
  out: OptimizerTransferPlayer[];
  pairs: OptimizerTransferPair[];
  missing_from_pool: number[];
}

// Step 16: two starters that meet each other in the tour, and how much of their
// expected points cancel out (one side's goals are the other side's clean sheet).
export interface OptimizerClash {
  match_id?: number | null;
  player_season_id: number;
  player_name?: string | null;
  role: Role;
  club_name?: string | null;
  opponent_player_season_id: number;
  opponent_player_name?: string | null;
  opponent_role: Role;
  opponent_club_name?: string | null;
  cancellation: number;
  penalty: number;
}

export interface OptimizerFixtures {
  conflict_weight: number;
  head_to_head: { match_id: number; clubs: { club_id: number; club_name?: string | null; starters: number }[] }[];
  clashes: OptimizerClash[];
  cancellation: number;
}

export interface OptimizerSolution {
  status: string;
  // False when the search budget ran out first: the squad is valid and the best
  // one found, but a better one may exist.
  proven_optimal?: boolean;
  objective_expected_points: number;
  // Expected points less the head-to-head penalty the objective paid (step 16).
  objective_score?: number;
  fixture_penalty?: number;
  fixtures?: OptimizerFixtures | null;
  starting_expected_points: number;
  formation: string;
  total_price: number;
  unused_budget: number;
  captain: OptimizerCandidate;
  vice_captain: OptimizerCandidate;
  squad: OptimizerCandidate[];
  starting: OptimizerCandidate[];
  bench: OptimizerCandidate[];
  transfers?: OptimizerTransfers | null;
  constraints?: OptimizerConstraints | null;
}

export interface OptimizerRules {
  total_budget: number;
  total_players: number;
  starting_players: number;
  full_limits: Record<string, [number, number]>;
  starting_limits: Record<string, [number, number]>;
  max_same_team: number;
  total_transfers?: number | null;
}

export interface OptimizerResponse {
  optimizer_version: string;
  model: string;
  mode: string;
  generated_at: string;
  run_id: number;
  season_id: number;
  season: Record<string, unknown>;
  tour: Record<string, unknown>;
  cutoff?: string | null;
  rules: OptimizerRules;
  counts: Record<string, unknown>;
  solution: OptimizerSolution;
  valid: boolean;
}

// Admin ingestion (steps 5 and 17). A refresh is enqueued, then polled until it
// reaches a terminal status; the status endpoint answers without a job id so a
// page reload recovers an in-flight refresh.

export type IngestionJobStatus = "pending" | "running" | "succeeded" | "failed";

export interface IngestionProgress {
  stage: string;
  percent: number;
  message?: string | null;
  updated_at?: string | null;
}

export interface IngestionJobResultSummary {
  snapshot_active?: boolean | null;
  data_freshness?: string | null;
  completed_at?: string | null;
  counts?: Record<string, number> | null;
  quality?: {
    passed?: boolean | null;
    counts?: { blocking?: number; warnings?: number } | null;
  } | null;
}

export interface IngestionJob {
  id: number;
  status: IngestionJobStatus;
  tournament_slug: string;
  trigger_type?: string | null;
  requested_season_id?: string | null;
  requested_season_name?: string | null;
  use_current_season?: boolean | null;
  ingestion_run_id?: number | null;
  error_message?: string | null;
  created_at?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  data_freshness?: string | null;
  progress?: IngestionProgress | null;
  result?: IngestionJobResultSummary | null;
}

export interface IngestionStatusResponse {
  tournament_slug: string;
  competition?: CompetitionModel | null;
  is_refreshing: boolean;
  active_job?: IngestionJob | null;
  latest_job?: IngestionJob | null;
  latest_successful_job?: IngestionJob | null;
  snapshot?: SnapshotMeta | null;
  season?: SeasonModel | null;
  target_tour?: TourModel | null;
  stages: { stage: string; percent: number }[];
}

export interface RefreshRequestBody {
  season_id?: string;
  season_name?: string;
  current?: boolean;
}

export interface ApiErrorBody {
  error: {
    type: string;
    message: string;
    details?: unknown;
  };
}
