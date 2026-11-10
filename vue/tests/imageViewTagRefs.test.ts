import assert from 'node:assert/strict'
import { test } from 'node:test'
import {
  MissingImageViewTagError,
  resolveImageViewTagIds,
  toImageViewTagRefs,
  type ImageViewTagSource
} from '../src/util/imageViewTagRefs'

test('saved tag references survive index rebuilds that change tag ids', () => {
  const before: ImageViewTagSource[] = [
    { id: 1, name: 'portrait', type: 'custom' },
    { id: 2, name: 'anime', type: 'custom' }
  ]
  const after: ImageViewTagSource[] = [
    { id: 91, name: 'anime', type: 'custom' },
    { id: 87, name: 'portrait', type: 'custom' }
  ]

  const refs = toImageViewTagRefs(before, [1, 2])
  assert.deepEqual(refs, [
    { name: 'portrait', type: 'custom' },
    { name: 'anime', type: 'custom' }
  ])
  assert.deepEqual(resolveImageViewTagIds(refs, after), [87, 91])
})

test('missing saved tags fail instead of silently changing the query', () => {
  assert.throws(
    () => resolveImageViewTagIds([{ name: 'missing', type: 'custom' }], []),
    MissingImageViewTagError
  )
})
