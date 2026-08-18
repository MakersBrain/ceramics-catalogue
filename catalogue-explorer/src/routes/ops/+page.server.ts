import type { Actions, PageServerLoad } from './$types';
import { fail } from '@sveltejs/kit';
import { ControlError, get, post, configured } from '$lib/server/control';
import type { QueueStatus, RunRow, Schedule, SourceRow, WorkerRow } from '$lib/ops/types';

export const load: PageServerLoad = async () => {
	if (!configured()) {
		// A clear message rather than a stack trace: this is the state a fresh
		// checkout is in, and "CATALOGUE_CONTROL_TOKEN is not set" is the whole
		// of what somebody needs to know.
		return { unavailable: 'CATALOGUE_CONTROL_TOKEN is not set for the explorer' };
	}
	try {
		const [workers, runs, sources, schedules, queue] = await Promise.all([
			get<{ workers: WorkerRow[] }>('/v1/workers'),
			get<{ runs: RunRow[] }>('/v1/runs?limit=5'),
			get<{ sources: SourceRow[] }>('/v1/sources'),
			get<{ schedules: Schedule[] }>('/v1/schedules'),
			get<QueueStatus>('/v1/queue')
		]);
		return {
			workers: workers.workers,
			runs: runs.runs,
			sources: sources.sources,
			schedules: schedules.schedules,
			queueStats: queue
		};
	} catch (error) {
		return { unavailable: error instanceof ControlError ? error.message : String(error) };
	}
};

export const actions: Actions = {
	/** The "Run now" control. */
	run: async ({ request }) => {
		const form = await request.formData();
		const selected = form.getAll('sources').map(String).filter(Boolean);
		try {
			return await post<{ run_id: string; jobs: number; sources: string[] }>('/v1/runs', {
				sources: selected.length ? selected.join(',') : 'all',
				requested_by: 'explorer',
				params: {
					// Explicit, because the default cache age is what decides
					// whether a run actually prices anything. An operator pressing
					// "Run now" wants current prices, not yesterday's pages.
					cache_mode: form.get('cache_mode') || 'refresh'
				}
			});
		} catch (error) {
			return fail(400, { error: error instanceof ControlError ? error.message : String(error) });
		}
	},

	worker: async ({ request }) => {
		const form = await request.formData();
		try {
			return await post<{ worker_id: string; desired_state: string }>(
				`/v1/workers/${form.get('id')}/${form.get('action')}`
			);
		} catch (error) {
			return fail(400, { error: error instanceof ControlError ? error.message : String(error) });
		}
	}
};
