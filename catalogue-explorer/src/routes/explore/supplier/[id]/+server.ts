import { error, json } from '@sveltejs/kit';
import { supplierDetail } from '$lib/server/explore';
import type { RequestHandler } from './$types';

/**
 * A source id is the slug the sources file gives a shop (`les-cousins`), so it
 * is checked against that shape rather than left to reach Postgres as whatever
 * arrived in the path.
 */
const SLUG = /^[a-z0-9][a-z0-9-]{0,63}$/;

/** The shop behind a row, and what its catalogue turns out to hold. */
export const GET: RequestHandler = async ({ params }) => {
	if (!SLUG.test(params.id)) error(404, 'No such supplier');
	const detail = await supplierDetail(params.id);
	if (!detail.source) error(404, 'No such supplier');
	return json(detail);
};
