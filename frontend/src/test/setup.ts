/** Test environment: no network, no leaked DOM between cases. */

import { cleanup } from '@testing-library/react'
import { afterEach } from 'vitest'

afterEach(() => {
  cleanup()
})
