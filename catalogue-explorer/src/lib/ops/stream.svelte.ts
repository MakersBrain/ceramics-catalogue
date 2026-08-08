/**
 * One stream per tab, in a shared store.
 *
 * The layout subscribes once and every page reads from here. Only
 * `/ops/runs/[id]` opens a second, narrower subscription, which it closes on
 * navigate. Without that discipline each page owns a connection, and HTTP/1.1
 * caps a browser at roughly six per origin — two tabs open and the app
 * deadlocks against its own streams.
 *
 * Three details are deliberate:
 *
 * - **Heartbeat age ticks client-side.** The roster arrives with
 *   `last_heartbeat_at` and a local interval renders the age, so a worker that
 *   died goes amber and then red on its own. No event fires when a process
 *   stops existing, which is exactly the case where waiting for a message is
 *   wrong.
 * - **`resync` is handled, not ignored.** A client that fell behind and keeps
 *   rendering stale data looks like it is working, which is worse than showing
 *   nothing.
 * - **The connection state is visible.** An operations page that has quietly
 *   stopped updating is worse than one that admits it.
 */

import type { JobProgress, Notification, Worker } from './types';

export type { JobProgress, Notification, Worker };

export type ConnectionState = 'connecting' | 'live' | 'reconnecting' | 'offline';

/** Milliseconds between polls while the stream is down. */
const POLL_INTERVAL = 5000;

/** How long without a heartbeat before a worker is shown as suspect / lost. */
export const HEARTBEAT_WARN_SECONDS = 15;
export const HEARTBEAT_LOST_SECONDS = 30;

export class OpsStream {
	connection = $state<ConnectionState>('connecting');
	workers = $state<Worker[]>([]);
	notifications = $state<Notification[]>([]);
	queue = $state<Record<string, number>>({});
	activeRuns = $state<{ id: string; kind: string; status: string; started_at: string }[]>([]);
	progress = $state<Record<string, JobProgress>>({});
	jobEvents = $state<{ id: number; type: string; job_id: string | null; source: string | null }[]>(
		[]
	);

	/** Ticks once a second so heartbeat ages re-render without any event. */
	now = $state(Date.now());

	private source: EventSource | null = null;
	private clock: ReturnType<typeof setInterval> | null = null;
	private poller: ReturnType<typeof setInterval> | null = null;
	private onResync: (() => void) | null = null;

	constructor(
		private readonly topics: string,
		private readonly runId?: string
	) {}

	connect(onResync?: () => void): void {
		this.onResync = onResync ?? null;
		const query = new URLSearchParams({ topics: this.topics });
		if (this.runId) query.set('run_id', this.runId);

		this.source = new EventSource(`/ops/events?${query}`);
		this.connection = 'connecting';

		this.source.addEventListener('open', () => {
			this.connection = 'live';
			this.stopPolling();
		});

		this.source.addEventListener('error', () => {
			// EventSource reconnects on its own with Last-Event-ID; the fallback
			// poll covers the window where it cannot.
			this.connection = this.source?.readyState === EventSource.CLOSED ? 'offline' : 'reconnecting';
			this.startPolling();
		});

		this.source.addEventListener('bootstrap', (event) => {
			const data = JSON.parse((event as MessageEvent).data);
			this.workers = data.workers ?? [];
			this.notifications = data.notifications ?? [];
			this.queue = data.queue ?? {};
			this.activeRuns = data.active_runs ?? [];
			for (const job of data.jobs ?? []) {
				if (job.job_id) this.progress[job.job_id] = job;
			}
			this.connection = 'live';
		});

		// A level: the roster arrives whole, every few seconds, and replaces
		// what was there. Nothing is merged, because a worker that vanished from
		// the roster has gone.
		this.source.addEventListener('worker.roster', (event) => {
			this.workers = JSON.parse((event as MessageEvent).data).workers ?? [];
		});

		this.source.addEventListener('worker.changed', (event) => {
			const data = JSON.parse((event as MessageEvent).data);
			this.workers = this.workers.map((worker) =>
				worker.worker_id === data.worker_id ? { ...worker, ...data } : worker
			);
		});

		this.source.addEventListener('job.progress', (event) => {
			const data = JSON.parse((event as MessageEvent).data) as JobProgress;
			this.progress[data.job_id] = data;
		});

		this.source.addEventListener('notification.raised', (event) => {
			const data = JSON.parse((event as MessageEvent).data);
			this.notifications = [data, ...this.notifications];
		});

		this.source.addEventListener('notification.resolved', (event) => {
			const data = JSON.parse((event as MessageEvent).data);
			this.notifications = this.notifications.filter((entry) => entry.id !== data.id);
		});

		// Job state changes are edges. The run page re-loads on these rather than
		// trying to patch a row from a partial payload — a table that is subtly
		// out of step with the database is worse than one that refetches.
		for (const type of [
			'job.succeeded',
			'job.failed',
			'job.cancelled',
			'job.started',
			'job.leased',
			'run.complete',
			'run.degraded',
			'run.failed',
			'run.started'
		]) {
			this.source.addEventListener(type, (event) => {
				const data = JSON.parse((event as MessageEvent).data);
				this.jobEvents = [{ ...data, type }, ...this.jobEvents].slice(0, 200);
				this.onResync?.();
			});
		}

		this.source.addEventListener('resync', () => {
			// We fell behind far enough that replaying is worse than refetching.
			this.onResync?.();
		});

		this.clock = setInterval(() => {
			this.now = Date.now();
		}, 1000);
	}

	private startPolling(): void {
		if (this.poller || !this.onResync) return;
		this.poller = setInterval(() => this.onResync?.(), POLL_INTERVAL);
	}

	private stopPolling(): void {
		if (this.poller) {
			clearInterval(this.poller);
			this.poller = null;
		}
	}

	disconnect(): void {
		this.source?.close();
		this.source = null;
		if (this.clock) clearInterval(this.clock);
		this.clock = null;
		this.stopPolling();
		this.connection = 'offline';
	}

	/** Seconds since a worker last reported, derived locally. */
	heartbeatAge(worker: Worker): number {
		return Math.max(0, (this.now - new Date(worker.last_heartbeat_at).getTime()) / 1000);
	}

	health(worker: Worker): 'ok' | 'suspect' | 'lost' {
		const age = this.heartbeatAge(worker);
		if (age > HEARTBEAT_LOST_SECONDS) return 'lost';
		if (age > HEARTBEAT_WARN_SECONDS) return 'suspect';
		return 'ok';
	}

	get unacknowledged(): Notification[] {
		return this.notifications.filter((entry) => !entry.acknowledged_at && !entry.resolved_at);
	}
}
