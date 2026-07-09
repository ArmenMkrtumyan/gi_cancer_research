// Chart palette for recharts, kept in one place so every chart reads as one system.
// Anchored on the brand teal + gold, with cohesive categorical / semantic accents.
export const CHART = {
  brand: '#0d3b4f',
  teal: '#0e7490',
  cyan: '#06b6d4',
  gold: '#f4b942',
  green: '#16a34a',
  red: '#dc2626',
  violet: '#7c3aed',
  slate: '#64748b',
}

// Ordered categorical series for distribution bars (first colors are the strongest).
export const SERIES = [
  CHART.teal,
  CHART.brand,
  CHART.gold,
  CHART.violet,
  CHART.green,
  CHART.cyan,
  CHART.red,
  CHART.slate,
]
