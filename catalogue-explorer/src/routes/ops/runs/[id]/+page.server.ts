import type { Actions, PageServerLoad } from './$types';
import { fail } from '@sveltejs/kit';
import { ControlError, configured, get, post } from '$lib/server/control';
import type { Job, Run } from '$lib/ops/types';

export const load: PageServerLoad = async ({ params }) => {
	if (!configured()) return { unavailable: 'CATALOGUE_CONTROL_TOKEN is not set for the explorer' };
	try {
		return await get<{ run: Run; jobs: Job[] }>(`/v1/runs/${params.id}`);
	} catch (error) {
		return { unavailable: error instanceof ControlError ? error.message : String(error) };
	}
};

export const actions: Actions = {
	/** Per-row pause, resume, cancel and retry. */
	job: async ({ request }) => {
		const form = await request.formData();
		try {
			return await post<{ job_id: string; action: string }>(
				`/v1/jobs/${form.get('id')}/${form.get('action')}`
			);
		} catch (error) {
			return fail(400, { error: error instanceof ControlError ? error.message : String(error) });
		}
	},

	cancel: async ({ params }) => {
		try {
			return await post<{ cancelled: number }>(`/v1/runs/${params.id}/cancel`);
		} catch (error) {
			return fail(400, { error: error instanceof ControlError ? error.message : String(error) });
		}
	}
};
