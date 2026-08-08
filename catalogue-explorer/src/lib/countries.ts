/**
 * Where a supplier ships from, as ISO 3166-1 alpha-2, and the one grouping a
 * potter actually orders by.
 *
 * The codes come from catalogue.sources, which the loader fills from
 * sources.json - the crawler's own record of each shop. Nothing here is
 * inferred from a domain at read time: a supplier with no country stays
 * uncounted rather than being guessed into a group.
 */

/**
 * The Schengen area as of 2026: Bulgaria and Romania joined in January 2025,
 * Croatia in 2023.
 *
 * Worth knowing what this does and does not mean. It is a border-control area,
 * not the EU customs union, so the four non-EU members here - Switzerland,
 * Norway, Iceland and Liechtenstein - still put a customs declaration and
 * possibly duty on a parcel. Ireland and Cyprus are EU but not Schengen, so
 * they are absent from this list while shipping to them stays duty-free.
 */
export const SCHENGEN = [
	'AT', 'BE', 'BG', 'CH', 'CZ', 'DE', 'DK', 'EE', 'ES', 'FI', 'FR', 'GR', 'HR',
	'HU', 'IS', 'IT', 'LI', 'LT', 'LU', 'LV', 'MT', 'NL', 'NO', 'PL', 'PT', 'RO',
	'SE', 'SI', 'SK'
];

const SCHENGEN_SET = new Set(SCHENGEN);

export function isSchengen(code: string) {
	return SCHENGEN_SET.has(code.toUpperCase());
}

/**
 * Names for every country the catalogue configures a supplier in, plus the rest
 * of the Schengen area so the quick pick can always be labelled.
 */
const NAMES: Record<string, string> = {
	AT: 'Austria',
	BE: 'Belgium',
	BG: 'Bulgaria',
	CA: 'Canada',
	CH: 'Switzerland',
	CY: 'Cyprus',
	CZ: 'Czechia',
	DE: 'Germany',
	DK: 'Denmark',
	EE: 'Estonia',
	ES: 'Spain',
	FI: 'Finland',
	FR: 'France',
	GB: 'United Kingdom',
	GR: 'Greece',
	HR: 'Croatia',
	HU: 'Hungary',
	IE: 'Ireland',
	IS: 'Iceland',
	IT: 'Italy',
	LI: 'Liechtenstein',
	LT: 'Lithuania',
	LU: 'Luxembourg',
	LV: 'Latvia',
	MT: 'Malta',
	NL: 'Netherlands',
	NO: 'Norway',
	PL: 'Poland',
	PT: 'Portugal',
	RO: 'Romania',
	SE: 'Sweden',
	SI: 'Slovenia',
	SK: 'Slovakia',
	US: 'United States'
};

/** A code nobody has named is shown as the bare code, never as a blank. */
export function countryName(code: string) {
	return NAMES[code.toUpperCase()] ?? code.toUpperCase();
}
