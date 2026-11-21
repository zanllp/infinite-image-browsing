import { uniqueId } from 'lodash-es'
import { Dict } from '.'

export class HistoryRecord<F extends Dict, T extends { id: string; time: string } = F & { id: string; time: string }> {
  constructor(
    private maxLength: number = 128,
    private records: T[] = [],
    private pinnedValues: T[] = []) {
  }

  isPinned (value: T): boolean {
    return this.pinnedValues.some(item => item.id === value.id)
  }

  add (value: F) {
    // Remove oldest record if maximum length is reached
    if (this.records.length >= this.maxLength) {
      this.records.pop()
    }

    this.records.unshift({ ...value, id: uniqueId() + Date.now(), time: new Date().toLocaleString() } as unknown as T)
  }

  pin (value: T) {
    // Remove value from records if it exists
    const index = this.records.findIndex(item => item.id === value.id)
    if (index !== -1) {
      this.records.splice(index, 1)
    }

    // Add value to pinned values
    this.pinnedValues.push(value)
  }

  unpin (value: T) {
    // Remove value from pinned values if it exists
    const index = this.pinnedValues.findIndex(item => item.id === value.id)
    if (index !== -1) {
      this.pinnedValues.splice(index, 1)
    }

    // Add value back to records
    this.records.unshift(value)
  }

  switchPin (value: T) {
    if (this.isPinned(value)) {
      this.unpin(value)
    } else {
      this.pin(value)
    }
  }

  getRecords () {
    return [...this.pinnedValues, ...this.records] as readonly T[]
  }

  getPinnedValues () {
    return this.pinnedValues as readonly T[]
  }

  /** Remove a record, pinned or not. */
  remove (value: T) {
    for (const list of [this.records, this.pinnedValues]) {
      const index = list.findIndex(item => item.id === value.id)
      if (index !== -1) {
        list.splice(index, 1)
      }
    }
  }
}
