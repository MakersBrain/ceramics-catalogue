import { sql } from './db';
import { createStableCache } from './cache-core.js';

const cache = createStableCache(async () => {
	const [row] = await sql<{ generation: number }[]>`
		select generation::int from catalogue.catalogue_generation where singleton
	`;
	return row?.generation ?? 0;
});

/** Cache coherent aggregate payloads and invalidate on canonical promotion. */
export async function stable<T>(key: string, build: () => Promise<T>): Promise<T> {
	return cache.stable(key, build) as Promise<T>;
}

export function clearStableCache() {
	cache.clear();
}
