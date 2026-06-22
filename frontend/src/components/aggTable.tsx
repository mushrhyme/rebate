/**
 * 제품별 집계(분해) 표 — 선언적 컬럼 스펙 + 범용 셀 렌더러 (P4).
 *
 * 동적 컬럼 표시를 Results.tsx에 하드코딩하는 대신(널바이트 버그가 났던 자리),
 * 컬럼 스펙(kind 기반)으로 기술하고 범용 렌더러가 해석한다. 새 표 형태는
 * 코드가 아니라 스펙(aggColumns 또는 백엔드 emit)만 바꾸면 된다.
 */
import type { ProductAggregateRow, AggColumn } from '../api/client'

// 표시 스펙 타입은 데이터 계약(client.ts)이 단일 출처. 백엔드가 emit하면 그대로,
// 없으면 아래 aggColumns()가 condition_columns에서 동일 구조로 생성(폴백).
export type AggCol = AggColumn

/** [폴백] condition_columns(동적 조건들) → 분해 표 컬럼 스펙. 백엔드 display_columns 부재 시. */
export function aggColumns(conditionColumns: string[]): AggCol[] {
  return [
    { key: '_mark', label: '', kind: 'mark' },
    { key: '_qty', label: '수량', kind: 'qty' },
    ...conditionColumns.map((c): AggCol => ({ key: c, label: c, kind: 'unit' })),
    { key: '_amount', label: '금액', kind: 'amount' },
  ]
}

/** 스펙 → thead 셀들 (기존 <tr> 안에 슬롯). */
export function AggHeadCells({ columns }: { columns: AggCol[] }) {
  return (
    <>
      {columns.map(col => (
        <th
          key={col.key}
          style={{
            padding: col.kind === 'mark' ? '4px 4px' : '4px 8px',
            fontWeight: 600,
            fontSize: 10,
            textAlign: col.kind === 'mark' ? 'left' : 'right',
            letterSpacing: '0.04em',
            borderBottom: '1px solid var(--border)',
            whiteSpace: 'nowrap',
          }}
        >
          {col.label}
        </th>
      ))}
    </>
  )
}

/** 스펙 + 분해 행 데이터 → tbody 셀들 (기존 <tr> 안에 슬롯). */
export function AggDecompCells({ columns, row }: { columns: AggCol[]; row: ProductAggregateRow }) {
  return (
    <>
      {columns.map(col => {
        switch (col.kind) {
          case 'mark':
            return <td key={col.key} style={{ padding: '3px 4px 3px 12px', color: 'var(--text-3)', fontSize: 10 }}>·</td>
          case 'qty':
            return <td key={col.key} style={{ padding: '3px 8px', textAlign: 'right', fontFamily: 'var(--mono)', color: 'var(--text-2)' }}>{row.qty.toLocaleString()}</td>
          case 'unit': {
            const v = row.units[col.key]
            return (
              <td key={col.key} style={{ padding: '3px 8px', textAlign: 'right', fontFamily: 'var(--mono)', color: v != null ? 'var(--text-2)' : 'var(--text-3)' }}>
                {v != null ? v.toLocaleString() : ''}
              </td>
            )
          }
          case 'amount':
            return <td key={col.key} style={{ padding: '3px 0', textAlign: 'right', fontFamily: 'var(--mono)', color: 'var(--text-1)' }}>{row.amount.toLocaleString()}</td>
        }
      })}
    </>
  )
}
