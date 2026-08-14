/** Small dependency-injected cache core so generation invalidation is testable
 * without a live PostgreSQL connection.
 * @param {() => Promise<number>} readGeneration
 * @param {{ttlMs?: number, pollMs?: number, now?: () => number}} options
 */
export function createStableCache(readGeneration, options = {}) {
	const ttlMs = options.ttlMs ?? 10 * 60 * 1000;
	const pollMs = options.pollMs ?? 5_000;
	const clock = options.now ?? Date.now;
	/** @type {Map<string, {at: number, generation: number, value: unknown}>} */
	const entries = new Map();
	let generation = { at: 0, value: -1 };

	async function currentGeneration() {
		const now = clock();
		if (now - generation.at < pollMs) return generation.value;
		generation = { at: now, value: await readGeneration() };
		return generation.value;
	}

	return {
		/** @param {string} key @param {() => Promise<unknown>} build */
		async stable(key, build) {
			const now = clock();
			const version = await currentGeneration();
			const found = entries.get(key);
			if (found && found.generation === version && now - found.at < ttlMs) {
				return found.value;
			}
			const value = await build();
			entries.set(key, { at: now, generation: version, value });
			return value;
		},
		clear() {
			entries.clear();
			generation.at = 0;
		}
	};
}
