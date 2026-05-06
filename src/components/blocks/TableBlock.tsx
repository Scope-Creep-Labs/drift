import {
  Paper,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  Typography,
} from '@mui/material'
import type { TableBlock as TableBlockT } from '../../types/blocks'

export function TableBlock({ block }: { block: TableBlockT }) {
  return (
    <Paper variant="outlined" sx={{ borderColor: 'divider', overflow: 'hidden' }}>
      {block.title && (
        <Typography
          variant="caption"
          sx={{
            display: 'block',
            px: 2,
            py: 1.2,
            color: 'text.secondary',
            borderBottom: 1,
            borderColor: 'divider',
            textTransform: 'uppercase',
            letterSpacing: 0.4,
          }}
        >
          {block.title}
        </Typography>
      )}
      <TableContainer sx={{ maxHeight: 420 }}>
        <Table size="small" stickyHeader>
          <TableHead>
            <TableRow>
              {block.columns.map((c) => (
                <TableCell key={c} sx={{ fontWeight: 600, bgcolor: 'background.paper' }}>
                  {c}
                </TableCell>
              ))}
            </TableRow>
          </TableHead>
          <TableBody>
            {block.rows.map((row, i) => (
              <TableRow key={i} hover>
                {row.map((cell, j) => (
                  <TableCell key={j}>{String(cell)}</TableCell>
                ))}
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </TableContainer>
    </Paper>
  )
}
