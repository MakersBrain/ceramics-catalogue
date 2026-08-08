import { error, json } from '@sveltejs/kit';
import { productDetail } from '$lib/server/explore';
import type { RequestHandler } from './$types';

/**
 * The id reaches the query as a uuid, so anything that is not shaped like one
 * is turned away here rather than reaching Postgres and coming back as a 500.
 */
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** Everything held about one product, fetched when its row is opened. */
export const GET: RequestHandler = async ({ params }) => {
	if (!UUID.test(params.id)) error(404, 'No such product');
	const detail = await productDetail(params.id);
	if (!detail.product) error(404, 'No such product');
	return json(detail);
};
