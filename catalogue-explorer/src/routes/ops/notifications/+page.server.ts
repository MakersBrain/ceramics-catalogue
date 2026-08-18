import type { Actions, PageServerLoad } from './$types';
import { fail } from '@sveltejs/kit';
import { ControlError, configured, get, post } from '$lib/server/control';
import type { NotificationRow } from '$lib/ops/types';

export const load: PageServerLoad = async ({ url }) => {
	if (!configured()) return { unavailable: 'CATALOGUE_CONTROL_TOKEN is not set for the explorer' };
	const severity = url.searchParams.get('severity');
	const query = new URLSearchParams({ limit: '200' });
	if (severity) query.set('severity', severity);
	try {
		const body = await get<{ notifications: NotificationRow[] }>(`/v1/notifications?${query}`);
		return { notifications: body.notifications, severity };
	} catch (error) {
		return { unavailable: error instanceof ControlError ? error.message : String(error) };
	}
};

export const actions: Actions = {
	ack: async ({ request }) => {
		const form = await request.formData();
		try {
			return await post<{ id: number; acknowledged: boolean }>(
				`/v1/notifications/${form.get('id')}/ack`,
				{ by: 'explorer' }
			);
		} catch (error) {
			return fail(400, { error: error instanceof ControlError ? error.message : String(error) });
		}
	},
	bulkAck: async ({ request }) => {
		const form = await request.formData();
		const ids = [
			...new Set(
				form
					.getAll('ids')
					.map(Number)
					.filter((value) => Number.isInteger(value) && value > 0)
			)
		];
		if (!ids.length) return fail(400, { error: 'Select at least one notification' });
		try {
			return await post<{ ids: number[]; acknowledged: number }>('/v1/notifications/ack', {
				ids,
				by: 'explorer:bulk'
			});
		} catch (error) {
			return fail(400, { error: error instanceof ControlError ? error.message : String(error) });
		}
	}
};
