import type { Actions, PageServerLoad } from './$types';
import { fail } from '@sveltejs/kit';
import { ControlError, configured, get, put } from '$lib/server/control';
import type { Schedule, SourceRow } from '$lib/ops/types';

export const load: PageServerLoad = async () => {
	if (!configured()) return { unavailable: 'CATALOGUE_CONTROL_TOKEN is not set for the explorer' };
	try {
		const [sources, schedules] = await Promise.all([
			get<{ sources: SourceRow[] }>('/v1/sources'),
			get<{ schedules: Schedule[] }>('/v1/schedules')
		]);
		return { sources: sources.sources, schedules: schedules.schedules };
	} catch (error) {
		return { unavailable: error instanceof ControlError ? error.message : String(error) };
	}
};

export const actions: Actions = {
	update: async ({ request }) => {
		const form = await request.formData();
		const id = String(form.get('id'));
		try {
			return await put<{ source: SourceRow }>(`/v1/sources/${id}`, {
				enabled: form.get('enabled') === 'on',
				paused: form.get('paused') === 'on',
				schedule_id: form.get('schedule_id') || null,
				updated_by: 'explorer'
			});
		} catch (error) {
			return fail(400, { error: error instanceof ControlError ? error.message : String(error) });
		}
	}
};
