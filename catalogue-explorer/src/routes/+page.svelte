<script lang="ts">
	import BarChart from '$lib/charts/BarChart.svelte';
	import ChartCard from '$lib/charts/ChartCard.svelte';
	import RangeChart from '$lib/charts/RangeChart.svelte';
	import StackedBar from '$lib/charts/StackedBar.svelte';
	import StatTile from '$lib/components/StatTile.svelte';
	import type { PageData } from './$types';

	let { data }: { data: PageData } = $props();

	const count = (value: number) => value.toLocaleString('en-US');
	const euros = (value: number) => `EUR ${value.toFixed(2)}`;

	const supplierBars = $derived(
		data.suppliers.map((row) => ({
			label: row.supplier,
			value: row.products,
			note: `${count(row.coded)} carry a manufacturer code`
		}))
	);

	const familySegments = $derived(
		data.families.map((row) => ({ label: row.family, value: row.products }))
	);

	const medianBars = $derived(
		data.medians.map((row) => ({
			label: row.supplier,
			value: row.median,
			note: `${count(row.offers)} comparable offers`
		}))
	);

	const spreadRows = $derived(
		data.spread.map((row) => ({
			label: row.code,
			sublabel: row.name,
			low: row.low,
			high: row.high,
			note: `${row.suppliers} suppliers`,
			href: `/compare?q=${encodeURIComponent(row.code)}`
		}))
	);

	const bandLabel = $derived(data.bands.find((entry) => entry.id === data.bandId)?.label ?? '');
</script>

<h1 class="text-2xl font-semibold" style="color: var(--text-primary)">Reference catalogue</h1>
<p class="mt-1 text-sm" style="color: var(--text-secondary)">
	Public listings collected from {data.totals.suppliers} ceramics suppliers, loaded into the local
	PostgreSQL <code>catalogue</code> schema.
</p>
<p class="mt-1 text-xs" style="color: var(--text-muted)">
	Prices are converted to EUR at the ECB reference rate{data.fx.date
		? ` of ${data.fx.date}`
		: ''}{data.fx.stale ? ' (last stored rates - the ECB was unreachable)' : ''}, which is
	indicative and not a transaction rate.
</p>

<div class="mt-6 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
	<StatTile
		label="Supplier products"
		value={count(data.totals.products)}
		note="one row per purchasable variant"
		hero
	/>
	<StatTile label="Price observations" value={count(data.totals.offers)} note="append-only" />
	<StatTile
		label="Manufacturer codes"
		value={count(data.totals.codes)}
		note="the cross-supplier key"
	/>
	<StatTile
		label="Suppliers"
		value={count(data.totals.suppliers)}
		note={data.totals.observed
			? `last observed ${new Date(data.totals.observed).toISOString().slice(0, 10)}`
			: undefined}
	/>
</div>

<!-- One filter row, above everything it scopes. -->
<form
	method="GET"
	class="mt-8 flex flex-wrap items-center gap-3 text-sm"
	style="color: var(--text-secondary)"
>
	<label for="band">Pack size</label>
	<select
		id="band"
		name="band"
		class="rounded-lg px-3 py-1.5 text-sm"
		style="background: var(--surface-1); color: var(--text-primary); border: 1px solid var(--hairline)"
		value={data.bandId}
		onchange={(event) => event.currentTarget.form?.requestSubmit()}
	>
		{#each data.bands as entry (entry.id)}
			<option value={entry.id}>{entry.label}</option>
		{/each}
	</select>
	<label for="brand">Brand</label>
	<select
		id="brand"
		name="brand"
		class="max-w-56 rounded-lg px-3 py-1.5 text-sm"
		style="background: var(--surface-1); color: var(--text-primary); border: 1px solid var(--hairline)"
		value={data.brand ?? ''}
		onchange={(event) => event.currentTarget.form?.requestSubmit()}
	>
		<option value="">all brands</option>
		{#each data.brandOptions as option (option.key)}
			<option value={option.key}>{option.label} ({count(option.products)})</option>
		{/each}
	</select>

	<label for="family">Product type</label>
	<select
		id="family"
		name="family"
		class="max-w-56 rounded-lg px-3 py-1.5 text-sm"
		style="background: var(--surface-1); color: var(--text-primary); border: 1px solid var(--hairline)"
		value={data.family ?? ''}
		onchange={(event) => event.currentTarget.form?.requestSubmit()}
	>
		<option value="">all types</option>
		{#each data.familyOptions as option (option.key)}
			<option value={option.key}>{option.label} ({count(option.products)})</option>
		{/each}
	</select>

	{#if data.brand || data.family}
		<a href="/?band={data.bandId}" class="text-xs" style="color: var(--text-secondary)">clear</a>
	{/if}

	<span class="text-xs" style="color: var(--text-muted)">
		the pack size scopes both price panels: a small jar always costs more per litre than a large pot,
		so suppliers are only compared inside one band
	</span>
	<noscript><button type="submit" class="rounded-lg px-3 py-1.5">Apply</button></noscript>
</form>

<div class="mt-4 grid gap-4 lg:grid-cols-2">
	<ChartCard title="Products per supplier" subtitle="Variants collected from each storefront">
		{#snippet chart()}
			<BarChart items={supplierBars} format={count} labelWidth={144} />
		{/snippet}
		{#snippet table()}
			<table class="w-full text-xs" style="color: var(--text-secondary)">
				<thead>
					<tr class="text-left">
						<th class="py-1 pr-4">Supplier</th>
						<th class="py-1 pr-4 text-right">Products</th>
						<th class="py-1 text-right">With code</th>
					</tr>
				</thead>
				<tbody>
					{#each data.suppliers as row (row.supplier)}
						<tr>
							<td class="py-1 pr-4">{row.supplier}</td>
							<td class="py-1 pr-4 text-right tabular-nums">{count(row.products)}</td>
							<td class="py-1 text-right tabular-nums">{count(row.coded)}</td>
						</tr>
					{/each}
				</tbody>
			</table>
		{/snippet}
	</ChartCard>

	<div class="flex flex-col gap-4">
		<ChartCard
			title="Catalogue by family"
			subtitle="Share of all collected variants"
			note="Families are derived by the importer from the product name and its categories, not published by the suppliers. This panel always shows the whole mix - it is the one that says what the types are - so the product type filter does not scope it."
		>
			{#snippet chart()}
				<StackedBar segments={familySegments} format={count} />
			{/snippet}
			{#snippet table()}
				<table class="w-full text-xs" style="color: var(--text-secondary)">
					<thead>
						<tr class="text-left">
							<th class="py-1 pr-4">Family</th>
							<th class="py-1 text-right">Products</th>
						</tr>
					</thead>
					<tbody>
						{#each data.families as row (row.family)}
							<tr>
								<td class="py-1 pr-4">{row.family}</td>
								<td class="py-1 text-right tabular-nums">{count(row.products)}</td>
							</tr>
						{/each}
					</tbody>
				</table>
			{/snippet}
		</ChartCard>

		<ChartCard
			title="Median {data.pricedFamily} price per litre"
			subtitle="{bandLabel} packs, EUR offers only{data.family
				? ''
				: ' - glaze unless another type is chosen'}"
			note="Medians over the latest offer per product. Prices are supplier observations that can be VAT-inclusive or exclusive, and non-EUR listings are converted at the ECB reference rate."
		>
			{#snippet chart()}
				<BarChart items={medianBars} format={euros} emphasis={0} labelWidth={144} />
			{/snippet}
			{#snippet table()}
				<table class="w-full text-xs" style="color: var(--text-secondary)">
					<thead>
						<tr class="text-left">
							<th class="py-1 pr-4">Supplier</th>
							<th class="py-1 pr-4 text-right">Median EUR/L</th>
							<th class="py-1 text-right">Offers</th>
						</tr>
					</thead>
					<tbody>
						{#each data.medians as row (row.supplier)}
							<tr>
								<td class="py-1 pr-4">{row.supplier}</td>
								<td class="py-1 pr-4 text-right tabular-nums">{row.median.toFixed(2)}</td>
								<td class="py-1 text-right tabular-nums">{count(row.offers)}</td>
							</tr>
						{/each}
					</tbody>
				</table>
			{/snippet}
		</ChartCard>
	</div>
</div>

<div class="mt-4">
	<ChartCard
		title="Widest price spread between suppliers"
		subtitle="{bandLabel} packs sold by three or more suppliers, EUR per litre{data.family
			? ` - ${data.family} only`
			: ''}"
		note="A similar name does not prove two products are equivalent. These rows share a manufacturer code and a pack band; VAT basis and shipping still differ between suppliers."
	>
		{#snippet chart()}
			{#if spreadRows.length}
				<RangeChart items={spreadRows} />
			{:else}
				<p class="text-sm" style="color: var(--text-muted)">
					No product in this pack band is sold by three or more suppliers in EUR.
				</p>
			{/if}
		{/snippet}
		{#snippet table()}
			<table class="w-full text-xs" style="color: var(--text-secondary)">
				<thead>
					<tr class="text-left">
						<th class="py-1 pr-4">Code</th>
						<th class="py-1 pr-4">Product</th>
						<th class="py-1 pr-4 text-right">Suppliers</th>
						<th class="py-1 pr-4 text-right">Low</th>
						<th class="py-1 pr-4 text-right">High</th>
						<th class="py-1 text-right">Ratio</th>
					</tr>
				</thead>
				<tbody>
					{#each data.spread as row (row.code)}
						<tr>
							<td class="py-1 pr-4">{row.code}</td>
							<td class="py-1 pr-4">{row.name}</td>
							<td class="py-1 pr-4 text-right tabular-nums">{row.suppliers}</td>
							<td class="py-1 pr-4 text-right tabular-nums">{row.low.toFixed(2)}</td>
							<td class="py-1 pr-4 text-right tabular-nums">{row.high.toFixed(2)}</td>
							<td class="py-1 text-right tabular-nums">{row.ratio.toFixed(2)}x</td>
						</tr>
					{/each}
				</tbody>
			</table>
		{/snippet}
	</ChartCard>
</div>
