import { type MatchImageByTagsReq, type TagId } from '@/api/db'
import { HistoryRecord } from '@/util/HistoryRecord'
import { prefix } from '@/util/const'
import { useLocalStorage } from '@vueuse/core'
import { Ref } from 'vue'

export type FuzzySearchHistoryRecord ={ 
  substr: string, 
  folder_paths_str: string,
  isRegex: boolean,
  mediaType?: string,
  and_tags?: TagId[]
}

export const fuzzySearchHistory = useLocalStorage(`${prefix}fuzzy-search-HistoryRecord`, new HistoryRecord(), {
  serializer: {
    read: (str) => {
      const val = JSON.parse(str)
      return new HistoryRecord(val.maxLength, val.records, val.pinnedValues)
    },
    write: JSON.stringify,
  },
}) as Ref<HistoryRecord<FuzzySearchHistoryRecord>>

export const tagSearchHistory = useLocalStorage(`${prefix}tag-search-HistoryRecord`, new HistoryRecord(), {
  serializer: {
    read: (str) => {
      const val = JSON.parse(str)
      return new HistoryRecord(val.maxLength, val.records, val.pinnedValues)
    },
    write: JSON.stringify,
  },
}) as Ref<HistoryRecord<MatchImageByTagsReq>>

