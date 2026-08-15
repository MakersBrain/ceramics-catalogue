<script lang="ts">
	import { countryName } from '$lib/countries';
	import type { SupplierDetail as Detail } from '$lib/server/explore';

	/**
	 * The shop behind a row: who they are, and what their catalogue turns out to
	 * hold.
	 *
	 * The coverage list is the part that earns the panel. Every other page here
	 * treats a missing firing range or a missing code as a property of a product,
	 * when it is almost always a property of the shop — this one publishes a
	 * specification table and that one publishes a photograph. Seeing it per
	 * supplier is what turns "the data is patchy" into "this supplier never
	 * publishes a code, so its rows cannot join anything".
	 */

	let { supplier, onClose }: { supplier: { id: string; label?: string } | null; onClose: () => void } =
		$props();

	let dialog: HTMLDialogElement;
	let detail = $state<Detail | null>(null);
	let failed = $state(false);

	$effect(() => {
		if (supplier) dialog?.showModal();
		else dialog?.close();
	});

	$effect(() => {
		const id = supplier?.id;
		detail = null;
		failed = false;
		if (!id) return;
		let current = true;
		fetch(`/explore/supplier/${id}`)
			.then((response) => (response.ok ? response.json() : Promise.reject(response.status)))
			.then((body: Detail) => {
				if (current) detail = body;
			})
			.catch(() => {
				if (current) failed = true;
			});
		return () => {
			current = false;
		};
	});

	const source = $derived((detail?.source ?? null) as Record<string, unknown> | null);
	const metadata = $derived((source?.metadata ?? null) as Record<string, unknown> | null);
	const totals = $derived(detail?.totals ?? null);

	const number = (value: number | null | undefined) =>
		value == null ? '-' : value.toLocaleString('en-US');

	const when = (value: unknown) => {
		if (!value) return '-';
		const date = new Date(String(value));
		return Number.isNaN(date.getTime())
			? String(value)
			: date.toLocaleDateString('en-GB', { year: 'numeric', month: 'short', day: 'numeric' });
	};

	/**
	 * Coverage as a share of the shop's own rows, so a small catalogue that
	 * publishes everything does not read as worse than a large one that
	 * publishes almost nothing.
	 */
	const coverage = $derived.by(() => {
		const total = totals?.products ?? 0;
		if (!total) return [];
		return (detail?.coverage ?? []).map((entry) => ({
			...entry,
			share: (entry.products / total) * 100
		}));
	});
</script>

<dialog
	bind:this={dialog}
	class="detail"
	onclose={onClose}
	onclick={(event) => {
		if (event.target === dialog) onClose();
	}}
>
	{#if supplier}
		<article class="flex max-h-[calc(100dvh-1rem)] flex-col sm:max-h-[85dvh]">
			<header
				class="flex items-start gap-2 px-4 py-3 sm:gap-4 sm:px-5 sm:py-4"
				style="border-bottom: 1px solid var(--hairline)"
			>
				<div class="min-w-0 flex-1">
					<h2 class="text-base font-semibold" style="color: var(--text-primary)">
						{(source?.label as string) ?? supplier.label ?? supplier.id}
					</h2>
					<p class="mt-1 text-xs" style="color: var(--text-secondary)">
						{supplier.id}{metadata?.country ? ` - ${countryName(String(metadata.country))}` : ''}
					</p>
				</div>
				{#if source?.homepage_url}
					<a
						href={String(source.homepage_url)}
						target="_blank"
						rel="noreferrer noopener"
						class="shrink-0 rounded-lg px-2.5 py-1.5 text-xs whitespace-nowrap sm:px-3"
						style="border: 1px solid var(--hairline); color: var(--primary)"
					>
						<span class="hidden sm:inline">Visit shop</span>
						<span class="sm:hidden">Shop</span>
					</a>
				{/if}
				<button
					type="button"
					onclick={onClose}
					class="shrink-0 rounded-lg px-2 py-1.5 text-xs"
					style="border: 1px solid var(--hairline); color: var(--text-secondary)"
					aria-label="Close">Close</button
				>
			</header>

			<div class="min-h-0 flex-1 overflow-y-auto px-4 py-4 sm:px-5">
				{#if failed}
					<p class="text-sm" style="color: var(--critical)">
						This supplier could not be loaded.
					</p>
				{:else if !detail}
					<p class="text-sm" style="color: var(--text-muted)">Loading…</p>
				{:else}
					{#if totals}
						<dl class="grid grid-cols-2 gap-3 sm:grid-cols-4">
							{#each [['Products', number(totals.products)], ['In stock listings', number(totals.active)], ['Brands carried', number(totals.brands)], ['Price observations', number(totals.observations)]] as [term, value] (term)}
								<div class="viz-surface rounded-lg px-3 py-2">
									<dt class="text-[10px]" style="color: var(--text-muted)">{term}</dt>
									<dd
										class="mt-0.5 text-lg font-semibold tabular-nums"
										style="color: var(--text-primary)"
									>
										{value}
									</dd>
								</div>
							{/each}
						</dl>

						<p class="mt-2 text-[11px]" style="color: var(--text-muted)">
							First recorded {when(totals.first_seen)}, last seen {when(totals.last_seen)}.
						</p>
					{/if}

					{#if coverage.length}
						<h3 class="mt-6 text-xs font-semibold" style="color: var(--text-secondary)">
							What this shop publishes
						</h3>
						<p class="mt-0.5 text-[11px]" style="color: var(--text-muted)">
							Share of its own {number(totals?.products)} rows carrying each field.
						</p>
						<ul class="mt-2 flex flex-col gap-1.5">
							{#each coverage as entry (entry.field)}
								<li class="flex items-center gap-2 text-xs">
									<span class="w-20 shrink-0 truncate sm:w-32" style="color: var(--text-secondary)"
										title={entry.field}>{entry.field}</span
									>
									<span
										class="h-2 flex-1 overflow-hidden rounded"
										style="background: var(--gridline)"
									>
										<span
											class="block h-full rounded"
											style="width: {entry.share}%; background: var(--primary)"
										></span>
									</span>
									<span
										class="w-16 shrink-0 text-right tabular-nums sm:w-20"
										style="color: var(--text-primary)"
									>
										{entry.share.toFixed(0)}% ({number(entry.products)})
									</span>
								</li>
							{/each}
						</ul>
					{/if}

					<div class="mt-6 grid gap-6 sm:grid-cols-2">
						{#if detail.families.length}
							<div>
								<h3 class="text-xs font-semibold" style="color: var(--text-secondary)">
									Product families
								</h3>
								<ul class="mt-2 flex flex-col gap-1 text-xs" style="color: var(--text-secondary)">
									{#each detail.families as family (family.value)}
										<li class="flex justify-between gap-4">
											<span>{family.label}</span>
											<span class="tabular-nums" style="color: var(--text-primary)"
												>{number(family.products)}</span
											>
										</li>
									{/each}
								</ul>
							</div>
						{/if}

						{#if detail.brands.length}
							<div>
								<h3 class="text-xs font-semibold" style="color: var(--text-secondary)">
									Brands carried
								</h3>
								<ul class="mt-2 flex flex-col gap-1 text-xs" style="color: var(--text-secondary)">
									{#each detail.brands as brand (brand.value)}
										<li class="flex justify-between gap-4">
											<span>{brand.label}</span>
											<span class="tabular-nums" style="color: var(--text-primary)"
												>{number(brand.products)}</span
											>
										</li>
									{/each}
								</ul>
							</div>
						{/if}
					</div>

					{#if detail.currencies.length}
						<h3 class="mt-6 text-xs font-semibold" style="color: var(--text-secondary)">
							Listed in
						</h3>
						<p class="mt-1 text-xs" style="color: var(--text-secondary)">
							{detail.currencies
								.map((entry) => `${entry.value} (${number(entry.products)})`)
								.join(', ')}
						</p>
					{/if}
				{/if}
			</div>
		</article>
	{/if}
</dialog>

<style>
	/* The same sheet the product panel uses, so the two read as one surface. */
	dialog.detail {
		/* What centres a dialog in the top layer; without it the sheet sits in
		   the corner. The product panel has always had it. */
		margin: auto;
		width: calc(100vw - 0.75rem);
		padding: 0;
		border: 1px solid var(--hairline);
		border-radius: 0.75rem;
		background: var(--surface-1);
		color: var(--text-primary);
	}

	@media (min-width: 640px) {
		dialog.detail {
			width: min(56rem, calc(100vw - 2rem));
		}
	}

	dialog.detail::backdrop {
		background: rgba(0, 0, 0, 0.45);
	}
</style>
