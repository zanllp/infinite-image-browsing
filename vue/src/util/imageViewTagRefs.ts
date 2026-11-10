export type ImageViewTagId = string | number

export interface ImageViewTagSource {
  id: ImageViewTagId
  name: string
  type: string
}

export interface ImageViewTagRef {
  name: string
  type: string
}

export class MissingImageViewTagError extends Error {
  constructor(public readonly tag: Partial<ImageViewTagRef>) {
    super(`Image view tag is missing: ${tag.type ?? '?'} / ${tag.name ?? '?'}`)
    this.name = 'MissingImageViewTagError'
  }
}

const tagRefKey = (tag: Pick<ImageViewTagRef, 'name' | 'type'>) => `${tag.type}\u0000${tag.name}`

/** Tag IDs are not stable across index rebuilds; saved views must use name + type. */
export const toImageViewTagRefs = (tags: ImageViewTagSource[], ids: ImageViewTagId[]): ImageViewTagRef[] => {
  const tagMap = new Map(tags.map((tag) => [tag.id, tag]))
  return ids.map((id) => {
    const tag = tagMap.get(id)
    if (!tag) {
      throw new Error(`Cannot resolve selected tag: ${String(id)}`)
    }
    return { name: tag.name, type: tag.type }
  })
}

/** Resolve stable tag references against the current tag table. */
export const resolveImageViewTagIds = (refs: ImageViewTagRef[], tags: ImageViewTagSource[]): ImageViewTagId[] => {
  const tagMap = new Map(tags.map((tag) => [tagRefKey(tag), tag.id]))
  return refs.map((ref) => {
    if (typeof ref?.name !== 'string' || typeof ref?.type !== 'string') {
      throw new MissingImageViewTagError(ref)
    }
    const id = tagMap.get(tagRefKey(ref))
    if (id === undefined) {
      throw new MissingImageViewTagError(ref)
    }
    return id
  })
}
