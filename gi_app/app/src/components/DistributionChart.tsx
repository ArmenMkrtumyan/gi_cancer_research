import { BarChart, Bar, Cell, XAxis, YAxis, Tooltip, ResponsiveContainer } from 'recharts'
import { type LabelCount } from '@/lib/api'
import { SERIES } from '@/lib/theme'

interface DistributionChartProps {
  title: string
  data: LabelCount[]
}

/** Small categorical bar chart for a distribution (sex, stage, sample type, …). */
export default function DistributionChart({ title, data }: DistributionChartProps) {
  return (
    <div className="border rounded-lg p-4">
      <h3 className="text-sm font-semibold text-brand mb-3">{title}</h3>
      {data.length === 0 ? (
        <p className="text-sm text-muted-foreground py-8 text-center">No data</p>
      ) : (
        <ResponsiveContainer width="100%" height={200}>
          <BarChart data={data} margin={{ top: 4, right: 8, left: -12, bottom: 4 }}>
            <XAxis
              dataKey="label"
              tick={{ fontSize: 11 }}
              interval={0}
              angle={data.length > 3 ? -25 : 0}
              textAnchor={data.length > 3 ? 'end' : 'middle'}
              height={data.length > 3 ? 56 : 24}
            />
            <YAxis allowDecimals={false} tick={{ fontSize: 11 }} width={36} />
            <Tooltip
              cursor={{ fill: 'rgba(13,59,79,0.06)' }}
              contentStyle={{ fontSize: 12, borderRadius: 8 }}
            />
            <Bar dataKey="count" radius={[4, 4, 0, 0]}>
              {data.map((_, i) => (
                <Cell key={i} fill={SERIES[i % SERIES.length]} />
              ))}
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      )}
    </div>
  )
}
