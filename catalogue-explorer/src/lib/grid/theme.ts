import { themeQuartz } from 'ag-grid-community';

/**
 * The grid, dressed in the palette the rest of the site already uses.
 *
 * Every colour is a `var(--...)` reference rather than a literal, so the theme
 * has one definition instead of a light copy and a dark copy: the variables in
 * app.css already swap with the theme toggle, and the grid follows them without
 * being told. `browserColorScheme: 'inherit'` is what carries that as far as
 * the scrollbars, which are drawn by the browser and not by us.
 *
 * The shape is deliberately a spreadsheet rather than a card: no outer border,
 * no rounded corners, no zebra striping, and a rule on all four sides of every
 * cell. Density comes from `spacing`, which every padding in the grid derives
 * from - one number moves the whole thing.
 */
export const gridTheme = themeQuartz.withParams({
	backgroundColor: 'var(--surface-1)',
	foregroundColor: 'var(--text-primary)',
	borderColor: 'var(--gridline)',
	chromeBackgroundColor: 'var(--surface-1)',
	accentColor: 'var(--accent)',
	browserColorScheme: 'inherit',

	fontFamily: 'inherit',
	fontSize: '12px',
	headerFontSize: '12px',
	headerFontWeight: 500,
	headerTextColor: 'var(--text-secondary)',

	spacing: 4,
	rowHeight: 30,
	headerHeight: 34,
	cellHorizontalPadding: 10,

	// The grid is the page here, so it carries no frame of its own and no
	// radius: it runs to the edge of the viewport on every side.
	wrapperBorder: false,
	wrapperBorderRadius: 0,
	borderRadius: 2,

	// Ruled both ways. The vertical rule is what makes a table read as a sheet
	// of cells rather than a list of records, and it is doing real work in a
	// fifteen-column table where the eye has to track across.
	rowBorder: true,
	columnBorder: true,
	headerRowBorder: true,
	headerColumnBorder: true,

	// No stripes: the gridlines already separate the rows, and a stripe would
	// be a second, weaker answer to the same question.
	oddRowBackgroundColor: 'transparent',
	rowHoverColor: 'color-mix(in srgb, var(--accent) 8%, transparent)',
	selectedRowBackgroundColor: 'color-mix(in srgb, var(--accent) 14%, transparent)',
	rangeSelectionBackgroundColor: 'color-mix(in srgb, var(--accent) 12%, transparent)',
	rangeSelectionBorderColor: 'var(--accent)',
	headerColumnResizeHandleColor: 'var(--baseline)'
});
