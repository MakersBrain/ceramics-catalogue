/**
 * GENERATED FILE — do not edit.
 *
 * Produced from catalogue-ops.openapi.json by `catalogue-ops-types`, which is
 * generated in turn from the Pydantic models catalogue-control serialises with.
 * Edit those; run `make openapi` and `make types`; commit the result.
 *
 * The drift this removes is the silent kind: a renamed field compiles fine
 * against a hand-written interface and renders blank.
 */


/** A control was applied. 202 rather than 200: the worker acts on it later. */
export interface Accepted {
	job_id?: string | null;
	worker_id?: string | null;
	action?: string | null;
	desired_state?: string | null;
	cancelled?: number | null;
}

export interface AcknowledgeRequest {
	by?: string | null;
}

export interface Acknowledgement {
	id: number;
	acknowledged: boolean;
}

/** The stream's first frame, so a client renders without a second request. */
export interface Bootstrap {
	workers: Worker[];
	active_runs: Record<string, unknown>[];
	notifications: Notification[];
	queue: Record<string, number>;
	watermark: number;
	jobs?: Job[] | null;
}

/** Everything that decides how a run collects. Validated once, used twice. */
export interface CrawlParams {
	limit?: number | null;
	sources?: number | null;
	concurrency?: number | null;
	delay?: number | null;
	browser?: 'never' | 'auto' | 'always' | null;
	impersonate?: 'never' | 'auto' | null;
	robots?: 'obey' | 'ignore' | null;
	cache_mode?: 'off' | 'auto' | 'replay' | 'refresh' | null;
	cache_max_age_hours?: number | null;
	source_timeout_seconds?: number | null;
	log_level?: 'DEBUG' | 'INFO' | 'WARNING' | 'ERROR' | null;
	dry_run?: boolean | null;
	allow_empty?: boolean | null;
}

export interface CreateRunRequest {
	/** A source id, a comma-separated list, or 'all'. */
	sources?: string | null;
	kind?: 'manual' | 'scheduled' | 'retry' | 'backfill' | null;
	requested_by?: string | null;
	/** Validated against the same model the CLI and the scheduler use, so a run created here cannot mean something a run created there would not. */
	params?: CrawlParams | null;
}

export interface CreateRunResponse {
	run_id: string;
	jobs: number;
	sources: string[];
}

export interface Health {
	status: 'ok' | 'unavailable';
}

export interface InFlight {
	seconds: number;
	url: string;
}

export interface Job {
	id: string;
	run_id: string;
	source_id: string;
	host: string;
	state: 'queued' | 'leased' | 'running' | 'paused' | 'succeeded' | 'failed' | 'cancelled' | 'skipped';
	attempt: number;
	max_attempts: number;
	priority: number;
	requires?: string[] | null;
	scheduled_for: string;
	started_at?: string | null;
	finished_at?: string | null;
	error?: string | null;
	/** 32 lower-case hex characters when tracing is active. */
	trace_id?: string | null;
	artifact_path?: string | null;
	artifact_sha256?: string | null;
	artifact_size?: number | null;
	cancel_requested?: boolean | null;
	pause_requested?: boolean | null;
	summary?: JobSummary | null;
	phase?: string | null;
	records?: number | null;
	requests?: number | null;
	rendered_pages?: number | null;
	error_count?: number | null;
	discovered?: number | null;
	truncated?: boolean | null;
	in_flight?: InFlight[] | null;
	/** The previous successful run's record count, so a progress bar has a scale. */
	previous_records?: number | null;
}

export interface JobDetail {
	job: Job;
}

/** A level. Unnumbered and never replayed; the counters are cumulative. */
export interface JobProgress {
	job_id: string;
	run_id: string;
	source: string;
	phase?: string | null;
	records?: number | null;
	requests?: number | null;
	rendered_pages?: number | null;
	errors?: number | null;
	discovered?: number | null;
	truncated?: boolean | null;
	in_flight?: InFlight[] | null;
	at: string;
}

export interface JobStateChanged {
	id: number;
	at: string;
	type: string;
	run_id: string;
	job_id: string;
	source: string;
	state?: string | null;
	attempt?: number | null;
	records?: number | null;
	error?: string | null;
}

/** The per-source summary `run_source` produces, stored verbatim on the job. */
export interface JobSummary {
	source: string;
	label?: string | null;
	scraper: string;
	extraction_method?: string | null;
	records?: number | null;
	discovered?: number | null;
	requests?: number | null;
	rendered_pages?: number | null;
	truncated?: boolean | null;
	robots_ignored?: boolean | null;
	error_count?: number | null;
	errors?: Record<string, string>[] | null;
	notes?: string[] | null;
	/** Rows carrying each field, so a thin scraper is visible. */
	field_coverage?: Record<string, number> | null;
	write_status?: string | null;
	interrupted?: boolean | null;
	loaded?: number | null;
	retired?: number | null;
}

export interface LogLine {
	id: number;
	at: string;
	level: 'debug' | 'info' | 'warning' | 'error';
	event?: string | null;
	message: string;
	data?: Record<string, unknown> | null;
}

export interface LogPage {
	lines: LogLine[];
	next_after?: number | null;
}

export interface Notification {
	id: number;
	at: string;
	severity: 'info' | 'warning' | 'critical';
	kind: string;
	title: string;
	body?: string | null;
	run_id?: string | null;
	job_id?: string | null;
	source_id?: string | null;
	worker_id?: string | null;
	dedup_key: string;
	resolved_at?: string | null;
	acknowledged_at?: string | null;
	acknowledged_by?: string | null;
}

export interface NotificationEvent {
	id: number;
	at: string;
	type: string;
	severity?: string | null;
	kind?: string | null;
	title?: string | null;
	source?: string | null;
}

export interface NotificationList {
	notifications: Notification[];
}

/** RFC 9457 `application/problem+json`. */
export interface Problem {
	type?: string | null;
	title: string;
	status: number;
	detail?: string | null;
}

/** The gap was too large to replay. Refetch over the JSON endpoints. */
export interface Resync {
	reason: string;
}

export interface Run {
	id: string;
	kind: string;
	status: 'queued' | 'running' | 'complete' | 'degraded' | 'failed' | 'cancelled';
	schedule_id?: string | null;
	requested_by?: string | null;
	created_at: string;
	started_at?: string | null;
	finished_at?: string | null;
	params?: Record<string, unknown> | null;
	summary?: RunSummary | null;
	jobs?: number | null;
	succeeded?: number | null;
	failed?: number | null;
	active?: number | null;
}

export interface RunDetail {
	run: Run;
	jobs: Job[];
}

export interface RunEvent {
	id: number;
	at: string;
	type: string;
	run_id: string;
	succeeded?: number | null;
	failed?: number | null;
	records?: number | null;
}

export interface RunList {
	runs: Run[];
	next_cursor?: string | null;
}

export interface RunSummary {
	succeeded?: number | null;
	failed?: number | null;
	cancelled?: number | null;
	skipped?: number | null;
	records?: number | null;
	requests?: number | null;
}

export interface Schedule {
	id: string;
	enabled?: boolean | null;
	/** Five-field cron, evaluated in `timezone`. */
	cron: string;
	timezone?: string | null;
	source_filter?: Record<string, unknown> | null;
	params?: Record<string, unknown> | null;
	last_fired_at?: string | null;
	next_fire_at?: string | null;
}

export interface ScheduleList {
	schedules: Schedule[];
}

export interface ScheduleUpdated {
	schedule: Schedule;
}

export interface Source {
	source_id: string;
	label: string;
	url: string;
	scraper: string;
	country?: string | null;
	enabled?: boolean | null;
	paused?: boolean | null;
	schedule_id?: string | null;
	params?: Record<string, unknown> | null;
	last_success_at?: string | null;
	last_records?: number | null;
	previous_records?: number | null;
	delta?: number | null;
	staleness_seconds?: number | null;
	runs_7d?: number | null;
	failures_7d?: number | null;
}

export interface SourceList {
	sources: Source[];
}

export interface SourceSettings {
	enabled?: boolean | null;
	/** Pausing also pauses this source's jobs that are in flight. Resuming does not automatically resume individually paused jobs: a broad administrative toggle must not silently restart work somebody stopped on purpose. */
	paused?: boolean | null;
	schedule_id?: string | null;
	params?: Record<string, unknown> | null;
	updated_by?: string | null;
}

export interface SourceUpdated {
	source: Record<string, unknown>;
}

export interface Worker {
	worker_id: string;
	hostname: string;
	pid: number;
	version?: string | null;
	capabilities?: string[] | null;
	status: 'starting' | 'idle' | 'busy' | 'paused' | 'draining' | 'stopped';
	desired_state: 'running' | 'paused' | 'draining' | 'stopping';
	started_at: string;
	/** Derive the age from this locally. A worker that has silently died emits no event — nothing fires when a process stops existing — so staleness has to be computed from a clock rather than waited for. */
	last_heartbeat_at: string;
	current_job_id?: string | null;
	current_source?: string | null;
	heartbeat_age_seconds?: number | null;
}

/** An edge: `idle -> busy`, a drain, a stop. */
export interface WorkerChanged {
	id: number;
	at: string;
	type: string;
	worker_id: string;
	status?: string | null;
	current_job_id?: string | null;
	desired_state?: string | null;
}

export interface WorkerList {
	workers: Worker[];
}

/** A level, pushed every five seconds. */
export interface WorkerRoster {
	workers: Worker[];
}

// Names the explorer already imports, mapped onto the generated ones.
export type RunRow = Run;
export type WorkerRow = Worker;
export type SourceRow = Source;
export type NotificationRow = Notification;
