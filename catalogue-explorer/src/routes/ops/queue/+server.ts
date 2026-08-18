import { json } from '@sveltejs/kit';
import type { RequestHandler } from './$types';
import { configured, ControlError, get } from '$lib/server/control';
import type { QueueStatus } from '$lib/ops/types';

/** Browser-safe polling proxy; the control bearer token never leaves SvelteKit. */
export const GET: RequestHandler = async () => {
	if (!configured()) return json({ error: 'control service is not configured' }, { status: 503 });
	try {
		return json(await get<QueueStatus>('/v1/queue'));
	} catch (error) {
		return json(
			{ error: error instanceof ControlError ? error.message : String(error) },
			{ status: error instanceof ControlError ? error.status : 502 }
		);
	}
};
