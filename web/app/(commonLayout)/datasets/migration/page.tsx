import Migration from '@/app/components/datasets/migration'
import { Suspense } from 'react'

export default function MigrationPage() {
  return (
    <Suspense>
      <Migration />
    </Suspense>
  )
}
