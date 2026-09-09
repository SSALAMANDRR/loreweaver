/** Generic, server-authored character creation state (protocol 2.4). */
export interface CreationPresentation {
  title?: string
  description?: string
  choice?: string
  effect?: string
}

export interface CreationProfile {
  id: string
  label: string
  detail?: string[]
  source?: string
  effect?: CreationEffect
  choices?: CreationChoiceGroup[]
}

export interface CreationCatalog {
  staged: boolean
  requires_profile: boolean
  profiles: CreationProfile[]
  presentation?: CreationPresentation
}

export interface CreationEffectValue {
  label: string
  value: unknown
}

export interface CreationEffect {
  grants?: string[]
  skills?: CreationEffectValue[]
  equipment?: string[]
  attributes?: CreationEffectValue[]
}

export interface CreationChoiceOption {
  id: string
  label: string
  specialization?: boolean
  effect?: CreationEffect
}

export interface CreationChoiceGroup {
  id: string
  label: string
  free: boolean
  family?: string
  options: CreationChoiceOption[]
}

export interface CreationLayerOption {
  id: string
  label: string
  fixed: boolean
  choices: CreationChoiceGroup[]
  detail?: string[]
  source?: string
  effect?: CreationEffect
}

export interface CreationRerollTarget {
  id: string
  label: string
  value?: number
}

export interface CreationDuplicateRequirement {
  field: string
  count: number
  current?: string[]
  choices: Array<{ id: string; label: string }>
}

export interface CreationAdvancementPurchase {
  category: string
  category_label?: string
  target: string
  label: string
  stage: string
  stage_label?: string
  current: number
  next: number
  cost: number
  affordable: boolean
}

export interface CreationEquipmentItem {
  id: string
  label: string
  kind: string
  availability: number
}

export interface CharacterContextChoice {
  id: string
  label: string
}

export interface CharacterContextField {
  id: string
  kind: "choice" | "text" | "textarea"
  required: boolean
  label: string
  placeholder?: string
  options?: CharacterContextChoice[]
}

export interface CharacterContextState {
  available: boolean
  optional: boolean
  complete: boolean
  skipped: boolean
  fields: CharacterContextField[]
  values: Record<string, string>
}

export interface CreationStage {
  id: string
  kind: "profile_reroll" | "layer" | "duplicates" | "advancement" | "starting_equipment" | string
  presentation?: CreationPresentation
  can_skip?: boolean
  targets?: CreationRerollTarget[]
  layer?: string
  fixed?: boolean
  options?: CreationLayerOption[]
  requirements?: CreationDuplicateRequirement[]
  budget?: Record<string, number>
  purchases?: CreationAdvancementPurchase[]
  items?: CreationEquipmentItem[]
  inventory?: string[]
}

export interface CreationState {
  active: boolean
  complete: boolean
  profile_id: string
  stage_index: number
  stage_count: number
  completed_stages: string[]
  stage: CreationStage | null
  context?: CharacterContextState
}

export interface CharacterReadinessState {
  ready: boolean
  managed: boolean
  phase: "creation" | "finalization" | "blocked" | "invalid" | "ready"
  blocked_reference: string
  message?: string
}

export interface CharacterFinalizationState {
  can_roll: boolean
  can_resolve: boolean
  complete: boolean
  expression: string
  choices: CreationChoiceGroup[]
  result?: {
    roll: number
    id: string
    label: string
    source: string
    rules: string[]
  }
}

/** Sent as URI-encoded JSON through .__creation_action finalize. */
export type CharacterFinalizationAction =
  | { character: string; action: "roll" }
  | {
      character: string
      action: "resolve"
      roll: number
      row_id: string
      selections: Record<string, string>
    }
