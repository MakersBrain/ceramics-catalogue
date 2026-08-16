import type { Handle, HandleServerError } from '@sveltejs/kit';
import { randomUUID } from 'node:crypto';

const validRequestId = /^[A-Za-z0-9._:-]{1,128}$/;

export const handle: Handle = async ({ event, resolve }) => {
	const supplied = event.request.headers.get('x-request-id');
	const requestId = supplied && validRequestId.test(supplied) ? supplied : randomUUID();
	event.locals.requestId = requestId;
	const started = performance.now();
	let status = 500;
	try {
		const response = await resolve(event);
		status = response.status;
		response.headers.set('x-request-id', requestId);
		return response;
	} finally {
		console.info(
			JSON.stringify({
				event: 'http.request',
				service: 'explorer',
				method: event.request.method,
				path: event.route.id ?? 'unmatched',
				status,
				duration_ms: Math.round((performance.now() - started) * 1000) / 1000,
				request_id: requestId
			})
		);
	}
};

export const handleError: HandleServerError = ({ error, event, status, message }) => {
	const requestId = event.locals.requestId || randomUUID();
	console.error(
		JSON.stringify({
			event: 'http.error',
			service: 'explorer',
			status,
			request_id: requestId,
			error: error instanceof Error ? error.stack || error.message : String(error)
		})
	);
	return { message: status >= 500 ? 'The request could not be completed.' : message, requestId };
};
