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
	},
	bulk: async ({ request }) => {
		const form = await request.formData();
		const ids = [...new Set(form.getAll('ids').map(String).filter(Boolean))];
		const action = String(form.get('action') ?? '');
		if (!ids.length) return fail(400, { error: 'Select at least one source' });

		const update: Record<string, unknown> = { updated_by: 'explorer:bulk' };
		if (action === 'enable') update.enabled = true;
		else if (action === 'disable') update.enabled = false;
		else if (action === 'pause') update.paused = true;
		else if (action === 'resume') update.paused = false;
		else if (action === 'schedule') update.schedule_id = form.get('schedule_id') || null;
		else return fail(422, { error: 'Choose a bulk action' });

		const results = await Promise.allSettled(
			ids.map((id) => put<{ source: SourceRow }>(`/v1/sources/${id}`, update))
		);
		const failed = results.flatMap((result, index) =>
			result.status === 'rejected' ? [ids[index]] : []
		);
		const updated = ids.length - failed.length;
		return failed.length
			? { updated, error: `${updated} updated; ${failed.length} failed: ${failed.join(', ')}` }
			: { updated };
	}
};
