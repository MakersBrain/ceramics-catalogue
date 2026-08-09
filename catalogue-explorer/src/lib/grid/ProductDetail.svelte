<script lang="ts">
	import AvailabilityBand from '$lib/charts/AvailabilityBand.svelte';
	import SupplierDetail from './SupplierDetail.svelte';
	import PriceHistory from '$lib/charts/PriceHistory.svelte';
	import { countryName } from '$lib/countries';
	// Aliased: this component is itself called ProductDetail, and the two names
	// in one module would resolve to the component rather than to the record.
	import type { ProductDetail as Detail, ProductSeed } from '$lib/catalogue';

	/**
	 * Everything the catalogue holds about one product, over the sheet.
	 *
	 * The panel is deliberately not a curated summary. A storefront that
	 * publishes a firing schedule and one that publishes nothing but a price are
	 * both in this catalogue, and the only honest way to show that is to render
	 * the imported document as it stands - including the keys no facet reads and
	 * the ones this particular shop left empty.
	 *
	 * The row already in the grid opens the panel immediately, so the name and
	 * the price are on screen while the full record is still in flight.
	 */

	let { row, onClose }: { row: ProductSeed | null; onClose: () => void } = $props();

	let dialog: HTMLDialogElement;
	let detail = $state<Detail | null>(null);
	let failed = $state(false);

	// showModal() rather than an open attribute: it is what brings the focus
	// trap, the inert background and Escape-to-close, none of which are worth
	// reimplementing.
	$effect(() => {
		if (row) dialog?.showModal();
		else dialog?.close();
	});

	$effect(() => {
		const id = row?.id;
		detail = null;
		failed = false;
		if (!id) return;
		let current = true;
		fetch(`/explore/product/${id}`)
			.then((response) => (response.ok ? response.json() : Promise.reject(response.status)))
			.then((body: Detail) => {
				if (current) detail = body;
			})
			.catch(() => {
				if (current) failed = true;
			});
		// The reader can move to the next row before the fetch lands; a stale
		// answer must not overwrite the row they are looking at now.
		return () => {
			current = false;
		};
	});

	/** Keys already spelled out in the summary above the document. */
	const COVERED = new Set(['id', 'source_id', 'attributes', 'name', 'product_url', 'image_url']);

	const fields = $derived(
		Object.entries(detail?.product ?? {}).filter(
			([key, value]) => !COVERED.has(key) && value !== null && value !== ''
		)
	);

	const attributes = $derived(
		(detail?.product?.attributes ?? null) as Record<string, unknown> | null
	);

	/**
	 * Every picture the importer kept, not just the one the table shows.
	 *
	 * Storefronts that publish a swatch per firing temperature put them all in
	 * `all_image_urls`, and for a glaze those are the most useful thing on the
	 * page - the difference between 1000 and 1250 degrees is the whole decision.
	 * The row's own image leads, and duplicates are dropped rather than shown
	 * twice at different positions.
	 */
	const pictures = $derived.by(() => {
		const listed = attributes?.all_image_urls;
		const all = [row?.image_url, ...(Array.isArray(listed) ? listed : [])];
		return [...new Set(all.filter((url): url is string => typeof url === 'string' && !!url))];
	});

	/**
	 * A url that fails is dropped rather than left as an empty frame. Storefronts
	 * rewrite their image paths and the catalogue keeps whatever was published at
	 * import time, so a dead link here is ordinary, not exceptional.
	 */
	const usable = $derived(pictures.filter((url) => !broken.has(url)));

	/** Which picture is large; the rest are thumbnails under it. */
	let shown = $state(0);

	/** Clamped, because the chosen index can be dropped out from under it. */
	const large = $derived(usable[Math.min(shown, usable.length - 1)]);
	$effect(() => {
		row;
		shown = 0;
	});

	/** A url that 404s should cost a thumbnail, not leave a broken frame. */
	let broken = $state(new Set<string>());
	$effect(() => {
		row;
		broken = new Set();
	});

	function label(key: string) {
		return key.replace(/_/g, ' ');
	}

	/** ISO timestamps are not for reading; dates are. */
	function when(value: unknown) {
		const date = new Date(String(value));
		return Number.isNaN(date.getTime())
			? String(value)
			: date.toLocaleDateString('en-GB', { year: 'numeric', month: 'short', day: 'numeric' });
	}

	function scalar(value: unknown) {
		if (value === null || value === undefined) return '-';
		if (typeof value === 'boolean') return value ? 'yes' : 'no';
		if (typeof value === 'number') return value.toLocaleString('en-US', { maximumFractionDigits: 4 });
		return String(value);
	}

	const isBranch = (value: unknown) =>
		value !== null && typeof value === 'object' && Object.keys(value).length > 0;

	/**
	 * A key the storefront never filled in is shown rather than hidden - that
	 * absence is exactly what the panel is for - but it recedes, so the eye finds
	 * the fields that carry something.
	 */
	const ink = (value: unknown) =>
		value === null || value === undefined || value === '' ? 'var(--text-muted)' : 'var(--text-primary)';

	/** The offer history, newest first, as the observations table holds it. */
	const offers = $derived(detail?.offers ?? []);

	/**
	 * The same history as two pictures. The table below stays: it is the record,
	 * and a chart is a reading of it — a reader checking whether a price really
	 * moved wants the numbers, not a shape.
	 *
	 * Both charts read the listed currency rather than a converted one. A price
	 * history is about what this shop did, and folding in a moving exchange rate
	 * would show the euro wobbling as if the shop had changed its mind.
	 */
	const priceSeries = $derived(
		offers
			.filter((offer) => typeof offer.price === 'number')
			.map((offer) => ({ at: offer.observed_at, value: offer.price as number }))
	);

	const unitPriceSeries = $derived(
		offers
			.filter((offer) => typeof offer.unit_price === 'number')
			.map((offer) => ({ at: offer.observed_at, value: offer.unit_price as number }))
	);

	const stockSeries = $derived(
		offers.map((offer) => ({ at: offer.observed_at, state: offer.availability ?? null }))
	);

	/** The currency the shop lists in; blank if it published none or several. */
	const listedCurrency = $derived.by(() => {
		const seen = new Set(offers.map((offer) => offer.currency).filter(Boolean));
		return seen.size === 1 ? ([...seen][0] as string) : '';
	});

	/** The shop's catalogue id, which only the fetched record carries. */
	const sourceId = $derived(String(detail?.product?.source_id ?? ''));

	let openedSupplier = $state<{ id: string; label?: string } | null>(null);

	const unitPer = $derived.by(() => {
		const seen = new Set(offers.map((offer) => offer.unit_price_per).filter(Boolean));
		return seen.size === 1 ? ([...seen][0] as string) : '';
	});
</script>

<!-- Clicking the backdrop closes. The check is on the dialog itself because the
     backdrop is not an element of its own and cannot take a listener. -->
<dialog
	bind:this={dialog}
	class="detail"
	onclose={onClose}
	onclick={(event) => {
		if (event.target === dialog) onClose();
	}}
>
	{#if row}
		<!-- Nearly the whole window on a phone, a centred sheet from `sm` up. The
		     panel is a record with a dozen sections in it, so on a small screen the
		     honest thing is to take the screen rather than show a third of it. -->
		<article class="flex max-h-[calc(100dvh-1rem)] flex-col sm:max-h-[85dvh]">
			<header
				class="flex items-start gap-2 px-4 py-3 sm:gap-4 sm:px-5 sm:py-4"
				style="border-bottom: 1px solid var(--hairline)"
			>
				<div class="min-w-0 flex-1">
					<h2 class="text-base font-semibold" style="color: var(--text-primary)">{row.name}</h2>
					<p class="mt-1 text-xs" style="color: var(--text-secondary)">
						<!-- The shop is a thing in its own right, not a label on this row:
						     whether it publishes firing ranges at all is the context for
						     every empty field below. -->
						<button
							type="button"
							class="underline decoration-dotted underline-offset-2"
							style="color: var(--accent)"
							onclick={() => (openedSupplier = { id: sourceId, label: row?.supplier_label })}
							disabled={!sourceId}
						>
							{row.supplier_label}
						</button>{row.country ? ` (${countryName(row.country)})` : ''}{row.brand
							? ` - ${row.brand}`
							: ''}{row.code ? ` - ${row.code}` : ''}
					</p>
				</div>
				<a
					href={row.url}
					target="_blank"
					rel="noreferrer noopener"
					class="shrink-0 rounded-lg px-2.5 py-1.5 text-xs whitespace-nowrap sm:px-3"
					style="border: 1px solid var(--hairline); color: var(--accent)"
				>
					<!-- The full phrase costs a phone most of the title's line. -->
					<span class="hidden sm:inline">Open on storefront</span>
					<span class="sm:hidden">Shop</span>
				</a>
				<button
					type="button"
					onclick={onClose}
					aria-label="Close"
					class="shrink-0 rounded-lg px-2 py-1 text-base"
					style="color: var(--text-secondary)">&times;</button
				>
			</header>

			<div class="min-h-0 flex-1 overflow-y-auto px-4 py-4 sm:px-5">
				<div class="flex flex-col gap-4 sm:flex-row sm:gap-5">
					{#if usable.length}
						<div class="shrink-0 self-center sm:self-start">
							<!-- Keyed on the url so a change of picture remounts the element;
							     a plain src swap keeps the previous image's error state. -->
							{#key large}
								<img
									src={large}
									alt=""
									referrerpolicy="no-referrer"
									class="h-48 w-48 rounded-lg object-contain"
									style="background: color-mix(in srgb, var(--recede) 30%, transparent)"
									onerror={() => (broken = new Set([...broken, large]))}
								/>
							{/key}
							{#if usable.length > 1}
								<!-- One button per picture. A glaze photographed at three
								     temperatures is three different answers, so they are all
								     reachable rather than folded into a carousel. -->
								<div class="mt-2 flex w-48 flex-wrap gap-1">
									{#each usable as url, index (url)}
										<button
											type="button"
											onclick={() => (shown = index)}
											aria-label="Picture {index + 1} of {usable.length}"
											aria-pressed={url === large}
											class="h-10 w-10 overflow-hidden rounded"
											style="outline: 2px solid {url === large
												? 'var(--accent)'
												: 'transparent'}; outline-offset: 1px"
										>
											<img
												src={url}
												alt=""
												referrerpolicy="no-referrer"
												class="h-full w-full object-cover"
												onerror={() => (broken = new Set([...broken, url]))}
											/>
										</button>
									{/each}
								</div>
							{/if}
						</div>
					{:else}
						<div
							class="flex h-48 w-48 shrink-0 items-center justify-center rounded-lg text-xs"
							style="background: color-mix(in srgb, var(--recede) 30%, transparent); color: var(--text-muted)"
						>
							no picture imported
						</div>
					{/if}

					<!-- The promoted columns, which every product has whether or not the
					     storefront filled them in. -->
					<div class="min-w-0 flex-1">
						<h3 class="text-xs font-semibold" style="color: var(--text-secondary)">Record</h3>
						{#if failed}
							<p class="mt-2 text-xs" style="color: var(--critical)">
								The full record could not be loaded. What the table already knew is above.
							</p>
						{:else if !detail}
							<p class="mt-2 text-xs" style="color: var(--text-muted)">Loading the record...</p>
						{:else}
							<dl class="mt-2 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-xs">
								{#each fields as [key, value] (key)}
									<dt class="whitespace-nowrap" style="color: var(--text-muted)">{label(key)}</dt>
									<dd class="min-w-0 break-words" style="color: {ink(value)}">
										{key.endsWith('_at') ? when(value) : scalar(value)}
									</dd>
								{/each}
							</dl>
						{/if}
					</div>
				</div>

				{#if attributes && Object.keys(attributes).length}
					<h3 class="mt-6 text-xs font-semibold" style="color: var(--text-secondary)">
						Imported attributes
					</h3>
					<div class="mt-2">
						{@render tree(attributes)}
					</div>
				{/if}

				{#if offers.length}
					{#if priceSeries.length > 1 || stockSeries.length > 1}
						<div class="viz-surface mt-6 rounded-lg p-3">
							{#if priceSeries.length > 1}
								<PriceHistory points={priceSeries} currency={listedCurrency} />
							{/if}

							{#if unitPriceSeries.length > 1}
								<div class="mt-4">
									<PriceHistory
										points={unitPriceSeries}
										currency={listedCurrency ? `${listedCurrency}/${unitPer}` : ''}
										label="Unit price"
									/>
								</div>
							{/if}

							{#if stockSeries.length > 1}
								<div class="mt-4">
									<h4 class="text-xs font-semibold" style="color: var(--text-secondary)">
										Availability
									</h4>
									<div class="mt-1">
										<AvailabilityBand points={stockSeries} />
									</div>
								</div>
							{/if}

							<!-- Said plainly rather than implied by a short axis: a reader
							     seeing four days of history should know it is four days of
							     collection, not four days of the shop's life. -->
							<p class="mt-3 text-[10px]" style="color: var(--text-muted)">
								History begins when this catalogue first recorded the product, not when the
								shop first sold it.
							</p>
						</div>
					{/if}

					<h3 class="mt-6 text-xs font-semibold" style="color: var(--text-secondary)">
						Observed prices ({offers.length})
					</h3>
					<!-- Five columns of numbers do not fold, so on a narrow screen the
					     table scrolls inside its own box rather than making the panel
					     scroll sideways under the reader. -->
					<div class="mt-2 -mx-4 overflow-x-auto px-4 sm:mx-0 sm:px-0">
					<table class="w-full min-w-[26rem] text-xs" style="color: var(--text-secondary)">
						<thead>
							<tr class="text-left" style="border-bottom: 1px solid var(--hairline)">
								<th class="py-1 pr-4 font-medium">Seen</th>
								<th class="py-1 pr-4 text-right font-medium">Price</th>
								<th class="py-1 pr-4 font-medium">Pack</th>
								<th class="py-1 pr-4 text-right font-medium">Unit price</th>
								<th class="py-1 font-medium">VAT</th>
							</tr>
						</thead>
						<tbody>
							{#each offers as offer, index (index)}
								<tr style="border-bottom: 1px solid var(--hairline)">
									<td class="py-1 pr-4 whitespace-nowrap">{when(offer.observed_at)}</td>
									<td class="py-1 pr-4 text-right tabular-nums" style="color: var(--text-primary)">
										{scalar(offer.price)}
										{offer.currency ?? ''}
									</td>
									<td class="py-1 pr-4 whitespace-nowrap">
										{offer.quantity ? `${scalar(offer.quantity)} ${offer.unit ?? ''}` : '-'}
									</td>
									<td class="py-1 pr-4 text-right tabular-nums">
										{offer.unit_price
											? `${scalar(offer.unit_price)}/${offer.unit_price_per ?? ''}`
											: '-'}
									</td>
									<td class="py-1">{offer.vat_status ?? 'unknown'}</td>
								</tr>
							{/each}
						</tbody>
					</table>
					</div>
				{/if}
			</div>
		</article>
	{/if}
</dialog>

<!-- The imported document, at whatever depth it happens to have. A snippet that
     calls itself, because the shape is the supplier's and not ours to flatten. -->
{#snippet tree(node: Record<string, unknown>)}
	<dl class="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-xs">
		{#each Object.entries(node) as [key, value] (key)}
			<dt class="whitespace-nowrap" style="color: var(--text-muted)">{label(key)}</dt>
			<dd class="min-w-0">
				{#if Array.isArray(value)}
					{#if value.every((entry) => !isBranch(entry))}
						<span style="color: {ink(value.length ? value : null)}">{value.map(scalar).join(', ') || '-'}</span>
					{:else}
						<div class="flex flex-col gap-1">
							{#each value as entry, index (index)}
								{#if isBranch(entry)}
									<div style="border-left: 1px solid var(--hairline); padding-left: 0.5rem">
										{@render tree(entry as Record<string, unknown>)}
									</div>
								{:else}
									<span style="color: {ink(entry)}">{scalar(entry)}</span>
								{/if}
							{/each}
						</div>
					{/if}
				{:else if isBranch(value)}
					<div style="border-left: 1px solid var(--hairline); padding-left: 0.5rem">
						{@render tree(value as Record<string, unknown>)}
					</div>
				{:else}
					<span class="break-words" style="color: {ink(value)}">{scalar(value)}</span>
				{/if}
			</dd>
		{/each}
	</dl>
{/snippet}

<style>
	.detail {
		margin: auto;
		/* Almost the whole width on a phone; the gutter only has to show that
		   something is behind it. */
		width: calc(100vw - 0.75rem);
		padding: 0;
		border: 1px solid var(--hairline);
		border-radius: 0.75rem;
		background: var(--surface-1);
		color: var(--text-primary);
	}

	@media (min-width: 640px) {
		.detail {
			width: min(56rem, calc(100vw - 2rem));
		}
	}

	.detail::backdrop {
		background: rgba(0, 0, 0, 0.45);
	}
</style>

<SupplierDetail supplier={openedSupplier} onClose={() => (openedSupplier = null)} />
