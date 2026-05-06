import { Alert } from '@mui/material'
import type { ChartBlock as ChartBlockT } from '../../types/blocks'

export default function ChartBlockEcharts({ block: _block }: { block: ChartBlockT }) {
  return (
    <Alert severity="info" variant="outlined">
      ECharts renderer is not wired up yet — the discriminator is reserved for a future drop-in.
    </Alert>
  )
}
